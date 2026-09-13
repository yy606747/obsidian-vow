package app.obsidianvow.core;

import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.util.Base64;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

/**
 * 原生麦克风桥接 — 绕过 WebView getUserMedia 的 HTTPS 限制
 * 录音线程实时采集 PCM，通过 evaluateJavascript 推送给前端 JS
 *
 * 前端调用: window.ObsidianAudio.start() / stop() / isRecording()
 * 前端接收: remoteVoice._onNativeChunk(base64) / _voiceNativeOnChunk(base64)
 */
public class AudioBridge {

    private static final int SAMPLE_RATE = 16000;
    private static final int CHANNEL = AudioFormat.CHANNEL_IN_MONO;
    private static final int ENCODING = AudioFormat.ENCODING_PCM_16BIT;
    // 40ms 一帧: 16000 * 0.04 * 2bytes = 1280
    private static final int FRAME_BYTES = 1280;

    private final WebView webView;
    private volatile AudioRecord recorder;
    private volatile boolean recording = false;

    public AudioBridge(WebView webView) {
        this.webView = webView;
    }

    @JavascriptInterface
    public synchronized boolean start() {
        if (recording) return false;

        int minBuf = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL, ENCODING);
        int bufSize = Math.max(minBuf, FRAME_BYTES * 4);

        try {
            recorder = new AudioRecord(
                    MediaRecorder.AudioSource.VOICE_RECOGNITION,
                    SAMPLE_RATE, CHANNEL, ENCODING, bufSize);
        } catch (SecurityException e) {
            return false;
        }

        if (recorder.getState() != AudioRecord.STATE_INITIALIZED) {
            recorder.release();
            recorder = null;
            return false;
        }

        recorder.startRecording();
        recording = true;
        final AudioRecord activeRecorder = recorder;

        new Thread(() -> {
            byte[] buf = new byte[FRAME_BYTES];
            while (recording && recorder == activeRecorder) {
                int read;
                try { read = activeRecorder.read(buf, 0, buf.length); }
                catch (IllegalStateException e) { break; }
                if (read < 0) break;
                if (read > 0) {
                    String b64 = Base64.encodeToString(buf, 0, read, Base64.NO_WRAP);
                    // 推送到前端 JS（必须在 UI 线程执行）
                    webView.post(() -> {
                        if (recording && recorder == activeRecorder) {
                            webView.evaluateJavascript(
                                    "(function(c){"
                                            + "if(typeof remoteVoice!=='undefined'&&remoteVoice._onNativeChunk){remoteVoice._onNativeChunk(c);}"
                                            + "if(typeof _voiceNativeOnChunk==='function'){_voiceNativeOnChunk(c);}"
                                            + "})('" + b64 + "')",
                                    null);
                        }
                    });
                }
            }
            synchronized (AudioBridge.this) {
                if (recorder == activeRecorder) stop();
            }
        }, "ObsidianMicThread").start();

        return true;
    }

    @JavascriptInterface
    public synchronized void stop() {
        recording = false;
        if (recorder != null) {
            try { recorder.stop(); } catch (Exception ignored) {}
            try { recorder.release(); } catch (Exception ignored) {}
            recorder = null;
        }
    }

    @JavascriptInterface
    public boolean isRecording() {
        return recording;
    }
}
