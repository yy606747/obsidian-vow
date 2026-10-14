package app.obsidianvow.core;

import android.content.Context;
import android.content.Intent;
import android.os.SystemClock;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;
import androidx.core.content.ContextCompat;
import org.json.*;
import okhttp3.*;
import java.io.IOException;
import java.util.*;
import java.util.concurrent.TimeUnit;

/** UI facade for the service-owned player. A WebView is never the playback owner. */
public final class SceneDeviceBridge {
    private static volatile SceneDeviceBridge current;
    private final Context context;
    private final BleBridge ble;
    private final BleBridge cx;
    private final MuseBleBridge muse;
    private final SceneSkTransport sk;
    private final SceneOutputPlayer player = new SceneOutputPlayer(SystemClock::elapsedRealtime);
    private final OkHttpClient http = new OkHttpClient.Builder().retryOnConnectionFailure(false)
            .followRedirects(false).followSslRedirects(false)
            .connectionPool(new ConnectionPool(2,3,TimeUnit.SECONDS))
            .connectTimeout(1200,TimeUnit.MILLISECONDS).readTimeout(1400,TimeUnit.MILLISECONDS)
            .callTimeout(1500,TimeUnit.MILLISECONDS).build();
    private final Deque<PendingAction> actions = new ArrayDeque<>();
    private boolean foreground;
    private String base = "", credential = "";
    private Run run, last;
    private PendingAction sendingAction;

    private static final class Run {
        final String id,owner,base,credential,token;
        final long generation;
        final Map<String,String> bindings;
        Call poll;
        long requested = -1000;
        final Map<String,Long> gattRevisions=new HashMap<>();
        boolean museZero;
        Run(String id,long generation,String owner,String base,String credential,String token,Map<String,String> bindings) {
            this.id=id;this.generation=generation;this.owner=owner;this.base=base;this.credential=credential;
            this.token=token;this.bindings=new LinkedHashMap<>(bindings);
        }
    }
    private static final class PendingAction {
        final Run run; final String action; final long until;
        long next;
        PendingAction(Run run,String action) { this.run=run;this.action=action;until=SystemClock.elapsedRealtime()+30000; }
    }
    private SceneDeviceBridge(Context context) {
        this.context=context.getApplicationContext();
        ble=new BleBridge(null,this.context); muse=new MuseBleBridge(null,this.context);
        cx=new BleBridge(null,this.context);sk=new SceneSkTransport(this.context);
        cx.configure("{\"namePrefix\":\"CX492B\",\"serviceUuid\":\"0000ffe0-0000-1000-8000-00805f9b34fb\","
                +"\"writeUuid\":\"0000ffe1-0000-1000-8000-00805f9b34fb\",\"notifyUuid\":\"0000ffe2-0000-1000-8000-00805f9b34fb\","
                +"\"framing\":\"raw\",\"writeType\":\"no_response\"}");
    }
    static synchronized SceneDeviceBridge obtain(Context context) {
        if(current==null) current=new SceneDeviceBridge(context);
        return current;
    }
    synchronized void attach(WebView view,String url,String token) {
        HttpUrl parsed=HttpUrl.parse(url);
        String origin=parsed==null ? "" : parsed.newBuilder().encodedPath("/").query(null).fragment(null).build().toString().replaceAll("/$","");
        if(!base.equals(origin)||!credential.equals(token)) { stopInternal("pause",false); last=null; }
        base=origin;credential=token;
        ble.attach(view);muse.attach(view);
    }
    void detach(WebView view) { ble.detach(view);muse.detach(view); }
    BleBridge ble() { return ble; }
    MuseBleBridge muse() { return muse; }
    synchronized void foreground(boolean value) { foreground=value; } // Only gates explicit new starts.

    @JavascriptInterface public synchronized String status() {
        try {
            JSONObject result=new JSONObject().put("version",3).put("background_player",true)
                    .put("sosexy_connected",ble.isSosexyConnected()).put("muse_supported",muse.isSupported())
                    .put("foreground",foreground).put("active",run!=null).put("pending_stop",!actions.isEmpty());
            JSONObject drivers=new JSONObject();
            for(String driver:Arrays.asList("muse_ble","sosexy_ble","cx492b_ble","sk30_ble","sk40_ble"))
                drivers.put(driver,ready(driver));
            result.put("drivers",drivers);
            JSONArray devices=new JSONArray();
            if(last!=null) {
                result.put("session_id",last.id).put("owner_id",last.owner).put("generation",last.generation);
                for(Map.Entry<String,String> device:last.bindings.entrySet())
                    devices.put(new JSONObject().put("id",device.getKey()).put("driver",device.getValue()));
            }
            return result.put("devices",devices).toString();
        } catch(Exception ignored) { return "{}"; }
    }
    @JavascriptInterface public synchronized boolean connectSosexy() {
        if(!foreground||run!=null) return false;
        if(!ble.isSosexyConnected()) {
            ble.connectSceneProfile("{\"namePrefix\":\"SOSEXY\",\"serviceUuid\":\"0000ee01-0000-1000-8000-00805f9b34fb\","
                    +"\"writeUuid\":\"0000ee03-0000-1000-8000-00805f9b34fb\",\"notifyUuid\":\"0000ee02-0000-1000-8000-00805f9b34fb\","
                    +"\"framing\":\"chunked\",\"writeType\":\"default\"}");
        }
        return true;
    }
    @JavascriptInterface public synchronized void disconnectSosexy() { stopInternal("pause",false);ble.disconnectAfterWrites(); }
    private boolean ready(String driver) {
        switch(driver) {
            case "muse_ble":return muse.isSupported();
            case "sosexy_ble":return ble.isSosexyConnected();
            case "cx492b_ble":return cx.isSceneConnected(driver);
            case "sk30_ble":case "sk40_ble":return sk.supported();
            default:return false;
        }
    }
    @JavascriptInterface public synchronized boolean connectDevice(String driver) {
        if(!foreground || run!=null) return false;
        if(driver.equals("sosexy_ble")) return connectSosexy();
        if(driver.equals("cx492b_ble")) {
            // Release a legacy CX connection only; SOSEXY stays connected.
            if(ble.isSceneConnected(driver)) ble.disconnectAfterWrites();
            cx.connect();return true;
        }
        return Arrays.asList("muse_ble","sk30_ble","sk40_ble").contains(driver) && ready(driver);
    }
    @JavascriptInterface public synchronized void disconnectDevice(String driver) {
        if(!Arrays.asList("muse_ble","sosexy_ble","cx492b_ble","sk30_ble","sk40_ble").contains(driver)) return;
        stopInternal("pause",false);
        if(driver.equals("sosexy_ble")) ble.disconnectAfterWrites();
        if(driver.equals("cx492b_ble")) cx.disconnectAfterWrites();
    }
    @JavascriptInterface public synchronized boolean begin(String json) {
        if(!foreground||base.isEmpty()||DomForegroundService.isRunning()) return false;
        try {
            JSONObject value=new JSONObject(json);
            String id=value.getString("id"), owner=value.getString("owner_id");long generation=value.getLong("generation");
            Map<String,String> bindings=new LinkedHashMap<>();JSONArray devices=value.getJSONArray("devices");
            for(int i=0;i<devices.length();i++) {
                JSONObject device=devices.getJSONObject(i);String driver=device.getString("driver");
                if(bindings.put(device.getString("id"),driver)!=null) return false;
                if(!ready(driver)) return false;
            }
            if(run!=null) return run.id.equals(id)&&run.generation==generation&&run.owner.equals(owner)&&run.bindings.equals(bindings);
            String token=player.start(id,generation,owner,bindings);if(token.isEmpty()) return false;
            run=last=new Run(id,generation,owner,base,credential,token,bindings);
            try { ContextCompat.startForegroundService(context,new Intent(context,SceneForegroundService.class)); }
            catch(Exception error) { stopInternal("pause",false); return false; }
            return true;
        } catch(Exception error) { return false; }
    }
    @JavascriptInterface public synchronized void stop(String id,long generation,String action) {
        if(run!=null&&run.id.equals(id)&&run.generation==generation)
            stopInternal(action.equals("end")?"end":"pause",false);
    }
    @JavascriptInterface public synchronized void panic() { stopInternal("end",false); }
    @JavascriptInterface public synchronized void observe(String id,long generation,String status) {
        if(run!=null&&run.id.equals(id)&&(generation>run.generation||generation==run.generation&&Arrays.asList("paused","ended").contains(status)))
            stopInternal(null,false);
    }
    synchronized void notificationAction(String id,long generation,String action) { stop(id,generation,action); }
    synchronized String notificationId() { return run==null?"":run.id; }
    synchronized long notificationGeneration() { return run==null?-1:run.generation; }
    synchronized boolean hasWork() { return run!=null||!actions.isEmpty()||sk.hasWork(); }

    /** Called only by the foreground service; no JS timer, Activity or chat socket is involved. */
    synchronized void pulse() {
        long now=SystemClock.elapsedRealtime();
        if(run!=null) {
            Run target=run;
            if(!player.alive()) stopInternal("pause",false);
            else try {
                for(Map<String,Object> frame:player.frames()) { if(run!=target) break; dispatch(target,frame); }
                if(run==target&&target.poll==null&&now-target.requested>=1000) poll(target,now);
            } catch(Exception error) { if(run==target) stopInternal("pause",false); }
        }
        sk.pulse(now);
        if(sendingAction==null&&!actions.isEmpty()) {
            PendingAction action=actions.peek();
            if(now>=action.until) actions.remove();
            else if(now>=action.next) sendAction(action);
        }
    }
    private void poll(Run target,long now) throws Exception {
        long nonce=player.request();if(nonce==0) { stopInternal("pause",false);return; }
        JSONArray connected=new JSONArray();
        for(Map.Entry<String,String> device:target.bindings.entrySet())
            if(ready(device.getValue())) connected.put(device.getKey());
        JSONObject body=new JSONObject().put("owner_id",target.owner).put("generation",target.generation)
                .put("connected",connected).put("adopted",new JSONObject(player.adopted()));
        target.requested=now;
        Call call=http.newCall(request(target,"output",body));target.poll=call;
        call.enqueue(new Callback() {
            @Override public void onFailure(Call call,IOException error) { receive(target,nonce,null); }
            @Override public void onResponse(Call call,Response response) {
                Map<String,Object> value=null;
                try(Response closed=response) {
                    if(response.isSuccessful()&&response.body()!=null) value=object(new JSONObject(response.body().string()));
                } catch(Exception ignored) {}
                receive(target,nonce,value);
            }
        });
    }
    private synchronized void receive(Run target,long nonce,Map<String,Object> value) {
        if(run!=target) return;
        target.poll=null;
        try {
            if(value!=null&&player.accept(nonce,value)) return;
            boolean serverStopped=value!=null&&(SceneProgram.integer(value.get("generation"),0,Long.MAX_VALUE)!=target.generation
                    ||Arrays.asList("paused","ended").contains(value.get("status")));
            stopInternal(serverStopped?null:"pause",false);
        } catch(Exception ignored) { stopInternal("pause",false); }
    }
    private void dispatch(Run target,Map<String,Object> frame) throws Exception {
        String driver=(String)frame.get("driver");long revision=((Number)frame.get("revision")).longValue();
        Map<String,Object> raw=SceneProgram.map(frame.get("values"));
        Map<String,Integer> values=new LinkedHashMap<>();
        for(String key:raw.keySet()) values.put(key,((Number)raw.get(key)).intValue());
        if(driver.equals("muse_ble")) {
            boolean zero=values.get("vib_pattern")==0&&values.get("thrust_pattern")==0;
            if(zero&&target.museZero) return;
            target.museZero=zero;
            // The transport's TTL is renewed even when only the other device changes.
            muse.playFrame(new JSONObject(values).put("ttl_ms",3000).put("scene_token",target.token).put("scene_revision",revision).toString());
        } else if(driver.equals("sk30_ble") || driver.equals("sk40_ble")) {
            sk.update(driver,target.token,revision,values);
        } else {
            BleBridge transport=driver.equals("cx492b_ble")?cx:ble;
            if(!transport.isSceneConnected(driver)) { stopInternal("pause",false);return; }
            if(target.gattRevisions.getOrDefault(driver,-1L)!=revision) {
                target.gattRevisions.put(driver,revision);
                transport.sendScene(driver,target.token,values,()->permits(target.token,driver,revision));
            }
        }
    }
    private void stopInternal(String action,boolean fromMuse) {
        Run stopped=run;if(stopped==null) return;
        run=null;player.stop();
        if(stopped.poll!=null) stopped.poll.cancel();
        if(stopped.bindings.containsValue("sosexy_ble")) ble.stopScene();
        if(stopped.bindings.containsValue("cx492b_ble")) cx.stopScene();
        sk.stop(stopped.token);
        if(stopped.bindings.containsValue("muse_ble")&&!fromMuse) muse.stop();
        if(action!=null) actions.add(new PendingAction(stopped,action));
    }
    private Request request(Run target,String action,JSONObject body) {
        Request.Builder request=new Request.Builder().url(target.base+"/api/scenes/sessions/"+target.id+"/"+action)
                .post(RequestBody.create(body.toString(),MediaType.get("application/json; charset=utf-8")));
        if(!target.credential.isEmpty()) request.header("Authorization","Bearer "+target.credential);
        return request.build();
    }
    private void sendAction(PendingAction action) {
        try {
            sendingAction=action;action.next=SystemClock.elapsedRealtime()+3000;
            JSONObject body=new JSONObject().put("owner_id",action.run.owner).put("generation",action.run.generation);
            http.newCall(request(action.run,action.action,body)).enqueue(new Callback() {
                @Override public void onFailure(Call call,IOException error) { actionDone(action,false); }
                @Override public void onResponse(Call call,Response response) {
                    try(Response closed=response) { actionDone(action,response.isSuccessful()||response.code()==404||response.code()==403); }
                }
            });
        } catch(Exception ignored) { actionDone(action,false); }
    }
    private synchronized void actionDone(PendingAction action,boolean done) {
        if(sendingAction==action) sendingAction=null;
        if(done) actions.remove(action);
    }
    synchronized void serviceDestroyed(String id,long generation) {
        if(run!=null&&run.id.equals(id)&&run.generation==generation) stopInternal("pause",false);
        if(!actions.isEmpty()&&sendingAction==null) sendAction(actions.peek());
    }
    static boolean active() { SceneDeviceBridge bridge=current;return bridge!=null&&bridge.isRunning(); }
    private synchronized boolean isRunning() { return run!=null; }
    static boolean ownsLiveMuse() { SceneDeviceBridge bridge=current;return bridge!=null&&bridge.ownsMuse(); }
    private synchronized boolean ownsMuse() { return run!=null&&run.bindings.containsValue("muse_ble")&&player.alive(); }
    static boolean permits(String token,String driver,long revision) { SceneDeviceBridge bridge=current;return bridge!=null&&bridge.player.permits(token,driver,revision); }
    static void stoppedByMuse() { SceneDeviceBridge bridge=current;if(bridge!=null) synchronized(bridge) { bridge.stopInternal("pause",true); } }
    static void gattLost(String driver) { SceneDeviceBridge bridge=current;if(bridge!=null) synchronized(bridge) {
        if(bridge.run!=null&&bridge.run.bindings.containsValue(driver)) bridge.stopInternal("pause",false);
    } }
    static void transportFailed(String token,String driver,long revision) { SceneDeviceBridge bridge=current;if(bridge!=null) synchronized(bridge) {
        if(bridge.player.permits(token,driver,revision)) bridge.stopInternal("pause",false);
    } }
    static void museFailed() { SceneDeviceBridge bridge=current;if(bridge!=null) synchronized(bridge) {
        if(bridge.run!=null&&bridge.run.bindings.containsValue("muse_ble")) bridge.stopInternal("pause",false);
    } }
    private static Map<String,Object> object(JSONObject value) throws JSONException {
        Map<String,Object> result=new LinkedHashMap<>();Iterator<String> keys=value.keys();
        while(keys.hasNext()) { String key=keys.next();result.put(key,convert(value.get(key))); }return result;
    }
    private static Object convert(Object value) throws JSONException {
        if(value instanceof JSONObject) return object((JSONObject)value);
        if(value instanceof JSONArray) { List<Object> list=new ArrayList<>();JSONArray array=(JSONArray)value;
            for(int i=0;i<array.length();i++) list.add(convert(array.get(i)));return list; }
        return value==JSONObject.NULL?null:value;
    }
}
