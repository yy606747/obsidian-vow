package app.obsidianvow.core;

import android.content.Context;
import android.os.Handler;
import android.os.SystemClock;
import org.json.JSONArray;
import org.json.JSONObject;
import okhttp3.*;
import java.util.HashSet;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.function.Consumer;

/** Serial mutation outbox plus an independent long-poll connection. No model polling. */
final class TogetherClient {
    static final String CONNECTION_PROBLEM="暂时没连上，写好的话和待发送的消息都留着。";
    interface Host {
        void snapshot(JSONObject snapshot); void delta(String text); void capture(JSONObject request);
        void stopped(); void problem(String message); String captureState(); boolean inputBusy(); void offline(); void connected(); void maintenance();
    }
    private final Handler main;
    private final Host host;
    private final TogetherApi api;
    private final TogetherStore store;
    private final String device;
    private final ExecutorService commands=Executors.newSingleThreadExecutor(), poller=Executors.newSingleThreadExecutor();
    private final TogetherLease lease=new TogetherLease();
    private final Set<String> handledCaptures=new HashSet<>();
    private JSONObject state;
    private volatile boolean closed;
    private long cursor, snapshotCursor, nextHeartbeat;
    private boolean heartbeatPending, flushPending, refreshing, lost;
    private final TogetherStream stream=new TogetherStream();
    private final String sid;
    private final Runnable ticker=this::tick;

    TogetherClient(Context context,Handler main,JSONObject initial,Host host) {
        this.main=main;this.host=host;api=new TogetherApi(context);store=new TogetherStore(context);
        device=DeviceIdentity.id(context);state=initial;sid=initial.optString("id");
        cursor=initial.optLong("cursor");snapshotCursor=cursor;
    }
    void start(){apply(state,true);poll();main.post(ticker);flush();}
    String sid(){return sid;} long version(){return state.optLong("source_version");}
    String source(){return state.optString("source","screen");}
    boolean online(){return lease.online(SystemClock.elapsedRealtime())&&!closed;}
    double serverNow(){return lease.serverNow(SystemClock.elapsedRealtime());}
    JSONObject state(){return state;}
    private String path(String tail){return "/api/together/sessions/"+sid+tail;}
    private JSONObject deviceBody(){return TogetherApi.object("device_id",device);}
    private void apply(JSONObject next,boolean acknowledged) {
        if(closed||next==null)return;
        if("ended".equals(next.optString("status"))){close();store.clear();host.stopped();return;}
        if(next.optLong("source_version")<version()||next.optLong("cursor")<snapshotCursor)return;
        state=next;snapshotCursor=next.optLong("cursor");
        if(acknowledged){lease.acknowledge(SystemClock.elapsedRealtime(),next.optDouble("server_now"),next.optLong("lease_seconds",45)*1000,version(),next.optBoolean("enabled"));lost=false;if(next.optBoolean("enabled"))host.connected();}
        store.state(TogetherApi.object("id",sid,"source",source(),"source_version",version(),"model_key",state.optString("model_key"),"conv_id",state.optString("conv_id")));
        // Keep the last streamed text on screen until a snapshot includes the
        // finished turn. Otherwise a slow refresh briefly removes the whole reply.
        if(stream.snapshotIncludesFinish(snapshotCursor))host.delta("");
        host.snapshot(next);
        if(!next.optBoolean("enabled")){lease.end();host.offline();}
        JSONArray captures=next.optJSONArray("captures");
        if(captures!=null)for(int i=0;i<captures.length();i++)capture(captures.optJSONObject(i));
    }
    private void capture(JSONObject request) {
        if(request==null||!online())return;
        String id=request.optString("capture_id",request.optString("id"));
        if(id.isEmpty()||"manual".equals(request.optString("purpose"))||(!request.optString("status").isEmpty()&&!"requested".equals(request.optString("status"))))return;
        if(!lease.accepts(SystemClock.elapsedRealtime(),request.optLong("source_version"))||request.optDouble("deadline")<serverNow())return;
        if(handledCaptures.add(id)){host.capture(request);if(handledCaptures.size()>500)handledCaptures.clear();}
    }
    private void poll(){
        if(closed)return;
        final long after=cursor;
        poller.execute(()->{
            try {
                JSONObject events=api.get(path("/events?device_id="+device+"&after="+after));
                main.post(()->{
                    if(closed)return;
                    if("ended".equals(events.optString("status"))){close();store.clear();host.stopped();return;}
                    if(!events.optBoolean("enabled")){lease.end();host.offline();main.postDelayed(this::poll,5000);return;}
                    if(events.optBoolean("resync")){refresh();cursor=events.optLong("cursor",cursor);}
                    JSONArray rows=events.optJSONArray("events");boolean reload=false;
                    if(rows!=null)for(int i=0;i<rows.length();i++){
                        JSONObject row=rows.optJSONObject(i);if(row==null||row.optLong("seq")<=cursor)continue;
                        cursor=row.optLong("seq");JSONObject payload=row.optJSONObject("payload");if(payload==null)payload=new JSONObject();
                        String type=row.optString("type");
                        if(type.equals("reply_delta")){
                            host.delta(stream.append(payload.optString("turn_id"),payload.optString("text")));
                        } else if(type.equals("capture_requested"))capture(payload);
                        else if(type.equals("turn_finished")||type.equals("turn_interrupted")){stream.finish(cursor);reload=true;}
                        else if(type.equals("ended")){close();store.clear();host.stopped();return;}
                        else reload=true;
                    }
                    if(reload)refresh();poll();
                });
            } catch(Exception error){main.post(()->{if(!closed)main.postDelayed(this::poll,3000);});}
        });
    }
    void refresh(){
        if(closed||refreshing)return;refreshing=true;
        commands.execute(()->{
            try{JSONObject next=api.get(path("?device_id="+device));main.post(()->{refreshing=false;apply(next,false);});}
            catch(Exception error){main.post(()->{refreshing=false;problem(error);});}
        });
    }
    private void tick(){
        if(closed)return;
        long now=SystemClock.elapsedRealtime();
        host.maintenance();
        if(!online()&&!lost){lost=true;host.offline();}
        if(now>=nextHeartbeat&&!heartbeatPending){heartbeatPending=true;nextHeartbeat=now+state.optLong("heartbeat_seconds",10)*1000;
            JSONObject body=TogetherApi.object("device_id",device,"source_version",version(),"capture_state",host.captureState(),"input_busy",host.inputBusy());
            commands.execute(()->{
                try{JSONObject next=api.post(path("/heartbeat"),body);main.post(()->{heartbeatPending=false;apply(next,true);flush();});}
                catch(Exception error){main.post(()->{heartbeatPending=false;if(error instanceof TogetherApi.ApiError){problem(error);refresh();}});}
            });
        }
        main.postDelayed(ticker,1000);
    }
    void heartbeatSoon(){nextHeartbeat=0;}
    boolean send(String text,String capture){
        if(closed)return false;
        if(text.trim().isEmpty()&&(capture==null||capture.isEmpty()))return false;
        try{store.enqueue(text,capture);host.snapshot(state);flush();return true;}catch(RuntimeException error){host.problem(error.getMessage());return false;}
    }
    private void flush(){
        if(closed||flushPending||!online())return;
        flushPending=true;
        commands.execute(()->{
            Exception failure=null;
            JSONArray queue=store.outbox();
            for(int i=0;i<queue.length()&&!closed;i++){
                JSONObject body=queue.optJSONObject(i);if(body==null||body.has("error"))continue;
                try{body.put("device_id",device);api.post(path("/messages"),body);store.acknowledge(body.optString("client_id"));}
                catch(Exception error){
                    failure=error;
                    if(error instanceof TogetherApi.ApiError&&((TogetherApi.ApiError)error).status==422){store.failed(body.optString("client_id"),readable(error));continue;}
                    break;
                }
            }
            final Exception error=failure;
            main.post(()->{flushPending=false;if(error!=null)problem(error);refresh();});
        });
    }
    void action(String action,JSONObject extra,Consumer<JSONObject> done){
        if(closed)return;
        try{extra.put("device_id",device);extra.put("action",action);}catch(Exception ignored){}
        commands.execute(()->{
            try{JSONObject next=api.post(path("/actions"),extra);main.post(()->{if(closed)return;apply(next,false);if(done!=null)done.accept(next);});}
            catch(Exception error){main.post(()->problem(error));}
        });
    }
    void manual(byte[] jpeg,double capturedAt,long sourceVersion,Consumer<JSONObject> ready,Consumer<String> failed){
        if(closed)return;
        commands.execute(()->{
            try{
                JSONObject request=api.post(path("/captures"),deviceBody());
                JSONObject result=uploadFrame(request.optString("id"),sourceVersion,capturedAt,jpeg,"");
                main.post(()->{if(!closed)ready.accept(result);});
            }catch(Exception error){main.post(()->{if(!closed)failed.accept(readable(error));});}
        });
    }
    void frame(JSONObject request,byte[] jpeg,double capturedAt,String error){
        if(closed)return;
        commands.execute(()->{
            try{uploadFrame(request.optString("capture_id",request.optString("id")),request.optLong("source_version"),capturedAt,jpeg,error);}
            catch(Exception failure){main.post(()->problem(failure));}
        });
    }
    private JSONObject uploadFrame(String id,long version,double at,byte[] bytes,String error) throws Exception {
        MultipartBody.Builder body=new MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("device_id",device).addFormDataPart("source_version",String.valueOf(version)).addFormDataPart("captured_at",String.valueOf(at));
        if(bytes!=null)body.addFormDataPart("image","frame.jpg",RequestBody.create(bytes,MediaType.get("image/jpeg")));
        else body.addFormDataPart("error",error.isEmpty()?"unavailable":error);
        return api.upload(path("/captures/"+id+"/result"),body.build());
    }
    void transcribe(byte[] wav,Consumer<String> success,Consumer<String> failed){
        // ASR gets its own worker so a slow transcription cannot starve lease renewal.
        new Thread(()->{
            try{
                JSONObject result=api.upload(path("/asr"),new MultipartBody.Builder().setType(MultipartBody.FORM)
                    .addFormDataPart("device_id",device).addFormDataPart("recording","speech.wav",RequestBody.create(wav,MediaType.get("audio/wav"))).build());
                main.post(()->{if(!closed)success.accept(result.optString("text"));});
            }catch(Exception error){main.post(()->{if(!closed)failed.accept(readable(error));});}
        },"TogetherAsr").start();
    }
    void end(){
        if(closed)return;
        JSONObject receipt=TogetherApi.object("session_id",sid,"device_id",device,"action","end","ended_at",serverNow());
        store.markEnd(receipt);close();host.stopped();
        new Thread(()->flushEnd(api,store),"TogetherEnd").start();
    }
    static void flushEnd(TogetherApi api,TogetherStore store){
        JSONObject end=store.end();String sid=end.optString("session_id");if(sid.isEmpty())return;
        try{api.post("/api/together/sessions/"+sid+"/actions",end);store.ended(sid);}
        catch(Exception ignored){ } // Keep the receipt for the push service / next visible entry.
    }
    void close(){closed=true;lease.end();main.removeCallbacks(ticker);commands.shutdownNow();poller.shutdownNow();api.http.dispatcher().cancelAll();}
    private void problem(Exception error){if(!closed)host.problem(readable(error));}
    static String readable(Exception error){
        String code=error.getMessage()==null?"":error.getMessage();
        switch(code){
            case "together_disabled":return "相伴已关闭，可在设置中开启";
            case "together_other_activity_active":return "正在进行另一段陪伴或小剧场，请结束后再回来";
            case "together_model_no_vision":return "这个模型还不能看图，可以先聊，或换一个模型。";
            case "together_capture_paused":return "画面暂停了，聊天照常。";
            case "together_capture_expired":case "together_draft_expired":case "together_draft_unavailable":return "这张图需要重新选，写好的话还在。";
            case "together_source_changed":return "已经换了画面，重新选一张吧。";
            case "together_asr_not_configured":return "语音输入还没设置，可以先打字。";
            case "together_capture_busy":return "正在取图，稍等一下";
            default:return error instanceof TogetherApi.ApiError?"操作未完成（"+((TogetherApi.ApiError)error).status+"）："+code:CONNECTION_PROBLEM;
        }
    }
}
