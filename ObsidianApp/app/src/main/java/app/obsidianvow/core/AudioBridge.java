package app.obsidianvow.core;

import android.util.Base64;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

/** Existing WebView callbacks backed by the same recorder as the native overlay. */
public class AudioBridge {
    private final WebView webView;
    private final PcmRecorder recorder = new PcmRecorder();
    private volatile long generation;
    public AudioBridge(WebView webView) { this.webView = webView; }

    @JavascriptInterface public synchronized boolean start() {
        if(recorder.recording()) return false;
        final long current = ++generation;
        return recorder.start(0, new PcmRecorder.Listener() {
            public void chunk(byte[] pcm) {
                String b64 = Base64.encodeToString(pcm, Base64.NO_WRAP);
                webView.post(() -> {
                    if(current != generation || !recorder.recording()) return;
                    webView.evaluateJavascript("(function(c){"
                        + "if(typeof remoteVoice!=='undefined'&&remoteVoice._onNativeChunk){remoteVoice._onNativeChunk(c);}"
                        + "if(typeof _voiceNativeOnChunk==='function'){_voiceNativeOnChunk(c);}"
                        + "})('" + b64 + "')", null);
                });
            }
            public void stopped(String error) { }
        });
    }
    @JavascriptInterface public synchronized void stop() { generation++; recorder.stop(); }
    @JavascriptInterface public boolean isRecording() { return recorder.recording(); }
}
