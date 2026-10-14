package app.obsidianvow.core;

import android.Manifest;
import android.app.*;
import android.app.usage.UsageEvents;
import android.app.usage.UsageStatsManager;
import android.content.*;
import android.content.pm.PackageManager;
import android.os.*;
import android.provider.Settings;
import android.telephony.PhoneStateListener;
import android.telephony.TelephonyManager;
import android.view.WindowManager;
import androidx.core.app.NotificationCompat;
import androidx.core.content.ContextCompat;
import org.json.JSONArray;
import org.json.JSONObject;
import okhttp3.*;
import java.lang.ref.WeakReference;
import java.util.concurrent.TimeUnit;

/** The phone owns consent, monotonic deadlines and emergency release. */
final class LookAtMeController {
    private static final String CHANNEL="look_at_me_invitation", RESIDENCE="look_at_me_residence";
    private static final int NOTIFICATION=4202, ACTIVE_NOTIFICATION=4203;
    static LookAtMeController instance;
    static WeakReference<WebViewActivity> activity=new WeakReference<>(null);
    private final ObsidianPushService service;
    private final LookAtMeStore store;
    private final LookAtMeOverlay overlay;
    private final Handler handler=Build.VERSION.SDK_INT>=28 ? Handler.createAsync(Looper.getMainLooper()) : new Handler(Looper.getMainLooper());
    private final OkHttpClient http;
    private volatile boolean running=true;
    private Thread pollThread, uploadThread;
    private long backgroundSince, lastReturn, lastSaved, lastNotification;
    private boolean chatVisible;
    private boolean serverTogetherHold;
    private String dailyId="";
    private final Runnable ticker=this::tick;
    private final TelephonyManager telephony;
    private boolean listening;
    private final PhoneStateListener calls=new PhoneStateListener() {
        @Override public void onCallStateChanged(int state,String ignored) {
            if(state!=TelephonyManager.CALL_STATE_IDLE && s()!=null && (s().resident()||s().accepted())) finish("call");
        }
    };
    private final BroadcastReceiver screen=new BroadcastReceiver() {
        @Override public void onReceive(Context context,Intent intent) {
            if(Intent.ACTION_SCREEN_OFF.equals(intent.getAction()) && s()!=null) {
                if(s().resident()||s().accepted()) finish("screen_off");
                else if(s().waiting()) {overlay.hide();notifyInvitation();}
            }
        }
    };

    LookAtMeController(ObsidianPushService service,OkHttpClient client) {
        this.service=service;store=new LookAtMeStore(service);overlay=new LookAtMeOverlay(service);
        http=client.newBuilder().dispatcher(new okhttp3.Dispatcher()).readTimeout(40,TimeUnit.SECONDS).callTimeout(45,TimeUnit.SECONDS).build();
        telephony=service.getSystemService(TelephonyManager.class);
        NotificationManager nm=service.getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel(CHANNEL,"邀约通知",NotificationManager.IMPORTANCE_HIGH));
        nm.createNotificationChannel(new NotificationChannel(RESIDENCE,"邀约",NotificationManager.IMPORTANCE_LOW));
        service.registerReceiver(screen,new IntentFilter(Intent.ACTION_SCREEN_OFF));
        instance=this;
        recover();
        pollThread=new Thread(this::poll,"LookAtMePoll");pollThread.start();
        uploadThread=new Thread(this::upload,"LookAtMeOutbox");uploadThread.start();
        handler.post(ticker);
    }

    private LookAtMeSession s(){return store.session;}
    private long now(){return SystemClock.elapsedRealtime();}
    private long boot(){return Settings.Global.getInt(service.getContentResolver(),Settings.Global.BOOT_COUNT,-1);}
    private boolean unlocked(){
        PowerManager p=service.getSystemService(PowerManager.class);
        KeyguardManager k=service.getSystemService(KeyguardManager.class);
        return p!=null&&p.isInteractive()&&k!=null&&!k.isKeyguardLocked();
    }
    static boolean permissions(Context c) {
        android.app.AppOpsManager ops=c.getSystemService(android.app.AppOpsManager.class);
        return Settings.canDrawOverlays(c) && ops!=null
            && ops.checkOpNoThrow(android.app.AppOpsManager.OPSTR_GET_USAGE_STATS,android.os.Process.myUid(),c.getPackageName())==android.app.AppOpsManager.MODE_ALLOWED
            && (Build.VERSION.SDK_INT<33||ContextCompat.checkSelfPermission(c,Manifest.permission.POST_NOTIFICATIONS)==PackageManager.PERMISSION_GRANTED)
            && ContextCompat.checkSelfPermission(c,Manifest.permission.READ_PHONE_STATE)==PackageManager.PERMISSION_GRANTED;
    }
    static JSONObject json(Object... pairs) {
        JSONObject o=new JSONObject();try {for(int i=0;i<pairs.length;i+=2)o.put((String)pairs[i],pairs[i+1]);}
        catch(Exception e){throw new IllegalArgumentException(e);} return o;
    }

    private void recover() {
        if(s()==null) return;
        if(s().resident()||s().accepted()) {
            boolean active=s().resident();long observed=active?s().start+s().actualMs:s().acceptedAt;
            s().recover();store.event(active?"finished":"start_failed",observed,
                json("outcome",s().outcome,"actual_ms",s().actualMs,"reason",s().reason));
        } else if(s().waiting()) {
            if(s().togetherHold>0)return; // Restore the frozen server deadline before deciding expiry.
            // A fresh process may retain a notification, but never auto-pop an old invitation.
            if(s().boot!=boot()) {
                s().phase="finished";s().reason="process_restart";
                store.event("delivery_failed",s().anchorElapsed,json("reason","process_restart"));
            } else if(s().expired(now())) finish("deadline");
            else notifyInvitation();
        }
        if(!s().live()) clearUi();
    }

    private String base(){return service.lookAtMeHttpBase();}
    private void pause(long millis){try{Thread.sleep(millis);}catch(InterruptedException ignored){}}
    private void poll() {
        while(running) {
            String base=base();if(base==null){pause(2000);continue;}
            try {
                String url=base+"/api/look-at-me/pending?device_id="+android.net.Uri.encode(DeviceIdentity.id(service))+"&timeout=30";
                long sent=SystemClock.elapsedRealtime();
                try(Response response=http.newCall(new Request.Builder().url(url).build()).execute()) {
                    if(response.isSuccessful()&&response.code()!=204&&response.body()!=null) {
                        JSONObject payload=new JSONObject(response.body().string());
                        long received=SystemClock.elapsedRealtime();
                        handler.post(()->receive(payload,sent,received));
                    }
                }
            } catch(Exception e){android.util.Log.d("LookAtMe","Poll deferred: "+e.getClass().getSimpleName());}
            pause(1000);
        }
    }
    private void upload() {
        while(running) {
            String base=base();if(base==null){pause(2000);continue;}
            try {
                TogetherStore together=new TogetherStore(service);
                JSONObject end=together.end();
                if(!end.optString("session_id").isEmpty()) {
                    String sid=end.optString("session_id");
                    Request finish=new Request.Builder().url(base+"/api/together/sessions/"+sid+"/actions")
                        .post(RequestBody.create(end.toString(),MediaType.get("application/json"))).build();
                    try(Response response=http.newCall(finish).execute()){if(response.isSuccessful())together.ended(sid);}
                }
                JSONObject batch=store.firstBatch();
                if(batch!=null) {
                    JSONObject body=json("device_id",DeviceIdentity.id(service),"events",batch.getJSONArray("events"));
                    Request request=new Request.Builder().url(base+"/api/look-at-me/"+batch.getString("request_id")+"/events")
                        .post(RequestBody.create(body.toString(),MediaType.get("application/json"))).build();
                    try(Response response=http.newCall(request).execute()) {
                        if(response.isSuccessful()&&response.body()!=null) {
                            long acknowledged=new JSONObject(response.body().string()).optLong("processed_seq");
                            JSONArray sent=batch.getJSONArray("events");
                            if(sent.length()>0&&sent.getJSONObject(sent.length()-1).optLong("sequence")<=acknowledged)
                                handler.post(()->store.acknowledge(batch));
                        }
                    }
                }
                try(Response response=http.newCall(new Request.Builder().url(base+"/api/look-at-me/status?device_id="+android.net.Uri.encode(DeviceIdentity.id(service))).build()).execute()) {
                    if(response.isSuccessful()&&response.body()!=null) {
                        JSONObject status=new JSONObject(response.body().string());
                        handler.post(()->{
                            serverTogetherHold=status.optBoolean("deferred");
                            if(s()!=null&&s().waiting()&&s().togetherHold>0&&!serverTogetherHold&&!TogetherMediaService.holding()) {
                                JSONObject pending=status.optJSONObject("request");
                                if(pending!=null&&s().id.equals(pending.optString("id"))) {
                                    long now=now();double serverNow=status.optDouble("server_now");
                                    s().anchorElapsed=now;s().anchorWall=(long)(serverNow*1000);
                                    s().expires=now+(long)((pending.optDouble("expires_at")-serverNow)*1000);
                                    s().choiceDeadline=pending.isNull("choice_deadline")?0:now+(long)((pending.optDouble("choice_deadline")-serverNow)*1000);
                                    s().togetherHold=0;s().boot=boot();store.persist();
                                    handler.removeCallbacks(ticker);handler.post(ticker);
                                }
                            }
                            setEnabled(status.optBoolean("enabled")&&DeviceIdentity.id(service).equals(status.optString("device_id")));
                            String next=status.optString("conv_id","");
                            if(!next.isEmpty()&&!next.equals(dailyId)) {
                                dailyId=next;
                                if(s()!=null&&s().followDaily(next)){store.persist();openChat();}
                            }
                        });
                    }
                }
            }catch(Exception e){android.util.Log.d("LookAtMe","Receipt retained: "+e.getClass().getSimpleName());}
            pause(2000);
        }
    }
    private void setEnabled(boolean enabled) {
        store.enabled=enabled;
        if(!enabled&&s()!=null&&s().live()) finish("disabled");
        else store.persist();
    }
    private void receive(JSONObject payload,long sent,long received) {
        if(!running)return;
        setEnabled(payload.optBoolean("enabled"));
        JSONObject r=payload.optJSONObject("request");
        if(!store.enabled||r==null)return;
        String id=r.optString("id");
        if(s()!=null&&(s().id.equals(id)||s().live()))return;
        LookAtMeSession next=new LookAtMeSession();
        next.id=id;next.convId=r.optString("conv_id");next.aiName=r.optString("ai_name");
        next.expression=r.optString("expression");next.seconds=r.optLong("duration_seconds");
        next.anchorResponse((long)(payload.optDouble("server_now")*1000),
            (long)(r.optDouble("expires_at")*1000),sent,received);
        next.boot=boot();
        store.session=next;dailyId=next.convId;store.persist();
        handler.removeCallbacks(ticker);handler.post(ticker);
        if(next.expired(now())){finish("deadline");return;}
        if(!permissions(service)){finish("permission_missing");return;}
        if(unlocked())showInvitation();else notifyInvitation();
    }

    void notificationOpened(String id) {
        if(s()==null||!s().id.equals(id)||!s().waiting()||!unlocked())return;
        if(s().expired(now())){finish("deadline");return;}
        showInvitation();
    }
    private void showInvitation() {
        if(TogetherMediaService.holding()||serverTogetherHold||s()!=null&&s().togetherHold>0)return;
        if(!store.enabled||s()==null||!s().waiting()||!unlocked())return;
        if(s().expired(now())){finish("deadline");return;}
        if(overlay.showing())return;
        try {
            String offeredId=s().id;
            overlay.invite(s(),this::decide,()->{
                if(s()==null||!s().id.equals(offeredId)||!s().waiting())return;
                boolean first=s().phase.equals("queued");
                if(!s().offer(now())){finish("deadline");return;}
                if(first)store.event("offered",now(),json());else store.persist();
            });
            service.getSystemService(NotificationManager.class).cancel(NOTIFICATION);
        } catch(Exception e){finish("permission_missing");}
    }
    private void decide(boolean accept,String reason) {
        if(TogetherMediaService.holding()||serverTogetherHold)return;
        if(s()==null||!unlocked())return;
        if(!s().decide(accept?"accepted":"rejected",now())){if(s().expired(now()))finish("deadline");return;}
        store.event(accept?"accepted":"rejected",now(),json("reason",reason));
        clearUi();
        if(accept){listenCalls();openChat();}
    }

    private void openChat() {
        if(s()==null||(!s().accepted()&&!s().resident()))return;
        if(!s().resident())chatVisible=false;lastReturn=now();
        try{service.startActivity(WebViewActivity.openChatIntent(service).putExtra("look_at_me_request",s().id));}
        catch(Exception ignored){} // Success is confirmed only by the visual handshake.
    }
    String requestId(){return s()!=null&&(s().accepted()||s().resident())?s().id:"";}
    void ready(String requestId,String convId,String currentDaily) {
        if(s()==null||!unlocked())return;
        WebViewActivity a=activity.get();
        if(a==null||!a.lookAtMeVisible())return;
        if(!currentDaily.equals(dailyId)||!convId.equals(currentDaily)){chatVisible=false;return;}
        if(s().ready(requestId,convId,currentDaily,true,now())) {
            store.event("started",now(),json("request_id",s().id,"conv_id",convId));
            try{overlay.residence(()->finish("manual"));}catch(Exception e){finish("permission_missing");return;}
            residenceNotification();
        }
        if(s().resident()&&s().id.equals(requestId)&&convId.equals(currentDaily)&&!currentDaily.isEmpty()) {
            s().convId=currentDaily;chatVisible=true;backgroundSince=0;
            a.getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        }
    }
    void visibility(boolean visible){if(!visible && (s()==null || !s().resident()))chatVisible=false;}
    boolean resident(){return s()!=null&&s().resident();}

    private void tick() {
        if(!running)return;
        if(s()!=null&&s().waiting()&&(TogetherMediaService.holding()||serverTogetherHold||s().togetherHold>0)) {
            if(s().togetherHold==0){s().togetherHold=Math.max(1,now());store.persist();}
            overlay.hide();service.getSystemService(NotificationManager.class).cancel(NOTIFICATION);
            handler.postDelayed(ticker,1000);return;
        }
        try {
            if(s()!=null&&s().live()) {
                long now=now();
                if(s().waiting()) {
                    if(s().expired(now))finish("deadline");
                    else {if(!unlocked()&&overlay.showing()){overlay.hide();notifyInvitation();}overlay.update(s().deadline()-now,false);}
                } else if(s().accepted()) {
                    if(!unlocked())finish("screen_off");
                    else if(s().startExpired(now))finish("start_failed");
                } else if(s().resident()) {
                    if(now>=s().end)finish("deadline");
                    else if(!unlocked())finish("screen_off");
                    else {
                        s().observe(now);overlay.update(s().end-now,true);
                        if(now-lastSaved>=1000){store.persist();lastSaved=now;}
                        if(now-lastNotification>=1000){residenceNotification();lastNotification=now;}
                        WebViewActivity a=activity.get();
                        boolean visible=chatVisible&&a!=null&&a.lookAtMeVisible();
                        if(visible)backgroundSince=0;
                        else {
                            if(backgroundSince==0)backgroundSince=now;
                            String foreground=foregroundPackage();
                            if(isPhone(foreground))finish("call");
                            else if(now-backgroundSince>=5000)finish("return_failed");
                            else if(now-backgroundSince>=400&&now-lastReturn>=750)openChat();
                        }
                    }
                }
            }
        }catch(Exception e){android.util.Log.e("LookAtMe","Local execution stopped",e);finish("local_error");}
        if(s()!=null&&s().live()) handler.postDelayed(ticker,s().waiting()?1000:250);
    }

    void finish(String why) {
        if(s()==null||!s().live()){clearUi();return;}
        boolean active=s().resident(), accepted=s().accepted();
        s().finish(why,now()); // Release before disk, UI or network cleanup.
        handler.removeCallbacks(ticker);
        chatVisible=false;backgroundSince=0;
        clearUi();
        String event=active?"finished":s().decision.equals("timed_out")?"timed_out":accepted?"start_failed":
            why.equals("deadline")?"expired":why.equals("disabled")?"cancelled":"delivery_failed";
        store.event(event,now(),json("outcome",s().outcome,"actual_ms",s().actualMs,"reason",why));
    }
    private void clearUi() {
        overlay.hide();NotificationManager nm=service.getSystemService(NotificationManager.class);
        nm.cancel(NOTIFICATION);nm.cancel(ACTIVE_NOTIFICATION);
        WebViewActivity a=activity.get();if(a!=null)a.getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        if(listening){try{telephony.listen(calls,PhoneStateListener.LISTEN_NONE);}catch(Exception ignored){}listening=false;}
    }
    private void listenCalls(){
        if(listening||telephony==null)return;
        try{telephony.listen(calls,PhoneStateListener.LISTEN_CALL_STATE);listening=true;}
        catch(SecurityException e){finish("permission_missing");}
    }
    private String foregroundPackage(){
        UsageStatsManager usage=service.getSystemService(UsageStatsManager.class);
        if(usage==null)return "";
        long at=System.currentTimeMillis();UsageEvents events=usage.queryEvents(at-30000,at);
        UsageEvents.Event e=new UsageEvents.Event();String name="";
        if(events!=null)while(events.hasNextEvent()){events.getNextEvent(e);if(e.getEventType()==UsageEvents.Event.ACTIVITY_RESUMED)name=e.getPackageName();}
        return name;
    }
    private boolean isPhone(String name){return name.equals("com.android.incallui")||name.equals("com.android.phone")||name.contains("dialer")||name.contains("incall");}

    private void notifyInvitation(){
        if(s()==null||!s().waiting()||s().expired(now()))return;
        if(!s().notified){s().notified=true;store.event("notified",now(),json());}
        PendingIntent open=PendingIntent.getActivity(service,NOTIFICATION,
            WebViewActivity.openChatIntent(service).setData(android.net.Uri.parse("obsidianvow://look-at-me/invite/"+s().id))
            .putExtra("look_at_me_notification",s().id),PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
        Notification n=new NotificationCompat.Builder(service,CHANNEL).setSmallIcon(R.mipmap.ic_launcher)
            .setColor(0xffd4943a).setSubText("◆ Obsidian Vow · 邀约")
            .setContentTitle(s().aiName+" · "+LookAtMeOverlay.duration(s().seconds)).setContentText(s().expression)
            .setStyle(new NotificationCompat.BigTextStyle().bigText(s().expression+"\n希望陪伴 "+LookAtMeOverlay.duration(s().seconds)+" · 解锁后查看邀请"))
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC).setCategory(NotificationCompat.CATEGORY_MESSAGE)
            .setContentIntent(open).setAutoCancel(true).setOnlyAlertOnce(true)
            .setTimeoutAfter(Math.max(1,s().deadline()-now())).build();
        service.getSystemService(NotificationManager.class).notify(NOTIFICATION,n);
    }
    private void residenceNotification(){
        if(!resident())return;
        PendingIntent end=PendingIntent.getService(service,ACTIVE_NOTIFICATION,new Intent(service,ObsidianPushService.class)
            .putExtra("action","look_at_me_end"),PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
        PendingIntent open=PendingIntent.getActivity(service,ACTIVE_NOTIFICATION,WebViewActivity.openChatIntent(service)
            .putExtra("look_at_me_request",s().id),PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
        Notification n=new NotificationCompat.Builder(service,RESIDENCE).setSmallIcon(R.mipmap.ic_launcher)
            .setColor(0xffd4943a).setContentTitle("◆ Obsidian Vow · 邀约")
            .setContentText("还剩 "+LookAtMeOverlay.duration(Math.max(0,(s().end-now()+999)/1000)))
            .setContentIntent(open).setOngoing(true).setOnlyAlertOnce(true).addAction(0,"立即结束",end)
            .setTimeoutAfter(Math.max(1,s().end-now())).build();
        service.getSystemService(NotificationManager.class).notify(ACTIVE_NOTIFICATION,n);
    }

    void close(){running=false;finish("process_restart");handler.removeCallbacksAndMessages(null);
        pollThread.interrupt();uploadThread.interrupt();http.dispatcher().cancelAll();
        try{service.unregisterReceiver(screen);}catch(Exception ignored){}if(instance==this)instance=null;}
}
