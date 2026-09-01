package app.obsidianvow.core;

import android.annotation.SuppressLint;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothManager;
import android.bluetooth.le.AdvertiseCallback;
import android.bluetooth.le.AdvertiseData;
import android.bluetooth.le.AdvertiseSettings;
import android.bluetooth.le.BluetoothLeAdvertiser;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.Looper;
import android.os.ParcelUuid;
import android.util.Log;
import android.webkit.WebView;

import java.util.Random;
import java.util.UUID;

@SuppressLint("MissingPermission")
public class DomForegroundService extends Service {

    private static final String TAG = "DomFgSvc";
    private static final String CHANNEL_ID = "aion_dom";
    private static final int NOTIFICATION_ID = 9001;
    private static final String PHONE_ID = "0000";
    private static final int STOP_GAP_MS = 100;
    private static final int ADV_DWELL_MS = 600;
    private static final int BURST_DWELL_MS = 350;
    private static final int BURST_TICKS = 4;
    private static final int STOP_FRAME_MS = 320;
    private static final int STOP_BURST_ROUNDS = 5;
    private static final int SINGLE_REFRESH_MS = 2000;

    public static final String ACTION_PLAY = "app.obsidianvow.core.dom.PLAY";
    public static final String ACTION_STOP = "app.obsidianvow.core.dom.STOP";
    public static final String ACTION_EMERGENCY_STOP = "app.obsidianvow.core.dom.EMERGENCY_STOP";
    public static final String EXTRA_PAYLOAD = "payload";
    public static final String EXTRA_DEVICE = "device";
    public static final String EXTRA_TRIGGER_PANIC_JS = "trigger_panic_js";

    private static volatile WebView sWebView;
    private static volatile DomForegroundService sInstance;
    private static final Handler sUiHandler = new Handler(Looper.getMainLooper());

    private HandlerThread bleThread;
    private Handler bleHandler;
    private BluetoothLeAdvertiser advertiser;
    private final Random rng = new Random();

    private volatile boolean running = false;
    private volatile String device = "sk30";
    private volatile int curSx = 0;
    private volatile int curPj = 0;
    private volatile int burstRemain = 0;

    private final AdvertiseCallback advCallback = new AdvertiseCallback() {
        @Override
        public void onStartSuccess(AdvertiseSettings settings) { /* ok */ }

        @Override
        public void onStartFailure(int errorCode) {
            Log.w(TAG, "advertise failed: " + errorCode);
        }
    };

    public static void setWebView(WebView wv) {
        sWebView = wv;
    }

    public static boolean isRunning() {
        return sInstance != null && sInstance.running;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        sInstance = this;
        createNotificationChannel();
        bleThread = new HandlerThread("DomBle");
        bleThread.start();
        bleHandler = new Handler(bleThread.getLooper());
        BluetoothManager bm = (BluetoothManager) getSystemService(Context.BLUETOOTH_SERVICE);
        if (bm != null) {
            BluetoothAdapter adapter = bm.getAdapter();
            if (adapter != null) advertiser = adapter.getBluetoothLeAdvertiser();
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null || intent.getAction() == null) {
            stopSelf();
            return START_NOT_STICKY;
        }

        // startForegroundService 要求5秒内调用 startForeground，否则 ANR
        if (!running) {
            Notification notif = buildNotification();
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                startForeground(NOTIFICATION_ID, notif,
                        ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE);
            } else {
                startForeground(NOTIFICATION_ID, notif);
            }
        }

        String dev = intent.getStringExtra(EXTRA_DEVICE);
        if (dev != null) device = dev;

        switch (intent.getAction()) {
            case ACTION_PLAY:
                handlePlay(intent.getStringExtra(EXTRA_PAYLOAD));
                break;
            case ACTION_STOP:
                handleStop();
                break;
            case ACTION_EMERGENCY_STOP:
                handleEmergencyStop(intent.getBooleanExtra(EXTRA_TRIGGER_PANIC_JS, true));
                break;
        }

        return START_NOT_STICKY;
    }

    private void handlePlay(String json) {
        if (advertiser == null) {
            callJs("toyNativeBle.onError('设备不支持BLE广播')");
            return;
        }
        final int oldPj = curPj, oldSx = curSx;
        try {
            org.json.JSONObject j = new org.json.JSONObject(json);
            curPj = Math.max(0, Math.min(10, j.optInt("v", 0)));
            curSx = Math.max(0, Math.min(10, j.optInt("s", 0)));
        } catch (Exception e) {
            callJs("toyNativeBle.onError('参数错误')");
            return;
        }
        bleHandler.removeCallbacksAndMessages(null);
        burstRemain = BURST_TICKS;
        if (!running) {
            running = true;
            callJs("toyNativeBle.onConnected()");
        }
        // 切指令时先停旧的，让 toy 收到停止帧再收新指令
        final boolean modeChanged = (oldPj != curPj || oldSx != curSx) && (oldPj > 0 || oldSx > 0);
        bleHandler.post(() -> {
            if (modeChanged) {
                stopCurrentAdv();
                if (oldSx > 0) { advOnce(buildStopUuid("sx")); sleepSafe(STOP_FRAME_MS); }
                if (oldPj > 0) { advOnce(buildStopUuid("pj")); sleepSafe(STOP_FRAME_MS); }
                stopCurrentAdv();
                sleepSafe(STOP_GAP_MS);
            }
            advLoop();
        });
    }

    private void handleStop() {
        running = false;
        bleHandler.removeCallbacksAndMessages(null);
        callJs("toyNativeBle.onDisconnected()");
        bleHandler.post(() -> {
            burstStop();
            finishStopIfIdle("toyNativeBle.onLog('adv stopped')");
        });
    }

    private void handleEmergencyStop(boolean triggerPanicJs) {
        running = false;
        bleHandler.removeCallbacksAndMessages(null);
        callJs("toyNativeBle.onLog('adv emergency stopping')");
        callJs("toyNativeBle.onDisconnected()");
        if (triggerPanicJs) {
            callJs("typeof aiDomPanic==='function'&&aiDomPanic('device_emergency_stop')");
        }
        bleHandler.post(() -> {
            burstStop();
            finishStopIfIdle("toyNativeBle.onLog('adv emergency stopped')");
        });
    }

    private void finishStopIfIdle(String logJs) {
        if (running) {
            callJs("toyNativeBle.onLog('adv stop interrupted by new play')");
            return;
        }
        callJs(logJs);
        stopForeground(STOP_FOREGROUND_REMOVE);
        stopSelf();
    }

    // ── burst stop: 多轮双通道停止帧 ──

    private void burstStop() {
        curSx = 0;
        curPj = 0;
        String[] targets = stopDeviceOrder();
        for (int i = 0; i < STOP_BURST_ROUNDS; i++) {
            for (String target : targets) {
                if (running) break;
                advOnce(buildStopUuid("sx", target));
                sleepSafe(STOP_FRAME_MS);
                if (running) break;
                advOnce(buildStopUuid("pj", target));
                sleepSafe(STOP_FRAME_MS);
            }
            if (running) break;
        }
        if (!running) stopCurrentAdv();
    }

    private String[] stopDeviceOrder() {
        return "sk40".equals(device)
                ? new String[]{"sk40", "sk30"}
                : new String[]{"sk30", "sk40"};
    }

    // ── 广播循环 ──

    private void advLoop() {
        if (!running) return;
        int sx = curSx, pj = curPj;
        if (sx == 0 && pj == 0) {
            running = false;
            callJs("toyNativeBle.onDisconnected()");
            burstStop();
            finishStopIfIdle("toyNativeBle.onLog('adv stopped (zero)')");
            return;
        }

        boolean burst = burstRemain > 0;
        if (burst) burstRemain--;
        int dwell = burst ? BURST_DWELL_MS : ADV_DWELL_MS;

        if (sx > 0 && pj > 0) {
            advOnce(buildRunUuid("sx", sx));
            bleHandler.postDelayed(() -> {
                if (!running) return;
                advOnce(buildRunUuid("pj", pj));
                bleHandler.postDelayed(this::advLoop, dwell);
            }, dwell);
        } else {
            advOnce(sx > 0 ? buildRunUuid("sx", sx) : buildRunUuid("pj", pj));
            bleHandler.postDelayed(this::advLoop, burst ? dwell : SINGLE_REFRESH_MS);
        }
    }

    private void advOnce(String uuidStr) {
        if (advertiser == null) return;
        try {
            stopCurrentAdv();
            sleepSafe(STOP_GAP_MS);
            AdvertiseSettings settings = new AdvertiseSettings.Builder()
                    .setAdvertiseMode(AdvertiseSettings.ADVERTISE_MODE_LOW_LATENCY)
                    .setTxPowerLevel(AdvertiseSettings.ADVERTISE_TX_POWER_HIGH)
                    .setConnectable(false)
                    .build();
            AdvertiseData data = new AdvertiseData.Builder()
                    .addServiceUuid(new ParcelUuid(UUID.fromString(uuidStr)))
                    .setIncludeDeviceName(false)
                    .setIncludeTxPowerLevel(false)
                    .build();
            advertiser.startAdvertising(settings, data, advCallback);
        } catch (Exception e) {
            Log.e(TAG, "advOnce error", e);
        }
    }

    private void stopCurrentAdv() {
        if (advertiser == null) return;
        try { advertiser.stopAdvertising(advCallback); } catch (Exception ignored) {}
    }

    private void sleepSafe(long ms) {
        try { Thread.sleep(ms); } catch (InterruptedException ignored) {}
    }

    // ── 通知 ──

    private void createNotificationChannel() {
        NotificationChannel ch = new NotificationChannel(
                CHANNEL_ID, "主控模式", NotificationManager.IMPORTANCE_LOW);
        ch.setDescription("主控模式运行中通知");
        ch.setShowBadge(false);
        getSystemService(NotificationManager.class).createNotificationChannel(ch);
    }

    private Notification buildNotification() {
        Intent contentIntent = new Intent(this, WebViewActivity.class);
        contentIntent.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent contentPi = PendingIntent.getActivity(this, 0, contentIntent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        Intent stopIntent = new Intent(this, DomForegroundService.class);
        stopIntent.setAction(ACTION_EMERGENCY_STOP);
        PendingIntent stopPi = PendingIntent.getService(this, 1, stopIntent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        String deviceLabel = "sk40".equals(device) ? "失控4.0" : "失控3.0";

        return new Notification.Builder(this, CHANNEL_ID)
                .setSmallIcon(android.R.drawable.ic_lock_lock)
                .setContentTitle("🔒 主控中")
                .setContentText(deviceLabel)
                .setContentIntent(contentPi)
                .addAction(new Notification.Action.Builder(
                        null, "⏹ 停止", stopPi).build())
                .setOngoing(true)
                .build();
    }

    // ── 协议：构造 UUID ──

    private byte deviceByte() {
        return deviceByte(device);
    }

    private byte deviceByte(String targetDevice) {
        return (byte) ("sk40".equals(targetDevice) ? 0x17 : 0x0b);
    }

    private int sxIntensity(int v) {
        int progress = 50 + v * 5;
        return (int) (progress * 0.5 + 50);
    }

    private int pjIntensity(int s) {
        int gear = Math.max(1, 51 - s * 5);
        if ("sk40".equals(device)) {
            return Math.max(2, 102 - gear);
        } else {
            return Math.max(3, (int) (60 - gear * 0.57));
        }
    }

    private String buildRunUuid(String channel, int level) {
        int cmdSeq = rng.nextInt(0xFF - 0x64 + 1) + 0x64;
        String dev = String.format("%02x", deviceByte() & 0xFF);
        String seq = String.format("%02x", cmdSeq);
        String template;
        if ("sx".equals(channel)) {
            int I = sxIntensity(level);
            if ("sk40".equals(device)) {
                template = String.format("7100%s%s-5100-%s-0100-640000%02x02", dev, seq, PHONE_ID, I);
            } else {
                template = String.format("7100%s%s-8200-%s-0100-640000%02x02", dev, seq, PHONE_ID, I);
            }
        } else {
            int I = pjIntensity(level);
            if ("sk40".equals(device)) {
                template = String.format("7100%s%s-5200-%s-0100-2d%02x150002", dev, seq, PHONE_ID, I);
            } else {
                template = String.format("7100%s%s-8100-%s-0100-0a%02x1c0002", dev, seq, PHONE_ID, I);
            }
        }
        return appendChecksum(template);
    }

    private String buildStopUuid(String channel) {
        return buildStopUuid(channel, device);
    }

    private String buildStopUuid(String channel, String targetDevice) {
        int cmdSeq = rng.nextInt(0xFF - 0x64 + 1) + 0x64;
        String dev = String.format("%02x", deviceByte(targetDevice) & 0xFF);
        String seq = String.format("%02x", cmdSeq);
        String template;
        if ("sx".equals(channel)) {
            if ("sk40".equals(targetDevice)) {
                template = String.format("7100%s%s-0100-%s-0100-6400000002", dev, seq, PHONE_ID);
            } else {
                template = String.format("7100%s%s-0200-%s-0100-6400000002", dev, seq, PHONE_ID);
            }
        } else {
            if ("sk40".equals(targetDevice)) {
                template = String.format("7100%s%s-0200-%s-0100-6400000002", dev, seq, PHONE_ID);
            } else {
                template = String.format("7100%s%s-0100-%s-0100-6400000002", dev, seq, PHONE_ID);
            }
        }
        return appendChecksum(template);
    }

    private String appendChecksum(String uuidNoDash34) {
        String noDash = uuidNoDash34.replace("-", "");
        int sum = 0;
        for (int i = 0; i < noDash.length(); i += 2) {
            sum += Integer.parseInt(noDash.substring(i, i + 2), 16);
        }
        String cs = String.format("%02x", sum % 256);
        return uuidNoDash34 + cs;
    }

    // ── 工具 ──

    private void callJs(String js) {
        WebView wv = sWebView;
        if (wv == null) return;
        sUiHandler.post(() -> wv.evaluateJavascript(
                "typeof toyNativeBle!=='undefined'&&" + js, null));
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        running = false;
        if (bleHandler != null) bleHandler.removeCallbacksAndMessages(null);
        if (bleThread != null) bleThread.quitSafely();
        sInstance = null;
        super.onDestroy();
    }
}
