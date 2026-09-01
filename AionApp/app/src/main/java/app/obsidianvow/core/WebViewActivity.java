package app.obsidianvow.core;

import android.Manifest;
import android.app.KeyguardManager;
import android.annotation.SuppressLint;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.Settings;
import android.webkit.ConsoleMessage;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import androidx.activity.result.ActivityResultLauncher;
import androidx.appcompat.app.AlertDialog;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;

/**
 * WebView 全屏聊天页
 * - 支持 JS / DOM Storage / WebSocket
 * - 自动授予麦克风权限（给 Web 端 getUserMedia 用）
 * - 支持文件上传（图片/视频选择）
 */
public class WebViewActivity extends AppCompatActivity {

    private static final int REQ_AUDIO = 1001;
    private WebView webView;
    private String targetUrl;
    private boolean pageLoaded = false;
    private boolean permissionsRequested = false;
    private int retryCount = 0;
    private static final int MAX_RETRY = 5;
    private static final int COLOR_BG = 0xFF1A1714;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private ValueCallback<Uri[]> fileCallback;
    private PermissionRequest pendingPermRequest;
    private BleBridge bleBridge;
    private volatile boolean chatVisible = false;

    private final ActivityResultLauncher<Intent> fileChooserLauncher =
            registerForActivityResult(new ActivityResultContracts.StartActivityForResult(), result -> {
                if (fileCallback == null) return;
                Uri[] uris = null;
                if (result.getResultCode() == RESULT_OK && result.getData() != null) {
                    if (result.getData().getClipData() != null) {
                        int count = result.getData().getClipData().getItemCount();
                        uris = new Uri[count];
                        for (int i = 0; i < count; i++) {
                            uris[i] = result.getData().getClipData().getItemAt(i).getUri();
                        }
                    } else if (result.getData().getData() != null) {
                        uris = new Uri[]{result.getData().getData()};
                    }
                }
                fileCallback.onReceiveValue(uris);
                fileCallback = null;
            });

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // 与新版前端的暗色琥珀主题保持一致。
        getWindow().setStatusBarColor(COLOR_BG);
        getWindow().setNavigationBarColor(COLOR_BG);
        getWindow().getDecorView().setSystemUiVisibility(0);

        boolean debuggable = (getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) != 0;
        WebView.setWebContentsDebuggingEnabled(debuggable);

        webView = new WebView(this);
        setContentView(webView);

        // 原生麦克风桥接（绕过 getUserMedia 的 HTTPS 限制）
        webView.addJavascriptInterface(new AudioBridge(webView), "AionAudio");

        // 聊天页真正显示到某条消息后，按消息时间撤掉它及更早的通知。
        webView.addJavascriptInterface(new NotificationReadBridge(), "AionNotifications");

        // 原生 BLE 桥接（绕过 WebView 不支持 Web Bluetooth API 的限制）
        bleBridge = new BleBridge(webView, this);
        webView.addJavascriptInterface(bleBridge, "AionBle");

        // 失控系列 BLE 广播桥（手机当广播源，玩具扫描接收）
        webView.addJavascriptInterface(new BleAdvBridge(webView, this), "AionAdv");

        // Muse 潮汐 BLE 广播桥
        webView.addJavascriptInterface(new MuseBleBridge(webView, this), "AionMuse");

        // 让 DomForegroundService 能回调 WebView JS
        DomForegroundService.setWebView(webView);
        MuseForegroundService.setWebView(webView);

        // 权限请求延迟到页面加载完成后，避免系统弹窗阻塞 WebView 加载
        // 见 onPageFinished → requestPermissionsSequentially()

        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);               // localStorage
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false); // 允许自动播放音频（TTS / 闹铃）
        s.setAllowFileAccess(true);
        s.setAllowContentAccess(true);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);       // 尊重服务端 Cache-Control
        s.setUserAgentString(s.getUserAgentString() + " ObsidianVowApp/1.0");

        // 让 WebView 的渲染和真实 Chrome 保持一致
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            webView.getSettings().setSafeBrowsingEnabled(false);
        }

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                String scheme = request.getUrl().getScheme();
                // 错误页按钮：重试 / 切换地址
                if ("aion".equals(scheme)) {
                    String host = request.getUrl().getHost();
                    if ("retry".equals(host)) {
                        retryCount = 0;
                        pageLoaded = false;
                        webView.loadUrl(targetUrl);
                    } else if ("switch".equals(host)) {
                        SharedPreferences prefs = getSharedPreferences("aion_prefs", MODE_PRIVATE);
                        prefs.edit().putBoolean("auto_connect", false).apply();
                        startActivity(new Intent(WebViewActivity.this, LauncherActivity.class));
                        finish();
                    }
                    return true;
                }
                // 站内导航留在 WebView，外部链接用浏览器打开
                // 同源域名（targetUrl 的 host）一律留在 WebView
                String urlHost = request.getUrl().getHost();
                String targetHost = android.net.Uri.parse(targetUrl).getHost();
                if (urlHost != null && targetHost != null && urlHost.equals(targetHost)) {
                    return false;
                }
                // 兼容常见内网/Tailscale 段
                if (urlHost != null && (urlHost.startsWith("192.168.")
                        || urlHost.startsWith("10.")
                        || urlHost.startsWith("100.")           // Tailscale CGNAT
                        || urlHost.endsWith(".ts.net")           // Tailscale MagicDNS
                        || "localhost".equals(urlHost)
                        || "127.0.0.1".equals(urlHost))) {
                    return false;
                }
                startActivity(new Intent(Intent.ACTION_VIEW, request.getUrl()));
                return true;
            }

            @Override
            public void onPageStarted(WebView view, String url, Bitmap favicon) {
                super.onPageStarted(view, url, favicon);
                pageLoaded = false;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                super.onPageFinished(view, url);
                // 过滤掉错误页的 onPageFinished（data: URL）
                if (url != null && !url.startsWith("data:")) {
                    pageLoaded = true;
                    retryCount = 0;
                    // 页面加载成功后，延迟请求权限（串行，不阻塞页面）
                    if (!permissionsRequested) {
                        permissionsRequested = true;
                        mainHandler.postDelayed(() -> requestPermissionsSequentially(0), 1500);
                    }
                }
            }

            @Override
            public void onReceivedSslError(WebView view, android.webkit.SslErrorHandler handler,
                                           android.net.http.SslError error) {
                // 自部署场景可能使用自签证书，鉴权仍由 Bearer Token 负责。
                handler.proceed();
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                // 只处理主页面加载失败（非子资源）
                if (request.isForMainFrame()) {
                    pageLoaded = false;
                    android.util.Log.e("AionWebView", "页面加载失败: " + error.getDescription());
                    showErrorPage(view, error.getDescription().toString());
                }
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            // ── 麦克风权限自动授予（给网页 getUserMedia 用） ──
            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                String[] resources = request.getResources();
                for (String res : resources) {
                    if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(res)) {
                        if (ContextCompat.checkSelfPermission(
                                WebViewActivity.this, Manifest.permission.RECORD_AUDIO)
                                == PackageManager.PERMISSION_GRANTED) {
                            request.grant(resources);
                            return;
                        } else {
                            // 存下来，等 Android 权限回调后再授予
                            pendingPermRequest = request;
                            ActivityCompat.requestPermissions(WebViewActivity.this,
                                    new String[]{Manifest.permission.RECORD_AUDIO}, REQ_AUDIO);
                            return;
                        }
                    }
                }
                request.deny();
            }

            // ── 文件上传（图片/视频选择） ──
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                fileCallback = callback;
                Intent intent = params.createIntent();
                intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                try {
                    fileChooserLauncher.launch(intent);
                } catch (Exception e) {
                    fileCallback = null;
                    Toast.makeText(WebViewActivity.this, "无法打开文件选择器", Toast.LENGTH_SHORT).show();
                    return false;
                }
                return true;
            }

            // ── 控制台日志（方便调试） ──
            @Override
            public boolean onConsoleMessage(ConsoleMessage msg) {
                android.util.Log.d("AionWebView",
                        msg.message() + " -- line " + msg.lineNumber() + " of " + msg.sourceId());
                return true;
            }
        });

        // 加载目标 URL
        targetUrl = getIntent().getStringExtra("url");
        if (targetUrl == null || targetUrl.isEmpty()) {
            // 没配置 → 回到设置页
            startActivity(new Intent(this, LauncherActivity.class));
            finish();
            return;
        }
        // 先由原生侧写入鉴权 cookie，避免 WebView 首次 ?token= 跳转后 cookie
        // 没来得及生效，或 HTTP 自部署场景下 Secure cookie 被丢弃。
        String token = getIntent().getStringExtra("token");
        if (token != null && !token.isEmpty()) {
            seedAuthCookie(targetUrl, token);
        }
        webView.loadUrl(targetUrl);
    }

    private void seedAuthCookie(String url, String token) {
        try {
            CookieManager cm = CookieManager.getInstance();
            cm.setAcceptCookie(true);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                cm.setAcceptThirdPartyCookies(webView, true);
            }
            cm.setCookie(url, "aion_token=" + token + "; Path=/; SameSite=Lax");
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                cm.flush();
            }
        } catch (Exception e) {
            android.util.Log.e("AionWebView", "seed auth cookie failed: " + e.getMessage());
        }
    }

    /**
     * 加载失败时显示错误页：自动重试 + 手动按钮
     */
    private void showErrorPage(WebView view, String errorMsg) {
        if (retryCount < MAX_RETRY) {
            retryCount++;
            int delay = Math.min(retryCount * 2000, 8000); // 2s, 4s, 6s, 8s, 8s
            android.util.Log.i("AionWebView", "自动重试 " + retryCount + "/" + MAX_RETRY + "，" + delay + "ms 后重试");
            String retryHtml = buildErrorHtml(
                    "正在连接服务器",
                    "第 " + retryCount + " 次重试（最多 " + MAX_RETRY + " 次）",
                    errorMsg,
                    null);
            view.loadDataWithBaseURL(null, retryHtml, "text/html", "utf-8", null);
            mainHandler.postDelayed(() -> {
                if (webView != null && !pageLoaded) {
                    webView.loadUrl(targetUrl);
                }
            }, delay);
        } else {
            // 重试耗尽，显示手动操作页面
            String actions = "<button class='btn primary' onclick='window.location.href=\"aion://retry\"'>重新连接</button>"
                    + "<button class='btn' onclick='window.location.href=\"aion://switch\"'>切换地址</button>";
            String failHtml = buildErrorHtml(
                    "无法连接到服务器",
                    targetUrl,
                    errorMsg,
                    actions);
            view.loadDataWithBaseURL(null, failHtml, "text/html", "utf-8", null);
        }
    }

    private String buildErrorHtml(String title, String subtitle, String detail, String actionsHtml) {
        String actions = actionsHtml == null ? "" : "<div class='actions'>" + actionsHtml + "</div>";
        return "<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
                + "<style>"
                + "html,body{margin:0;min-height:100%;background:#1a1714;color:#e8dcc8;"
                + "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;}"
                + "body{min-height:100vh;min-height:100dvh;display:flex;align-items:center;justify-content:center;"
                + "padding:28px;box-sizing:border-box;background:#1a1714;}"
                + ".panel{width:min(100%,360px);padding:24px 22px;border:1px solid rgba(212,148,58,.18);"
                + "border-radius:12px;background:rgba(36,33,32,.92);box-shadow:0 18px 48px rgba(0,0,0,.38);}"
                + ".mark{width:42px;height:3px;border-radius:999px;background:#d4943a;box-shadow:0 0 14px rgba(212,148,58,.35);"
                + "margin:0 0 18px;}"
                + "h1{font-size:20px;line-height:1.3;margin:0 0 8px;font-weight:650;color:#f3e7d5;}"
                + ".sub{font-size:13px;line-height:1.55;color:#b5a898;word-break:break-all;margin-bottom:10px;}"
                + ".detail{font-size:12px;line-height:1.55;color:#75695d;word-break:break-word;}"
                + ".actions{display:flex;gap:10px;margin-top:22px;}"
                + ".btn{flex:1;padding:11px 12px;border-radius:8px;border:1px solid rgba(212,148,58,.25);"
                + "background:#2e2a26;color:#e8dcc8;font-size:14px;}"
                + ".btn.primary{border-color:#d4943a;background:#d4943a;color:#1a1714;font-weight:650;}"
                + "</style></head><body><main class='panel'><div class='mark'></div>"
                + "<h1>" + escapeHtml(title) + "</h1>"
                + "<div class='sub'>" + escapeHtml(subtitle) + "</div>"
                + "<div class='detail'>" + escapeHtml(detail) + "</div>"
                + actions
                + "</main></body></html>";
    }

    private String escapeHtml(String s) {
        if (s == null) return "";
        return s.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("\"", "&quot;")
                .replace("'", "&#39;");
    }

    // ── 串行权限请求链：页面加载后依次请求，每次只弹一个 ──
    private static final int PERM_STEP_NOTIFICATION = 0;
    private static final int PERM_STEP_AUDIO = 1;
    private static final int PERM_STEP_LOCATION = 2;
    private static final int PERM_STEP_BLUETOOTH = 3;
    private static final int PERM_STEP_BATTERY = 4;
    private static final int PERM_STEP_DONE = 5;
    private static final int REQ_BLUETOOTH = 4001;

    /**
     * 串行请求权限：step 0→通知, 1→麦克风, 2→定位, 3→蓝牙, 4→电池优化
     * 每一步完成后在 onRequestPermissionsResult 中调用下一步
     */
    private void requestPermissionsSequentially(int step) {
        if (step >= PERM_STEP_DONE) return;

        switch (step) {
            case PERM_STEP_NOTIFICATION:
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
                        && ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                        != PackageManager.PERMISSION_GRANTED) {
                    ActivityCompat.requestPermissions(this,
                            new String[]{Manifest.permission.POST_NOTIFICATIONS}, 2001);
                    return; // 等回调
                }
                requestPermissionsSequentially(PERM_STEP_AUDIO);
                break;

            case PERM_STEP_AUDIO:
                if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
                        != PackageManager.PERMISSION_GRANTED) {
                    ActivityCompat.requestPermissions(this,
                            new String[]{Manifest.permission.RECORD_AUDIO}, REQ_AUDIO);
                    return;
                }
                requestPermissionsSequentially(PERM_STEP_LOCATION);
                break;

            case PERM_STEP_LOCATION:
                if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
                        != PackageManager.PERMISSION_GRANTED) {
                    ActivityCompat.requestPermissions(this,
                            new String[]{
                                    Manifest.permission.ACCESS_FINE_LOCATION,
                                    Manifest.permission.ACCESS_COARSE_LOCATION
                            }, REQ_LOCATION);
                    return;
                }
                // 前台定位已有，尝试后台定位
                requestBackgroundLocationOrNext();
                break;

            case PERM_STEP_BLUETOOTH:
                requestBluetoothOrNext();
                break;

            case PERM_STEP_BATTERY:
                requestBatteryOptimization();
                // 电池优化是 startActivity，没有回调，直接结束
                break;
        }
    }

    private void requestBackgroundLocationOrNext() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q
                && ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_BACKGROUND_LOCATION)
                != PackageManager.PERMISSION_GRANTED) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                new AlertDialog.Builder(this)
                        .setTitle("需要后台定位权限")
                        .setMessage("为了在后台持续上报位置信息，请在接下来的设置中选择「始终允许」")
                        .setPositiveButton("去设置", (d, w) -> {
                            ActivityCompat.requestPermissions(this,
                                    new String[]{Manifest.permission.ACCESS_BACKGROUND_LOCATION},
                                    REQ_BACKGROUND_LOCATION);
                        })
                        .setNegativeButton("跳过", (d, w) -> {
                            requestPermissionsSequentially(PERM_STEP_BLUETOOTH);
                        })
                        .show();
                return;
            } else {
                ActivityCompat.requestPermissions(this,
                        new String[]{Manifest.permission.ACCESS_BACKGROUND_LOCATION},
                        REQ_BACKGROUND_LOCATION);
                return;
            }
        }
        requestPermissionsSequentially(PERM_STEP_BLUETOOTH);
    }

    private void requestBluetoothOrNext() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            boolean needScan = ContextCompat.checkSelfPermission(this, "android.permission.BLUETOOTH_SCAN")
                    != PackageManager.PERMISSION_GRANTED;
            boolean needConnect = ContextCompat.checkSelfPermission(this, "android.permission.BLUETOOTH_CONNECT")
                    != PackageManager.PERMISSION_GRANTED;
            boolean needAdvertise = ContextCompat.checkSelfPermission(this, "android.permission.BLUETOOTH_ADVERTISE")
                    != PackageManager.PERMISSION_GRANTED;
            if (needScan || needConnect || needAdvertise) {
                java.util.List<String> perms = new java.util.ArrayList<>();
                if (needScan) perms.add("android.permission.BLUETOOTH_SCAN");
                if (needConnect) perms.add("android.permission.BLUETOOTH_CONNECT");
                if (needAdvertise) perms.add("android.permission.BLUETOOTH_ADVERTISE");
                ActivityCompat.requestPermissions(this,
                        perms.toArray(new String[0]), REQ_BLUETOOTH);
                return;
            }
        }
        requestPermissionsSequentially(PERM_STEP_BATTERY);
    }

    // ── Android 权限回调：完成后继续下一步 ──
    @Override
    public void onRequestPermissionsResult(int code, @NonNull String[] perms, @NonNull int[] results) {
        super.onRequestPermissionsResult(code, perms, results);
        if (code == REQ_AUDIO && results.length > 0
                && results[0] == PackageManager.PERMISSION_GRANTED) {
            if (pendingPermRequest != null) {
                pendingPermRequest.grant(pendingPermRequest.getResources());
                pendingPermRequest = null;
            }
        }

        // 根据 requestCode 继续下一步
        switch (code) {
            case 2001: // POST_NOTIFICATIONS
                requestPermissionsSequentially(PERM_STEP_AUDIO);
                break;
            case REQ_AUDIO:
                requestPermissionsSequentially(PERM_STEP_LOCATION);
                break;
            case REQ_LOCATION:
                if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
                    requestBackgroundLocationOrNext();
                } else {
                    requestPermissionsSequentially(PERM_STEP_BLUETOOTH);
                }
                break;
            case REQ_BACKGROUND_LOCATION:
                requestPermissionsSequentially(PERM_STEP_BLUETOOTH);
                break;
            case REQ_BLUETOOTH:
                requestPermissionsSequentially(PERM_STEP_BATTERY);
                break;
        }
    }

    // ── 返回键 / 手势返回 ──
    @SuppressWarnings("deprecation")
    @Override
    public void onBackPressed() {
        if (webView.canGoBack()) {
            webView.goBack();
        } else {
            // 已经退到最顶层，弹出选择对话框
            new AlertDialog.Builder(this, R.style.Theme_ObsidianVow_Dialog)
                .setTitle("Obsidian Vow")
                .setMessage("要切换连接地址还是退出？")
                .setPositiveButton("切换地址", (d, w) -> {
                    SharedPreferences prefs = getSharedPreferences("aion_prefs", MODE_PRIVATE);
                    prefs.edit().putBoolean("auto_connect", false).apply();
                    startActivity(new Intent(this, LauncherActivity.class));
                    finish();
                })
                .setNegativeButton("退出", (d, w) -> finish())
                .setNeutralButton("取消", null)
                .show();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        chatVisible = true;
        // 告诉推送服务：前台已打开，不需要弹通知
        notifyServiceForeground(true);
        // 回到前台：强制重连 WebSocket + 重新加载当天消息
        if (webView != null && pageLoaded) {
            webView.evaluateJavascript(
                "(function(){" +
                "  if(typeof ws!=='undefined' && ws.readyState!==1){" +
                "    console.log('[AionApp] WS断线，重连+刷新');" +
                "    connectWS();" +
                "    setTimeout(function(){if(typeof loadMessages==='function')loadMessages();},1500);" +
                "  }" +
                "})();",
                null);
            // BLE 状态同步：如果主控 Service 还活着，告诉前端广播仍在
            if (DomForegroundService.isRunning()) {
                webView.evaluateJavascript(
                    "typeof toyNativeBle!=='undefined'&&typeof toyNativeBle.onConnected==='function'&&toyNativeBle.onConnected()",
                    null);
            }
        }
    }

    @Override
    protected void onPause() {
        chatVisible = false;
        super.onPause();
        // 告诉推送服务：前台已关闭，需要弹通知
        notifyServiceForeground(false);
    }

    private void notifyServiceForeground(boolean active) {
        Intent intent = new Intent(this, ObsidianPushService.class);
        intent.putExtra("action", "set_foreground");
        intent.putExtra("active", active);
        startService(intent);
    }

    private final class NotificationReadBridge {
        @JavascriptInterface
        public void seenThrough(double createdAtSeconds) {
            if (!canAcknowledgeMessagesSeen()
                    || !Double.isFinite(createdAtSeconds)
                    || createdAtSeconds <= 0) {
                return;
            }
            long cutoffMs = Math.round(createdAtSeconds * 1000.0);
            ObsidianPushService.clearMessageNotificationsThrough(
                    getApplicationContext(), cutoffMs
            );
        }
    }

    /**
     * WebView 的 document.visibilityState 和 Activity 生命周期在部分 ROM 锁屏时
     * 仍可能短暂报告 visible。只有屏幕可交互且 Keyguard 未显示时才算真的看见。
     */
    private boolean canAcknowledgeMessagesSeen() {
        PowerManager powerManager = getSystemService(PowerManager.class);
        boolean screenInteractive = powerManager != null && powerManager.isInteractive();
        KeyguardManager keyguardManager = getSystemService(KeyguardManager.class);
        boolean deviceLocked = keyguardManager == null || keyguardManager.isKeyguardLocked();
        boolean visible = MessageNotificationPolicy.isChatActuallyVisible(
                chatVisible,
                screenInteractive,
                deviceLocked
        );
        android.util.Log.d("AionNotifications", "ack visible=" + visible
                + " activityVisible=" + chatVisible
                + " interactive=" + screenInteractive
                + " locked=" + deviceLocked);
        return visible;
    }

    private void requestBatteryOptimization() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
            try {
                if (pm != null && !pm.isIgnoringBatteryOptimizations(getPackageName())) {
                    Intent intent = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
                    intent.setData(Uri.parse("package:" + getPackageName()));
                    startActivity(intent);
                }
            } catch (Exception e) {
                android.util.Log.w("AionWebView", "电池优化引导失败: " + e.getMessage());
            }
        }
    }

    private static final int REQ_LOCATION = 3001;
    private static final int REQ_BACKGROUND_LOCATION = 3002;

    @Override
    protected void onDestroy() {
        DomForegroundService.setWebView(null);
        MuseForegroundService.setWebView(null);
        mainHandler.removeCallbacksAndMessages(null);
        if (bleBridge != null) {
            bleBridge.shutdown();
            bleBridge = null;
        }
        if (webView != null) {
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }
}
