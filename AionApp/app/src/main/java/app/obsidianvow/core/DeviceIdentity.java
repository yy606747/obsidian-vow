package app.obsidianvow.core;

import android.content.Context;
import android.content.SharedPreferences;
import android.content.res.Configuration;
import android.os.Build;

import java.security.SecureRandom;
import java.util.Locale;

/**
 * 稳定的每安装设备身份，用于后端多设备路由与数据归属。
 *
 * device_id 首次启动生成一次（android_<随机十六进制>），存入与其它设置共享的
 * "aion_prefs"，之后不变。device_name / device_type 允许用户在设置页覆盖，
 * 未设置时分别回退到系统型号、按屏幕尺寸推断的 phone/tablet。
 */
final class DeviceIdentity {

    private static final String PREFS    = "aion_prefs";
    private static final String KEY_ID   = "device_id";
    private static final String KEY_NAME = "device_name";
    private static final String KEY_TYPE = "device_type";

    static final String PLATFORM = "android";

    private DeviceIdentity() {}

    /** 稳定机器标识，首次访问时生成并持久化。 */
    static synchronized String id(Context ctx) {
        SharedPreferences p = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        String id = p.getString(KEY_ID, null);
        if (id == null || id.isEmpty()) {
            id = "android_" + randomHex(6);
            p.edit().putString(KEY_ID, id).apply();
        }
        return id;
    }

    /** 用户可读名称，缺省用系统型号。 */
    static String name(Context ctx) {
        String name = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY_NAME, null);
        if (name != null && !name.trim().isEmpty()) return name.trim();
        String model = Build.MODEL;
        return (model != null && !model.trim().isEmpty()) ? model.trim() : "Android 设备";
    }

    /** phone / tablet / unknown，未手动设置时按屏幕尺寸推断。 */
    static String type(Context ctx) {
        String type = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY_TYPE, null);
        if (type != null && !type.trim().isEmpty()) return type.trim();
        return detectType(ctx);
    }

    /** 派生 DeviceService kind：android_phone / android_tablet / android_device。 */
    static String kind(Context ctx) {
        String t = type(ctx);
        if ("phone".equals(t)) return "android_phone";
        if ("tablet".equals(t)) return "android_tablet";
        return "android_device";
    }

    static void setName(Context ctx, String name) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                .putString(KEY_NAME, name == null ? "" : name.trim()).apply();
    }

    static void setType(Context ctx, String type) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                .putString(KEY_TYPE, type == null ? "" : type.trim()).apply();
    }

    /** 最小宽度 >= 600dp 视为平板，否则手机；用户可在设置里改写。 */
    static String detectType(Context ctx) {
        try {
            Configuration cfg = ctx.getResources().getConfiguration();
            return cfg.smallestScreenWidthDp >= 600 ? "tablet" : "phone";
        } catch (Exception e) {
            return "unknown";
        }
    }

    private static String randomHex(int numBytes) {
        byte[] buf = new byte[numBytes];
        new SecureRandom().nextBytes(buf);
        StringBuilder sb = new StringBuilder(numBytes * 2);
        for (byte b : buf) sb.append(String.format(Locale.US, "%02x", b));
        return sb.toString();
    }
}
