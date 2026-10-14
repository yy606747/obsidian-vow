package app.obsidianvow.core;

import android.Manifest;
import android.app.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.os.*;
import android.provider.Settings;
import android.view.Surface;
import android.widget.Toast;
import androidx.core.app.NotificationCompat;
import androidx.core.content.ContextCompat;
import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.nio.file.Files;

/** User-visible session lifetime. A picture channel can stop without ending chat. */
public class TogetherMediaService extends Service implements TogetherClient.Host, TogetherOverlay.Actions {
    static final String END="together.end", SHOW="together.show";
    private static final String CHANNEL="together_companionship";
    private static final int NOTIFICATION=7301;
    static TogetherMediaService instance;
    private final Handler main=new Handler(Looper.getMainLooper());
    private HandlerThread mediaThread;
    private Handler media;
    private volatile TogetherFrames.Screen screen;
    private volatile TogetherFrames.Camera camera;
    private TogetherOverlay overlay;
    private TogetherAvatar avatar;
    private android.widget.FrameLayout pageHost;
    private TogetherClient client;
    private TogetherStore store;
    private final PcmRecorder recorder=new PcmRecorder();
    private String captureState="paused", selected="screen";
    private Surface preview;
    private boolean recording, transcribing, capturing, micPrimed, cameraPrimed, screenGranted;
    private volatile boolean closed;
    private volatile int mediaGeneration;
    private int projectionGeneration;
    private int audioGeneration;
    private boolean draftUploading;
    private int draftGeneration;
    private String draftCapture="";
    private double draftExpiry;
    private Bitmap thumbnail;
    private boolean speechRetry;
    private String problem="";
    private boolean connectionProblem;
    private boolean editing;
    private final Runnable saveDraftLater=this::saveDraft;
    private final BroadcastReceiver screenReceiver=new BroadcastReceiver(){
        @Override public void onReceive(Context context,Intent intent){
            if(Intent.ACTION_SCREEN_OFF.equals(intent.getAction())){
                cancelVoice();pauseLocal("锁屏后画面已暂停");if(overlay!=null)overlay.visibility(false);
            }else if(Intent.ACTION_USER_PRESENT.equals(intent.getAction())){refreshVisibility();}
        }
    };
    static boolean holding(){return instance!=null&&instance.client!=null&&instance.client.online();}
    boolean canShowRoom(){return !closed&&client!=null&&overlay!=null;}
    void attachPage(android.widget.FrameLayout host){
        if(!canShowRoom())return;
        pageHost=host;overlay.attachTo(host);snapshot(client.state());refreshVisibility();
    }
    void detachPage(android.widget.FrameLayout host){
        if(host!=pageHost)return;pageHost=null;
        if(!closed&&overlay!=null){overlay.detachFromPage();refreshVisibility();}
    }
    void refreshVisibility(){
        if(overlay!=null)overlay.visibility(getSystemService(PowerManager.class).isInteractive()
            &&!getSystemService(KeyguardManager.class).isKeyguardLocked()
            &&(overlay.embedded()||!TogetherUi.foreground()));
    }
    boolean hasScreen(){return screenGranted&&screen!=null&&screen.active();}
    static Intent setup(Context context,String mode){return new Intent(context,TogetherActivity.class).putExtra("mode",mode).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);}
    @Override public void onCreate(){
        super.onCreate();instance=this;store=new TogetherStore(this);
        mediaThread=new HandlerThread("TogetherFrames");mediaThread.start();media=new Handler(mediaThread.getLooper());
        getSystemService(NotificationManager.class).createNotificationChannel(new NotificationChannel(CHANNEL,"相伴",NotificationManager.IMPORTANCE_LOW));
        IntentFilter filter=new IntentFilter();filter.addAction(Intent.ACTION_SCREEN_OFF);filter.addAction(Intent.ACTION_USER_PRESENT);
        if(Build.VERSION.SDK_INT>=33)registerReceiver(screenReceiver,filter,Context.RECEIVER_NOT_EXPORTED);else registerReceiver(screenReceiver,filter);
    }
    @Override public int onStartCommand(Intent intent,int flags,int startId){
        if(intent==null){stopSelf();return START_NOT_STICKY;}
        if(END.equals(intent.getAction())){end();return START_NOT_STICKY;}
        ResultReceiver receiver=intent.getParcelableExtra("receiver");
        try{
            if(!Settings.canDrawOverlays(this))throw new IllegalStateException("请允许显示悬浮窗");
            if(intent.getBooleanExtra("microphone",false))micPrimed=true;
            if(intent.getBooleanExtra("camera",false))cameraPrimed=true;
            Intent grant=intent.getParcelableExtra("projection");if(grant!=null){screenGranted=true;projectionGeneration++;}
            foreground(); // Activity remains visible until this succeeds and acknowledges it.
            if(client==null){
                JSONObject snapshot=new JSONObject(intent.getStringExtra("snapshot"));
                selected=snapshot.optString("source","screen");
                overlay=new TogetherOverlay(this,this);overlay.show();
                avatar=new TogetherAvatar(this,main,overlay::avatar);avatar.start();
                JSONObject draft=store.draft();overlay.text(draft.optString("text"));draftCapture=draft.optString("capture_id");draftExpiry=draft.optDouble("expires_at",0);
                if(!draftCapture.isEmpty()&&store.frame().isFile()){thumbnail=BitmapFactory.decodeFile(store.frame().getPath());overlay.image(thumbnail,"这张图还没发出");}
                else store.frame().delete();
                speechRetry=store.recording().isFile();overlay.voice(false,speechRetry);
                client=new TogetherClient(this,main,snapshot,this);client.start();
            }else {overlay.show();overlay.collapse(false);}
            refreshVisibility();
            if(intent.getBooleanExtra("pause_capture",false))pauseLocal("");
            String requested=intent.getStringExtra("source");
            if(requested!=null){selected=requested;startSource(grant,intent.getIntExtra("result_code",Activity.RESULT_OK));}
            if(intent.getBooleanExtra("record",false))main.postDelayed(this::record,200);
            if(receiver!=null)receiver.send(1,Bundle.EMPTY);
        }catch(Exception error){
            problem(error.getMessage()==null?"暂时无法打开浮窗":error.getMessage());
            if(receiver!=null){Bundle result=new Bundle();result.putString("error",problem);receiver.send(0,result);}
            if(client==null)stopSelf();
        }
        return START_NOT_STICKY;
    }
    private void foreground(){
        PendingIntent open=PendingIntent.getActivity(this,0,setup(this,"settings"),PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
        PendingIntent end=PendingIntent.getService(this,1,new Intent(this,TogetherMediaService.class).setAction(END),PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
        Notification notification=new NotificationCompat.Builder(this,CHANNEL).setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle("相伴").setContentText("相伴 · 点此调整画面和聊天设置")
            .setContentIntent(open).setOngoing(true).setSilent(true).addAction(0,"结束",end).build();
        if(Build.VERSION.SDK_INT>=29){
            int types=Build.VERSION.SDK_INT>=34?ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE:ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC;
            if(screenGranted)types|=ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION;
            if(Build.VERSION.SDK_INT>=30){if(cameraPrimed)types|=ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA;if(micPrimed)types|=ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE;}
            startForeground(NOTIFICATION,notification,types);
        }else startForeground(NOTIFICATION,notification);
    }
    private void startSource(Intent projection,int resultCode){
        final int generation=++mediaGeneration;captureState="paused";client.heartbeatSoon();
        final int projectionVersion=projectionGeneration;
        final String source=selected;
        media.post(()->{
            if(closed||generation!=mediaGeneration)return;
            if(camera!=null){camera.close();camera=null;}
            if(screen!=null)screen.park(!source.equals("screen"));
            TogetherFrames.Status status=(ready,reason)->main.post(()->{
                // Projection can outlive several camera/screen source switches.
                // Its revocation uses the grant generation, not the source epoch.
                if(reason.equals("screen_stopped")&&projectionVersion==projectionGeneration){
                    screenGranted=false;
                    if(selected.equals("screen")){captureState="unavailable";if(client!=null)client.heartbeatSoon();}
                    if(!closed)try{foreground();}catch(RuntimeException ignored){}
                }
                if(closed||generation!=mediaGeneration)return;
                captureState=ready?"ready":"unavailable";if(!ready)problem("画面暂时看不到了，聊天照常。可以在设置里重新打开。");
                if(ready&&selected.equals("camera")&&camera!=null&&overlay!=null)overlay.previewRotation(camera.rotation());
                if(client!=null)client.heartbeatSoon();
            });
            if(source.equals("screen")){
                if(projection!=null){if(screen!=null)screen.close();screen=new TogetherFrames.Screen(this,media,status);screen.start(resultCode,projection);}
                else if(screen!=null){screen.park(false);status.changed(true,"");}
                else status.changed(false,"screen_stopped");
            }else if(cameraPrimed){
                camera=new TogetherFrames.Camera(this,media,status);camera.preview(preview);camera.start();
            }else status.changed(false,"permission_denied");
        });
    }
    private void switchLocalSource(String source){
        selected=source;
        final int generation=++mediaGeneration;
        captureState="paused";capturing=false;
        media.post(()->{
            if(closed||generation!=mediaGeneration)return;
            if(camera!=null){camera.close();camera=null;}
            // The server switches before Activity finishes requesting permission.
            // Park the existing projection until startSource resumes it; only
            // explicit pause, revocation or lease loss releases its grant.
            if(screen!=null)screen.park(true);
        });
        if(overlay!=null){overlay.frameDone();refreshVisibility();}
        if(client!=null)client.heartbeatSoon();
    }
    private void pauseLocal(String reason){
        mediaGeneration++;projectionGeneration++;captureState="paused";capturing=false;
        media.post(()->{if(camera!=null){camera.close();camera=null;}if(screen!=null){screen.close();screen=null;}});
        screenGranted=false;cameraPrimed=false;
        if(overlay!=null){overlay.frameDone();refreshVisibility();}
        if(client!=null)client.heartbeatSoon();if(!reason.isEmpty())problem(reason);
        try{foreground();}catch(RuntimeException ignored){}
    }
    @Override public void snapshot(JSONObject state){
        if(closed||overlay==null)return;
        if(!state.optString("source").equals(selected))switchLocalSource(state.optString("source"));
        overlay.render(state);
        overlay.outbox(store.outbox());
        if(!draftCapture.isEmpty()&&draftExpiry>0&&client!=null&&client.serverNow()>=draftExpiry){removeImage();problem("这张图需要重新选，写好的话还在。");}
        if(problem.isEmpty())status();
    }
    private void status(){
        if(client==null||overlay==null)return;
        long seconds=client.state().optLong("active_seconds");
        overlay.presence(seconds,client.online(),captureState,selected);
    }
    @Override public void delta(String text){if(overlay!=null)overlay.delta(text);}
    @Override public void capture(JSONObject request){
        if(closed||client==null)return;
        if(recording||overlay.editing()){
            if(client.serverNow()>=request.optDouble("deadline")-.5){client.frame(request,null,client.serverNow(),"input_busy");return;}
            main.postDelayed(()->capture(request),200);return;
        }
        captureFrame(false,(bytes,error,at,version)->client.frame(request,bytes,at,error));
    }
    private interface Captured {void done(byte[] bytes,String error,double at,long version);}
    private void captureFrame(boolean manual,Captured callback){
        if(client==null||!client.online()||!"ready".equals(captureState)||capturing){callback.done(null,"unavailable",client==null?0:client.serverNow(),client==null?0:client.version());return;}
        if(getSystemService(KeyguardManager.class).isKeyguardLocked()){callback.done(null,"locked",client.serverNow(),client.version());return;}
        final long version=client.version();final int generation=mediaGeneration;final String source=selected;
        capturing=true;
        if(source.equals("screen"))overlay.hideForFrame();
        main.postDelayed(()->media.post(()->{
            TogetherFrames.Frame result=(bytes,error)->main.post(()->{
                capturing=false;if(overlay!=null){overlay.frameDone();refreshVisibility();}
                if(closed||client==null)return;
                if(generation!=mediaGeneration||version!=client.version()){callback.done(null,"unavailable",client.serverNow(),version);return;}
                callback.done(bytes,error,client.serverNow(),version);
            });
            if(generation!=mediaGeneration){result.done(null,"unavailable");return;}
            if(source.equals("screen")&&screen!=null)screen.capture(result);
            else if(source.equals("camera")&&camera!=null)camera.capture(result);
            else result.done(null,"unavailable");
        }),source.equals("screen")?180:0);
    }
    @Override public void look(){
        if(draftUploading){problem("上一张图片正在准备，稍等一下");return;}
        if(client==null||!client.state().optBoolean("image_supported")){problem("这个模型还不能看图，可以先聊，或在设置里换一个。");return;}
        if(recording){problem("先结束录音，再选画面");return;}
        if(!"ready".equals(captureState)){settings();return;}
        captureFrame(true,(bytes,error,at,version)->{
            if(bytes==null){problem("刚才没取到画面，再试一次就好。");return;}
            // Freeze on the tap, BEFORE making an HTTP request.
            try{Files.write(store.frame().toPath(),bytes);}catch(Exception failure){problem("无法保存图片草稿");return;}
            draftUploading=true;draftCapture="";draftExpiry=0;saveDraft();
            final int draftVersion=++draftGeneration;
            replaceThumbnail(bytes);overlay.image(thumbnail,"正在准备图片……");
            client.manual(bytes,at,version,result->{
                if(draftVersion!=draftGeneration)return;
                draftUploading=false;
                if(!"ready".equals(result.optString("status")))return;
                draftCapture=result.optString("id");draftExpiry=result.optDouble("expires_at");
                overlay.image(thumbnail,"会和这条消息一起发出");saveDraft();
            },failure->{if(draftVersion!=draftGeneration)return;draftUploading=false;removeImage();problem(failure+" · 图片没发出去，写好的话还在");});
        });
    }
    private void replaceThumbnail(byte[] bytes){if(thumbnail!=null)thumbnail.recycle();BitmapFactory.Options options=new BitmapFactory.Options();options.inSampleSize=4;thumbnail=BitmapFactory.decodeByteArray(bytes,0,bytes.length,options);}
    @Override public void send(String text){
        if(client==null||recording)return;
        if(draftUploading){problem("图片还在准备，稍后和这句话一起发。");return;}
        if(text.trim().isEmpty()&&draftCapture.isEmpty())return;
        if(client.send(text.trim(),draftCapture)){overlay.text("");removeImage();overlay.blur();problem="";status();}
    }
    @Override public void draft(String text,boolean editing){
        if(closed)return;
        main.removeCallbacks(saveDraftLater);main.postDelayed(saveDraftLater,180);
        if(this.editing!=editing){this.editing=editing;if(client!=null)client.heartbeatSoon();}
    }
    private void saveDraft(){if(!closed&&overlay!=null)store.draft(TogetherApi.object("text",overlay.text(),"capture_id",draftCapture,"expires_at",draftExpiry));}
    @Override public void removeImage(){draftGeneration++;draftUploading=false;draftCapture="";draftExpiry=0;store.frame().delete();if(overlay!=null)overlay.image(null,"");if(thumbnail!=null){thumbnail.recycle();thumbnail=null;}saveDraft();}
    @Override public void record(){
        if(recording){recorder.stop();return;}
        if(transcribing){problem("正在转成文字，稍后可以改好再发。");return;}
        if(!micPrimed||ContextCompat.checkSelfPermission(this,Manifest.permission.RECORD_AUDIO)!=PackageManager.PERMISSION_GRANTED){openSetup("microphone");return;}
        final int generation=++audioGeneration;final ByteArrayOutputStream pcm=new ByteArrayOutputStream();
        recording=true;speechRetry=false;overlay.voice(true,false);client.heartbeatSoon();
        if(!recorder.start(60,new PcmRecorder.Listener(){
            public void chunk(byte[] bytes){pcm.write(bytes,0,bytes.length);}
            public void stopped(String error){
                byte[] wav=TogetherAudio.wav(pcm.toByteArray());
                main.post(()->{
                    if(closed||generation!=audioGeneration)return;
                    recording=false;overlay.voice(false,false);client.heartbeatSoon();
                    if(!error.isEmpty()||wav.length<=44){problem("没录到声音，请重试");return;}
                    try{Files.write(store.recording().toPath(),wav);}catch(Exception failure){problem("录音保存失败");return;}
                    transcribe(wav,generation);
                });
            }
        })){recording=false;overlay.voice(false,false);problem("麦克风暂时用不了，可以先打字。");}
    }
    private void transcribe(byte[] wav,int generation){
        transcribing=true;overlay.transcribing(true);problem("正在把刚才的话转成文字…");
        client.transcribe(wav,text->{
            if(closed||generation!=audioGeneration)return;
            transcribing=false;overlay.transcribing(false);speechRetry=false;store.recording().delete();
            String previous=overlay.text();overlay.text(previous+(previous.isEmpty()?"":"\n")+text);saveDraft();overlay.voice(false,false);problem="";status();
        },error->{if(closed||generation!=audioGeneration)return;transcribing=false;overlay.transcribing(false);speechRetry=true;overlay.voice(false,true);problem(error+" · 可点重试转写");});
    }
    @Override public void retryVoice(){try{transcribe(Files.readAllBytes(store.recording().toPath()),++audioGeneration);}catch(Exception error){problem("录音已清理，请重新录制");}}
    @Override public void cancelVoice(){audioGeneration++;recording=false;transcribing=false;speechRetry=false;recorder.stop();if(store!=null)store.recording().delete();if(overlay!=null){overlay.transcribing(false);overlay.voice(false,false);}if(client!=null)client.heartbeatSoon();}
    @Override public void pause(){if(!"paused".equals(captureState)&&!"unavailable".equals(captureState)){pauseLocal("");client.action("pause_capture",new JSONObject(),ignored->{});}else settings();}
    void prepareSetup(){if(overlay!=null)overlay.collapse(true);}
    private void openSetup(String mode){prepareSetup();startActivity(setup(this,mode));}
    @Override public void settings(){openSetup("settings");}
    @Override public void end(){cancelVoice();pauseLocal("");if(client!=null)client.end();else stopSelf();}
    @Override public void stopped(){shutdown();stopSelf();}
    @Override public void problem(String text){problem=text;connectionProblem=TogetherClient.CONNECTION_PROBLEM.equals(text);if(overlay!=null)overlay.status(text);else Toast.makeText(this,text,Toast.LENGTH_LONG).show();}
    @Override public String captureState(){return captureState;}
    @Override public boolean inputBusy(){return recording||overlay!=null&&overlay.editing();}
    @Override public void offline(){pauseLocal("暂时没连上，画面先暂停。写好的话会留着。");connectionProblem=true;}
    @Override public void connected(){if(connectionProblem){connectionProblem=false;problem="";status();}}
    @Override public void maintenance(){
        // Unlock broadcasts may be missed on OEM builds. Reconcile visibility
        // with the actual keyguard state; this never resumes a picture source.
        refreshVisibility();
        if(client!=null&&!draftCapture.isEmpty()&&draftExpiry>0&&client.serverNow()>=draftExpiry){removeImage();problem("这张图需要重新选，写好的话还在。");}
    }
    @Override public void preview(Surface surface){preview=surface;media.post(()->{if(camera!=null)camera.preview(surface);});}
    @Override public void retry(String messageId){
        if(messageId.startsWith("outbox:")){
            String id=messageId.substring(7);org.json.JSONArray queue=store.outbox();
            for(int i=0;i<queue.length();i++){
                JSONObject row=queue.optJSONObject(i);if(row!=null&&id.equals(row.optString("client_id"))){
                    String previous=overlay.text();overlay.text(previous+(previous.isEmpty()?"":"\n")+row.optString("text"));
                    saveDraft();store.acknowledge(id);problem("写好的话已放回输入框，图片需要重新选。");client.refresh();return;
                }
            }
        }else client.action("retry",TogetherApi.object("message_id",messageId),ignored->{});
    }
    private void shutdown(){
        if(closed)return;closed=true;audioGeneration++;mediaGeneration++;recorder.stop();
        main.removeCallbacks(saveDraftLater);
        if(client!=null)client.close();if(avatar!=null)avatar.close();if(overlay!=null)overlay.close();
        if(TogetherRoomActivity.current!=null)TogetherRoomActivity.current.finish();
        media.post(()->{if(screen!=null)screen.close();if(camera!=null)camera.close();mediaThread.quitSafely();});
        if(thumbnail!=null){thumbnail.recycle();thumbnail=null;}
        stopForeground(true);
    }
    @Override public void onDestroy(){shutdown();try{unregisterReceiver(screenReceiver);}catch(Exception ignored){}if(instance==this)instance=null;super.onDestroy();}
    @Override public void onConfigurationChanged(android.content.res.Configuration configuration){
        super.onConfigurationChanged(configuration);if(overlay!=null)overlay.relayout();
        if(camera!=null&&overlay!=null)overlay.previewRotation(camera.rotation());
    }
    @Override public IBinder onBind(Intent intent){return null;}
}
