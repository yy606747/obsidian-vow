package app.obsidianvow.core;

import android.app.Activity;
import android.app.Application;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;

/** App foreground owns the full page; background owns the single floating view. */
final class TogetherUi implements Application.ActivityLifecycleCallbacks {
    private static int started;
    private static boolean browsingHistory;
    private static boolean flushingEnd;
    private static final Handler main=new Handler(Looper.getMainLooper());
    static boolean foreground(){return started>0;}
    static void browseHistory(){browsingHistory=true;}
    static void open(Activity activity){
        browsingHistory=false;
        if(!new TogetherStore(activity).end().optString("session_id").isEmpty()){
            activity.startActivity(new Intent(activity,TogetherActivity.class).putExtra("mode","room_resume"));return;
        }
        activity.startActivity(new Intent(activity,TogetherRoomActivity.class).addFlags(Intent.FLAG_ACTIVITY_REORDER_TO_FRONT));
    }
    @Override public void onActivityStarted(Activity activity){started++;sync();}
    @Override public void onActivityResumed(Activity activity){
        if(activity instanceof WebViewActivity&&!activity.isFinishing()){
            TogetherStore store=new TogetherStore(activity);
            if(!store.end().optString("session_id").isEmpty()){
                if(!flushingEnd){
                    flushingEnd=true;
                    android.content.Context app=activity.getApplicationContext();
                    new Thread(()->{try{TogetherClient.flushEnd(new TogetherApi(app),store);}catch(Exception ignored){}finally{main.post(()->flushingEnd=false);}},"TogetherEndResume").start();
                }
            }else if(!browsingHistory&&!store.state().optString("id").isEmpty())open(activity);
        }
        sync();
    }
    @Override public void onActivityStopped(Activity activity){
        started=Math.max(0,started-1);
        main.postDelayed(()->{if(started==0)browsingHistory=false;sync();},250);
    }
    private static void sync(){if(TogetherMediaService.instance!=null)TogetherMediaService.instance.refreshVisibility();}
    @Override public void onActivityCreated(Activity activity,Bundle state){}
    @Override public void onActivityPaused(Activity activity){}
    @Override public void onActivitySaveInstanceState(Activity activity,Bundle state){}
    @Override public void onActivityDestroyed(Activity activity){}
}
