package app.obsidianvow.core;

import android.annotation.SuppressLint;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCallback;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattDescriptor;
import android.bluetooth.BluetoothGattService;
import android.bluetooth.BluetoothManager;
import android.bluetooth.le.BluetoothLeScanner;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanResult;
import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import org.json.JSONObject;

import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.TimeUnit;

/**
 * 原生 BLE 桥接 — 绕过 WebView 不支持 Web Bluetooth API 的限制
 *
 * 支持多设备：连接前调用 configure(json) 传入设备描述符（namePrefix/服务 UUID/
 * 写特征/通知特征/封包方式/写类型）。默认配置兼容 SOSEXY（原行为）。
 *
 * 前端：
 *   window.AionBle.configure(JSON.stringify({namePrefix:"CX492B",
 *       serviceUuid:"...", writeUuid:"...", notifyUuid:"...",
 *       framing:"raw", writeType:"no_response"}))
 *   window.AionBle.connect() / disconnect() / isConnected() / sendData(hex)
 * 回调：toyNativeBle.onConnected() / onDisconnected() / onError(msg) / onLog(msg)
 */
@SuppressLint("MissingPermission")
public class BleBridge {

    private static final String TAG = "AionBle";
    private static final UUID CCCD_UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb");

    // 默认 = SOSEXY（保持与老逻辑完全一致的默认行为）
    private static final String DEFAULT_NAME_PREFIX = "SOSEXY";
    private static final String DEFAULT_SERVICE = "0000ee01-0000-1000-8000-00805f9b34fb";
    private static final String DEFAULT_WRITE   = "0000ee03-0000-1000-8000-00805f9b34fb";
    private static final String DEFAULT_NOTIFY  = "0000ee02-0000-1000-8000-00805f9b34fb";
    private static final String DEFAULT_FRAMING = "chunked";    // chunked | raw
    private static final String DEFAULT_WRITE_TYPE = "default"; // default | no_response

    private final WebView webView;
    private final Context context;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final ExecutorService writeExecutor = Executors.newSingleThreadExecutor();

    private BluetoothAdapter adapter;
    private BluetoothLeScanner scanner;
    private BluetoothGatt gatt;
    private BluetoothGattCharacteristic writeChar;
    private volatile boolean connected = false;
    private volatile boolean connecting = false;
    private volatile boolean scanning = false;
    private volatile CountDownLatch writeLatch;
    private volatile int lastWriteStatus = BluetoothGatt.GATT_SUCCESS;

    // 当前设备描述符（可被 configure 覆盖）
    private volatile String namePrefix = DEFAULT_NAME_PREFIX;
    private volatile UUID serviceUuid = UUID.fromString(DEFAULT_SERVICE);
    private volatile UUID writeUuid   = UUID.fromString(DEFAULT_WRITE);
    private volatile UUID notifyUuid  = UUID.fromString(DEFAULT_NOTIFY);
    private volatile String framing   = DEFAULT_FRAMING;
    private volatile String writeType = DEFAULT_WRITE_TYPE;

    public BleBridge(WebView webView, Context context) {
        this.webView = webView;
        this.context = context;
        BluetoothManager bm = (BluetoothManager) context.getSystemService(Context.BLUETOOTH_SERVICE);
        if (bm != null) adapter = bm.getAdapter();
    }

    // ── JS 接口 ──

    /**
     * 配置设备描述符。必须在 connect() 之前调用。
     * 字段都是可选，未提供则保留上次值（初始为 SOSEXY 默认）。
     */
    @JavascriptInterface
    public void configure(String json) {
        try {
            JSONObject j = new JSONObject(json == null ? "{}" : json);
            if (j.has("namePrefix"))  namePrefix  = j.optString("namePrefix", namePrefix);
            if (j.has("serviceUuid")) serviceUuid = UUID.fromString(j.getString("serviceUuid"));
            if (j.has("writeUuid"))   writeUuid   = UUID.fromString(j.getString("writeUuid"));
            if (j.has("notifyUuid"))  notifyUuid  = UUID.fromString(j.getString("notifyUuid"));
            if (j.has("framing"))     framing     = j.optString("framing", framing);
            if (j.has("writeType"))   writeType   = j.optString("writeType", writeType);
            callJs("toyNativeBle.onLog('cfg " + escapeJs(namePrefix) + "/" + escapeJs(framing) + "')");
        } catch (Exception e) {
            callJs("toyNativeBle.onError('配置失败: " + escapeJs(e.getMessage()) + "')");
        }
    }

    @JavascriptInterface
    public void connect() {
        if (adapter == null || !adapter.isEnabled()) {
            callJs("toyNativeBle.onError('蓝牙未开启')");
            return;
        }
        if (connected || connecting || scanning) return;

        scanner = adapter.getBluetoothLeScanner();
        if (scanner == null) {
            callJs("toyNativeBle.onError('无法获取BLE扫描器')");
            return;
        }

        scanning = true;
        callJs("toyNativeBle.onLog('搜索 " + escapeJs(namePrefix) + "...')");

        try {
            scanner.startScan(scanCb);
        } catch (Exception e) {
            scanning = false;
            callJs("toyNativeBle.onError('扫描失败: " + escapeJs(errorText(e)) + "')");
            return;
        }

        // 10秒超时
        mainHandler.postDelayed(() -> {
            if (scanning) {
                stopScan();
                callJs("toyNativeBle.onError('未找到设备')");
            }
        }, 10000);
    }

    @JavascriptInterface
    public void disconnect() {
        disconnectInternal(true);
    }

    private void disconnectInternal(boolean notify) {
        stopScan();
        connecting = false;
        boolean wasConnected = connected;
        connected = false;
        writeChar = null;
        CountDownLatch latch = writeLatch;
        if (latch != null) latch.countDown();
        if (gatt != null) {
            try { gatt.disconnect(); gatt.close(); } catch (Exception ignored) {}
            gatt = null;
        }
        if (notify && wasConnected) {
            callJs("toyNativeBle.onDisconnected()");
        }
    }

    @JavascriptInterface
    public boolean isConnected() {
        return connected;
    }

    /**
     * 发送控制指令（hex 字符串）。
     * framing="chunked"：沿用 SOSEXY 的 "00 前缀 + 18 字节分包 + [rnd,idx] 包头"
     * framing="raw"    ：把 hex 直接转字节整条写入（用于 CX492B 7 字节定长帧等）
     */
    @JavascriptInterface
    public void sendData(final String hexCmd) {
        if (!connected || writeChar == null || writeExecutor.isShutdown()) return;
        try {
            writeExecutor.execute(() -> {
                if (!connected || writeChar == null) return;
                if (!isValidHex(hexCmd)) {
                    callJs("toyNativeBle.onError('BLE指令格式错误')");
                    return;
                }
                if ("raw".equalsIgnoreCase(framing)) sendRaw(hexCmd);
                else sendChunked(hexCmd);
            });
        } catch (RejectedExecutionException ignored) {}
    }

    public void shutdown() {
        mainHandler.removeCallbacksAndMessages(null);
        disconnectInternal(false);
        writeExecutor.shutdownNow();
    }

    // ── BLE 扫描 ──

    private final ScanCallback scanCb = new ScanCallback() {
        @Override
        public void onScanResult(int callbackType, ScanResult result) {
            if (!scanning) return;
            BluetoothDevice dev = result.getDevice();
            String name = null;
            try { name = dev.getName(); } catch (Exception ignored) {}
            if (name == null && result.getScanRecord() != null) {
                try { name = result.getScanRecord().getDeviceName(); } catch (Exception ignored) {}
            }
            if (name != null && name.startsWith(namePrefix)) {
                stopScan();
                callJs("toyNativeBle.onLog('" + escapeJs(name) + "')");
                connectGatt(dev);
            }
        }

        @Override
        public void onScanFailed(int errorCode) {
            scanning = false;
            callJs("toyNativeBle.onError('扫描失败: " + errorCode + "')");
        }
    };

    private void stopScan() {
        if (!scanning) return;
        scanning = false;
        try { if (scanner != null) scanner.stopScan(scanCb); } catch (Exception ignored) {}
    }

    // ── GATT 连接 ──

    private void connectGatt(BluetoothDevice dev) {
        try {
            connecting = true;
            gatt = dev.connectGatt(context, false, gattCb, BluetoothDevice.TRANSPORT_LE);
            BluetoothGatt pendingGatt = gatt;
            mainHandler.postDelayed(() -> {
                if (connecting && !connected && gatt == pendingGatt) {
                    callJs("toyNativeBle.onError('连接超时')");
                    disconnectInternal(false);
                }
            }, 12000);
        } catch (Exception e) {
            connecting = false;
            callJs("toyNativeBle.onError('连接失败: " + escapeJs(errorText(e)) + "')");
        }
    }

    @SuppressWarnings("deprecation")
    private final BluetoothGattCallback gattCb = new BluetoothGattCallback() {
        @Override
        public void onConnectionStateChange(BluetoothGatt g, int status, int newState) {
            if (newState == BluetoothGatt.STATE_CONNECTED) {
                connecting = true;
                try { g.requestConnectionPriority(BluetoothGatt.CONNECTION_PRIORITY_HIGH); } catch (Exception ignored) {}
                try { g.requestMtu(185); } catch (Exception ignored) {}
                if (!g.discoverServices()) {
                    connecting = false;
                    callJs("toyNativeBle.onError('服务发现启动失败')");
                    try { g.close(); } catch (Exception ignored) {}
                    if (gatt == g) gatt = null;
                }
            } else if (newState == BluetoothGatt.STATE_DISCONNECTED) {
                boolean shouldNotify = connected || connecting;
                connecting = false;
                connected = false;
                writeChar = null;
                if (shouldNotify) callJs("toyNativeBle.onDisconnected()");
                try { g.close(); } catch (Exception ignored) {}
                if (gatt == g) gatt = null;
            }
        }

        @Override
        public void onServicesDiscovered(BluetoothGatt g, int status) {
            connecting = false;
            if (status != BluetoothGatt.GATT_SUCCESS) {
                callJs("toyNativeBle.onError('服务发现失败')");
                try { g.disconnect(); g.close(); } catch (Exception ignored) {}
                if (gatt == g) gatt = null;
                return;
            }
            BluetoothGattService svc = g.getService(serviceUuid);
            if (svc == null) {
                callJs("toyNativeBle.onError('未找到BLE服务')");
                try { g.disconnect(); g.close(); } catch (Exception ignored) {}
                if (gatt == g) gatt = null;
                return;
            }
            writeChar = svc.getCharacteristic(writeUuid);
            if (writeChar == null) {
                callJs("toyNativeBle.onError('未找到写入特征')");
                try { g.disconnect(); g.close(); } catch (Exception ignored) {}
                if (gatt == g) gatt = null;
                return;
            }
            // 写入类型
            if ("no_response".equalsIgnoreCase(writeType)) {
                writeChar.setWriteType(BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE);
            } else {
                // default：按特征属性自适应（沿用 SOSEXY 的老行为）
                if ((writeChar.getProperties() & BluetoothGattCharacteristic.PROPERTY_WRITE) != 0) {
                    writeChar.setWriteType(BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT);
                } else {
                    writeChar.setWriteType(BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE);
                }
            }
            // 订阅通知
            BluetoothGattCharacteristic nc = svc.getCharacteristic(notifyUuid);
            if (nc != null) {
                g.setCharacteristicNotification(nc, true);
                BluetoothGattDescriptor desc = nc.getDescriptor(CCCD_UUID);
                if (desc != null) {
                    desc.setValue(BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE);
                    g.writeDescriptor(desc);
                }
            }
            connected = true;
            callJs("toyNativeBle.onConnected()");
        }

        @Override
        public void onCharacteristicWrite(BluetoothGatt g, BluetoothGattCharacteristic c, int status) {
            lastWriteStatus = status;
            if (status != BluetoothGatt.GATT_SUCCESS) {
                Log.w(TAG, "write failed status=" + status);
            }
            CountDownLatch l = writeLatch;
            if (l != null) l.countDown();
        }
    };

    // ── 数据发送 ──

    /** 分包写入（SOSEXY：前缀 00、18 字节一包、每包带 [rnd, idx] 包头） */
    @SuppressWarnings("deprecation")
    private void sendChunked(String hexCmd) {
        if (gatt == null || writeChar == null) return;
        try {
            byte[] data = hexToBytes("00" + hexCmd);
            int chunkSize = 18;
            int numChunks = Math.max(1, (data.length + chunkSize - 1) / chunkSize);
            int rnd = (int) (Math.random() * 255);

            for (int i = 0; i < numChunks; i++) {
                int start = i * chunkSize;
                int end = Math.min(start + chunkSize, data.length);
                byte[] pkt = new byte[2 + (end - start)];
                pkt[0] = (byte) rnd;
                pkt[1] = (byte) (i + 1);
                System.arraycopy(data, start, pkt, 2, end - start);

                if (!writeChunk(pkt)) {
                    Log.w(TAG, "writeChunk timeout at chunk " + i);
                    return;
                }
            }
            if (data.length > 0 && data.length % chunkSize == 0) {
                writeChunk(new byte[]{(byte) rnd, (byte) (numChunks + 1)});
            }
        } catch (Exception e) {
            Log.e(TAG, "sendData(chunked) error", e);
        }
    }

    /** 整条直写（CX492B：7 字节定长帧，直接写） */
    @SuppressWarnings("deprecation")
    private void sendRaw(String hexCmd) {
        if (gatt == null || writeChar == null) return;
        try {
            byte[] data = hexToBytes(hexCmd);
            writeChunk(data);
        } catch (Exception e) {
            Log.e(TAG, "sendData(raw) error", e);
        }
    }

    @SuppressWarnings("deprecation")
    private boolean writeChunk(byte[] value) throws InterruptedException {
        if (gatt == null || writeChar == null || !connected) return false;
        CountDownLatch latch = new CountDownLatch(1);
        writeLatch = latch;
        lastWriteStatus = BluetoothGatt.GATT_SUCCESS;
        int type = writeChar.getWriteType();
        writeChar.setValue(value);
        boolean accepted = gatt.writeCharacteristic(writeChar);
        if (!accepted) {
            writeLatch = null;
            return false;
        }
        if (type == BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE) {
            Thread.sleep(25);
            if (writeLatch == latch) writeLatch = null;
            return true;
        }
        boolean ok = latch.await(2, TimeUnit.SECONDS)
                && lastWriteStatus == BluetoothGatt.GATT_SUCCESS;
        if (writeLatch == latch) writeLatch = null;
        return ok;
    }

    // ── 工具方法 ──

    private byte[] hexToBytes(String hex) {
        int len = hex.length();
        byte[] out = new byte[len / 2];
        for (int i = 0; i < len; i += 2)
            out[i / 2] = (byte) ((Character.digit(hex.charAt(i), 16) << 4)
                    + Character.digit(hex.charAt(i + 1), 16));
        return out;
    }

    private boolean isValidHex(String hex) {
        if (hex == null || hex.length() == 0 || hex.length() % 2 != 0) return false;
        for (int i = 0; i < hex.length(); i++) {
            if (Character.digit(hex.charAt(i), 16) < 0) return false;
        }
        return true;
    }

    private void callJs(String js) {
        mainHandler.post(() -> webView.evaluateJavascript(
                "typeof toyNativeBle!=='undefined'&&" + js, null));
    }

    private String escapeJs(String s) {
        return s == null ? "" : s.replace("\\", "\\\\").replace("'", "\\'");
    }

    private String errorText(Exception e) {
        String msg = e == null ? null : e.getMessage();
        return msg == null || msg.isEmpty() ? "unknown" : msg;
    }
}
