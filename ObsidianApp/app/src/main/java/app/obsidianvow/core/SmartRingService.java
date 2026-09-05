package app.obsidianvow.core;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.util.Log;

import androidx.annotation.Nullable;
import androidx.core.app.NotificationCompat;
import androidx.core.content.ContextCompat;

import org.json.JSONObject;

import java.time.Instant;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class SmartRingService extends Service {
    private static final String TAG = "SmartRingService";
    private static final String CH_RING = "obsidian_smart_ring";
    private static final int NOTIF_ID = 32;
    private static final String ACTION_TOUCH = "app.obsidianvow.core.RING_TOUCH";
    private static final String ACTION_CONNECT = "app.obsidianvow.core.RING_CONNECT";
    private static final String EXTRA_PAYLOAD = "payload";
    private static final String DEFAULT_NAME_PREFIX = "AIZO";
    private static final String PREFS = "smart_ring";
    private static final String PREF_KEEP_CONNECTED = "keep_connected";
    private static final String PREF_LAST_ADDRESS = "last_address";
    private static final String PREF_LAST_NAME = "last_name";
    private static final long IDLE_DISCONNECT_MS = 10 * 60 * 1000L;
    private static final long KEEPALIVE_RETRY_MS = 60 * 1000L;

    private static volatile boolean cachedBleConnected = false;
    private static volatile boolean keepConnected = false;
    private static volatile String cachedStateError = "";
    private static volatile SmartRingService activeService;
    private static volatile long lastKeepAliveStartMs = 0L;

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final Handler idleHandler = new Handler(Looper.getMainLooper());
    private SmartRingBridge bridge;
    private final Runnable reconnectRunnable = () -> {
        if (!keepConnected || isBridgeConnected()) return;
        JSONObject data = new JSONObject();
        try { data.put("keep_connected", true); } catch (Exception ignored) {}
        connectAndReport(getApplicationContext(), data);
    };
    private final Runnable idleStopRunnable = () -> {
        Log.i(TAG, "ring idle timeout, disconnecting");
        try {
            if (bridge != null) bridge.disconnect();
        } catch (Exception ignored) {}
        reportState("offline", "", "idle_disconnect");
        updateRingNotification("💤 戒指已休眠");
        stopSelf();
    };

    public static void execute(Context context, JSONObject data) {
        start(context, ACTION_TOUCH, data);
    }

    public static void connectAndReport(Context context, JSONObject data) {
        start(context, ACTION_CONNECT, data);
    }

    public static void reportCachedStateForWs() {
        try {
            boolean connected = isBleConnectedNow();
            JSONObject meta = new JSONObject();
            meta.put("phone_ws_online", true);
            meta.put("ble_connected", connected);
            if (connected) {
                meta.put("hint", "💍 戒指在线 ✨");
                meta.put("keep_connected", keepConnected);
            } else {
                meta.put("hint", "💍 点击连接戒指");
                meta.put("keep_connected", keepConnected);
                if (!cachedStateError.isEmpty()) meta.put("connect_error", cachedStateError);
            }
            ObsidianPushService.sendSmartRingStateReport(connected ? "online" : "offline", meta);
        } catch (Exception e) {
            Log.w(TAG, "cached state report failed: " + e.getMessage());
        }
    }

    public static void reportCachedStateForWs(Context context) {
        loadKeepConnected(context);
        reportCachedStateForWs();
    }

    public static void keepAliveFromWs(Context context, JSONObject data) {
        rememberKeepConnected(context, data);
        if (keepConnected && !isBleConnectedNow()) {
            long now = System.currentTimeMillis();
            if (now - lastKeepAliveStartMs < KEEPALIVE_RETRY_MS) {
                reportCachedStateForWs();
                return;
            }
            lastKeepAliveStartMs = now;
            start(context, ACTION_CONNECT, data == null ? new JSONObject() : data);
        } else {
            reportCachedStateForWs();
        }
    }

    private static void start(Context context, String action, JSONObject data) {
        Intent intent = new Intent(context, SmartRingService.class);
        intent.setAction(action);
        intent.putExtra(EXTRA_PAYLOAD, data == null ? "{}" : data.toString());
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) context.startForegroundService(intent);
        else context.startService(intent);
    }

    @Override public void onCreate() {
        super.onCreate();
        activeService = this;
        createNotificationChannel();
        loadKeepConnected(getApplicationContext());
        bridge = new SmartRingBridge(getApplicationContext());
        bridge.setConnectionStateCallback(reason ->
                idleHandler.post(() -> handleConnectionLost(reason)));
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (isBridgeConnected()) {
            startRingForeground("💍 戒指在线 ✨");
        } else {
            startRingForeground("💍 戒指连接中…");
        }
        idleHandler.removeCallbacks(idleStopRunnable);
        if (intent != null && ACTION_TOUCH.equals(intent.getAction())) {
            String payload = intent.getStringExtra(EXTRA_PAYLOAD);
            executor.execute(() -> {
                runTouch(payload);
                finishCommand(startId);
            });
        } else if (intent != null && ACTION_CONNECT.equals(intent.getAction())) {
            String payload = intent.getStringExtra(EXTRA_PAYLOAD);
            executor.execute(() -> {
                connectOnly(payload);
                finishCommand(startId);
            });
        }
        return START_NOT_STICKY;
    }

    @Nullable @Override public IBinder onBind(Intent intent) {
        return null;
    }

    @Override public void onDestroy() {
        idleHandler.removeCallbacks(idleStopRunnable);
        idleHandler.removeCallbacks(reconnectRunnable);
        executor.shutdownNow();
        boolean wasConnected = isBridgeConnected();
        if (bridge != null) bridge.disconnect();
        if (wasConnected) reportState("offline", "", "service_destroyed");
        else reportCachedConnection(false, "");
        if (activeService == this) activeService = null;
        super.onDestroy();
    }

    private void runTouch(String payload) {
        String requestId = "";
        try {
            JSONObject data = new JSONObject(payload == null ? "{}" : payload);
            requestId = data.optString("request_id", "");
            rememberKeepConnected(data);
            if (isExpired(data.optString("expires_at", ""))) {
                sendAck(requestId, "skipped_stale", "expired");
                return;
            }
            if (!hasBlePermission()) {
                reportState("offline", requestId, "permission_missing");
                sendAck(requestId, "failed", "permission_missing");
                return;
            }
            if (!bridge.isConnected()) bridge.connect(namePrefix(data), cachedRingAddress());
            rememberConnectedRing();
            reportState("online", requestId, "");
            int taps = clamp(data.optInt("taps", 1), 1, 10);
            int intervalMs = clamp(data.optInt("interval_ms", 2000), 1000, 5000);
            int alertType = clamp(data.optInt("alert_type", 5), 1, 6);
            for (int i = 0; i < taps; i++) {
                bridge.sendAlert(alertType);
                if (i + 1 < taps) Thread.sleep(intervalMs);
            }
            sendAck(requestId, "executed", "");
        } catch (Exception e) {
            Log.w(TAG, "ring touch failed: " + e.getMessage());
            sendAck(requestId, "failed", e.getMessage());
            reportState("offline", requestId, e.getMessage());
            try {
                if (bridge != null) bridge.disconnect();
            } catch (Exception ignored) {}
        }
    }

    private void connectOnly(String payload) {
        String requestId = "";
        try {
            JSONObject data = new JSONObject(payload == null ? "{}" : payload);
            requestId = data.optString("request_id", "");
            rememberKeepConnected(data);
            if (!hasBlePermission()) {
                reportState("offline", requestId, "permission_missing");
                return;
            }
            if (!bridge.isConnected()) bridge.connect(namePrefix(data), cachedRingAddress());
            rememberConnectedRing();
            reportState("online", requestId, "");
        } catch (Exception e) {
            Log.w(TAG, "ring connect failed: " + e.getMessage());
            reportState("offline", requestId, e.getMessage());
            try {
                if (bridge != null) bridge.disconnect();
            } catch (Exception ignored) {}
        }
    }

    private void reportState(String status, String requestId, String error) {
        try {
            boolean online = "online".equals(status);
            reportCachedConnection(online, online ? "" : error);
            JSONObject meta = new JSONObject();
            meta.put("phone_ws_online", true);
            meta.put("ble_connected", online);
            meta.put("keep_connected", keepConnected);
            meta.put("request_id", requestId == null ? "" : requestId);
            if ("idle_disconnect".equals(error)) {
                meta.put("hint", "💤 空闲休眠了，点击重连");
            } else if ("service_destroyed".equals(error)) {
                meta.put("hint", "💍 服务已停止，点击重连");
            } else if (error != null && !error.isEmpty()) {
                meta.put("connect_error", error);
            }
            ObsidianPushService.sendSmartRingStateReport(status, meta);
        } catch (Exception e) {
            Log.w(TAG, "state report failed: " + e.getMessage());
        }
    }

    private void finishCommand(int startId) {
        idleHandler.post(() -> {
            if (isBridgeConnected()) {
                idleHandler.removeCallbacks(reconnectRunnable);
                updateRingNotification("💍 戒指在线 ✨");
                if (keepConnected) {
                    // keep service alive — do NOT call stopSelf
                } else {
                    idleHandler.postDelayed(idleStopRunnable, IDLE_DISCONNECT_MS);
                }
            } else {
                updateRingNotification("💍 戒指未连接");
                if (keepConnected) scheduleReconnect(KEEPALIVE_RETRY_MS);
                else stopSelf(startId);
            }
        });
    }

    private void handleConnectionLost(String reason) {
        reportState("offline", "", reason == null || reason.isEmpty() ? "connection_lost" : reason);
        updateRingNotification("💍 戒指断开，等待重连");
        idleHandler.removeCallbacks(idleStopRunnable);
        if (keepConnected) scheduleReconnect(0L);
        else stopSelf();
    }

    private void scheduleReconnect(long delayMs) {
        idleHandler.removeCallbacks(reconnectRunnable);
        idleHandler.postDelayed(reconnectRunnable, Math.max(0L, delayMs));
    }

    private boolean isBridgeConnected() {
        try {
            return bridge != null && bridge.isConnected();
        } catch (Exception ignored) {
            return false;
        }
    }

    private static boolean isBleConnectedNow() {
        SmartRingService service = activeService;
        if (service == null) return cachedBleConnected;
        boolean connected = service.isBridgeConnected();
        if (!connected && cachedBleConnected) reportCachedConnection(false, "connection_lost");
        return connected;
    }

    private static void reportCachedConnection(boolean connected, String error) {
        cachedBleConnected = connected;
        if (connected) lastKeepAliveStartMs = 0L;
        cachedStateError = connected
                || error == null
                || "idle_disconnect".equals(error)
                || "service_destroyed".equals(error) ? "" : error;
    }

    private void rememberKeepConnected(JSONObject data) {
        rememberKeepConnected(getApplicationContext(), data);
    }

    private static void rememberKeepConnected(Context context, JSONObject data) {
        if (data == null || !data.has("keep_connected")) {
            loadKeepConnected(context);
            return;
        }
        keepConnected = data.optBoolean("keep_connected", keepConnected);
        if (context == null) return;
        prefs(context).edit().putBoolean(PREF_KEEP_CONNECTED, keepConnected).apply();
    }

    private static void loadKeepConnected(Context context) {
        if (context == null) return;
        keepConnected = prefs(context).getBoolean(PREF_KEEP_CONNECTED, keepConnected);
    }

    private String cachedRingAddress() {
        return prefs(getApplicationContext()).getString(PREF_LAST_ADDRESS, "");
    }

    private void rememberConnectedRing() {
        if (bridge == null || !bridge.isConnected()) return;
        String address = bridge.getConnectedAddress();
        if (address == null || address.isEmpty()) return;
        SharedPreferences.Editor editor = prefs(getApplicationContext()).edit()
                .putString(PREF_LAST_ADDRESS, address);
        String name = bridge.getConnectedName();
        if (name != null && !name.isEmpty()) editor.putString(PREF_LAST_NAME, name);
        editor.apply();
    }

    private static SharedPreferences prefs(Context context) {
        return context.getApplicationContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    private void sendAck(String requestId, String status, String error) {
        try {
            JSONObject data = new JSONObject();
            data.put("request_id", requestId == null ? "" : requestId);
            data.put("status", status);
            if (error != null && !error.isEmpty()) data.put("error", error);
            boolean sent = ObsidianPushService.sendRingTouchAck(data);
            Log.i(TAG, "ack " + status + " request=" + requestId + " sent=" + sent);
        } catch (Exception e) {
            Log.w(TAG, "ack failed: " + e.getMessage());
        }
    }

    private boolean isExpired(String expiresAt) {
        if (expiresAt == null || expiresAt.isEmpty()) return false;
        try {
            return Instant.parse(expiresAt).isBefore(Instant.now());
        } catch (Exception ignored) {
            return false;
        }
    }

    private boolean hasBlePermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) return true;
        return ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_SCAN) == PackageManager.PERMISSION_GRANTED
                && ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED;
    }

    private String namePrefix(JSONObject data) {
        String prefix = data.optString("name_prefix", DEFAULT_NAME_PREFIX).trim();
        return prefix.isEmpty() ? DEFAULT_NAME_PREFIX : prefix;
    }

    private int clamp(int value, int low, int high) {
        return Math.max(low, Math.min(high, value));
    }

    private void updateRingNotification(String text) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm == null) return;
        Notification notification = new NotificationCompat.Builder(this, CH_RING)
                .setSmallIcon(R.mipmap.ic_launcher)
                .setContentTitle("Obsidian Vow")
                .setContentText(text)
                .setOngoing(true)
                .setPriority(NotificationCompat.PRIORITY_LOW)
                .build();
        nm.notify(NOTIF_ID, notification);
    }

    private void startRingForeground(String text) {
        Notification notification = new NotificationCompat.Builder(this, CH_RING)
                .setSmallIcon(R.mipmap.ic_launcher)
                .setContentTitle("Obsidian Vow")
                .setContentText(text)
                .setOngoing(true)
                .setPriority(NotificationCompat.PRIORITY_LOW)
                .build();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(NOTIF_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE);
        } else {
            startForeground(NOTIF_ID, notification);
        }
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm == null) return;
        NotificationChannel channel = new NotificationChannel(CH_RING, "💍 智能戒指", NotificationManager.IMPORTANCE_LOW);
        channel.setShowBadge(false);
        BrandCompatibility.createNotificationChannel(nm, channel);
    }
}
