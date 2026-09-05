package app.obsidianvow.core;

import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.content.Context;
import android.content.SharedPreferences;
import android.webkit.WebView;

import java.util.Map;
import java.util.Set;

/** Upgrade existing installations without changing their saved identity. */
final class BrandCompatibility {
    private BrandCompatibility() {}

    @SuppressWarnings("unchecked")
    static synchronized void migratePreferences(Context context) {
        SharedPreferences current = context.getSharedPreferences("obsidian_prefs", Context.MODE_PRIVATE);
        if (current.getBoolean("brand_migrated_v1", false)) return;
        SharedPreferences previous = context.getSharedPreferences("aion_prefs", Context.MODE_PRIVATE);
        SharedPreferences.Editor editor = current.edit();
        for (Map.Entry<String, ?> entry : previous.getAll().entrySet()) {
            String key = entry.getKey();
            if (current.contains(key)) continue;
            Object value = entry.getValue();
            if (value instanceof String) editor.putString(key, (String) value);
            else if (value instanceof Boolean) editor.putBoolean(key, (Boolean) value);
            else if (value instanceof Integer) editor.putInt(key, (Integer) value);
            else if (value instanceof Long) editor.putLong(key, (Long) value);
            else if (value instanceof Float) editor.putFloat(key, (Float) value);
            else if (value instanceof Set) editor.putStringSet(key, (Set<String>) value);
        }
        // Complete before activities or background services read their settings.
        editor.putBoolean("brand_migrated_v1", true).commit();
    }

    static void addJavascriptInterface(WebView view, Object bridge, String name) {
        view.addJavascriptInterface(bridge, name);
        // Cached pages can still be served by an earlier backend during upgrade.
        view.addJavascriptInterface(bridge, "Aion" + name.substring("Obsidian".length()));
    }

    static boolean isInternalScheme(String scheme) {
        return "obsidianvow".equals(scheme) || "aion".equals(scheme);
    }

    static String[] authCookieNames() {
        return new String[] {"obsidian_token", "aion_token"};
    }

    static void createNotificationChannel(NotificationManager manager, NotificationChannel channel) {
        if (channel.getId().startsWith("obsidian_")
                && manager.getNotificationChannel(channel.getId()) == null) {
            NotificationChannel previous = manager.getNotificationChannel(
                    "aion_" + channel.getId().substring("obsidian_".length()));
            if (previous != null) {
                channel.setImportance(previous.getImportance());
                channel.setSound(previous.getSound(), previous.getAudioAttributes());
                channel.setVibrationPattern(previous.getVibrationPattern());
                channel.enableVibration(previous.shouldVibrate());
                channel.enableLights(previous.shouldShowLights());
                channel.setLightColor(previous.getLightColor());
                channel.setShowBadge(previous.canShowBadge());
                channel.setLockscreenVisibility(previous.getLockscreenVisibility());
                channel.setBypassDnd(previous.canBypassDnd());
            }
        }
        manager.createNotificationChannel(channel);
    }
}
