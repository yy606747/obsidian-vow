package app.obsidianvow.core;

import android.annotation.SuppressLint;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothManager;
import android.bluetooth.le.BluetoothLeAdvertiser;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

@SuppressLint("MissingPermission")
public class MuseBleBridge {
    private final WebView webView;
    private final Context context;
    private final Handler uiHandler = new Handler(Looper.getMainLooper());
    private final boolean supported;

    public MuseBleBridge(WebView webView, Context context) {
        this.webView = webView;
        this.context = context;
        BluetoothManager bm = (BluetoothManager) context.getSystemService(Context.BLUETOOTH_SERVICE);
        BluetoothLeAdvertiser adv = null;
        if (bm != null) {
            BluetoothAdapter adapter = bm.getAdapter();
            if (adapter != null) adv = adapter.getBluetoothLeAdvertiser();
        }
        supported = adv != null;
    }

    @JavascriptInterface
    public boolean isSupported() {
        return supported;
    }

    @JavascriptInterface
    public void playFrame(String json) {
        if (!supported) {
            callJs("tideNativeBle.onError('设备不支持BLE广播')");
            return;
        }
        Intent intent = new Intent(context, MuseForegroundService.class);
        intent.setAction(MuseForegroundService.ACTION_FRAME);
        intent.putExtra(MuseForegroundService.EXTRA_PAYLOAD, json);
        start(intent);
    }

    @JavascriptInterface
    public void stop() {
        Intent intent = new Intent(context, MuseForegroundService.class);
        intent.setAction(MuseForegroundService.ACTION_STOP);
        start(intent);
    }

    @JavascriptInterface
    public void emergencyStop() {
        Intent intent = new Intent(context, MuseForegroundService.class);
        intent.setAction(MuseForegroundService.ACTION_EMERGENCY_STOP);
        start(intent);
    }

    private void start(Intent intent) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) context.startForegroundService(intent);
            else context.startService(intent);
        } catch (Exception e) {
            callJs("tideNativeBle.onError('Muse服务启动失败')");
        }
    }

    private void callJs(String js) {
        uiHandler.post(() -> webView.evaluateJavascript(
                "typeof tideNativeBle!=='undefined'&&" + js, null));
    }
}
