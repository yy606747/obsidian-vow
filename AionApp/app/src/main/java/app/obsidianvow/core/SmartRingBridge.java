package app.obsidianvow.core;

import android.Manifest;
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
import android.content.pm.PackageManager;
import android.os.Build;
import android.util.Log;

import androidx.core.content.ContextCompat;

import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

@SuppressLint("MissingPermission")
public class SmartRingBridge {
    public interface PrivateFrameCallback {
        void onFrame(int command, byte[] body);
    }

    public interface ConnectionStateCallback {
        void onConnectionLost(String reason);
    }

    private static final String TAG = "SmartRingBridge";
    private static final UUID SERVICE_UUID = UUID.fromString("0000fe02-0000-1000-8000-00805f9b34fb");
    private static final UUID WRITE_UUID = UUID.fromString("00000101-0000-1000-8000-00805f9b34fb");
    private static final UUID NOTIFY_UUID = UUID.fromString("0000010a-0000-1000-8000-00805f9b34fb");
    private static final UUID CCCD_UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb");

    private final Context context;
    private final BluetoothAdapter adapter;
    private final AtomicInteger sequence = new AtomicInteger(1);
    private BluetoothLeScanner scanner;
    private volatile BluetoothGatt gatt;
    private BluetoothGattCharacteristic writeChar;
    private BluetoothGattCharacteristic notifyChar;
    private CountDownLatch connectLatch;
    private CountDownLatch writeLatch;
    private volatile boolean connected = false;
    private volatile String connectedAddress = "";
    private volatile String connectedName = "";
    private volatile int lastWriteStatus = BluetoothGatt.GATT_FAILURE;
    private volatile PrivateFrameCallback frameCallback;
    private volatile ConnectionStateCallback connectionStateCallback;

    public SmartRingBridge(Context context) {
        this.context = context.getApplicationContext();
        BluetoothManager bm = (BluetoothManager) this.context.getSystemService(Context.BLUETOOTH_SERVICE);
        this.adapter = bm != null ? bm.getAdapter() : null;
    }

    public boolean isConnected() {
        return connected && writeChar != null && gatt != null;
    }

    public synchronized void connect(String namePrefix) {
        connect(namePrefix, "");
    }

    public synchronized void connect(String namePrefix, String preferredAddress) {
        if (isConnected()) return;
        if (!hasBlePermission()) throw new IllegalStateException("permission_missing");
        if (adapter == null || !adapter.isEnabled()) throw new IllegalStateException("bluetooth_unavailable");

        final String wantedPrefix = namePrefix == null ? "" : namePrefix.trim();
        final String wantedAddress = preferredAddress == null ? "" : preferredAddress.trim();
        BluetoothDevice preferredDevice = remoteDevice(wantedAddress);
        if (preferredDevice != null) {
            try {
                connectDevice(preferredDevice, "cached_connect_timeout");
                return;
            } catch (Exception e) {
                Log.w(TAG, "cached connect failed: " + e.getMessage());
            }
        }

        scanner = adapter.getBluetoothLeScanner();
        if (scanner == null) throw new IllegalStateException("scanner_unavailable");

        final CountDownLatch scanLatch = new CountDownLatch(1);
        final BluetoothDevice[] found = new BluetoothDevice[1];
        ScanCallback scanCallback = new ScanCallback() {
            @Override public void onScanResult(int callbackType, ScanResult result) {
                BluetoothDevice device = result.getDevice();
                String name = null;
                try { name = device.getName(); } catch (Exception ignored) {}
                if (name == null && result.getScanRecord() != null) {
                    try { name = result.getScanRecord().getDeviceName(); } catch (Exception ignored) {}
                }
                boolean addressMatch = false;
                try {
                    addressMatch = !wantedAddress.isEmpty() && wantedAddress.equalsIgnoreCase(device.getAddress());
                } catch (Exception ignored) {}
                boolean nameMatch = name != null && !wantedPrefix.isEmpty() && name.startsWith(wantedPrefix);
                if (addressMatch || nameMatch) {
                    found[0] = device;
                    scanLatch.countDown();
                }
            }
            @Override public void onScanFailed(int errorCode) {
                Log.w(TAG, "scan failed: " + errorCode);
                scanLatch.countDown();
            }
        };

        try {
            scanner.startScan(scanCallback);
            await(scanLatch, 10, "scan_timeout");
        } finally {
            try { scanner.stopScan(scanCallback); } catch (Exception ignored) {}
        }
        if (found[0] == null) throw new IllegalStateException("ring_not_found");

        connectDevice(found[0], "connect_timeout");
    }

    public String getConnectedAddress() {
        return connectedAddress == null ? "" : connectedAddress;
    }

    public String getConnectedName() {
        return connectedName == null ? "" : connectedName;
    }

    public void setConnectionStateCallback(ConnectionStateCallback callback) {
        connectionStateCallback = callback;
    }

    private void connectDevice(BluetoothDevice device, String timeoutError) {
        disconnect();
        connectLatch = new CountDownLatch(1);
        gatt = device.connectGatt(context, false, gattCallback, BluetoothDevice.TRANSPORT_LE);
        if (gatt == null) throw new IllegalStateException("gatt_unavailable");
        try {
            await(connectLatch, 12, timeoutError);
        } catch (RuntimeException e) {
            disconnect();
            throw e;
        }
        if (!isConnected()) {
            disconnect();
            throw new IllegalStateException("gatt_not_ready");
        }
        rememberDevice(device);
    }

    public synchronized void disconnect() {
        connected = false;
        writeChar = null;
        notifyChar = null;
        if (gatt != null) {
            try { gatt.disconnect(); gatt.close(); } catch (Exception ignored) {}
            gatt = null;
        }
    }

    private BluetoothDevice remoteDevice(String address) {
        if (address == null || address.trim().isEmpty()) return null;
        try {
            return adapter.getRemoteDevice(address.trim());
        } catch (Exception e) {
            Log.w(TAG, "invalid cached address: " + e.getMessage());
            return null;
        }
    }

    public void subscribePrivateNotify(PrivateFrameCallback callback) {
        frameCallback = callback;
        BluetoothGatt localGatt = gatt;
        BluetoothGattCharacteristic localNotify = notifyChar;
        if (localGatt == null || localNotify == null) return;
        localGatt.setCharacteristicNotification(localNotify, true);
        BluetoothGattDescriptor cccd = localNotify.getDescriptor(CCCD_UUID);
        if (cccd != null) {
            cccd.setValue(BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE);
            localGatt.writeDescriptor(cccd);
        }
    }

    public synchronized void sendPrivateFrame(int command, byte[] body) {
        if (!isConnected()) throw new IllegalStateException("not_connected");
        byte[] frame = buildPrivateFrame(sequence.getAndIncrement() & 0xffff, command, body);
        writeLatch = new CountDownLatch(1);
        lastWriteStatus = BluetoothGatt.GATT_FAILURE;
        writeChar.setWriteType(BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT);
        writeChar.setValue(frame);
        if (!gatt.writeCharacteristic(writeChar)) throw new IllegalStateException("write_rejected");
        await(writeLatch, 5, "write_timeout");
        if (lastWriteStatus != BluetoothGatt.GATT_SUCCESS) {
            throw new IllegalStateException("write_failed:" + lastWriteStatus);
        }
    }

    public void sendAlert(int itemId) {
        sendPrivateFrame(0x1610, new byte[] {(byte) itemId, (byte) 0xff});
    }

    static byte[] buildPrivateFrame(int seq, int command, byte[] body) {
        byte[] payload = new byte[2 + (body == null ? 0 : body.length)];
        payload[0] = (byte) ((command >> 8) & 0xff);
        payload[1] = (byte) (command & 0xff);
        if (body != null) System.arraycopy(body, 0, payload, 2, body.length);
        int crc = crc16(payload);
        int payloadLen = payload.length + 2;
        byte[] frame = new byte[payloadLen + 6];
        frame[0] = (byte) ((payloadLen >> 8) & 0xff);
        frame[1] = (byte) (payloadLen & 0xff);
        frame[2] = (byte) 0x83;
        frame[3] = (byte) 0x40;
        frame[4] = (byte) ((seq >> 8) & 0xff);
        frame[5] = (byte) (seq & 0xff);
        System.arraycopy(payload, 0, frame, 6, payload.length);
        frame[frame.length - 2] = (byte) ((crc >> 8) & 0xff);
        frame[frame.length - 1] = (byte) (crc & 0xff);
        return frame;
    }

    private final BluetoothGattCallback gattCallback = new BluetoothGattCallback() {
        @Override public void onConnectionStateChange(BluetoothGatt g, int status, int newState) {
            if (!isCurrentGatt(g)) {
                try { g.close(); } catch (Exception ignored) {}
                return;
            }
            if (status == BluetoothGatt.GATT_SUCCESS && newState == android.bluetooth.BluetoothProfile.STATE_CONNECTED) {
                try { g.requestConnectionPriority(BluetoothGatt.CONNECTION_PRIORITY_HIGH); } catch (Exception ignored) {}
                if (!g.discoverServices()) {
                    connected = false;
                    closeGatt(g);
                    CountDownLatch latch = connectLatch;
                    if (latch != null) latch.countDown();
                }
            } else {
                boolean wasConnected = connected;
                connected = false;
                writeChar = null;
                notifyChar = null;
                closeGatt(g);
                CountDownLatch latch = connectLatch;
                if (latch != null) latch.countDown();
                if (wasConnected) notifyConnectionLost(status == BluetoothGatt.GATT_SUCCESS
                        ? "connection_lost" : "connection_lost:" + status);
            }
        }

        @Override public void onServicesDiscovered(BluetoothGatt g, int status) {
            if (!isCurrentGatt(g)) return;
            BluetoothGattService service = status == BluetoothGatt.GATT_SUCCESS ? g.getService(SERVICE_UUID) : null;
            writeChar = service != null ? service.getCharacteristic(WRITE_UUID) : null;
            notifyChar = service != null ? service.getCharacteristic(NOTIFY_UUID) : null;
            connected = writeChar != null;
            if (connected) {
                rememberDevice(g.getDevice());
                if (notifyChar != null) subscribePrivateNotify(frameCallback);
            } else {
                closeGatt(g);
            }
            CountDownLatch latch = connectLatch;
            if (latch != null) latch.countDown();
        }

        @Override public void onCharacteristicWrite(BluetoothGatt g, BluetoothGattCharacteristic c, int status) {
            if (!isCurrentGatt(g)) return;
            lastWriteStatus = status;
            CountDownLatch latch = writeLatch;
            if (latch != null) latch.countDown();
        }

        @Override public void onCharacteristicChanged(BluetoothGatt g, BluetoothGattCharacteristic c) {
            if (!isCurrentGatt(g)) return;
            PrivateFrameCallback callback = frameCallback;
            if (callback != null) decodeNotify(c.getValue(), callback);
        }
    };

    private boolean isCurrentGatt(BluetoothGatt g) {
        return g != null && g == gatt;
    }

    private void notifyConnectionLost(String reason) {
        ConnectionStateCallback callback = connectionStateCallback;
        if (callback != null) callback.onConnectionLost(reason);
    }

    private void closeGatt(BluetoothGatt g) {
        if (g == null) return;
        try { g.close(); } catch (Exception ignored) {}
        synchronized (this) {
            if (gatt == g) gatt = null;
        }
    }

    private void rememberDevice(BluetoothDevice device) {
        if (device == null) return;
        try { connectedAddress = device.getAddress(); } catch (Exception ignored) {}
        try {
            String name = device.getName();
            if (name != null && !name.isEmpty()) connectedName = name;
        } catch (Exception ignored) {}
    }

    private void decodeNotify(byte[] frame, PrivateFrameCallback callback) {
        if (frame == null || frame.length < 10) return;
        int command = ((frame[6] & 0xff) << 8) | (frame[7] & 0xff);
        byte[] body = new byte[Math.max(0, frame.length - 10)];
        if (body.length > 0) System.arraycopy(frame, 8, body, 0, body.length);
        callback.onFrame(command, body);
    }

    private boolean hasBlePermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) return true;
        return ContextCompat.checkSelfPermission(context, Manifest.permission.BLUETOOTH_SCAN) == PackageManager.PERMISSION_GRANTED
                && ContextCompat.checkSelfPermission(context, Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED;
    }

    private void await(CountDownLatch latch, int seconds, String error) {
        try {
            if (!latch.await(seconds, TimeUnit.SECONDS)) throw new IllegalStateException(error);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException(error);
        }
    }

    private static int crc16(byte[] bytes) {
        int crc = 0xffff;
        for (byte b : bytes) {
            crc ^= (b & 0xff) << 8;
            for (int i = 0; i < 8; i++) {
                crc = (crc & 0x8000) != 0 ? ((crc << 1) ^ 0x1021) & 0xffff : (crc << 1) & 0xffff;
            }
        }
        return crc & 0xffff;
    }
}
