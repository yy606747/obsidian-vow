package app.obsidianvow.core;

import android.content.Context;
import android.content.SharedPreferences;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.File;
import java.util.UUID;

/** Private bounded outbox. Full session history stays in the temporary server DB. */
final class TogetherStore {
    private final SharedPreferences prefs;
    private final File media;
    TogetherStore(Context context) {
        prefs=context.getSharedPreferences("together",Context.MODE_PRIVATE);
        media=new File(context.getFilesDir(),"together");
        media.mkdirs();
    }
    synchronized JSONObject state() { return read("state"); }
    synchronized void state(JSONObject value) { require(prefs.edit().putString("state",value.toString()).commit()); }
    private void require(boolean saved){if(!saved)throw new IllegalStateException("手机存储未能保存，请检查剩余空间");}
    private JSONObject read(String key) {
        try { return new JSONObject(prefs.getString(key,"{}")); } catch(Exception error) { return new JSONObject(); }
    }
    synchronized JSONObject end() { return read("end"); }
    synchronized void markEnd(JSONObject receipt) {
        require(prefs.edit().putString("end",receipt.toString()).remove("state").remove("outbox").remove("draft").commit());
        clearMedia();
    }
    synchronized void ended(String sid) {
        if(end().optString("session_id").equals(sid)) prefs.edit().remove("end").commit();
    }
    synchronized void clear() { prefs.edit().remove("state").remove("outbox").remove("draft").commit(); clearMedia(); }
    synchronized JSONArray outbox() {
        try { return new JSONArray(prefs.getString("outbox","[]")); } catch(Exception error) { return new JSONArray(); }
    }
    synchronized JSONObject enqueue(String text,String capture) {
        JSONArray queue=outbox();
        if(queue.length()>=50) throw new IllegalStateException("待发送消息太多，请连接后再发");
        JSONObject item=TogetherApi.object("client_id",UUID.randomUUID().toString(),"text",text);
        if(capture!=null&&!capture.isEmpty()) try { item.put("capture_id",capture); } catch(Exception ignored) { }
        queue.put(item); require(prefs.edit().putString("outbox",queue.toString()).commit()); return item;
    }
    synchronized void acknowledge(String id) {
        JSONArray remaining=new JSONArray(), queue=outbox();
        for(int i=0;i<queue.length();i++) {
            JSONObject item=queue.optJSONObject(i);
            if(item!=null&&!id.equals(item.optString("client_id"))) remaining.put(item);
        }
        prefs.edit().putString("outbox",remaining.toString()).commit();
    }
    synchronized void failed(String id,String reason){
        JSONArray queue=outbox();
        for(int i=0;i<queue.length();i++){
            JSONObject row=queue.optJSONObject(i);if(row!=null&&id.equals(row.optString("client_id")))
                try{row.put("error",reason);}catch(Exception ignored){}
        }
        require(prefs.edit().putString("outbox",queue.toString()).commit());
    }
    synchronized JSONObject draft() { return read("draft"); }
    synchronized void draft(JSONObject value) { prefs.edit().putString("draft",value.toString()).commit(); }
    File frame() { return new File(media,"draft.jpg"); }
    File recording() { return new File(media,"draft.wav"); }
    void clearMedia() {
        File[] files=media.listFiles();
        if(files!=null) for(File file:files) file.delete();
    }
}
