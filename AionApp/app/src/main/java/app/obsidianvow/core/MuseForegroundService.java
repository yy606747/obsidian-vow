package app.obsidianvow.core;

import android.annotation.SuppressLint;
import android.app.AlarmManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.bluetooth.le.AdvertiseCallback;
import android.bluetooth.le.BluetoothLeAdvertiser;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.Looper;
import android.os.SystemClock;
import android.webkit.WebView;

import org.json.JSONObject;

@SuppressLint("MissingPermission")
public class MuseForegroundService extends Service {
    private static final String TAG = "MuseFgSvc";
    private static final String CHANNEL_ID = "aion_muse";
    private static final int NOTIFICATION_ID = 9003;
    private static final int DEFAULT_TTL_MS = 3000;

    public static final String ACTION_FRAME = "app.obsidianvow.core.muse.FRAME";
    public static final String ACTION_STOP = "app.obsidianvow.core.muse.STOP";
    public static final String ACTION_EMERGENCY_STOP = "app.obsidianvow.core.muse.EMERGENCY_STOP";
    public static final String ACTION_TTL_STOP = "app.obsidianvow.core.muse.TTL_STOP";
    public static final String EXTRA_PAYLOAD = "payload";

    private static volatile WebView sWebView;
    private static final Handler sUiHandler = new Handler(Looper.getMainLooper());

    private volatile boolean running = false;
    private volatile boolean stopBurstCompleted = true;
    private volatile int curVib = 0;
    private volatile int curThrust = 0;
    private final Handler watchdogHandler = new Handler(Looper.getMainLooper());
    private final Runnable ttlStopRunnable = () -> {
        if (running) handleGlobalStop(ACTION_TTL_STOP);
    };
    private HandlerThread bleThread;
    private Handler bleHandler;
    private BluetoothLeAdvertiser advertiser;
    private AlarmManager alarmManager;

    private final AdvertiseCallback advCallback = MuseBleCommands.callback(TAG);

    public static void setWebView(WebView wv) {
        sWebView = wv;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        createNotificationChannel();
        bleThread = new HandlerThread("MuseBle");
        bleThread.start();
        bleHandler = new Handler(bleThread.getLooper());
        alarmManager = (AlarmManager) getSystemService(Context.ALARM_SERVICE);
        advertiser = MuseBleCommands.advertiser(this);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null || intent.getAction() == null) {
            stopSelf();
            return START_NOT_STICKY;
        }
        ensureForeground();
        String action = intent.getAction();
        if (ACTION_FRAME.equals(action)) {
            handleFrame(intent.getStringExtra(EXTRA_PAYLOAD));
        } else if (ACTION_STOP.equals(action) || ACTION_EMERGENCY_STOP.equals(action) || ACTION_TTL_STOP.equals(action)) {
            handleGlobalStop(action);
        }
        return START_NOT_STICKY;
    }

    private void handleFrame(String json) {
        if (advertiser == null) {
            callJs("tideNativeBle.onError('设备不支持BLE广播')");
            return;
        }
        final int nextVib;
        final int nextThrust;
        final int ttlMs;
        try {
            JSONObject obj = new JSONObject(json == null ? "{}" : json);
            nextVib = clampPattern(obj.optInt("vib_pattern", 0));
            nextThrust = clampPattern(obj.optInt("thrust_pattern", 0));
            ttlMs = Math.max(500, Math.min(DEFAULT_TTL_MS, obj.optInt("ttl_ms", DEFAULT_TTL_MS)));
        } catch (Exception e) {
            callJs("tideNativeBle.onError('Muse参数错误')");
            return;
        }
        if (nextVib == 0 && nextThrust == 0) {
            handleGlobalStop(ACTION_STOP);
            return;
        }

        final int oldVib = curVib;
        final int oldThrust = curThrust;
        running = true;
        stopBurstCompleted = false;
        curVib = nextVib;
        curThrust = nextThrust;
        scheduleLocalWatchdog(ttlMs);
        scheduleTtlStop(ttlMs);
        callJs("tideNativeBle.onConnected()");

        if (oldVib == nextVib && oldThrust == nextThrust) return;
        bleHandler.removeCallbacksAndMessages(null);
        bleHandler.post(() -> {
            if (oldVib != nextVib) {
                if (oldVib > 0) advertiseCommand(0x30);
                if (nextVib > 0) advertiseCommand(0x30 + nextVib);
            }
            if (oldThrust != nextThrust) {
                if (oldThrust > 0) advertiseCommand(0x40);
                if (nextThrust > 0) advertiseCommand(0x40 + nextThrust);
            }
        });
    }

    private void handleGlobalStop(String action) {
        running = false;
        stopBurstCompleted = false;
        curVib = 0;
        curThrust = 0;
        cancelLocalWatchdog();
        if (bleHandler != null) bleHandler.removeCallbacksAndMessages(null);
        callJs("tideNativeBle.onDisconnected()");
        if (bleHandler != null) {
            bleHandler.post(() -> {
                for (int i = 0; i < MuseBleCommands.STOP_REPEATS; i++) advertiseCommand(0x00);
                stopCurrentAdv();
                stopBurstCompleted = true;
                cancelTtlStop();
                stopForeground(STOP_FOREGROUND_REMOVE);
                stopSelf();
            });
        }
    }

    private int clampPattern(int value) {
        return Math.max(0, Math.min(9, value));
    }

    private void advertiseCommand(int command) {
        MuseBleCommands.advertiseCommand(advertiser, advCallback, command, TAG);
    }

    private void stopCurrentAdv() {
        MuseBleCommands.stopCurrentAdv(advertiser, advCallback);
    }

    private void scheduleLocalWatchdog(int ttlMs) {
        watchdogHandler.removeCallbacks(ttlStopRunnable);
        watchdogHandler.postDelayed(ttlStopRunnable, ttlMs);
    }

    private void cancelLocalWatchdog() {
        watchdogHandler.removeCallbacks(ttlStopRunnable);
    }

    private void scheduleTtlStop(int ttlMs) {
        if (alarmManager == null) return;
        PendingIntent pi = ttlPendingIntent();
        long at = SystemClock.elapsedRealtime() + ttlMs;
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                alarmManager.setExactAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, at, pi);
            } else {
                alarmManager.setExact(AlarmManager.ELAPSED_REALTIME_WAKEUP, at, pi);
            }
        } catch (SecurityException e) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                alarmManager.setAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, at, pi);
            } else {
                alarmManager.set(AlarmManager.ELAPSED_REALTIME_WAKEUP, at, pi);
            }
        }
    }

    private void cancelTtlStop() {
        if (alarmManager != null) alarmManager.cancel(ttlPendingIntent());
    }

    private PendingIntent ttlPendingIntent() {
        Intent intent = new Intent(this, MuseStopReceiver.class);
        intent.setAction(ACTION_TTL_STOP);
        return PendingIntent.getBroadcast(
                this,
                91,
                intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
    }

    private void ensureForeground() {
        Notification notification = buildNotification();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            startForeground(
                    NOTIFICATION_ID,
                    notification,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE
            );
        } else {
            startForeground(NOTIFICATION_ID, notification);
        }
    }

    private void createNotificationChannel() {
        NotificationChannel ch = new NotificationChannel(
                CHANNEL_ID, "亲密互动", NotificationManager.IMPORTANCE_LOW);
        ch.setDescription("亲密互动运行中通知");
        ch.setShowBadge(false);
        getSystemService(NotificationManager.class).createNotificationChannel(ch);
    }

    private Notification buildNotification() {
        Intent contentIntent = new Intent(this, WebViewActivity.class);
        contentIntent.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent contentPi = PendingIntent.getActivity(
                this, 0, contentIntent, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        Intent stopIntent = new Intent(this, MuseForegroundService.class);
        stopIntent.setAction(ACTION_EMERGENCY_STOP);
        PendingIntent stopPi = PendingIntent.getService(
                this, 1, stopIntent, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        return new Notification.Builder(this, CHANNEL_ID)
                .setSmallIcon(android.R.drawable.ic_lock_idle_alarm)
                .setContentTitle("亲密中")
                .setContentText("连接中断后会自动停止")
                .setContentIntent(contentPi)
                .addAction(new Notification.Action.Builder(null, "停止", stopPi).build())
                .setOngoing(true)
                .build();
    }

    private void callJs(String js) {
        WebView wv = sWebView;
        if (wv == null) return;
        sUiHandler.post(() -> wv.evaluateJavascript(
                "typeof tideNativeBle!=='undefined'&&" + js, null));
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        boolean stoppedCleanly = !running && stopBurstCompleted;
        running = false;
        cancelLocalWatchdog();
        if (stoppedCleanly) cancelTtlStop();
        if (bleHandler != null) bleHandler.removeCallbacksAndMessages(null);
        if (bleThread != null) bleThread.quitSafely();
        stopCurrentAdv();
        super.onDestroy();
    }
}
