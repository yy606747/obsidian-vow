package app.obsidianvow.core;

import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import java.util.Arrays;

/** The recorder has no WebView dependency. Chunks are delivered on its thread. */
final class PcmRecorder {
    interface Listener { void chunk(byte[] pcm); void stopped(String error); }
    private volatile AudioRecord recorder;
    private volatile boolean recording;

    synchronized boolean start(int maximumSeconds, Listener listener) {
        if (recording) return false;
        AudioRecord next;
        try {
            int size = Math.max(5120, AudioRecord.getMinBufferSize(16000,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT));
            next = new AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, 16000,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, size);
            if (next.getState() != AudioRecord.STATE_INITIALIZED) { next.release(); return false; }
            next.startRecording();
        } catch (RuntimeException error) { return false; }
        recorder = next; recording = true;
        new Thread(() -> {
            int total = 0;
            String error = "";
            byte[] buffer = new byte[1280];
            try {
                while (recording && recorder == next && (maximumSeconds <= 0 || total < maximumSeconds * 32000)) {
                    int read = next.read(buffer, 0, buffer.length);
                    if (read < 0) { error = "recording_failed"; break; }
                    if (read > 0) { total += read; listener.chunk(Arrays.copyOf(buffer, read)); }
                }
            } catch (RuntimeException ignored) { if (recording) error = "recording_failed"; }
            finally {
                synchronized (PcmRecorder.this) {
                    if (recorder == next) { recording = false; recorder = null; }
                }
                try { next.stop(); } catch (RuntimeException ignored) { }
                next.release();
                listener.stopped(error);
            }
        }, "ObsidianPcm").start();
        return true;
    }

    synchronized void stop() {
        recording = false;
        if (recorder != null) try { recorder.stop(); } catch (RuntimeException ignored) { }
    }
    boolean recording() { return recording; }
}
