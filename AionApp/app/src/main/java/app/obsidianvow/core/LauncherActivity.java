package app.obsidianvow.core;

import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.text.TextUtils;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.TextView;
import android.widget.Toast;

import androidx.activity.result.ActivityResultLauncher;
import androidx.appcompat.app.AppCompatActivity;
import androidx.health.connect.client.PermissionController;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/**
 * 启动 / 配置页 — 输入服务器 URL 与可选 Bearer Token，记住后自动连接
 */
public class LauncherActivity extends AppCompatActivity {

    static final String PREFS      = "aion_prefs";
    static final String KEY_URL    = "saved_url";
    static final String KEY_TOKEN  = "auth_token";
    static final String KEY_AUTO   = "auto_connect";

    private static final int COLOR_BG = 0xFF1A1714;

    private static final Set<String> HC_DATA_PERMS = new HashSet<>(
            HealthConnectReporter.dataPermissions());

    private ActivityResultLauncher<Set<String>> hcPermLauncher;
    private TextView tvHealthStatus;
    private String pendingAutoUrl;
    private String pendingAutoToken;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        applySystemBars();

        // 注册 ActivityResult 必须在 Activity STARTED 之前，放最前面
        hcPermLauncher = registerForActivityResult(
                PermissionController.createRequestPermissionResultContract(),
                this::updateHealthStatus
        );

        SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        String savedUrl   = prefs.getString(KEY_URL, "");
        String savedToken = prefs.getString(KEY_TOKEN, "");
        boolean auto      = prefs.getBoolean(KEY_AUTO, false);

        HealthAuthorizationStatus initialHealth =
                HealthConnectReporter.authorizationStatus(this);
        boolean mustPauseForBackgroundGrant = initialHealth.getSdkAvailable()
                && initialHealth.getBackgroundSupported()
                && !initialHealth.getBackgroundGranted();

        // 升级后若设备支持后台读取但尚未授权，先留下一个真实可达的授权入口。
        if (auto && !TextUtils.isEmpty(savedUrl)) {
            if (!mustPauseForBackgroundGrant) {
                launchWebView(savedUrl, savedToken);
                return;
            }
            pendingAutoUrl = savedUrl;
            pendingAutoToken = savedToken;
        }

        setContentView(R.layout.activity_launcher);

        EditText etUrl     = findViewById(R.id.etUrl);
        EditText etToken   = findViewById(R.id.etToken);
        EditText etDevName = findViewById(R.id.etDeviceName);
        Button   btnConn   = findViewById(R.id.btnConnect);
        Button   btnHealth = findViewById(R.id.btnHealthAuth);
        CheckBox cbRemember= findViewById(R.id.cbRemember);
        tvHealthStatus     = findViewById(R.id.tvHealthStatus);
        renderHealthStatus(initialHealth);

        etUrl.setText(savedUrl);
        etToken.setText(savedToken);
        // 预填已保存的设备名（未设置时显示当前生效的默认名，即系统型号）
        etDevName.setText(DeviceIdentity.name(this));
        cbRemember.setChecked(auto);

        btnHealth.setOnClickListener(v -> {
            try {
                hcPermLauncher.launch(HealthConnectReporter.requestedPermissions(this));
            } catch (Exception e) {
                Toast.makeText(this, "无法打开「健康连接」，请先在应用商店安装它", Toast.LENGTH_LONG).show();
            }
        });

        btnConn.setOnClickListener(v -> {
            String url   = etUrl.getText().toString().trim();
            String token = etToken.getText().toString().trim();
            if (TextUtils.isEmpty(url)) {
                Toast.makeText(this, "请填写服务器地址", Toast.LENGTH_SHORT).show();
                return;
            }
            // 容错：自动补全协议和 /chat 路径
            if (!url.startsWith("http://") && !url.startsWith("https://")) {
                url = "https://" + url;
            }
            if (!url.contains("/chat") && !url.contains("/ws")) {
                url = url.replaceAll("/+$", "") + "/chat";
            }

            SharedPreferences.Editor editor = prefs.edit();
            editor.putString(KEY_URL, url);
            editor.putString(KEY_TOKEN, token);
            editor.putBoolean(KEY_AUTO, cbRemember.isChecked());
            editor.apply();

            // 持久化用户设定的设备名（留空则继续用系统型号默认）
            DeviceIdentity.setName(this, etDevName.getText().toString());

            launchWebView(url, token);
        });
    }

    private void applySystemBars() {
        getWindow().setStatusBarColor(COLOR_BG);
        getWindow().setNavigationBarColor(COLOR_BG);
        getWindow().getDecorView().setSystemUiVisibility(0);
    }

    private void launchWebView(String url, String token) {
        startPushService(url, token);

        Intent intent = new Intent(this, WebViewActivity.class);
        intent.putExtra("url", url);
        intent.putExtra("token", token);
        startActivity(intent);
        finish();
    }

    private void updateHealthStatus(Set<String> ignoredResult) {
        HealthAuthorizationStatus status = HealthConnectReporter.authorizationStatus(this);
        renderHealthStatus(status);
        if (pendingAutoUrl != null
                && (!status.getBackgroundSupported() || status.getBackgroundGranted())) {
            String url = pendingAutoUrl;
            String token = pendingAutoToken;
            pendingAutoUrl = null;
            pendingAutoToken = null;
            launchWebView(url, token);
        }
    }

    private void renderHealthStatus(HealthAuthorizationStatus status) {
        if (tvHealthStatus == null) return;
        if (!status.getSdkAvailable()) {
            tvHealthStatus.setText("数据权限：Health Connect 不可用\n后台读取：不可用（保留前台降级）");
            tvHealthStatus.setTextColor(0xFF888888);
            return;
        }

        Set<String> granted = status.getGrantedDataPermissions();
        List<String> names = new ArrayList<>();
        if (granted.contains("android.permission.health.READ_HEART_RATE")) names.add("心率");
        if (granted.contains("android.permission.health.READ_OXYGEN_SATURATION")) names.add("血氧");
        if (granted.contains("android.permission.health.READ_SLEEP")) names.add("睡眠");
        if (granted.contains("android.permission.health.READ_STEPS")) names.add("步数");

        String dataLine = "数据权限：" + granted.size() + "/" + HC_DATA_PERMS.size();
        if (!names.isEmpty()) dataLine += "（" + String.join("/", names) + "）";
        String backgroundLine;
        if (!status.getBackgroundSupported()) {
            backgroundLine = "后台读取：设备不支持（保留前台降级）";
        } else if (status.getBackgroundGranted()) {
            backgroundLine = "后台读取：✓ 已授权";
        } else {
            backgroundLine = "后台读取：未授权 · 点击按钮补授权";
        }
        tvHealthStatus.setText(dataLine + "\n" + backgroundLine);

        if (granted.size() == HC_DATA_PERMS.size()
                && (!status.getBackgroundSupported() || status.getBackgroundGranted())) {
            tvHealthStatus.setTextColor(0xFF8FC8A3);
        } else if (!granted.isEmpty() || status.getBackgroundGranted()) {
            tvHealthStatus.setTextColor(0xFFE8B76A);
        } else {
            tvHealthStatus.setTextColor(0xFF888888);
        }
    }

    private void startPushService(String url, String token) {
        Intent serviceIntent = new Intent(this, ObsidianPushService.class);
        serviceIntent.putExtra("url", url);
        serviceIntent.putExtra("token", token);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(serviceIntent);
        } else {
            startService(serviceIntent);
        }
    }
}
