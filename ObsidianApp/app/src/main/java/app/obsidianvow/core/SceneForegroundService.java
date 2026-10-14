package app.obsidianvow.core;

import android.app.*;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.*;
import android.net.Uri;

/** One owner-initiated scene: native polling, looping and notification stop, without a WebView. */
public final class SceneForegroundService extends Service {
    private static final String CHANNEL="obsidian_scene";
    private static final int NOTICE=9006;
    private SceneDeviceBridge bridge;
    private HandlerThread thread;
    private Handler handler;
    private PowerManager.WakeLock wakeLock;
    private long wakeRenew, activatedGeneration=-1;
    private String noticeKey="", activatedId="";
    private volatile boolean destroyed;
    private volatile int latestStart;
    @Override public void onCreate() {
        super.onCreate();bridge=SceneDeviceBridge.obtain(this);
        getSystemService(NotificationManager.class).createNotificationChannel(new NotificationChannel(CHANNEL,"小剧场运行",NotificationManager.IMPORTANCE_LOW));
        thread=new HandlerThread("ScenePlayback");thread.start();handler=new Handler(thread.getLooper());
        wakeLock=getSystemService(PowerManager.class).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK,"ObsidianVow:ScenePlayback");
        wakeLock.setReferenceCounted(false);
    }
    @Override public int onStartCommand(Intent intent,int flags,int startId) {
        latestStart=startId;
        activatedId=bridge.notificationId();activatedGeneration=bridge.notificationGeneration();
        try {
            Notification notification=notification();
            if(Build.VERSION.SDK_INT>=Build.VERSION_CODES.R) startForeground(NOTICE,notification,ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE);
            else startForeground(NOTICE,notification);
            if(intent!=null&&intent.getAction()!=null) bridge.notificationAction(intent.getStringExtra("session"),intent.getLongExtra("generation",-1),intent.getAction());
            // Serialize rescheduling with the running tick, avoiding duplicate timer chains.
            handler.post(() -> { handler.removeCallbacks(tick);handler.post(tick); });
        } catch(RuntimeException error) { bridge.serviceDestroyed(activatedId,activatedGeneration);stopSelf(startId); }
        return START_NOT_STICKY;
    }
    private final Runnable tick=new Runnable() {
        @Override public void run() {
            if(destroyed) return;
            int start=latestStart;
            bridge.pulse();
            if(!bridge.hasWork()) { stopSelfResult(start);return; }
            long now=SystemClock.elapsedRealtime();
            if(SceneDeviceBridge.active()) {
                if(!wakeLock.isHeld()||now-wakeRenew>240000) { wakeLock.acquire(300000);wakeRenew=now; }
            } else if(wakeLock.isHeld()) wakeLock.release();
            String key=bridge.notificationId()+":"+bridge.notificationGeneration();
            if(!key.equals(noticeKey)) { noticeKey=key;getSystemService(NotificationManager.class).notify(NOTICE,notification()); }
            handler.postDelayed(this,50);
        }
    };
    private Notification notification() {
        String id=bridge.notificationId();long generation=bridge.notificationGeneration();
        Intent open=new Intent(this,LauncherActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK|Intent.FLAG_ACTIVITY_SINGLE_TOP);
        Notification.Builder builder=new Notification.Builder(this,CHANNEL).setSmallIcon(android.R.drawable.ic_media_play)
                .setContentTitle(id.isEmpty()?"小剧场已暂停":"小剧场正在进行")
                .setContentText(id.isEmpty()?"正在同步停止状态":"切换应用或锁屏后仍会继续")
                .setOngoing(true).setOnlyAlertOnce(true)
                .setContentIntent(PendingIntent.getActivity(this,9060,open,PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE));
        if(!id.isEmpty()) for(String action:new String[]{"pause","end"}) {
            Intent intent=new Intent(this,SceneForegroundService.class).setAction(action)
                    .setData(Uri.parse("obsidianvow://scene/"+id+"/"+generation+"/"+action))
                    .putExtra("session",id).putExtra("generation",generation);
            PendingIntent pi=PendingIntent.getService(this,action.equals("pause")?9061:9062,intent,PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
            builder.addAction(new Notification.Action.Builder(null,action.equals("pause")?"暂停":"结束",pi).build());
        }
        return builder.build();
    }
    @Override public void onDestroy() {
        destroyed=true;bridge.serviceDestroyed(activatedId,activatedGeneration);handler.removeCallbacksAndMessages(null);thread.quitSafely();
        if(wakeLock.isHeld()) wakeLock.release();stopForeground(STOP_FOREGROUND_REMOVE);super.onDestroy();
    }
    @Override public IBinder onBind(Intent intent) { return null; }
}
