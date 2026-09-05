package app.obsidianvow.core;

import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;
import android.util.Log;

import org.json.JSONObject;

import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.TimeUnit;

import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

/**
 * 社交脉搏：只盯微信/QQ 的通知元数据（时间戳 + 包名）。
 * 不读通知正文、标题、sender；完全合规。
 * 需要用户在「设置 → 通知访问权限」里手动开启一次。
 */
public class SocialPulseService extends NotificationListenerService {
    private static final String TAG = "ObsidianSocialPulse";
    private static final MediaType JSON = MediaType.get("application/json; charset=utf-8");

    // 白名单：只有这些包的通知才上报
    private static final Map<String, String> WHITELIST = new HashMap<>();
    static {
        WHITELIST.put("com.tencent.mm", "微信");
        WHITELIST.put("com.tencent.mobileqq", "QQ");
        WHITELIST.put("com.tencent.tim", "TIM");
    }

    // 防抖：同一包名 5 秒内只上报一次
    private static final long MIN_INTERVAL_MS = 5_000L;
    private final Map<String, Long> lastPostTime = new HashMap<>();

    private OkHttpClient client;

    @Override
    public void onCreate() {
        super.onCreate();
        client = new OkHttpClient.Builder()
                .connectTimeout(5, TimeUnit.SECONDS)
                .readTimeout(10, TimeUnit.SECONDS)
                .build();
        Log.i(TAG, "SocialPulseService onCreate");
    }

    @Override
    public void onNotificationPosted(StatusBarNotification sbn) {
        if (sbn == null) return;
        String pkg = sbn.getPackageName();
        String appName = WHITELIST.get(pkg);
        if (appName == null) return;  // 非白名单直接忽略

        long now = System.currentTimeMillis();
        Long last = lastPostTime.get(pkg);
        if (last != null && now - last < MIN_INTERVAL_MS) return;
        lastPostTime.put(pkg, now);

        postNotification(pkg, appName);
    }

    @Override
    public void onNotificationRemoved(StatusBarNotification sbn) {
        // 不关心移除
    }

    private void postNotification(String pkg, String appName) {
        String httpBase = resolveHttpBase();
        if (httpBase == null) return;
        String token = resolveAuthToken();

        // 在后台线程发送，避免阻塞 NotificationListener 回调
        new Thread(() -> {
            try {
                JSONObject body = new JSONObject();
                body.put("timestamp", System.currentTimeMillis() / 1000.0);
                body.put("app", appName);
                body.put("package", pkg);

                Request.Builder builder = new Request.Builder()
                        .url(httpBase + "/api/notification/tick")
                        .post(RequestBody.create(body.toString(), JSON));
                if (token != null && !token.isEmpty()) {
                    builder.header("Authorization", "Bearer " + token);
                }
                Request req = builder.build();
                try (Response resp = client.newCall(req).execute()) {
                    Log.d(TAG, "notif " + appName + " → " + resp.code());
                }
            } catch (Exception e) {
                Log.w(TAG, "post notif failed: " + e.getMessage());
            }
        }, "SocialPulsePost").start();
    }

    /** 从 ObsidianPushService 共享的 SharedPreferences 里拿服务端地址。 */
    private String resolveHttpBase() {
        SharedPreferences prefs = getSharedPreferences("obsidian_prefs", Context.MODE_PRIVATE);
        String saved = prefs.getString("saved_url", null);
        if (saved == null) return null;
        String base = saved.replace("ws://", "http://").replace("wss://", "https://");
        if (base.endsWith("/chat")) base = base.substring(0, base.length() - 5);
        if (base.endsWith("/ws"))   base = base.substring(0, base.length() - 3);
        if (base.endsWith("/"))     base = base.substring(0, base.length() - 1);
        return base;
    }

    private String resolveAuthToken() {
        SharedPreferences prefs = getSharedPreferences("obsidian_prefs", Context.MODE_PRIVATE);
        return prefs.getString("auth_token", "");
    }
}
