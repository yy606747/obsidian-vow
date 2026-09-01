package app.obsidianvow.core;

import android.annotation.SuppressLint;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.PowerManager;
import android.util.Log;

@SuppressLint("MissingPermission")
public class MuseStopReceiver extends BroadcastReceiver {
    private static final String TAG = "MuseStopReceiver";
    private static final long WAKELOCK_TIMEOUT_MS = 10000L;

    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null || !MuseForegroundService.ACTION_TTL_STOP.equals(intent.getAction())) {
            return;
        }
        PendingResult pending = goAsync();
        Context appContext = context.getApplicationContext();
        new Thread(() -> {
            PowerManager.WakeLock wakeLock = null;
            try {
                PowerManager pm = (PowerManager) appContext.getSystemService(Context.POWER_SERVICE);
                if (pm != null) {
                    wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "ObsidianVow:MuseTtlStop");
                    wakeLock.acquire(WAKELOCK_TIMEOUT_MS);
                }
                MuseBleCommands.sendStopBurst(appContext, TAG);
            } catch (Exception e) {
                Log.e(TAG, "ttl stop failed", e);
            } finally {
                if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
                pending.finish();
            }
        }, "MuseTtlStop").start();
    }
}
