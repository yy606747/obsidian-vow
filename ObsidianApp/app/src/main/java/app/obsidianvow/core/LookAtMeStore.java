package app.obsidianvow.core;

import android.content.Context;
import android.content.SharedPreferences;
import org.json.JSONArray;
import org.json.JSONObject;
import java.util.UUID;

/** State and upload outbox share one synchronous preference commit. */
final class LookAtMeStore {
    private final SharedPreferences prefs;
    volatile LookAtMeSession session;
    private JSONArray outbox = new JSONArray();
    boolean enabled;

    LookAtMeStore(Context context) {
        prefs = context.getSharedPreferences("look_at_me", Context.MODE_PRIVATE);
        try {
            JSONObject root = new JSONObject(prefs.getString("state", "{}"));
            enabled = root.optBoolean("enabled", false);
            outbox = root.optJSONArray("outbox");
            if (outbox == null) outbox = new JSONArray();
            JSONObject json = root.optJSONObject("session");
            if (json != null) {
                session = new LookAtMeSession();
                for (java.lang.reflect.Field f : LookAtMeSession.class.getDeclaredFields()) {
                    if (f.getType() == String.class) f.set(session, json.optString(f.getName(), ""));
                    else if (f.getType() == long.class) f.setLong(session, json.optLong(f.getName()));
                    else if (f.getType() == boolean.class) f.setBoolean(session, json.optBoolean(f.getName()));
                }
            }
        } catch (Exception e) { android.util.Log.e("LookAtMe", "State load failed", e); }
    }

    synchronized void persist() {
        try {
            JSONObject root = new JSONObject().put("enabled", enabled).put("outbox", outbox);
            if (session != null) {
                JSONObject json = new JSONObject();
                for (java.lang.reflect.Field f : LookAtMeSession.class.getDeclaredFields()) json.put(f.getName(), f.get(session));
                root.put("session", json);
            }
            if (!prefs.edit().putString("state", root.toString()).commit()) throw new IllegalStateException("local_commit_failed");
        } catch (ReflectiveOperationException | org.json.JSONException e) { throw new IllegalStateException(e); }
    }

    synchronized void event(String type, long elapsed, JSONObject payload) {
        try {
            JSONObject event = new JSONObject().put("event_id", UUID.randomUUID().toString())
                    .put("sequence", ++session.sequence).put("type", type)
                    .put("occurred_at", session.wall(elapsed) / 1000.0).put("payload", payload);
            outbox.put(new JSONObject().put("request_id", session.id).put("event", event));
            persist();
        } catch (org.json.JSONException e) { throw new IllegalStateException(e); }
    }

    synchronized JSONObject firstBatch() {
        if (outbox.length() == 0) return null;
        try {
            String id = outbox.getJSONObject(0).getString("request_id");
            JSONArray events = new JSONArray();
            for (int i = 0; i < outbox.length() && events.length() < 100; i++) {
                JSONObject entry = outbox.getJSONObject(i);
                if (id.equals(entry.getString("request_id"))) events.put(entry.getJSONObject("event"));
            }
            // The returned snapshot must not share mutable state with the worker.
            return new JSONObject(new JSONObject().put("request_id",id).put("events",events).toString());
        } catch (org.json.JSONException e) { throw new IllegalStateException(e); }
    }

    synchronized void acknowledge(JSONObject batch) {
        java.util.Set<String> ids = new java.util.HashSet<>();
        JSONArray sent = batch.optJSONArray("events");
        for (int i=0;i<sent.length();i++) ids.add(sent.optJSONObject(i).optString("event_id"));
        JSONArray remaining = new JSONArray();
        for (int i=0;i<outbox.length();i++) {
            JSONObject entry = outbox.optJSONObject(i);
            if (!ids.contains(entry.optJSONObject("event").optString("event_id"))) remaining.put(entry);
        }
        outbox = remaining; persist();
    }
}
