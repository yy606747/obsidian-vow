package app.obsidianvow.core;

import android.annotation.SuppressLint;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;
import java.lang.ref.WeakReference;

@SuppressLint("MissingPermission")
public class MuseBleBridge {
    private volatile WeakReference<WebView> webView;
    private final Context context;
    private final Handler uiHandler = new Handler(Looper.getMainLooper());

    public MuseBleBridge(WebView webView, Context context) {
        this.webView = new WeakReference<>(webView);
        this.context = context.getApplicationContext();
    }
    void attach(WebView view) { webView=new WeakReference<>(view); }
    void detach(WebView view) { if(webView.get()==view) webView.clear(); }

    @JavascriptInterface
    public boolean isSupported() {
        try { return MuseBleCommands.advertiser(context) != null; }
        catch (Exception ignored) { return false; }
    }

    @JavascriptInterface
    public void playFrame(String json) {
        if (!isSupported()) {
            SceneDeviceBridge.museFailed();
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
        if (SceneDeviceBridge.active()) return; // Old Tide cleanup does not own a scene.
        Intent intent = new Intent(context, MuseForegroundService.class);
        intent.setAction(MuseForegroundService.ACTION_STOP);
        start(intent);
    }

    @JavascriptInterface
    public void emergencyStop() {
        SceneDeviceBridge.stoppedByMuse();
        Intent intent = new Intent(context, MuseForegroundService.class);
        intent.setAction(MuseForegroundService.ACTION_EMERGENCY_STOP);
        start(intent);
    }

    private void start(Intent intent) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) context.startForegroundService(intent);
            else context.startService(intent);
        } catch (Exception e) {
            SceneDeviceBridge.museFailed();
            callJs("tideNativeBle.onError('Muse服务启动失败')");
        }
    }

    private void callJs(String js) {
        uiHandler.post(() -> { WebView view=webView.get();
            if(view!=null) view.evaluateJavascript("typeof tideNativeBle!=='undefined'&&" + js, null);
        });
    }
}
