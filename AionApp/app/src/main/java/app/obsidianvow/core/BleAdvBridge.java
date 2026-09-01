package app.obsidianvow.core;

import android.annotation.SuppressLint;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothManager;
import android.bluetooth.le.BluetoothLeAdvertiser;
import android.content.Context;
import android.content.Intent;
import android.os.Handler;
import android.os.Looper;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import org.json.JSONObject;

/**
 * 薄代理：JS 接口不变，BLE 广播逻辑全部委托给 DomForegroundService。
 */
@SuppressLint("MissingPermission")
public class BleAdvBridge {

    private final WebView webView;
    private final Context context;
    private final Handler uiHandler = new Handler(Looper.getMainLooper());
    private final boolean supported;
    private volatile String device = "sk30";

    public BleAdvBridge(WebView webView, Context context) {
        this.webView = webView;
        this.context = context;
        BluetoothManager bm = (BluetoothManager) context.getSystemService(Context.BLUETOOTH_SERVICE);
        BluetoothLeAdvertiser adv = null;
        if (bm != null) {
            BluetoothAdapter adapter = bm.getAdapter();
            if (adapter != null) adv = adapter.getBluetoothLeAdvertiser();
        }
        this.supported = adv != null;
    }

    @JavascriptInterface
    public boolean isSupported() {
        return supported;
    }

    @JavascriptInterface
    public void configure(String json) {
        try {
            JSONObject j = new JSONObject(json == null ? "{}" : json);
            if (j.has("device")) device = j.getString("device");
            callJs("toyNativeBle.onLog('adv cfg " + escapeJs(device) + "')");
        } catch (Exception e) {
            callJs("toyNativeBle.onError('adv配置失败: " + escapeJs(e.getMessage()) + "')");
        }
    }

    @JavascriptInterface
    public void play(String json) {
        if (!supported) {
            callJs("toyNativeBle.onError('设备不支持BLE广播')");
            return;
        }
        try {
            Intent intent = new Intent(context, DomForegroundService.class);
            intent.setAction(DomForegroundService.ACTION_PLAY);
            intent.putExtra(DomForegroundService.EXTRA_PAYLOAD, json);
            intent.putExtra(DomForegroundService.EXTRA_DEVICE, device);
            context.startForegroundService(intent);
        } catch (Exception e) {
            callJs("toyNativeBle.onError('服务启动失败')");
        }
    }

    @JavascriptInterface
    public void stop() {
        requestStop(DomForegroundService.ACTION_STOP);
    }

    @JavascriptInterface
    public void emergencyStop() {
        requestStop(DomForegroundService.ACTION_EMERGENCY_STOP, false);
    }

    @JavascriptInterface
    public void disconnect() {
        stop();
        callJs("toyNativeBle.onDisconnected()");
    }

    private void requestStop(String action) {
        requestStop(action, true);
    }

    private void requestStop(String action, boolean triggerPanicJs) {
        try {
            Intent intent = new Intent(context, DomForegroundService.class);
            intent.setAction(action);
            if (DomForegroundService.ACTION_EMERGENCY_STOP.equals(action)) {
                intent.putExtra(DomForegroundService.EXTRA_TRIGGER_PANIC_JS, triggerPanicJs);
            }
            context.startForegroundService(intent);
        } catch (Exception e) {
            callJs("toyNativeBle.onDisconnected()");
        }
    }

    private void callJs(String js) {
        uiHandler.post(() -> webView.evaluateJavascript(
                "typeof toyNativeBle!=='undefined'&&" + js, null));
    }

    private String escapeJs(String s) {
        return s == null ? "" : s.replace("\\", "\\\\").replace("'", "\\'");
    }
}
