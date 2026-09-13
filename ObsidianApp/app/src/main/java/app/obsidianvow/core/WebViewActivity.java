package app.obsidianvow.core;

import android.Manifest;
import android.app.KeyguardManager;
import android.content.Context;
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
import android.webkit.RenderProcessGoneDetail;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;

import androidx.activity.result.ActivityResultLauncher;
import androidx.appcompat.app.AlertDialog;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.annotation.NonNull;
import androidx.annotation.Nullable;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;
import androidx.webkit.WebViewCompat;
import androidx.webkit.WebViewFeature;
import androidx.webkit.WebViewRenderProcess;
import androidx.webkit.WebViewRenderProcessClient;

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
    private String authToken;
    private boolean pageLoaded = false;
    private boolean permissionsRequested = false;
    private int retryCount = 0;
    private static final int MAX_RETRY = 5;
    private static final int COLOR_BG = 0xFF1A1714;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private ValueCallback<Uri[]> fileCallback;
    private PermissionRequest pendingPermRequest;
    private BleBridge bleBridge;
    private AudioBridge audioBridge;
    private volatile boolean chatVisible = false;
    private boolean chatReady = false;
    private FrameLayout webContainer;
    private LinearLayout recoveryPanel;
    private TextView recoveryText;
    private Button recoveryButton;
    private int recoveryAttempts = 0;
    private boolean rendererRecoveryPending = false;
    private boolean pageLoadFailed = false;
    private int probeGeneration = 0;
    private int messageAckGeneration = 0;
    private static final int MAX_RENDER_RECOVERIES = 2;
    private static final long RENDER_PROBE_TIMEOUT_MS = 8000L;
    private Runnable probeTimeout;
    private Runnable nextProbe;
    private Runnable loadTimeout;
    private Runnable loadRetry;

    static Intent openChatIntent(Context context) {
        return new Intent(context, WebViewActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP
                        | Intent.FLAG_ACTIVITY_SINGLE_TOP);
    }

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

        if (!readLaunchConfiguration(getIntent())) return;
        webContainer = new FrameLayout(this);
        webContainer.setBackgroundColor(COLOR_BG);
        setContentView(webContainer);
        createRecoveryPanel();
        createWebView();
        ensurePushService();
        LookAtMeController.activity = new java.lang.ref.WeakReference<>(this);
    }

    private boolean readLaunchConfiguration(Intent intent) {
        SharedPreferences prefs = getSharedPreferences(LauncherActivity.PREFS, MODE_PRIVATE);
        String url = intent == null ? null : intent.getStringExtra("url");
        targetUrl = url == null || url.isEmpty() ? prefs.getString(LauncherActivity.KEY_URL, "") : url;
        String token = intent == null ? null : intent.getStringExtra("token");
        authToken = token == null ? prefs.getString(LauncherActivity.KEY_TOKEN, "") : token;
        if (authToken == null) authToken = "";
        if (targetUrl == null || targetUrl.isEmpty()) {
            startActivity(new Intent(this, LauncherActivity.class));
            finish();
            return false;
        }
        return true;
    }

    private void ensurePushService() {
        Intent intent = new Intent(this, ObsidianPushService.class)
                .putExtra("url", targetUrl).putExtra("token", authToken);
        ContextCompat.startForegroundService(this, intent);
    }

    private void createRecoveryPanel() {
        recoveryPanel = new LinearLayout(this);
        recoveryPanel.setOrientation(LinearLayout.VERTICAL);
        recoveryPanel.setGravity(Gravity.CENTER);
        recoveryPanel.setBackgroundColor(COLOR_BG);
        int padding = (int) (28 * getResources().getDisplayMetrics().density);
        recoveryPanel.setPadding(padding, padding, padding, padding);
        recoveryText = new TextView(this);
        recoveryText.setTextColor(0xFFF3E7D5);
        recoveryText.setTextSize(17);
        recoveryText.setGravity(Gravity.CENTER);
        recoveryPanel.addView(recoveryText);
        recoveryButton = new Button(this);
        recoveryButton.setText("重新加载");
        recoveryButton.setOnClickListener(v -> {
            recoveryAttempts = 0;
            retryCount = 0;
            if (webView == null) createWebView();
            else recoverWebView(webView, "manual_retry", true);
        });
        recoveryPanel.addView(recoveryButton);
        webContainer.addView(recoveryPanel, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        showRecovery("正在加载聊天…", false);
    }

    private void showRecovery(String text, boolean canRetry) {
        recoveryText.setText(text);
        recoveryButton.setVisibility(canRetry ? View.VISIBLE : View.GONE);
        recoveryPanel.setVisibility(View.VISIBLE);
        recoveryPanel.bringToFront();
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void createWebView() {
        if (isFinishing() || isDestroyed() || webView != null) return;
        pageLoaded = false;
        pageLoadFailed = false;
        rendererRecoveryPending = false;

        boolean debuggable = (getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) != 0;
        WebView.setWebContentsDebuggingEnabled(debuggable);

        webView = new WebView(this);
        webView.setBackgroundColor(COLOR_BG);
        webContainer.addView(webView, 0, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        // 原生麦克风桥接（绕过 getUserMedia 的 HTTPS 限制）
        audioBridge = new AudioBridge(webView);
        BrandCompatibility.addJavascriptInterface(webView, audioBridge, "ObsidianAudio");

        // 聊天页真正显示到某条消息后，按消息时间撤掉它及更早的通知。
        BrandCompatibility.addJavascriptInterface(webView, new NotificationReadBridge(webView), "ObsidianNotifications");
        BrandCompatibility.addJavascriptInterface(webView, new LookAtMeBridge(webView), "ObsidianLookAtMe");

        // 原生 BLE 桥接（绕过 WebView 不支持 Web Bluetooth API 的限制）
        bleBridge = new BleBridge(webView, this);
        BrandCompatibility.addJavascriptInterface(webView, bleBridge, "ObsidianBle");

        // 失控系列 BLE 广播桥（手机当广播源，玩具扫描接收）
        BrandCompatibility.addJavascriptInterface(webView, new BleAdvBridge(webView, this), "ObsidianAdv");

        // Muse 潮汐 BLE 广播桥
        BrandCompatibility.addJavascriptInterface(webView, new MuseBleBridge(webView, this), "ObsidianMuse");

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
                if (BrandCompatibility.isInternalScheme(scheme)) {
                    String host = request.getUrl().getHost();
                    if ("retry".equals(host)) {
                        retryCount = 0;
                        pageLoaded = false;
                        webView.loadUrl(targetUrl);
                    } else if ("switch".equals(host)) {
                        SharedPreferences prefs = getSharedPreferences("obsidian_prefs", MODE_PRIVATE);
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
                if (view != webView) return;
                cancelRendererProbe();
                pageLoaded = false;
                pageLoadFailed = false;
                setChatReady(false);
                if (loadTimeout != null) mainHandler.removeCallbacks(loadTimeout);
                loadTimeout = () -> {
                    if (view == webView && !chatReady && chatVisible) {
                        showRecovery("连接较慢，可以重新加载", true);
                        probeRenderer();
                    }
                };
                mainHandler.postDelayed(loadTimeout, 25000);
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                super.onPageFinished(view, url);
                if (view != webView) return;
                if (!pageLoadFailed && url != null && (url.startsWith("https://") || url.startsWith("http://"))) {
                    pageLoaded = true;
                    retryCount = 0;
                    if (!isChatDocument(url)) {
                        if (loadTimeout != null) mainHandler.removeCallbacks(loadTimeout);
                        recoveryPanel.setVisibility(View.GONE);
                    }
                    // 页面加载成功后，延迟请求权限（串行，不阻塞页面）
                    if (!permissionsRequested) {
                        permissionsRequested = true;
                        mainHandler.postDelayed(() -> requestPermissionsSequentially(0), 1500);
                    }
                    if (chatVisible) resumeChat();
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
                if (view == webView && request.isForMainFrame()) {
                    pageLoaded = false;
                    pageLoadFailed = true;
                    android.util.Log.e("ObsidianWebView", "页面加载失败: " + error.getDescription());
                    showErrorPage(view, error.getDescription().toString());
                }
            }

            @Override
            public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
                if (view == webView && request.isForMainFrame()) {
                    pageLoaded = false;
                    pageLoadFailed = true;
                    showErrorPage(view, "HTTP " + response.getStatusCode());
                }
            }

            @Override
            public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
                android.util.Log.w("ObsidianWebView", "renderer exited, crashed=" + detail.didCrash());
                if (view == webView) recoverWebView(view, "renderer_gone", false);
                else view.destroy();
                return true;
            }
        });

        if (WebViewFeature.isFeatureSupported(WebViewFeature.WEB_VIEW_RENDERER_CLIENT_BASIC_USAGE)) {
            WebViewCompat.setWebViewRenderProcessClient(webView, new WebViewRenderProcessClient() {
                @Override
                public void onRenderProcessUnresponsive(@NonNull WebView view, @Nullable WebViewRenderProcess renderer) {
                    if (view == webView && canAcknowledgeMessagesSeen()) {
                        recoverWebView(view, "renderer_unresponsive", true);
                    }
                }

                @Override
                public void onRenderProcessResponsive(@NonNull WebView view, @Nullable WebViewRenderProcess renderer) {
                    if (view == webView && chatVisible) probeRenderer();
                }
            });
        }

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
                android.util.Log.d("ObsidianWebView",
                        msg.message() + " -- line " + msg.lineNumber() + " of " + msg.sourceId());
                return true;
            }
        });

        // 加载目标 URL
        // 先由原生侧写入鉴权 cookie，避免 WebView 首次 ?token= 跳转后 cookie
        // 没来得及生效，或 HTTP 自部署场景下 Secure cookie 被丢弃。
        if (authToken != null && !authToken.isEmpty()) {
            seedAuthCookie(targetUrl, authToken);
        }
        showRecovery("正在加载聊天…", false);
        webView.loadUrl(targetUrl);
    }

    private void seedAuthCookie(String url, String token) {
        try {
            CookieManager cm = CookieManager.getInstance();
            cm.setAcceptCookie(true);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                cm.setAcceptThirdPartyCookies(webView, true);
            }
            for (String cookieName : BrandCompatibility.authCookieNames()) {
                cm.setCookie(url, cookieName + "=" + token + "; Path=/; SameSite=Lax");
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                cm.flush();
            }
        } catch (Exception e) {
            android.util.Log.e("ObsidianWebView", "seed auth cookie failed: " + e.getMessage());
        }
    }

    /**
     * 加载失败时显示错误页：自动重试 + 手动按钮
     */
    private void showErrorPage(WebView view, String errorMsg) {
        if (view != webView) return;
        setChatReady(false);
        if (loadRetry != null) mainHandler.removeCallbacks(loadRetry);
        if (retryCount < MAX_RETRY) {
            retryCount++;
            int delay = Math.min(retryCount * 2000, 8000); // 2s, 4s, 6s, 8s, 8s
            android.util.Log.i("ObsidianWebView", "自动重试 " + retryCount + "/" + MAX_RETRY + "，" + delay + "ms 后重试");
            showRecovery("正在重新连接…（" + retryCount + "/" + MAX_RETRY + "）", true);
            loadRetry = () -> {
                loadRetry = null;
                if (view == webView && !pageLoaded && chatVisible) {
                    view.loadUrl(targetUrl);
                }
            };
            mainHandler.postDelayed(loadRetry, delay);
        } else {
            showRecovery("暂时无法连接，点击重新加载", true);
        }
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
        if (LookAtMeController.instance != null && LookAtMeController.instance.resident()) return;
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            // 已经退到最顶层，弹出选择对话框
            new AlertDialog.Builder(this, R.style.Theme_ObsidianVow_Dialog)
                .setTitle("Obsidian Vow")
                .setMessage("要切换连接地址还是退出？")
                .setPositiveButton("切换地址", (d, w) -> {
                    SharedPreferences prefs = getSharedPreferences("obsidian_prefs", MODE_PRIVATE);
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
        LookAtMeController.activity = new java.lang.ref.WeakReference<>(this);
        maybeOpenCompanionshipInvitation();
        setChatReady(false);
        notifyServiceForeground(true);
        resumeChat();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        String previousUrl = targetUrl;
        String previousToken = authToken;
        setIntent(intent);
        maybeOpenCompanionshipInvitation();
        if (!readLaunchConfiguration(intent)) return;
        if (!targetUrl.equals(previousUrl) || !authToken.equals(previousToken)) {
            releaseWebView();
            recoveryAttempts = 0;
            createWebView();
            ensurePushService();
        } else if (webView != null && pageLoaded && !isChatDocument(webView.getUrl())) {
            webView.loadUrl(targetUrl);
        }
        if (chatVisible) resumeChat();
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus && chatVisible) { maybeOpenCompanionshipInvitation(); resumeChat(); }
    }

    private boolean isChatDocument(String url) {
        if (url == null) return false;
        String path = Uri.parse(url).getPath();
        return "/chat".equals(path) || "/chat/".equals(path);
    }

    private void resumeChat() {
        if (!chatVisible || webContainer == null || isFinishing()) return;
        if (webView == null) {
            if (recoveryAttempts <= MAX_RENDER_RECOVERIES) createWebView();
            return;
        }
        if (rendererRecoveryPending) return;
        webView.onResume();
        webView.resumeTimers();
        webView.requestLayout();
        webView.invalidate();
        if (pageLoadFailed && loadRetry == null) {
            retryCount = 0;
            webView.loadUrl(targetUrl);
        } else if (pageLoaded) {
            if (LookAtMeController.instance != null && !LookAtMeController.instance.requestId().isEmpty()
                    && !isChatDocument(webView.getUrl())) {
                webView.loadUrl(Uri.parse(targetUrl).buildUpon().path("/chat").clearQuery().fragment(null).build().toString());
                return;
            }
            webView.evaluateJavascript(
                    "(function(){"
                    + "if(typeof onChatForeground==='function'){onChatForeground();}"
                    + "else if(typeof selectConv==='function'&&typeof currentConvId!=='undefined'&&currentConvId){selectConv(currentConvId);}"
                    + "})();", null);
            if (DomForegroundService.isRunning()) {
                webView.evaluateJavascript(
                        "typeof toyNativeBle!=='undefined'&&typeof toyNativeBle.onConnected==='function'&&toyNativeBle.onConnected()",
                        null);
            }
        }
        probeRenderer();
    }

    private void cancelRendererProbe() {
        probeGeneration++;
        if (probeTimeout != null) mainHandler.removeCallbacks(probeTimeout);
        if (nextProbe != null) mainHandler.removeCallbacks(nextProbe);
        probeTimeout = nextProbe = null;
    }

    private void probeRenderer() {
        if (webView == null || rendererRecoveryPending || !canAcknowledgeMessagesSeen()) return;
        cancelRendererProbe();
        final WebView source = webView;
        final int generation = probeGeneration;
        probeTimeout = () -> {
            if (generation == probeGeneration && source == webView && canAcknowledgeMessagesSeen()) {
                recoverWebView(source, "render_probe_timeout", true);
            }
        };
        mainHandler.postDelayed(probeTimeout, RENDER_PROBE_TIMEOUT_MS);
        source.evaluateJavascript("!!document.body && document.body.getBoundingClientRect().height > 0", result -> {
            if (source != webView || generation != probeGeneration) return;
            if (pageLoaded && !pageLoadFailed && !"true".equals(result)) {
                recoverWebView(source, "empty_document", true);
                return;
            }
            source.postVisualStateCallback(generation, new WebView.VisualStateCallback() {
                @Override public void onComplete(long requestId) {
                    if (source != webView || generation != probeGeneration) return;
                    if (probeTimeout != null) mainHandler.removeCallbacks(probeTimeout);
                    probeTimeout = null;
                    source.invalidate();
                    nextProbe = () -> probeRenderer();
                    mainHandler.postDelayed(nextProbe, 30000);
                }
            });
        });
    }

    private void recoverWebView(WebView source, String reason, boolean terminateRenderer) {
        if (source != webView || isFinishing() || isDestroyed()) return;
        if (terminateRenderer && rendererRecoveryPending) return;
        android.util.Log.w("ObsidianWebView", "recovering page: " + reason);
        cancelRendererProbe();
        setChatReady(false);
        showRecovery("页面正在恢复…", false);
        if (terminateRenderer
                && WebViewFeature.isFeatureSupported(WebViewFeature.GET_WEB_VIEW_RENDERER)
                && WebViewFeature.isFeatureSupported(WebViewFeature.WEB_VIEW_RENDERER_TERMINATE)) {
            WebViewRenderProcess renderer = WebViewCompat.getWebViewRenderProcess(source);
            if (renderer != null) {
                rendererRecoveryPending = true;
                if (renderer.terminate()) {
                    // 正常由 onRenderProcessGone 清理；少数实现没有及时回调时也能退出空白页。
                    mainHandler.postDelayed(() -> {
                        if (source == webView && rendererRecoveryPending) {
                            recoverWebView(source, "renderer_exit_timeout", false);
                        }
                    }, 2500);
                    return;
                }
            }
        }
        releaseWebView();
        recoveryAttempts++;
        if (recoveryAttempts > MAX_RENDER_RECOVERIES) {
            showRecovery("页面恢复失败，点击重新加载", true);
        } else if (chatVisible) {
            mainHandler.post(this::createWebView);
        }
    }

    private void releaseWebView() {
        cancelRendererProbe();
        if (loadTimeout != null) mainHandler.removeCallbacks(loadTimeout);
        if (loadRetry != null) mainHandler.removeCallbacks(loadRetry);
        loadTimeout = loadRetry = null;
        rendererRecoveryPending = false;
        messageAckGeneration++;
        DomForegroundService.setWebView(null);
        MuseForegroundService.setWebView(null);
        if (audioBridge != null) { audioBridge.stop(); audioBridge = null; }
        if (bleBridge != null) { bleBridge.shutdown(); bleBridge = null; }
        if (fileCallback != null) { fileCallback.onReceiveValue(null); fileCallback = null; }
        if (pendingPermRequest != null) { pendingPermRequest.deny(); pendingPermRequest = null; }
        WebView previous = webView;
        webView = null;
        if (previous != null) {
            webContainer.removeView(previous);
            previous.destroy();
        }
    }

    @Override
    protected void onPause() {
        chatVisible = false;
        setChatReady(false);
        cancelRendererProbe();
        if (webView != null && !rendererRecoveryPending) {
            webView.evaluateJavascript("typeof onChatBackground==='function'&&onChatBackground()", null);
            webView.onPause();
        }
        super.onPause();
        // 告诉推送服务：前台已关闭，需要弹通知
        notifyServiceForeground(false);
    }

    private void notifyServiceForeground(boolean active) {
        Intent intent = new Intent(this, ObsidianPushService.class);
        intent.putExtra("action", "set_foreground");
        intent.putExtra("active", active);
        intent.putExtra("chat_ready", chatReady);
        startService(intent);
    }

    private void setChatReady(boolean ready) {
        if (!ready && LookAtMeController.instance != null) LookAtMeController.instance.visibility(false);
        if (!ready) messageAckGeneration++;
        if (chatReady == ready) return;
        chatReady = ready;
        Intent intent = new Intent(this, ObsidianPushService.class)
                .putExtra("action", "set_chat_ready").putExtra("active", ready);
        startService(intent);
    }

    boolean lookAtMeVisible() {
        return pageLoaded && webView != null && isChatDocument(webView.getUrl()) && canAcknowledgeMessagesSeen();
    }

    private void maybeOpenCompanionshipInvitation() {
        String id = getIntent().getStringExtra("look_at_me_notification");
        if (id == null || !canAcknowledgeMessagesSeen()) return;
        LookAtMeController c = LookAtMeController.instance;
        if (c == null) { mainHandler.postDelayed(this::maybeOpenCompanionshipInvitation, 300); return; }
        getIntent().removeExtra("look_at_me_notification");
        c.notificationOpened(id);
    }

    private final class LookAtMeBridge {
        private final WebView source;
        LookAtMeBridge(WebView source) { this.source = source; }
        @JavascriptInterface public String deviceId() { return DeviceIdentity.id(WebViewActivity.this); }
        @JavascriptInterface public String deviceName() { return DeviceIdentity.name(WebViewActivity.this); }
        @JavascriptInterface public String requestId() {
            LookAtMeController c = LookAtMeController.instance;
            return c == null ? "" : c.requestId();
        }
        @JavascriptInterface public void end() {
            mainHandler.post(() -> { if (LookAtMeController.instance != null) LookAtMeController.instance.finish("disabled"); });
        }
        @JavascriptInterface public void settings() { mainHandler.post(() -> showLookAtMeSettings()); }
        @JavascriptInterface public void ready(String request, String conv, String daily) {
            mainHandler.post(() -> {
                if (source != webView || !lookAtMeVisible()) return;
                final int generation = messageAckGeneration;
                source.postVisualStateCallback(generation, new WebView.VisualStateCallback() {
                    @Override public void onComplete(long ignored) {
                        if (source != webView || generation != messageAckGeneration || !lookAtMeVisible()) return;
                        LookAtMeController c = LookAtMeController.instance;
                        if (c != null) c.ready(request, conv, daily);
                    }
                });
            });
        }
    }

    private void showLookAtMeSettings() {
        new AlertDialog.Builder(this, R.style.Theme_ObsidianVow_Dialog)
            .setTitle("腻歪时间 · 手机设置")
            .setItems(new String[]{"悬浮窗", "使用情况访问", "通知与来电中断", "电池优化", "vivo 后台设置说明"}, (dialog, which) -> {
                Intent intent;
                switch (which) {
                    case 0: intent = new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:" + getPackageName())); break;
                    case 1: intent = new Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS); break;
                    case 2:
                        java.util.ArrayList<String> permissions = new java.util.ArrayList<>();
                        permissions.add(Manifest.permission.READ_PHONE_STATE);
                        if (android.os.Build.VERSION.SDK_INT >= 33) permissions.add(Manifest.permission.POST_NOTIFICATIONS);
                        requestPermissions(permissions.toArray(new String[0]), 4202); return;
                    case 3: requestBatteryOptimization(); return;
                    default:
                        new AlertDialog.Builder(this, R.style.Theme_ObsidianVow_Dialog)
                            .setTitle("后台邀请")
                            .setMessage("请在 vivo 应用管理中允许“后台弹出界面”，并在电池 → 后台耗电管理中打开“允许后台耗电”。电池优化豁免不能代替这两个开关。设置后请验证切到其他应用时能否收到邀请。")
                            .setPositiveButton("打开应用设置", (d, w) -> startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:" + getPackageName()))))
                            .setNegativeButton("知道了", null).show(); return;
                }
                try { startActivity(intent); } catch (Exception ignored) {}
            }).setNegativeButton("关闭", null).show();
    }

    private final class NotificationReadBridge {
        private final WebView source;

        NotificationReadBridge(WebView source) { this.source = source; }

        @JavascriptInterface
        public String pendingConversationId() {
            return ObsidianPushService.latestUnreadConversationId(getApplicationContext());
        }

        @JavascriptInterface
        public void syncStarted() {
            mainHandler.post(() -> { if (source == webView) setChatReady(false); });
        }

        @JavascriptInterface
        public void synced(double createdAtSeconds) {
            acknowledge(createdAtSeconds);
        }

        @JavascriptInterface
        public void seenThrough(double createdAtSeconds) {
            acknowledge(createdAtSeconds);
        }

        private void acknowledge(double createdAtSeconds) {
            if (!Double.isFinite(createdAtSeconds) || createdAtSeconds < 0) return;
            mainHandler.post(() -> {
                if (source != webView || !pageLoaded || !canAcknowledgeMessagesSeen()) return;
                final int generation = messageAckGeneration;
                source.postVisualStateCallback(generation, new WebView.VisualStateCallback() {
                    @Override public void onComplete(long requestId) {
                        if (source != webView || generation != messageAckGeneration
                                || !canAcknowledgeMessagesSeen()) return;
                        setChatReady(true);
                        recoveryAttempts = 0;
                        recoveryPanel.setVisibility(View.GONE);
                        if (loadTimeout != null) mainHandler.removeCallbacks(loadTimeout);
                        if (createdAtSeconds > 0) {
                            ObsidianPushService.clearMessageNotificationsThrough(
                                    getApplicationContext(), Math.round(createdAtSeconds * 1000.0));
                        }
                    }
                });
            });
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
                chatVisible && hasWindowFocus(),
                screenInteractive,
                deviceLocked
        );
        android.util.Log.d("ObsidianNotifications", "ack visible=" + visible
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
                android.util.Log.w("ObsidianWebView", "电池优化引导失败: " + e.getMessage());
            }
        }
    }

    private static final int REQ_LOCATION = 3001;
    private static final int REQ_BACKGROUND_LOCATION = 3002;

    @Override
    protected void onDestroy() {
        releaseWebView();
        mainHandler.removeCallbacksAndMessages(null);
        super.onDestroy();
    }
}
