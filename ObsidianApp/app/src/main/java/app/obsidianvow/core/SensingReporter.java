package app.obsidianvow.core;

import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.net.wifi.WifiInfo;
import android.net.wifi.WifiManager;
import android.os.BatteryManager;
import android.os.PowerManager;
import android.util.Log;

import org.json.JSONObject;

import java.util.concurrent.atomic.AtomicReference;

import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

/**
 * 手机体感采集：
 *   - 2s 加速度采样 → 推断静止/走动/跑动
 *   - 环境光 lux（一次性读取）
 *   - 气压 hPa（一次性读取）
 *   - WiFi SSID（如果已连）
 *   - 电量百分比 + 是否充电
 *   - 屏幕是否亮
 *
 * 结果 POST 到 /api/sensing/tick。
 * 由 ObsidianPushService 的周期线程驱动（每 5 分钟调一次 reportOnce）。
 */
public class SensingReporter {
    private static final String TAG = "ObsidianSensing";
    private static final MediaType JSON = MediaType.get("application/json; charset=utf-8");

    private final Context ctx;
    private final OkHttpClient http;
    private final String httpBase;

    public SensingReporter(Context ctx, OkHttpClient http, String httpBase) {
        this.ctx = ctx.getApplicationContext();
        this.http = http;
        this.httpBase = httpBase;
    }

    /** 阻塞式采集 + 上报，需在后台线程调用。 */
    public void reportOnce() {
        try {
            JSONObject body = new JSONObject();
            body.put("timestamp", System.currentTimeMillis() / 1000.0);

            String motion = sampleMotion(2000);  // 2 秒采样
            if (motion != null) body.put("motion", motion);

            // 光线采样 1 秒取中位数，避开瞬时遮挡（手挡、翻转等）造成的伪低值
            Float lux = readSensorMedian(Sensor.TYPE_LIGHT, 1000);
            if (lux != null) body.put("light_lux", lux.doubleValue());

            Float pressure = readSensorOnce(Sensor.TYPE_PRESSURE, 1500);
            if (pressure != null) body.put("pressure_hpa", pressure.doubleValue());

            String ssid = readWifiSsid();
            if (ssid != null) body.put("wifi_ssid", ssid);

            int[] battery = readBattery();
            if (battery != null) {
                body.put("battery_pct", battery[0]);
                body.put("charging", battery[1] == 1);
            }

            PowerManager pm = (PowerManager) ctx.getSystemService(Context.POWER_SERVICE);
            if (pm != null) body.put("screen_on", pm.isInteractive());

            post("/api/sensing/tick", body);
        } catch (Exception e) {
            Log.w(TAG, "reportOnce failed: " + e.getMessage());
        }
    }

    /** 触发一次「解锁」事件上报（由屏幕广播触发）。 */
    public void reportUnlock() {
        try {
            JSONObject body = new JSONObject();
            body.put("timestamp", System.currentTimeMillis() / 1000.0);
            post("/api/unlock/tick", body);
        } catch (Exception e) {
            Log.w(TAG, "reportUnlock failed: " + e.getMessage());
        }
    }

    // ── 加速度采样 → 运动状态 ─────────────────────
    private String sampleMotion(long durationMs) {
        SensorManager sm = (SensorManager) ctx.getSystemService(Context.SENSOR_SERVICE);
        if (sm == null) return null;
        Sensor acc = sm.getDefaultSensor(Sensor.TYPE_ACCELEROMETER);
        if (acc == null) return null;

        final double[] stats = new double[]{0, 0, 0};  // sumSq, count, maxMag
        final Object lock = new Object();

        SensorEventListener listener = new SensorEventListener() {
            @Override public void onSensorChanged(SensorEvent event) {
                double x = event.values[0], y = event.values[1], z = event.values[2];
                // 减去重力 9.8 的粗略估计：算与 1g 的偏差
                double mag = Math.sqrt(x * x + y * y + z * z) - SensorManager.GRAVITY_EARTH;
                double absMag = Math.abs(mag);
                synchronized (lock) {
                    stats[0] += absMag * absMag;
                    stats[1] += 1;
                    if (absMag > stats[2]) stats[2] = absMag;
                }
            }
            @Override public void onAccuracyChanged(Sensor s, int a) {}
        };

        sm.registerListener(listener, acc, SensorManager.SENSOR_DELAY_UI);
        try { Thread.sleep(durationMs); } catch (InterruptedException e) { /* ok */ }
        sm.unregisterListener(listener);

        if (stats[1] < 5) return "unknown";
        double rmsDev = Math.sqrt(stats[0] / stats[1]);
        double peak = stats[2];

        // 阈值凭经验：桌面/口袋静置 rms<0.3；走路 0.5~2；跑步 >3
        if (peak > 6.0 && rmsDev > 2.5) return "running";
        if (rmsDev > 0.8) return "walking";
        if (rmsDev > 0.3) return "tilting";
        return "still";
    }

    // ── 持续采样取中位数（抗瞬时遮挡） ─────────────
    private Float readSensorMedian(int type, long durationMs) {
        SensorManager sm = (SensorManager) ctx.getSystemService(Context.SENSOR_SERVICE);
        if (sm == null) return null;
        Sensor s = sm.getDefaultSensor(type);
        if (s == null) return null;

        final java.util.List<Float> samples = new java.util.ArrayList<>();
        SensorEventListener listener = new SensorEventListener() {
            @Override public void onSensorChanged(SensorEvent event) {
                synchronized (samples) { samples.add(event.values[0]); }
            }
            @Override public void onAccuracyChanged(Sensor s, int a) {}
        };
        sm.registerListener(listener, s, SensorManager.SENSOR_DELAY_NORMAL);
        try { Thread.sleep(durationMs); } catch (InterruptedException e) { /* ok */ }
        sm.unregisterListener(listener);

        synchronized (samples) {
            if (samples.isEmpty()) return null;
            java.util.Collections.sort(samples);
            return samples.get(samples.size() / 2);
        }
    }

    // ── 一次性读某传感器 ─────────────────────────
    private Float readSensorOnce(int type, long timeoutMs) {
        SensorManager sm = (SensorManager) ctx.getSystemService(Context.SENSOR_SERVICE);
        if (sm == null) return null;
        Sensor s = sm.getDefaultSensor(type);
        if (s == null) return null;

        final AtomicReference<Float> ref = new AtomicReference<>();
        final Object done = new Object();
        SensorEventListener listener = new SensorEventListener() {
            @Override public void onSensorChanged(SensorEvent event) {
                if (ref.get() == null) {
                    ref.set(event.values[0]);
                    synchronized (done) { done.notifyAll(); }
                }
            }
            @Override public void onAccuracyChanged(Sensor s, int a) {}
        };
        sm.registerListener(listener, s, SensorManager.SENSOR_DELAY_NORMAL);
        synchronized (done) {
            if (ref.get() == null) {
                try { done.wait(timeoutMs); } catch (InterruptedException e) { /* ok */ }
            }
        }
        sm.unregisterListener(listener);
        return ref.get();
    }

    // ── WiFi SSID ────────────────────────────────
    private String readWifiSsid() {
        try {
            WifiManager wm = (WifiManager) ctx.getApplicationContext()
                    .getSystemService(Context.WIFI_SERVICE);
            if (wm == null) return null;
            WifiInfo info = wm.getConnectionInfo();
            if (info == null) return null;
            String ssid = info.getSSID();
            if (ssid == null) return null;
            // Android 返回带引号的 "MySSID"，去掉
            if (ssid.startsWith("\"") && ssid.endsWith("\"")) {
                ssid = ssid.substring(1, ssid.length() - 1);
            }
            // 未授权定位时会返回 <unknown ssid>
            if (ssid.isEmpty() || ssid.equalsIgnoreCase("<unknown ssid>")) return null;
            return ssid;
        } catch (Exception e) {
            return null;
        }
    }

    // ── 电量 + 充电状态 ──────────────────────────
    private int[] readBattery() {
        try {
            IntentFilter filter = new IntentFilter(Intent.ACTION_BATTERY_CHANGED);
            Intent b = ctx.registerReceiver(null, filter);
            if (b == null) return null;
            int level = b.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
            int scale = b.getIntExtra(BatteryManager.EXTRA_SCALE, -1);
            if (level < 0 || scale <= 0) return null;
            int pct = (int) (level * 100f / scale);
            int status = b.getIntExtra(BatteryManager.EXTRA_STATUS, -1);
            int charging = (status == BatteryManager.BATTERY_STATUS_CHARGING
                    || status == BatteryManager.BATTERY_STATUS_FULL) ? 1 : 0;
            return new int[]{pct, charging};
        } catch (Exception e) {
            return null;
        }
    }

    // ── POST ─────────────────────────────────────
    void post(String path, JSONObject body) {
        if (httpBase == null) return;
        try {
            Request req = new Request.Builder()
                    .url(httpBase + path)
                    .post(RequestBody.create(body.toString(), JSON))
                    .build();
            try (Response resp = http.newCall(req).execute()) {
                Log.i(TAG, "POST " + path + " → " + resp.code());
            }
        } catch (Exception e) {
            Log.w(TAG, "POST " + path + " failed: " + e.getMessage());
        }
    }
}
