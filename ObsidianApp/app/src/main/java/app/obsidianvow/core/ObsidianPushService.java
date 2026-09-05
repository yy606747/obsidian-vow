package app.obsidianvow.core;

import android.app.AlarmManager;
import android.app.KeyguardManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.net.Uri;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.os.SystemClock;
import android.provider.AlarmClock;
import android.util.Log;
import android.service.notification.StatusBarNotification;

import androidx.annotation.Nullable;
import androidx.core.app.NotificationCompat;

import org.json.JSONArray;
import org.json.JSONObject;

import okhttp3.Interceptor;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;

import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.time.Duration;
import java.time.LocalDateTime;

import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
import android.media.AudioManager;
import android.media.MediaPlayer;

import android.Manifest;
import android.content.pm.PackageManager;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.os.Bundle;
import androidx.core.content.ContextCompat;
import okhttp3.MediaType;
import okhttp3.RequestBody;

import android.app.usage.UsageStats;
import android.app.usage.UsageStatsManager;
import android.app.usage.UsageEvents;
import android.provider.Settings;

import android.content.BroadcastReceiver;
import android.content.IntentFilter;
import android.text.TextUtils;

/**
 * 前台服务 — OkHttp WebSocket 长连接
 * 针对 vivo/OPPO 等 ROM 做了适配：
 * 1. Thread.sleep 心跳（不依赖 Handler/Looper）
 * 2. ConnectivityManager.NetworkCallback 监听网络变化
 * 3. synchronized connectWebSocket 防并发竞争
 * 4. onFailure 不阻塞 OkHttp 回调线程
 * 5. fullScreenIntent 闹铃通知（锁屏也能亮屏弹出）
 */
public class ObsidianPushService extends Service {

    private static final String TAG = "ObsidianPush";

    private static final String CH_KEEPALIVE     = "obsidian_keepalive";
    private static final String CH_MESSAGE       = "obsidian_message";
    private static final String CH_MESSAGE_V2    = "obsidian_message_v2";
    private static final String CH_ALARM         = "obsidian_alarm";

    private static final int NOTIF_FOREGROUND = 1;
    private static final int NOTIF_MSG_BASE   = 1000;
    private static final String NOTIF_MSG_TAG_PREFIX = "ai_message:";
    private static final String PREF_NATIVE_ALARM_IDS = "native_alarm_ids";
    private static final String PREF_NATIVE_ALARM_CANCEL_PREFIX = "native_alarm_cancel_";
    private static final String PREF_NATIVE_ALARM_SET_PREFIX = "native_alarm_set_";
    private static final long NATIVE_ALARM_MAX_LEAD_SECONDS = 24 * 60 * 60;
    private static final long ALARM_NOTIFICATION_TIMEOUT_MS = 5 * 60_000L;
    private static final long SCREEN_REQUEST_NOTIFICATION_TIMEOUT_MS = 2 * 60_000L;

    private static final long HEARTBEAT_MS  = 60_000;  // 60s 应用层心跳
    private static final long HEALTH_TIMEOUT = 180_000; // 180s 无消息 → 重连

    private static volatile ObsidianPushService activeInstance;

    private OkHttpClient client;
    private volatile WebSocket webSocket;
    private volatile String serverUrl;
    private volatile String authToken = "";
    private int notifCounter = 0;

    private final AtomicInteger wsGeneration = new AtomicInteger(0);
    private final AtomicBoolean wsConnected = new AtomicBoolean(false);

    private volatile int reconnectDelay = 3000;
    private static final int MAX_RECONNECT_DELAY = 30000;
    private volatile boolean shouldRun = true;
    private volatile boolean isForegroundActive = false;

    private PowerManager.WakeLock wakeLock;
    private WifiManager.WifiLock wifiLock;
    private Thread heartbeatThread;
    private MediaPlayer mediaPlayer;
    private AudioManager audioManager;
    private AudioFocusRequest musicFocusRequest;
    private volatile boolean resumeMusicOnFocusGain = false;
    private static final AudioAttributes MUSIC_AUDIO_ATTRIBUTES =
            new AudioAttributes.Builder()
                    .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .build();
    private final AudioManager.OnAudioFocusChangeListener musicFocusListener =
            this::handleMusicAudioFocusChange;

    private volatile int msgReceived = 0;
    private volatile long lastMessageTime = 0;

    private ConnectivityManager connectivityManager;
    private ConnectivityManager.NetworkCallback networkCallback;

    // ── 定位上报 ──
    private static final long LOCATION_INTERVAL = 10 * 60_000;          // 统一 10 分钟（服务端做智能过滤，非每次都调 API）
    private static final long LOCATION_INTERVAL_DISABLED = 10 * 60_000; // 功能未启用/静默时段时低频轮询开关状态
    private static final long LOCATION_CACHE_MAX_AGE_MS = 2 * 60_000;   // 只把很新的缓存当作当前定位
    private static final long LOCATION_FRESH_TIMEOUT_MS = 30_000;       // 单次定位最多等待 30 秒
    private static final long LOCATION_ACCURACY_STALENESS_GRACE_MS = 60_000;
    private Thread locationThread;
    private volatile long locationInterval = LOCATION_INTERVAL;
    private LocationManager locationManager;
    private volatile Location lastKnownLocation;
    private volatile boolean locationEnabled = false;  // 服务端定位开关状态

    // ── 活动上报 ──
    private static final long ACTIVITY_INTERVAL = 90_000;  // 90秒检测一次前台应用
    private static final long ACTIVITY_RE_REPORT_MS = 5 * 60_000;  // 同一App超过5分钟重新上报
    private Thread activityThread;
    private volatile String lastReportedApp = "";
    private volatile long lastReportedTime = 0;
    private volatile boolean screenOn = true;
    private BroadcastReceiver screenReceiver;

    // ── 体感 / 体征上报 ──
    // ── 移动端截图轮询 ──────────────────────────────────────
    private static final int SCREEN_POLL_TIMEOUT_SEC = 30;     // 与后端 wait_pending 上限一致
    private static final int NOTIF_SCREEN_REQ = 2000;
    private static final long SCREEN_POLL_ERROR_BACKOFF_MS = 10_000;
    private static final long SCREEN_POLL_PENDING_THROTTLE_MS = 5_000;  // pending 未处理时节流
    private Thread screenPollThread;

    private static final long SENSING_INTERVAL = 5 * 60_000L;  // 5 分钟采样一次（原 15 分钟太稀疏，哨兵判断信号不足）
    private Thread sensingThread;
    private SensingReporter sensingReporter;
    private HealthConnectReporter healthConnectReporter;

    // ══════════════════════════════════════════════════════════
    //  生命周期
    // ══════════════════════════════════════════════════════════

    @Override
    public void onCreate() {
        super.onCreate();
        Log.i(TAG, "=== onCreate ===");
        activeInstance = this;
        createNotificationChannels();
        audioManager = getSystemService(AudioManager.class);

        // 不再常驻持有 PARTIAL_WAKE_LOCK / HIGH_PERF WifiLock。
        // 旧实现会让前台服务持续阻止 CPU / Wi-Fi 进入省电状态，造成明显前台耗电。
        wakeLock = null;
        wifiLock = null;

        // Bearer Token 拦截器：authToken 非空时给所有请求加上 Authorization 头
        Interceptor authInterceptor = chain -> {
            Request original = chain.request();
            String t = authToken;
            if (t != null && !t.isEmpty() && original.header("Authorization") == null) {
                original = original.newBuilder()
                        .header("Authorization", "Bearer " + t)
                        .build();
            }
            return chain.proceed(original);
        };

        client = new OkHttpClient.Builder()
                .addInterceptor(authInterceptor)
                .readTimeout(0, TimeUnit.SECONDS)
                .connectTimeout(10, TimeUnit.SECONDS)
                .build();

        registerNetworkCallback();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null) {
            String action = intent.getStringExtra("action");
            if ("set_foreground".equals(action)) {
                isForegroundActive = intent.getBooleanExtra("active", false);
                if (isForegroundActive) {
                    stopMusic(); // WebView 接管，停止原生播放
                    new Handler(Looper.getMainLooper()).post(() -> {
                        retryPendingNativeAlarmCancellations();
                        retryPendingNativeAlarmSets();
                    });
                }
                Log.d(TAG, "foreground=" + isForegroundActive);
                return START_STICKY;
            }

            String token = intent.getStringExtra("token");
            if (token != null) authToken = token;

            String url = intent.getStringExtra("url");
            if (url != null) {
                String ws = toWebSocketUrl(url);
                if (ws.equals(serverUrl) && wsConnected.get()) {
                    Log.d(TAG, "Already connected to " + serverUrl);
                    return START_STICKY;
                }
                serverUrl = ws;
            }
        }

        if (serverUrl == null) {
            SharedPreferences prefs = getSharedPreferences("obsidian_prefs", MODE_PRIVATE);
            String saved = prefs.getString("saved_url", "");
            if (TextUtils.isEmpty(saved)) {
                Log.w(TAG, "No saved URL — service idle until user configures");
                stopSelf();
                return START_NOT_STICKY;
            }
            authToken = prefs.getString("auth_token", "");
            serverUrl = toWebSocketUrl(saved);
        }

        Log.i(TAG, "onStartCommand url=" + serverUrl);

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            // Android 14+: 需要声明所有用到的前台服务类型
            int serviceType = ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC;
            serviceType |= ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK;
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
                    == PackageManager.PERMISSION_GRANTED) {
                serviceType |= ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION;
            }
            startForeground(NOTIF_FOREGROUND, buildKeepAlive("连接中..."), serviceType);
        } else {
            startForeground(NOTIF_FOREGROUND, buildKeepAlive("连接中..."));
        }

        shouldRun = true;
        startHeartbeatThread();
        startLocationThread();
        startActivityThread();
        startSensingThread();
        startScreenPollThread();
        return START_STICKY;
    }

    @Nullable @Override
    public IBinder onBind(Intent intent) { return null; }

    @Override
    public void onDestroy() {
        Log.i(TAG, "=== onDestroy ===");
        shouldRun = false;
        wsGeneration.incrementAndGet();
        if (heartbeatThread != null) heartbeatThread.interrupt();
        if (locationThread != null) locationThread.interrupt();
        if (activityThread != null) activityThread.interrupt();
        if (sensingThread != null) sensingThread.interrupt();
        if (screenPollThread != null) screenPollThread.interrupt();
        unregisterScreenReceiver();
        if (webSocket != null) try { webSocket.cancel(); } catch (Exception ignored) {}
        if (client != null) client.dispatcher().executorService().shutdown();
        stopMusic();
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        if (wifiLock != null && wifiLock.isHeld()) wifiLock.release();
        unregisterNetworkCallback();
        if (activeInstance == this) activeInstance = null;
        super.onDestroy();
    }

    public static boolean sendRingTouchAck(JSONObject data) {
        ObsidianPushService service = activeInstance;
        if (service == null || service.webSocket == null || !service.wsConnected.get()) return false;
        try {
            JSONObject msg = new JSONObject();
            msg.put("type", "ring_touch_ack");
            msg.put("data", data == null ? new JSONObject() : data);
            return service.webSocket.send(msg.toString());
        } catch (Exception e) {
            Log.w(TAG, "ring ack send failed: " + e.getMessage());
            return false;
        }
    }

    public static boolean sendDeviceStateReport(JSONObject data) {
        ObsidianPushService service = activeInstance;
        if (service == null || service.webSocket == null || !service.wsConnected.get()) return false;
        try {
            JSONObject msg = new JSONObject();
            msg.put("type", "device_state_report");
            msg.put("data", data == null ? new JSONObject() : data);
            return service.webSocket.send(msg.toString());
        } catch (Exception e) {
            Log.w(TAG, "device state report failed: " + e.getMessage());
            return false;
        }
    }

    public static boolean sendSmartRingStateReport(String status, JSONObject metadata) {
        try {
            JSONObject data = new JSONObject();
            data.put("device_type", "smart_ring");
            data.put("device_id", "smart_ring");
            data.put("status", status == null || status.isEmpty() ? "offline" : status);
            data.put("name", "AIZO Ring");
            data.put("kind", "wearable");
            data.put("capabilities", new JSONArray().put("ring.touch").put("ring.status"));
            data.put("metadata", metadata == null ? new JSONObject() : metadata);
            return sendDeviceStateReport(data);
        } catch (Exception e) {
            Log.w(TAG, "ring state report failed: " + e.getMessage());
            return false;
        }
    }

    @Override
    public void onTaskRemoved(Intent rootIntent) {
        Log.w(TAG, "Task removed → schedule restart");
        Intent ri = new Intent(getApplicationContext(), ObsidianPushService.class);
        ri.setPackage(getPackageName());
        PendingIntent pi = PendingIntent.getService(getApplicationContext(), 1, ri,
                PendingIntent.FLAG_ONE_SHOT | PendingIntent.FLAG_IMMUTABLE);
        AlarmManager am = (AlarmManager) getSystemService(Context.ALARM_SERVICE);
        if (am != null) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S && !am.canScheduleExactAlarms()) {
                Log.w(TAG, "Exact alarm permission not granted; skip restart alarm");
            } else {
                am.setExactAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP,
                        SystemClock.elapsedRealtime() + 3000, pi);
            }
        }
        super.onTaskRemoved(rootIntent);
    }

    // ══════════════════════════════════════════════════════════
    //  网络变化监听 — 网络恢复时立即触发重连
    // ══════════════════════════════════════════════════════════

    private void registerNetworkCallback() {
        connectivityManager = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
        if (connectivityManager == null) return;

        networkCallback = new ConnectivityManager.NetworkCallback() {
            @Override
            public void onAvailable(Network network) {
                Log.i(TAG, "★ Network available, connected=" + wsConnected.get());
                if (!wsConnected.get() && shouldRun) {
                    reconnectDelay = 3000;
                    connectWebSocket();
                }
            }
            @Override
            public void onLost(Network network) {
                Log.w(TAG, "★ Network lost");
                wsConnected.set(false);
                updateKeepAlive("网络断开，等待恢复...");
            }
        };

        NetworkRequest req = new NetworkRequest.Builder()
                .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                .build();
        connectivityManager.registerNetworkCallback(req, networkCallback);
        Log.i(TAG, "NetworkCallback registered");
    }

    private void unregisterNetworkCallback() {
        if (connectivityManager != null && networkCallback != null) {
            try { connectivityManager.unregisterNetworkCallback(networkCallback); }
            catch (Exception ignored) {}
        }
    }

    // ══════════════════════════════════════════════════════════
    //  心跳线程 — 纯 Java Thread
    // ══════════════════════════════════════════════════════════

    private synchronized void startHeartbeatThread() {
        if (heartbeatThread != null && heartbeatThread.isAlive()) return;

        heartbeatThread = new Thread(() -> {
            Log.i(TAG, "♥ Heartbeat started tid=" + Thread.currentThread().getId());

            if (!wsConnected.get()) connectWebSocket();
            reportDeviceState();  // 启动即向 DeviceService 注册一次

            while (shouldRun) {
                try { Thread.sleep(HEARTBEAT_MS); }
                catch (InterruptedException e) { break; }
                if (!shouldRun) break;

                try {
                    reportDeviceState();  // 每 60s 刷新 last_seen_at（后端 120s 判离线）
                    if (wsConnected.get() && webSocket != null) {
                        boolean sent = webSocket.send("{\"type\":\"ping\"}");
                        long elapsed = (lastMessageTime > 0)
                                ? (System.currentTimeMillis() - lastMessageTime) / 1000 : 0;
                        Log.d(TAG, "♥ ping=" + sent + " msgs=" + msgReceived + " idle=" + elapsed + "s");

                        if (!sent) {
                            Log.w(TAG, "♥ ping failed → reconnect");
                            wsConnected.set(false);
                            connectWebSocket();
                        } else if (lastMessageTime > 0
                                && System.currentTimeMillis() - lastMessageTime > HEALTH_TIMEOUT) {
                            Log.w(TAG, "♥ health timeout → reconnect");
                            wsConnected.set(false);
                            connectWebSocket();
                        }
                    } else if (!wsConnected.get()) {
                        Log.i(TAG, "♥ not connected → reconnect");
                        connectWebSocket();
                    }
                } catch (Exception e) {
                    Log.e(TAG, "♥ error: " + e.getMessage());
                }
            }
            Log.i(TAG, "♥ Heartbeat exiting");
        }, "ObsidianHeartbeat");
        heartbeatThread.setDaemon(false);
        heartbeatThread.start();
    }

    // ══════════════════════════════════════════════════════════
    //  定位上报线程 — 每隔 N 分钟获取 GPS 坐标并 POST 到服务器
    // ══════════════════════════════════════════════════════════

    private synchronized void startLocationThread() {
        if (locationThread != null && locationThread.isAlive()) return;

        locationThread = new Thread(() -> {
            Log.i(TAG, "📍 Location thread started");
            // 首次等 15 秒让 WS 和 GPS 稳定
            try { Thread.sleep(15000); } catch (InterruptedException e) { return; }

            while (shouldRun) {
                try {
                    // 先检查服务端定位功能是否启用
                    checkLocationEnabled();
                    if (locationEnabled) {
                        requestLocationOnce();
                    } else {
                        Log.d(TAG, "📍 server location disabled, idle");
                    }
                } catch (Exception e) {
                    Log.e(TAG, "📍 error: " + e.getMessage());
                }

                long interval = locationEnabled ? locationInterval : LOCATION_INTERVAL_DISABLED;
                try { Thread.sleep(interval); }
                catch (InterruptedException e) { break; }
            }
            Log.i(TAG, "📍 Location thread exiting");
        }, "ObsidianLocation");
        locationThread.setDaemon(false);
        locationThread.start();
    }

    private void checkLocationEnabled() {
        String httpBase = httpBaseFromWs();
        if (httpBase == null) return;
        try {
            Request req = new Request.Builder()
                    .url(httpBase + "/api/location/config")
                    .get().build();
            try (Response resp = client.newCall(req).execute()) {
                if (resp.isSuccessful() && resp.body() != null) {
                    JSONObject cfg = new JSONObject(resp.body().string());
                    // active = enabled && 不在静默时段（服务端计算）
                    locationEnabled = cfg.optBoolean("active", false);
                }
            }
        } catch (Exception e) {
            Log.d(TAG, "📍 check config failed: " + e.getMessage());
        }
    }

    private void requestLocationOnce() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
                != PackageManager.PERMISSION_GRANTED) {
            Log.w(TAG, "📍 No location permission");
            postLocationDiagnostic("no_location_permission", null, "ACCESS_FINE_LOCATION missing", 0, null);
            return;
        }

        if (locationManager == null) {
            locationManager = (LocationManager) getSystemService(Context.LOCATION_SERVICE);
        }
        if (locationManager == null) {
            postLocationDiagnostic("location_manager_unavailable", null, "LocationManager unavailable", 0, null);
            return;
        }

        Location loc = bestRecentLocation(LOCATION_CACHE_MAX_AGE_MS);
        if (loc != null) {
            lastKnownLocation = loc;
            postLocationToServer(loc);
            return;
        }

        requestFreshLocation();
    }

    private void requestFreshLocation() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
                != PackageManager.PERMISSION_GRANTED) {
            postLocationDiagnostic("no_location_permission", null, "ACCESS_FINE_LOCATION missing", 0, null);
            return;
        }
        if (locationManager == null) {
            postLocationDiagnostic("location_manager_unavailable", null, "LocationManager unavailable", 0, null);
            return;
        }

        String provider = chooseLowPowerLocationProvider();
        if (provider == null) {
            Log.w(TAG, "📍 no enabled location provider");
            postLocationDiagnostic("no_enabled_provider", null, "No enabled location provider", 0, locationProviderMeta());
            return;
        }

        final long startedAt = SystemClock.elapsedRealtime();
        Handler handler = new Handler(getMainLooper());
        AtomicBoolean completed = new AtomicBoolean(false);
        final LocationListener[] listenerRef = new LocationListener[1];
        final Runnable[] timeoutRef = new Runnable[1];

        listenerRef[0] = new LocationListener() {
            @Override
            public void onLocationChanged(Location location) {
                if (!completed.compareAndSet(false, true)) return;
                handler.removeCallbacks(timeoutRef[0]);
                try { locationManager.removeUpdates(this); } catch (Exception ignored) {}

                if (location == null || locationAgeMs(location) > LOCATION_CACHE_MAX_AGE_MS) {
                    Log.w(TAG, "📍 fresh callback returned stale location");
                    postLocationDiagnostic(
                            "fresh_callback_stale",
                            location != null ? location.getProvider() : provider,
                            "Fresh callback returned null or stale location",
                            SystemClock.elapsedRealtime() - startedAt,
                            locationDiagnosticMeta(location));
                    return;
                }
                lastKnownLocation = location;
                postLocationToServer(location);
            }
            @Override public void onStatusChanged(String p, int s, Bundle e) {}
            @Override public void onProviderEnabled(String p) {}
            @Override public void onProviderDisabled(String p) {}
        };

        timeoutRef[0] = () -> {
            if (!completed.compareAndSet(false, true)) return;
            try { locationManager.removeUpdates(listenerRef[0]); } catch (Exception ignored) {}
            Log.w(TAG, "📍 fresh location timeout: " + provider);
            Location loc = bestRecentLocation(LOCATION_CACHE_MAX_AGE_MS);
            if (loc != null) {
                postLocationDiagnostic(
                        "fresh_timeout_fallback_recent_cache",
                        provider,
                        "Fresh location timed out; posting recent cached location",
                        SystemClock.elapsedRealtime() - startedAt,
                        locationDiagnosticMeta(loc));
                lastKnownLocation = loc;
                postLocationToServer(loc);
            } else {
                postLocationDiagnostic(
                        "fresh_timeout",
                        provider,
                        "Fresh location timed out and no recent cached location was available",
                        SystemClock.elapsedRealtime() - startedAt,
                        locationProviderMeta());
            }
        };

        try {
            handler.postDelayed(timeoutRef[0], LOCATION_FRESH_TIMEOUT_MS);
            locationManager.requestSingleUpdate(provider, listenerRef[0], getMainLooper());
        } catch (Exception e) {
            handler.removeCallbacks(timeoutRef[0]);
            completed.set(true);
            Log.e(TAG, "📍 requestSingleUpdate failed: " + e.getMessage());
            postLocationDiagnostic(
                    "request_single_update_failed",
                    provider,
                    e.getClass().getSimpleName() + ": " + e.getMessage(),
                    SystemClock.elapsedRealtime() - startedAt,
                    locationProviderMeta());
        }
    }

    private String chooseLowPowerLocationProvider() {
        try {
            if (locationManager.isProviderEnabled(LocationManager.NETWORK_PROVIDER)) {
                return LocationManager.NETWORK_PROVIDER;
            }
        } catch (Exception ignored) {}
        try {
            if (locationManager.isProviderEnabled(LocationManager.GPS_PROVIDER)) {
                return LocationManager.GPS_PROVIDER;
            }
        } catch (Exception ignored) {}
        return null;
    }

    private Location bestRecentLocation(long maxAgeMs) {
        Location gps = null;
        Location network = null;
        try { gps = locationManager.getLastKnownLocation(LocationManager.GPS_PROVIDER); } catch (Exception ignored) {}
        try { network = locationManager.getLastKnownLocation(LocationManager.NETWORK_PROVIDER); } catch (Exception ignored) {}
        Location best = betterRecentLocation(gps, network);
        if (best == null || locationAgeMs(best) > maxAgeMs) return null;
        return best;
    }

    private Location betterRecentLocation(Location a, Location b) {
        if (a == null) return b;
        if (b == null) return a;
        long ageA = locationAgeMs(a);
        long ageB = locationAgeMs(b);
        float accA = a.hasAccuracy() ? a.getAccuracy() : Float.MAX_VALUE;
        float accB = b.hasAccuracy() ? b.getAccuracy() : Float.MAX_VALUE;

        if (Math.abs(ageA - ageB) <= LOCATION_ACCURACY_STALENESS_GRACE_MS) {
            if (Math.abs(accA - accB) > 25.0f) {
                return accA <= accB ? a : b;
            }
        }
        return ageA <= ageB ? a : b;
    }

    private long locationAgeMs(Location loc) {
        if (loc == null) return Long.MAX_VALUE;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.JELLY_BEAN_MR1 && loc.getElapsedRealtimeNanos() > 0) {
            long ageNanos = SystemClock.elapsedRealtimeNanos() - loc.getElapsedRealtimeNanos();
            return Math.max(0L, TimeUnit.NANOSECONDS.toMillis(ageNanos));
        }
        return Math.max(0L, System.currentTimeMillis() - loc.getTime());
    }

    private boolean isMockLocation(Location loc) {
        if (loc == null) return false;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            return loc.isMock();
        }
        return loc.isFromMockProvider();
    }

    private void runLocationNetworkTask(String name, Runnable task) {
        if (Looper.myLooper() == Looper.getMainLooper()) {
            new Thread(task, name).start();
        } else {
            task.run();
        }
    }

    private JSONObject locationProviderMeta() {
        JSONObject meta = new JSONObject();
        try { meta.put("thread", Thread.currentThread().getName()); } catch (Exception ignored) {}
        try { meta.put("on_main_thread", Looper.myLooper() == Looper.getMainLooper()); } catch (Exception ignored) {}
        try { meta.put("location_enabled", locationEnabled); } catch (Exception ignored) {}
        try {
            if (locationManager != null) {
                meta.put("network_provider_enabled", locationManager.isProviderEnabled(LocationManager.NETWORK_PROVIDER));
                meta.put("gps_provider_enabled", locationManager.isProviderEnabled(LocationManager.GPS_PROVIDER));
            }
        } catch (Exception ignored) {}
        return meta;
    }

    private JSONObject locationDiagnosticMeta(@Nullable Location loc) {
        JSONObject meta = locationProviderMeta();
        if (loc == null) return meta;
        try { meta.put("location_provider", loc.getProvider()); } catch (Exception ignored) {}
        try { meta.put("location_age_ms", locationAgeMs(loc)); } catch (Exception ignored) {}
        try { meta.put("accuracy_m", loc.hasAccuracy() ? loc.getAccuracy() : -1); } catch (Exception ignored) {}
        try { meta.put("is_mock", isMockLocation(loc)); } catch (Exception ignored) {}
        return meta;
    }

    private void postLocationDiagnostic(String event, @Nullable String provider, String message,
                                        long elapsedMs, @Nullable JSONObject meta) {
        if (serverUrl == null) return;

        String httpBase = httpBaseFromWs();
        if (httpBase == null) return;

        String apiUrl = httpBase + "/api/location/diagnostic";
        runLocationNetworkTask("ObsidianLocationDiag", () -> {
            try {
                JSONObject body = new JSONObject();
                body.put("event", event);
                body.put("ok", false);
                if (provider != null && !provider.isEmpty()) body.put("provider", provider);
                if (message != null && !message.isEmpty()) body.put("message", message);
                body.put("elapsed_ms", Math.max(0L, elapsedMs));
                body.put("retryable", event != null && event.contains("timeout"));
                body.put("meta", meta != null ? meta : locationProviderMeta());

                MediaType JSON = MediaType.get("application/json; charset=utf-8");
                RequestBody reqBody = RequestBody.create(body.toString(), JSON);
                Request req = new Request.Builder().url(apiUrl).post(reqBody).build();
                try (Response resp = client.newCall(req).execute()) {
                    Log.i(TAG, "📍 diagnostic " + event + " → " + resp.code());
                }
            } catch (Exception e) {
                Log.d(TAG, "📍 diagnostic post failed: " + e.getMessage());
            }
        });
    }

    private void postLocationToServer(Location loc) {
        if (loc == null || serverUrl == null) return;

        String httpBase = httpBaseFromWs();
        if (httpBase == null) return;

        String apiUrl = httpBase + "/api/location/heartbeat";
        runLocationNetworkTask("ObsidianLocationPost", () -> {
            try {
                JSONObject body = new JSONObject();
                body.put("lng", loc.getLongitude());
                body.put("lat", loc.getLatitude());
                body.put("accuracy", loc.getAccuracy());
                body.put("is_gcj02", false);  // Android 原生 GPS 输出 WGS84
                body.put("provider", loc.getProvider());
                body.put("location_age_ms", locationAgeMs(loc));
                body.put("is_mock", isMockLocation(loc));

                MediaType JSON = MediaType.get("application/json; charset=utf-8");
                RequestBody reqBody = RequestBody.create(body.toString(), JSON);
                Request req = new Request.Builder().url(apiUrl).post(reqBody).build();

                try (Response resp = client.newCall(req).execute()) {
                    Log.i(TAG, "📍 posted loc (" + String.format("%.4f,%.4f", loc.getLongitude(), loc.getLatitude())
                            + " acc=" + (int) loc.getAccuracy()
                            + "m provider=" + loc.getProvider()
                            + " age=" + (locationAgeMs(loc) / 1000) + "s) → " + resp.code());
                }
            } catch (Exception e) {
                Log.e(TAG, "📍 post failed: " + e.getMessage());
            }
        });
    }

    // ══════════════════════════════════════════════════════════
    //  WebSocket 连接 — synchronized 防并发
    // ══════════════════════════════════════════════════════════

    private synchronized void connectWebSocket() {
        if (wsConnected.get()) return;
        if (serverUrl == null) { Log.e(TAG, "url=null"); return; }

        final int gen = wsGeneration.incrementAndGet();

        WebSocket old = webSocket;
        webSocket = null;
        if (old != null) try { old.cancel(); } catch (Exception ignored) {}

        Log.i(TAG, ">>> connect gen=" + gen + " → " + serverUrl);
        updateKeepAlive("连接中...");

        try {
            Request req = new Request.Builder().url(serverUrl).build();
            webSocket = client.newWebSocket(req, new WebSocketListener() {

                @Override
                public void onOpen(WebSocket ws, Response resp) {
                    if (gen != wsGeneration.get()) { ws.cancel(); return; }
                    Log.i(TAG, ">>> OPEN gen=" + gen);
                    wsConnected.set(true);
                    reconnectDelay = 3000;
                    msgReceived = 0;
                    lastMessageTime = System.currentTimeMillis();
                    updateKeepAlive("在线 ✨");
                    SmartRingService.reportCachedStateForWs(ObsidianPushService.this);
                }

                @Override
                public void onMessage(WebSocket ws, String text) {
                    if (gen != wsGeneration.get()) return;
                    lastMessageTime = System.currentTimeMillis();
                    handleMessage(text);
                }

                @Override
                public void onFailure(WebSocket ws, Throwable t, Response resp) {
                    if (gen != wsGeneration.get()) return;
                    String err = t != null ? t.getMessage() : "unknown";
                    Log.w(TAG, ">>> FAIL gen=" + gen + ": " + err);
                    wsConnected.set(false);
                    reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY);
                    updateKeepAlive("连接失败，正在重试…");
                    // 不在这里阻塞或重连！心跳线程会处理
                }

                @Override
                public void onClosed(WebSocket ws, int code, String reason) {
                    if (gen != wsGeneration.get()) return;
                    Log.i(TAG, ">>> CLOSED gen=" + gen + " code=" + code);
                    wsConnected.set(false);
                    updateKeepAlive("连接断开，正在重连…");
                }
            });
        } catch (Exception e) {
            Log.e(TAG, "connect error: " + e.getMessage());
            reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY);
            updateKeepAlive("连接失败，正在重试…");
        }
    }

    private String toWebSocketUrl(String url) {
        if (url == null) return null;
        String ws = url.trim()
                .replaceFirst("^http://", "ws://")
                .replaceFirst("^https://", "wss://");
        int query = ws.indexOf('?');
        if (query >= 0) ws = ws.substring(0, query);
        int hash = ws.indexOf('#');
        if (hash >= 0) ws = ws.substring(0, hash);
        ws = ws.replaceAll("/+$", "");
        if (ws.endsWith("/chat")) {
            ws = ws.substring(0, ws.length() - 5);
        }
        if (!ws.endsWith("/ws")) {
            ws += "/ws";
        }
        return ws;
    }

    // ══════════════════════════════════════════════════════════
    //  消息 → 通知
    // ══════════════════════════════════════════════════════════

    private void handleMessage(String text) {
        try {
            JSONObject json = new JSONObject(text);
            String type = json.optString("type", "");

            if ("pong".equals(type) || "ping".equals(type)) return;

            msgReceived++;
            Log.d(TAG, "MSG #" + msgReceived + " type=" + type);

            JSONObject data = json.optJSONObject("data");

            switch (type) {
                case "schedule_alarm": {
                    if (data != null && consumeDelegatedAlarmIds(data)) {
                        Log.i(TAG, "system Clock owns schedule alarm; skipping duplicate notification");
                        break;
                    }
                    String c = data != null ? data.optString("content", "闹铃") : "闹铃";
                    showTransientNotif(CH_ALARM, "⏰ 闹铃", c, true, true,
                            ALARM_NOTIFICATION_TIMEOUT_MS);
                    break;
                }
                case "android_alarm_set": {
                    if (data != null) {
                        new Handler(Looper.getMainLooper()).post(() -> setNativeAlarm(data));
                    }
                    break;
                }
                case "android_alarm_cancel": {
                    if (data != null) {
                        new Handler(Looper.getMainLooper()).post(() -> cancelNativeAlarm(data));
                    }
                    break;
                }
                case "monitor_alert": {
                    String c = data != null ? data.optString("content", "监控提醒") : "监控提醒";
                    showTransientNotif(CH_ALARM, "👁 监控", c, true, true,
                            ALARM_NOTIFICATION_TIMEOUT_MS);
                    break;
                }
                case "music": {
                    // 后台自动播放音乐（前台由 WebView JS 处理）
                    if (!isForegroundActive && data != null) {
                        JSONArray cards = data.optJSONArray("cards");
                        if (cards != null && cards.length() > 0) {
                            JSONObject firstCard = cards.optJSONObject(0);
                            if (firstCard != null) {
                                int songId = firstCard.optInt("id", 0);
                                if (songId > 0) {
                                    new Handler(Looper.getMainLooper()).post(() -> {
                                        if (!isForegroundActive) playMusicStream(songId);
                                    });
                                }
                            }
                        }
                    }
                    break;
                }
                case "msg_created": {
                    if (data != null) {
                        String role = data.optString("role", "");
                        if ("assistant".equals(role) && shouldShowMessageNotification()) {
                            showMessageNotification(data);
                        }
                    }
                    break;
                }
                case "ring_touch_request": {
                    if (data != null) {
                        SmartRingService.execute(this, data);
                    }
                    break;
                }
                case "ring_connect_request": {
                    SmartRingService.connectAndReport(this, data == null ? new JSONObject() : data);
                    break;
                }
                case "ring_keepalive_request": {
                    SmartRingService.keepAliveFromWs(this, data == null ? new JSONObject() : data);
                    break;
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "parse error: " + e.getMessage());
        }
    }

    /**
     * 聊天 Activity 的生命周期在部分 ROM 锁屏时不会及时进入 onPause。
     * 因此普通消息是否通知不能只依赖 isForegroundActive，还必须以系统当前的
     * 屏幕交互状态和锁屏状态兜底。这里不改写全局前台标记，避免影响音乐和闹钟。
     */
    private boolean shouldShowMessageNotification() {
        PowerManager powerManager = getSystemService(PowerManager.class);
        boolean screenInteractive = powerManager != null
                ? powerManager.isInteractive()
                : screenOn;
        KeyguardManager keyguardManager = getSystemService(KeyguardManager.class);
        boolean deviceLocked = keyguardManager != null && keyguardManager.isKeyguardLocked();
        boolean shouldNotify = MessageNotificationPolicy.shouldNotify(
                isForegroundActive,
                screenInteractive,
                deviceLocked
        );
        Log.d(TAG, "message notify=" + shouldNotify
                + " foreground=" + isForegroundActive
                + " interactive=" + screenInteractive
                + " locked=" + deviceLocked);
        return shouldNotify;
    }

    // ══════════════════════════════════════════════════════════
    //  对话闹钟 → 系统 Clock
    // ══════════════════════════════════════════════════════════

    private void setNativeAlarm(JSONObject data) {
        String scheduleId = data.optString("id", "").trim();
        String triggerAt = data.optString("trigger_at", "").trim();
        String content = data.optString("content", "闹钟").trim();
        if (scheduleId.isEmpty() || triggerAt.isEmpty()) return;
        if (hasDelegatedAlarmId(scheduleId)) {
            removePendingNativeAlarmSet(scheduleId);
            return;
        }

        // 普通对话在前台时才静默交给系统 Clock。桌面端创建或后台投递仍由
        // 服务端到点通知兜底，避免 Android 拦截后台 Activity 后误以为已设置。
        if (!isForegroundActive) {
            addPendingNativeAlarmSet(scheduleId, triggerAt, content);
            Log.i(TAG, "queued native alarm until app returns to foreground: " + scheduleId);
            return;
        }

        LocalDateTime target;
        try {
            target = LocalDateTime.parse(triggerAt.replace(' ', 'T'));
        } catch (Exception e) {
            removePendingNativeAlarmSet(scheduleId);
            Log.w(TAG, "invalid native alarm time " + triggerAt + ": " + e.getMessage());
            return;
        }
        long leadSeconds = Duration.between(LocalDateTime.now(), target).getSeconds();
        if (leadSeconds <= 0) {
            removePendingNativeAlarmSet(scheduleId);
            Log.i(TAG, "discarded expired pending native alarm: " + scheduleId);
            return;
        }
        if (leadSeconds > NATIVE_ALARM_MAX_LEAD_SECONDS) {
            addPendingNativeAlarmSet(scheduleId, triggerAt, content);
            Log.i(TAG, "queued native alarm until it enters the next-24h window: " + scheduleId);
            return;
        }

        Intent intent = new Intent(AlarmClock.ACTION_SET_ALARM)
                .putExtra(AlarmClock.EXTRA_HOUR, target.getHour())
                .putExtra(AlarmClock.EXTRA_MINUTES, target.getMinute())
                .putExtra(AlarmClock.EXTRA_MESSAGE, nativeAlarmLabel(scheduleId, content))
                .putExtra(AlarmClock.EXTRA_VIBRATE, true)
                .putExtra(AlarmClock.EXTRA_SKIP_UI, true)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        if (intent.resolveActivity(getPackageManager()) == null) {
            removePendingNativeAlarmSet(scheduleId);
            Log.w(TAG, "no system Clock app handles ACTION_SET_ALARM");
            return;
        }
        try {
            startActivity(intent);
            addDelegatedAlarmId(scheduleId);
            removePendingNativeAlarmSet(scheduleId);
            Log.i(TAG, "delegated alarm to system Clock: " + scheduleId + " @ " + triggerAt);
        } catch (Exception e) {
            addPendingNativeAlarmSet(scheduleId, triggerAt, content);
            Log.w(TAG, "system Clock alarm failed; keeping server fallback: " + e.getMessage());
        }
    }

    private void cancelNativeAlarm(JSONObject data) {
        String scheduleId = data.optString("id", "").trim();
        if (scheduleId.isEmpty()) return;
        removePendingNativeAlarmSet(scheduleId);
        if (!hasDelegatedAlarmId(scheduleId)) return;
        String content = data.optString("content", "闹钟").trim();
        String label = nativeAlarmLabel(scheduleId, content);

        // Android 10+ 会拦截后台启动 Clock Activity。先持久化取消请求，
        // 当前已在前台就立即执行，否则由下次 onResume 触发重放。
        addPendingNativeAlarmCancellation(scheduleId, label);
        if (!isForegroundActive) {
            Log.i(TAG, "queued system Clock alarm dismissal until foreground: " + scheduleId);
            return;
        }
        dismissNativeAlarm(scheduleId, label);
    }

    private boolean dismissNativeAlarm(String scheduleId, String label) {
        if (!isForegroundActive) return false;

        Intent intent = new Intent(AlarmClock.ACTION_DISMISS_ALARM)
                .putExtra(AlarmClock.EXTRA_ALARM_SEARCH_MODE, AlarmClock.ALARM_SEARCH_MODE_LABEL)
                .putExtra(AlarmClock.EXTRA_MESSAGE, label)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        if (intent.resolveActivity(getPackageManager()) == null) {
            Log.w(TAG, "system Clock app cannot dismiss alarm by label: " + scheduleId);
            return false;
        }
        try {
            startActivity(intent);
            completeNativeAlarmCancellation(scheduleId);
            Log.i(TAG, "dismissed system Clock alarm: " + scheduleId);
            return true;
        } catch (Exception e) {
            Log.w(TAG, "system Clock alarm dismissal failed: " + e.getMessage());
            return false;
        }
    }

    private void retryPendingNativeAlarmCancellations() {
        if (!isForegroundActive) return;
        Map<String, ?> values = alarmPreferences().getAll();
        for (Map.Entry<String, ?> entry : values.entrySet()) {
            String key = entry.getKey();
            if (!key.startsWith(PREF_NATIVE_ALARM_CANCEL_PREFIX)
                    || !(entry.getValue() instanceof String)) {
                continue;
            }
            String scheduleId = key.substring(PREF_NATIVE_ALARM_CANCEL_PREFIX.length());
            if (!hasDelegatedAlarmId(scheduleId)) {
                removePendingNativeAlarmCancellation(scheduleId);
                continue;
            }
            if (!dismissNativeAlarm(scheduleId, (String) entry.getValue())) {
                // 应用可能在多条重放中再次进入后台，剩余项留待下次前台。
                if (!isForegroundActive) return;
            }
        }
    }

    private void retryPendingNativeAlarmSets() {
        if (!isForegroundActive) return;
        Map<String, ?> values = alarmPreferences().getAll();
        for (Map.Entry<String, ?> entry : values.entrySet()) {
            String key = entry.getKey();
            if (!key.startsWith(PREF_NATIVE_ALARM_SET_PREFIX)
                    || !(entry.getValue() instanceof String)) {
                continue;
            }
            String scheduleId = key.substring(PREF_NATIVE_ALARM_SET_PREFIX.length());
            try {
                JSONObject queued = new JSONObject((String) entry.getValue());
                queued.put("id", scheduleId);
                setNativeAlarm(queued);
            } catch (Exception e) {
                removePendingNativeAlarmSet(scheduleId);
                Log.w(TAG, "discarded invalid pending native alarm " + scheduleId);
            }
            // App 可能在 Clock Activity 拉起后失去前台；剩余项留待下次前台。
            if (!isForegroundActive) return;
        }
    }

    private String nativeAlarmLabel(String scheduleId, String content) {
        String suffix = scheduleId.length() > 8
                ? scheduleId.substring(scheduleId.length() - 8)
                : scheduleId;
        String body = content == null ? "" : content.trim();
        if (body.length() > 40) body = body.substring(0, 40);
        return "ObsidianVow " + suffix + (body.isEmpty() ? "" : " · " + body);
    }

    private SharedPreferences alarmPreferences() {
        return getSharedPreferences("obsidian_prefs", MODE_PRIVATE);
    }

    private synchronized boolean hasDelegatedAlarmId(String scheduleId) {
        return alarmPreferences().getStringSet(PREF_NATIVE_ALARM_IDS, new HashSet<>())
                .contains(scheduleId);
    }

    private synchronized void addDelegatedAlarmId(String scheduleId) {
        Set<String> ids = new HashSet<>(
                alarmPreferences().getStringSet(PREF_NATIVE_ALARM_IDS, new HashSet<>())
        );
        ids.add(scheduleId);
        alarmPreferences().edit().putStringSet(PREF_NATIVE_ALARM_IDS, ids).apply();
    }

    private synchronized void addPendingNativeAlarmCancellation(String scheduleId, String label) {
        alarmPreferences().edit()
                .putString(PREF_NATIVE_ALARM_CANCEL_PREFIX + scheduleId, label)
                .apply();
    }

    private synchronized void addPendingNativeAlarmSet(
            String scheduleId, String triggerAt, String content) {
        JSONObject value = new JSONObject();
        try {
            value.put("trigger_at", triggerAt);
            value.put("content", content);
        } catch (Exception e) {
            Log.w(TAG, "failed to encode pending native alarm: " + scheduleId);
            return;
        }
        alarmPreferences().edit()
                .putString(PREF_NATIVE_ALARM_SET_PREFIX + scheduleId, value.toString())
                .apply();
    }

    private synchronized void removePendingNativeAlarmSet(String scheduleId) {
        alarmPreferences().edit()
                .remove(PREF_NATIVE_ALARM_SET_PREFIX + scheduleId)
                .apply();
    }

    private synchronized void removePendingNativeAlarmCancellation(String scheduleId) {
        alarmPreferences().edit()
                .remove(PREF_NATIVE_ALARM_CANCEL_PREFIX + scheduleId)
                .apply();
    }

    private synchronized void completeNativeAlarmCancellation(String scheduleId) {
        Set<String> ids = new HashSet<>(
                alarmPreferences().getStringSet(PREF_NATIVE_ALARM_IDS, new HashSet<>())
        );
        ids.remove(scheduleId);
        alarmPreferences().edit()
                .putStringSet(PREF_NATIVE_ALARM_IDS, ids)
                .remove(PREF_NATIVE_ALARM_CANCEL_PREFIX + scheduleId)
                .remove(PREF_NATIVE_ALARM_SET_PREFIX + scheduleId)
                .apply();
    }

    /** 消费本次到期的原生闹钟 ID；全部已由系统 Clock 接管时返回 true。 */
    private synchronized boolean consumeDelegatedAlarmIds(JSONObject data) {
        Set<String> dueIds = new HashSet<>();
        JSONArray array = data.optJSONArray("ids");
        if (array != null) {
            for (int i = 0; i < array.length(); i++) {
                String value = array.optString(i, "").trim();
                if (!value.isEmpty()) dueIds.add(value);
            }
        }
        if (dueIds.isEmpty()) {
            String single = data.optString("id", "").trim();
            if (!single.isEmpty()) dueIds.add(single);
        }
        if (dueIds.isEmpty()) return false;

        Set<String> delegated = new HashSet<>(
                alarmPreferences().getStringSet(PREF_NATIVE_ALARM_IDS, new HashSet<>())
        );
        boolean allDelegated = delegated.containsAll(dueIds);
        boolean changed = delegated.removeAll(dueIds);
        if (changed) {
            alarmPreferences().edit().putStringSet(PREF_NATIVE_ALARM_IDS, delegated).apply();
        }
        return allDelegated;
    }

    // ══════════════════════════════════════════════════════════
    //  原生音乐播放（后台 WebView 冻结时由 MediaPlayer 接管）
    // ══════════════════════════════════════════════════════════

    private void playMusicStream(int songId) {
        String httpBase = httpBaseFromWs();
        if (httpBase == null) return;
        String streamUrl = httpBase + "/api/music/stream/" + songId;
        Log.i(TAG, "♪ Playing music: " + streamUrl);

        stopMusic();

        try {
            mediaPlayer = new MediaPlayer();
            mediaPlayer.setAudioAttributes(MUSIC_AUDIO_ATTRIBUTES);
            String token = authToken;
            if (token != null && !token.isEmpty()) {
                Map<String, String> headers = new HashMap<>();
                headers.put("Authorization", "Bearer " + token);
                mediaPlayer.setDataSource(this, Uri.parse(streamUrl), headers);
            } else {
                mediaPlayer.setDataSource(streamUrl);
            }
            mediaPlayer.setOnPreparedListener(mp -> {
                if (mediaPlayer != mp) {
                    mp.release();
                    return;
                }
                if (!requestMusicAudioFocus()) {
                    Log.w(TAG, "♪ Audio focus denied; playback cancelled");
                    mp.release();
                    mediaPlayer = null;
                    abandonMusicAudioFocus();
                    return;
                }
                mp.start();
            });
            mediaPlayer.setOnCompletionListener(mp -> {
                Log.i(TAG, "♪ Music finished");
                mp.release();
                if (mediaPlayer == mp) {
                    mediaPlayer = null;
                    abandonMusicAudioFocus();
                }
            });
            mediaPlayer.setOnErrorListener((mp, what, extra) -> {
                Log.e(TAG, "♪ MediaPlayer error: " + what + "/" + extra);
                mp.release();
                if (mediaPlayer == mp) {
                    mediaPlayer = null;
                    abandonMusicAudioFocus();
                }
                return true;
            });
            mediaPlayer.prepareAsync();
        } catch (Exception e) {
            Log.e(TAG, "♪ Music play error: " + e.getMessage());
            if (mediaPlayer != null) {
                try { mediaPlayer.release(); } catch (Exception ignored) {}
                mediaPlayer = null;
            }
            abandonMusicAudioFocus();
        }
    }

    private void stopMusic() {
        resumeMusicOnFocusGain = false;
        MediaPlayer player = mediaPlayer;
        mediaPlayer = null;
        if (player != null) {
            try {
                if (player.isPlaying()) player.stop();
            } catch (Exception ignored) {}
            try { player.release(); } catch (Exception ignored) {}
        }
        abandonMusicAudioFocus();
    }

    private boolean requestMusicAudioFocus() {
        if (audioManager == null) return false;
        musicFocusRequest = new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN)
                .setAudioAttributes(MUSIC_AUDIO_ATTRIBUTES)
                .setOnAudioFocusChangeListener(
                        musicFocusListener,
                        new Handler(Looper.getMainLooper())
                )
                .build();
        resumeMusicOnFocusGain = false;
        return audioManager.requestAudioFocus(musicFocusRequest)
                == AudioManager.AUDIOFOCUS_REQUEST_GRANTED;
    }

    private void abandonMusicAudioFocus() {
        AudioFocusRequest request = musicFocusRequest;
        musicFocusRequest = null;
        if (audioManager != null && request != null) {
            audioManager.abandonAudioFocusRequest(request);
        }
    }

    private void handleMusicAudioFocusChange(int focusChange) {
        MediaPlayer player = mediaPlayer;
        if (player == null) return;
        try {
            switch (focusChange) {
                case AudioManager.AUDIOFOCUS_GAIN:
                    player.setVolume(1.0f, 1.0f);
                    if (resumeMusicOnFocusGain && !player.isPlaying()) player.start();
                    resumeMusicOnFocusGain = false;
                    break;
                case AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK:
                    player.setVolume(0.2f, 0.2f);
                    break;
                case AudioManager.AUDIOFOCUS_LOSS_TRANSIENT:
                    resumeMusicOnFocusGain = player.isPlaying();
                    if (resumeMusicOnFocusGain) player.pause();
                    break;
                case AudioManager.AUDIOFOCUS_LOSS:
                    stopMusic();
                    break;
                default:
                    break;
            }
        } catch (IllegalStateException e) {
            Log.w(TAG, "♪ Audio focus state change ignored: " + e.getMessage());
        }
    }

    private void showMessageNotification(JSONObject data) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm == null) return;

        String messageId = data.optString("id", "").trim();
        double createdAt = data.optDouble("created_at", 0);
        long whenMs = createdAt > 0
                ? Math.round(createdAt * 1000.0)
                : System.currentTimeMillis();
        if (messageId.isEmpty()) messageId = "at:" + whenMs;
        String text = data.optString("content", "");
        if (text.length() > 100) text = text.substring(0, 100) + "...";
        String aiName = data.optString("ai_name", "").trim();
        if (aiName.isEmpty()) aiName = "Obsidian Vow";  // 兼容尚未热更的旧服务端

        Log.i(TAG, "NOTIFY " + aiName + ": " + text);

        Intent i = new Intent(this, LauncherActivity.class);
        i.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        i.setData(Uri.parse("obsidianvow://message/" + Uri.encode(messageId)));
        PendingIntent pi = PendingIntent.getActivity(this, messageId.hashCode(), i,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        Notification notification = new NotificationCompat.Builder(this, CH_MESSAGE)
                .setSmallIcon(R.mipmap.ic_launcher)
                .setContentTitle(aiName)
                .setContentText(text)
                .setStyle(new NotificationCompat.BigTextStyle().bigText(text))
                .setPriority(NotificationCompat.PRIORITY_DEFAULT)
                .setContentIntent(pi)
                .setWhen(whenMs)
                .setShowWhen(true)
                .setAutoCancel(false)
                // 部分厂商 ROM 会把 ongoing 消息归为常驻状态并从锁屏隐藏。
                .setCategory(NotificationCompat.CATEGORY_MESSAGE)
                .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
                .build();

        nm.notify(NOTIF_MSG_TAG_PREFIX + messageId, NOTIF_MSG_BASE, notification);
    }

    private void showTransientNotif(String ch, String title, String text, boolean high,
                                    boolean fullScreen, long timeoutMs) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm == null) return;

        Log.i(TAG, "NOTIFY " + title + ": " + text);

        int requestCode = notifCounter++;
        Intent i = new Intent(this, LauncherActivity.class);
        i.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent pi = PendingIntent.getActivity(this, requestCode, i,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        NotificationCompat.Builder b = new NotificationCompat.Builder(this, ch)
                .setSmallIcon(R.mipmap.ic_launcher)
                .setContentTitle(title)
                .setContentText(text)
                .setStyle(new NotificationCompat.BigTextStyle().bigText(text))
                .setPriority(high ? NotificationCompat.PRIORITY_HIGH : NotificationCompat.PRIORITY_DEFAULT)
                .setContentIntent(pi)
                .setWhen(System.currentTimeMillis())
                .setShowWhen(true)
                .setAutoCancel(true)
                .setTimeoutAfter(timeoutMs)
                .setCategory(high ? NotificationCompat.CATEGORY_ALARM : NotificationCompat.CATEGORY_STATUS)
                .setVisibility(NotificationCompat.VISIBILITY_PUBLIC);

        if (high) {
            b.setDefaults(NotificationCompat.DEFAULT_ALL);
        }
        if (fullScreen) {
            Intent alertIntent = SystemAlertActivity.createIntent(this, title, text);
            alertIntent.setData(Uri.parse(
                    "obsidianvow://system-alert/" + requestCode + "/" + System.currentTimeMillis()));
            PendingIntent alertPi = PendingIntent.getActivity(
                    this, 30_000 + requestCode, alertIntent,
                    PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.UPSIDE_DOWN_CAKE
                    || nm.canUseFullScreenIntent()) {
                b.setFullScreenIntent(alertPi, true);
            } else {
                Log.w(TAG, "full-screen alert permission unavailable; using heads-up fallback");
            }
        }

        nm.notify(NOTIF_MSG_BASE + (requestCode % 50), b.build());
    }

    /**
     * 聊天页确认看到时间点 cutoffMs 后，只撤掉该时间及更早的消息通知。
     * 闹钟、截图授权和各前台服务通知位于其它 channel，不受影响。
     */
    public static void clearMessageNotificationsThrough(Context context, long cutoffMs) {
        if (context == null || cutoffMs <= 0) return;
        NotificationManager nm = context.getSystemService(NotificationManager.class);
        if (nm == null) return;
        int cleared = 0;
        for (StatusBarNotification sbn : nm.getActiveNotifications()) {
            Notification notification = sbn.getNotification();
            String channelId = notification.getChannelId();
            if (!CH_MESSAGE.equals(channelId) && !CH_MESSAGE_V2.equals(channelId)) continue;
            if (notification.when <= cutoffMs) {
                if (sbn.getTag() == null) nm.cancel(sbn.getId());
                else nm.cancel(sbn.getTag(), sbn.getId());
                cleared++;
            }
        }
        Log.d(TAG, "cleared message notifications=" + cleared + " through=" + cutoffMs);
    }

    // ══════════════════════════════════════════════════════════
    //  通知渠道
    // ══════════════════════════════════════════════════════════

    private void createNotificationChannels() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm == null) return;

        NotificationChannel c1 = new NotificationChannel(CH_KEEPALIVE, "Obsidian Vow 保活",
                NotificationManager.IMPORTANCE_LOW);
        c1.setShowBadge(false);
        BrandCompatibility.createNotificationChannel(nm, c1);

        NotificationChannel c2 = new NotificationChannel(CH_MESSAGE, "Obsidian Vow 消息",
                NotificationManager.IMPORTANCE_DEFAULT);
        c2.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
        BrandCompatibility.createNotificationChannel(nm, c2);

        NotificationChannel c3 = new NotificationChannel(CH_ALARM, "闹铃与监控",
                NotificationManager.IMPORTANCE_HIGH);
        c3.enableVibration(true);
        c3.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
        BrandCompatibility.createNotificationChannel(nm, c3);
    }

    private Notification buildKeepAlive(String text) {
        Intent i = new Intent(this, LauncherActivity.class);
        i.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent pi = PendingIntent.getActivity(this, 0, i,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        return new NotificationCompat.Builder(this, CH_KEEPALIVE)
                .setSmallIcon(R.mipmap.ic_launcher)
                .setContentTitle("Obsidian Vow")
                .setContentText(text)
                .setContentIntent(pi)
                .setOngoing(true)
                .setPriority(NotificationCompat.PRIORITY_LOW)
                .build();
    }

    private void updateKeepAlive(String text) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm != null) nm.notify(NOTIF_FOREGROUND, buildKeepAlive(text));
    }

    // ══════════════════════════════════════════════════════════
    //  活动上报线程 — UsageStatsManager 检测前台应用
    // ══════════════════════════════════════════════════════════

    private synchronized void startActivityThread() {
        if (activityThread != null && activityThread.isAlive()) return;

        // 注册屏幕开关广播
        registerScreenReceiver();

        activityThread = new Thread(() -> {
            Log.i(TAG, "📱 Activity thread started");
            // 等待 20 秒让服务稳定
            try { Thread.sleep(20000); } catch (InterruptedException e) { return; }

            while (shouldRun) {
                try {
                    if (hasUsageStatsPermission()) {
                        reportForegroundApp();
                    } else {
                        Log.d(TAG, "📱 Usage access permission not granted");
                    }
                } catch (Exception e) {
                    Log.e(TAG, "📱 activity error: " + e.getMessage());
                }

                try { Thread.sleep(ACTIVITY_INTERVAL); }
                catch (InterruptedException e) { break; }
            }
            Log.i(TAG, "📱 Activity thread exiting");
        }, "ObsidianActivity");
        activityThread.setDaemon(false);
        activityThread.start();
    }

    private boolean hasUsageStatsPermission() {
        try {
            UsageStatsManager usm = (UsageStatsManager) getSystemService(Context.USAGE_STATS_SERVICE);
            if (usm == null) return false;
            long now = System.currentTimeMillis();
            java.util.List<UsageStats> stats = usm.queryUsageStats(
                    UsageStatsManager.INTERVAL_DAILY, now - 60_000, now);
            return stats != null && !stats.isEmpty();
        } catch (Exception e) {
            return false;
        }
    }

    private void reportForegroundApp() {
        // 熄屏期间 UsageStats 仍返回 last-resumed 包（比如正在后台放的 bilibili），
        // 会误判为"在玩手机"。SCREEN_OFF 广播已经上报过一次 screen_off，
        // 摘要层会把它一直 carry-forward 成"锁屏"，这里直接跳过。
        if (!screenOn) return;

        UsageStatsManager usm = (UsageStatsManager) getSystemService(Context.USAGE_STATS_SERVICE);
        if (usm == null) return;

        long now = System.currentTimeMillis();

        // 方案一：UsageEvents（更可靠，能在后台获取真实的前台切换事件）
        String pkgName = null;
        try {
            UsageEvents events = usm.queryEvents(now - 120_000, now);
            UsageEvents.Event event = new UsageEvents.Event();
            while (events.hasNextEvent()) {
                events.getNextEvent(event);
                // ACTIVITY_RESUMED (=1 on older / =2) 表示 Activity 进入前台
                if (event.getEventType() == UsageEvents.Event.ACTIVITY_RESUMED
                        || event.getEventType() == 1) {
                    pkgName = event.getPackageName();
                }
            }
        } catch (Exception e) {
            Log.d(TAG, "📱 UsageEvents failed, fallback to queryUsageStats: " + e.getMessage());
        }

        // 方案二：如果 UsageEvents 没结果，fallback 到 queryUsageStats
        if (pkgName == null) {
            java.util.List<UsageStats> stats = usm.queryUsageStats(
                    UsageStatsManager.INTERVAL_DAILY, now - 120_000, now);
            if (stats != null && !stats.isEmpty()) {
                UsageStats recent = null;
                for (UsageStats s : stats) {
                    if (recent == null || s.getLastTimeUsed() > recent.getLastTimeUsed()) {
                        recent = s;
                    }
                }
                if (recent != null) pkgName = recent.getPackageName();
            }
        }

        if (pkgName == null) return;

        // 仅过滤自身
        if (pkgName.equals(getPackageName())) {
            return;
        }

        if (pkgName.equals(lastReportedApp)
                && now - lastReportedTime < ACTIVITY_RE_REPORT_MS) {
            Log.d(TAG, "📱 same foreground app, skip duplicate report: " + pkgName);
            return;
        }

        // App 切换时立即上报；同一 App 持续使用超过 ACTIVITY_RE_REPORT_MS 再补报一次。
        lastReportedApp = pkgName;
        lastReportedTime = now;

        // 直接发送包名，服务端做名称翻译（避免 vivo ROM 中文编码乱码）
        postActivityToServer(pkgName);
    }

    private void postActivityToServer(String pkgName) {
        String httpBase = httpBaseFromWs();
        if (httpBase == null) return;

        try {
            JSONObject body = new JSONObject();
            // 迁移期保留 legacy device=phone（Evidence source 仍按它归到 android.activity），
            // 同时双写稳定设备身份，后端按 device_id 分组、按 device_name 展示。
            body.put("device", "phone");
            body.put("device_id", DeviceIdentity.id(this));
            body.put("device_name", DeviceIdentity.name(this));
            body.put("device_type", DeviceIdentity.type(this));
            body.put("platform", DeviceIdentity.PLATFORM);
            body.put("app", pkgName);
            body.put("title", pkgName);
            body.put("timestamp", System.currentTimeMillis() / 1000.0);

            MediaType JSON_TYPE = MediaType.get("application/json; charset=utf-8");
            RequestBody reqBody = RequestBody.create(body.toString(), JSON_TYPE);
            Request req = new Request.Builder()
                    .url(httpBase + "/api/activity/report")
                    .post(reqBody)
                    .build();

            try (Response resp = client.newCall(req).execute()) {
                Log.i(TAG, "📱 reported activity: " + pkgName + " → " + resp.code());
            }
        } catch (Exception e) {
            Log.e(TAG, "📱 activity report failed: " + e.getMessage());
        }
    }

    /** 向 DeviceService 注册/刷新本机状态，使后端把手机/平板识别为独立在线设备。 */
    private void reportDeviceState() {
        String httpBase = httpBaseFromWs();
        if (httpBase == null) return;
        try {
            JSONObject body = new JSONObject();
            body.put("status", "online");
            body.put("name", DeviceIdentity.name(this));
            body.put("kind", DeviceIdentity.kind(this));
            JSONArray caps = new JSONArray();
            caps.put("activity.report");
            caps.put("sensing.report");
            caps.put("location.report");
            caps.put("screen.capture");
            body.put("capabilities", caps);
            JSONObject meta = new JSONObject();
            meta.put("platform", DeviceIdentity.PLATFORM);
            meta.put("device_type", DeviceIdentity.type(this));
            body.put("metadata", meta);

            MediaType JSON_TYPE = MediaType.get("application/json; charset=utf-8");
            RequestBody reqBody = RequestBody.create(body.toString(), JSON_TYPE);
            Request req = new Request.Builder()
                    .url(httpBase + "/api/devices/" + DeviceIdentity.id(this) + "/state")
                    .post(reqBody)
                    .build();
            try (Response resp = client.newCall(req).execute()) {
                Log.d(TAG, "📱 device state → " + resp.code());
            }
        } catch (Exception e) {
            Log.e(TAG, "📱 device state report failed: " + e.getMessage());
        }
    }

    // ══════════════════════════════════════════════════════════
    //  移动端截图轮询 — 长轮询 pending，收到请求弹确认页
    // ══════════════════════════════════════════════════════════

    private synchronized void startScreenPollThread() {
        if (screenPollThread != null && screenPollThread.isAlive()) return;

        screenPollThread = new Thread(() -> {
            Log.i(TAG, "📸 Screen poll thread started");
            try { Thread.sleep(15000); } catch (InterruptedException e) { return; }

            // 复用带 Bearer 拦截器的共享 client（readTimeout=0，适合长轮询）。
            // 本地去重：同一 pending 只弹一次，避免反复 startActivity/通知。
            String lastSurfaced = null;
            while (shouldRun) {
                String httpBase = httpBaseFromWs();
                if (httpBase == null) {
                    if (!sleepOrBreak(SCREEN_POLL_ERROR_BACKOFF_MS)) break;
                    continue;
                }
                try {
                    String url = httpBase + "/api/mobile-screen/pending?device_id="
                            + DeviceIdentity.id(this) + "&timeout=" + SCREEN_POLL_TIMEOUT_SEC;
                    Request req = new Request.Builder().url(url).get().build();
                    try (Response resp = client.newCall(req).execute()) {
                        int code = resp.code();
                        if (code == 200 && resp.body() != null) {
                            JSONObject obj = new JSONObject(resp.body().string());
                            String rid = obj.optString("request_id", "");
                            if (!rid.isEmpty() && !rid.equals(lastSurfaced)) {
                                lastSurfaced = rid;
                                launchScreenConsent(httpBase, obj);
                            }
                            // pending 仍在时服务端会立刻返回，节流避免空转打后端。
                            if (!sleepOrBreak(SCREEN_POLL_PENDING_THROTTLE_MS)) break;
                        } else if (code == 204) {
                            lastSurfaced = null;  // 无 pending（已处理/超时），重置去重
                        } else {
                            Log.w(TAG, "📸 poll http " + code);
                            if (!sleepOrBreak(SCREEN_POLL_ERROR_BACKOFF_MS)) break;
                        }
                    }
                } catch (Exception e) {
                    Log.e(TAG, "📸 screen poll error: " + e.getMessage());
                    if (!sleepOrBreak(SCREEN_POLL_ERROR_BACKOFF_MS)) break;
                }
            }
            Log.i(TAG, "📸 Screen poll thread exiting");
        }, "ObsidianScreenPoll");
        screenPollThread.setDaemon(false);
        screenPollThread.start();
    }

    /** sleep 指定毫秒；被中断返回 false 以便跳出轮询循环。 */
    private boolean sleepOrBreak(long ms) {
        try { Thread.sleep(ms); return true; }
        catch (InterruptedException e) { return false; }
    }

    /** 收到截图请求：弹高优先级通知拉起确认页。 */
    private void launchScreenConsent(String httpBase, JSONObject req) {
        String requestId = req.optString("request_id", "");
        if (requestId.isEmpty()) return;
        String reason = req.optString("reason", "想确认你当前在做什么");
        String aiName = req.optString("ai_name", "AI");

        Intent intent = new Intent(this, ScreenCaptureRequestActivity.class);
        intent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        intent.putExtra(ScreenCaptureRequestActivity.EXTRA_REQUEST_ID, requestId);
        intent.putExtra(ScreenCaptureRequestActivity.EXTRA_REASON, reason);
        intent.putExtra(ScreenCaptureRequestActivity.EXTRA_AI_NAME, aiName);
        intent.putExtra(ScreenCaptureRequestActivity.EXTRA_HTTP_BASE, httpBase);
        intent.putExtra(ScreenCaptureRequestActivity.EXTRA_TOKEN, authToken);

        PendingIntent pi = PendingIntent.getActivity(this, requestId.hashCode(), intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        // 应用在前台时直接拉起；否则靠通知触发。
        try {
            startActivity(intent);
        } catch (Exception e) {
            Log.d(TAG, "📸 direct startActivity blocked, relying on notification");
        }

        Notification n = new NotificationCompat.Builder(this, CH_ALARM)
                .setSmallIcon(R.mipmap.ic_launcher)
                .setContentTitle(aiName + " 想看一下这台设备的屏幕")
                .setContentText("原因：" + reason)
                .setStyle(new NotificationCompat.BigTextStyle().bigText("原因：" + reason))
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .setCategory(NotificationCompat.CATEGORY_CALL)
                .setContentIntent(pi)
                .setTimeoutAfter(SCREEN_REQUEST_NOTIFICATION_TIMEOUT_MS)
                .setAutoCancel(true)
                .build();
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm != null) nm.notify(NOTIF_SCREEN_REQ, n);
    }

    // ══════════════════════════════════════════════════════════
    //  屏幕开关监听 — 锁屏/亮屏时立即上报
    // ══════════════════════════════════════════════════════════

    private void registerScreenReceiver() {
        if (screenReceiver != null) return;
        screenReceiver = new BroadcastReceiver() {
            @Override
            public void onReceive(Context context, Intent intent) {
                if (intent == null || intent.getAction() == null) return;
                switch (intent.getAction()) {
                    case Intent.ACTION_SCREEN_OFF:
                        Log.i(TAG, "📱 Screen OFF");
                        screenOn = false;
                        lastReportedApp = "__screen_off__";
                        new Thread(() -> postActivityToServer("screen_off"), "ScreenOff").start();
                        break;
                    case Intent.ACTION_SCREEN_ON:
                        Log.i(TAG, "📱 Screen ON");
                        screenOn = true;
                        lastReportedApp = "__screen_on__";
                        new Thread(() -> postActivityToServer("screen_on"), "ScreenOn").start();
                        break;
                    case Intent.ACTION_USER_PRESENT:
                        Log.i(TAG, "🔓 User unlocked");
                        // 解锁事件走 sensing 通道（社交脉搏的补充信号）
                        new Thread(() -> {
                            if (sensingReporter != null) sensingReporter.reportUnlock();
                        }, "UnlockPost").start();
                        break;
                }
            }
        };
        IntentFilter filter = new IntentFilter();
        filter.addAction(Intent.ACTION_SCREEN_OFF);
        filter.addAction(Intent.ACTION_SCREEN_ON);
        filter.addAction(Intent.ACTION_USER_PRESENT);
        registerReceiver(screenReceiver, filter);
        Log.i(TAG, "📱 Screen receiver registered (with USER_PRESENT)");
    }

    // ══════════════════════════════════════════════════════════
    //  体感 + 体征采样线程（每 5 分钟）
    // ══════════════════════════════════════════════════════════

    private synchronized void startSensingThread() {
        if (sensingThread != null && sensingThread.isAlive()) return;

        sensingThread = new Thread(() -> {
            Log.i(TAG, "🫧 Sensing thread started");
            // 等 30 秒让 WS / 权限 / HC 初始化稳定
            try { Thread.sleep(30_000); } catch (InterruptedException e) { return; }

            while (shouldRun) {
                try {
                    String httpBase = httpBaseFromWs();
                    if (httpBase != null) {
                        if (sensingReporter == null) {
                            sensingReporter = new SensingReporter(
                                    getApplicationContext(), client, httpBase);
                        }
                        if (healthConnectReporter == null) {
                            healthConnectReporter = new HealthConnectReporter(
                                    getApplicationContext(), client, httpBase);
                        }
                        sensingReporter.reportOnce();
                        healthConnectReporter.reportOnce();
                    }
                } catch (Exception e) {
                    Log.e(TAG, "🫧 sensing error: " + e.getMessage());
                }

                try { Thread.sleep(SENSING_INTERVAL); }
                catch (InterruptedException e) { break; }
            }
            Log.i(TAG, "🫧 Sensing thread exiting");
        }, "ObsidianSensing");
        sensingThread.setDaemon(false);
        sensingThread.start();
    }

    private String httpBaseFromWs() {
        if (serverUrl == null) return null;
        String base = serverUrl
                .replace("ws://", "http://")
                .replace("wss://", "https://");
        if (base.endsWith("/ws")) {
            base = base.substring(0, base.length() - 3);
        }
        return base;
    }

    private void unregisterScreenReceiver() {
        if (screenReceiver != null) {
            try { unregisterReceiver(screenReceiver); } catch (Exception ignored) {}
            screenReceiver = null;
        }
    }
}
