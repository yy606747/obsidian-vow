package app.obsidianvow.core;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

/** Hosts the service's existing view: no second client, composer, recorder or queue. */
public final class TogetherRoomActivity extends Activity {
    static TogetherRoomActivity current;
    private final Handler main=new Handler(Looper.getMainLooper());
    private FrameLayout body;
    private LinearLayout recovery;
    private TogetherMediaService service;
    private boolean restoring,resumed;
    private final Runnable connect=new Runnable(){public void run(){bind();}};

    @Override public void onCreate(Bundle state){
        super.onCreate(state);current=this;
        getWindow().setStatusBarColor(TogetherChrome.INK);getWindow().setNavigationBarColor(TogetherChrome.INK);
        getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
        LinearLayout page=new LinearLayout(this);page.setOrientation(LinearLayout.VERTICAL);page.setBackgroundColor(TogetherChrome.INK);
        LinearLayout bar=new LinearLayout(this);bar.setGravity(Gravity.CENTER_VERTICAL);bar.setPadding(dp(18),0,dp(18),0);
        TextView back=label("‹ 日常聊天",14,TogetherChrome.GOLD);back.setGravity(Gravity.CENTER_VERTICAL);back.setOnClickListener(v->history());bar.addView(back,new LinearLayout.LayoutParams(0,dp(48),1));
        bar.addView(label("相伴",12,TogetherChrome.MUTED));page.addView(bar);
        body=new FrameLayout(this);page.addView(body,new LinearLayout.LayoutParams(-1,0,1));setContentView(page);
        recovery=new LinearLayout(this);recovery.setOrientation(LinearLayout.VERTICAL);recovery.setGravity(Gravity.CENTER);recovery.setPadding(dp(28),0,dp(28),0);
        TextView hint=label("正在接回刚才的聊天。\n写好的话还在，画面可以稍后再打开。",15,TogetherChrome.TEXT);hint.setGravity(Gravity.CENTER);hint.setLineSpacing(dp(8),1);recovery.addView(hint);
        TextView retry=label("连接设置 / 结束陪伴",14,TogetherChrome.GOLD);retry.setPadding(dp(16),dp(24),dp(16),dp(24));retry.setOnClickListener(v->restore());recovery.addView(retry);
        body.addView(recovery,new FrameLayout.LayoutParams(-1,-1));
    }
    private int dp(int value){return TogetherChrome.dp(this,value);}
    private TextView label(String value,int size,int color){TextView text=new TextView(this);text.setText(value);text.setTextSize(size);text.setTextColor(color);return text;}
    @Override protected void onResume(){super.onResume();resumed=true;bind();}
    private void bind(){
        if(!resumed||isFinishing())return;
        TogetherMediaService active=TogetherMediaService.instance;
        if(active!=null&&active.canShowRoom()){
            recovery.setVisibility(View.GONE);service=active;active.attachPage(body);restoring=false;return;
        }
        recovery.setVisibility(View.VISIBLE);
        TogetherStore store=new TogetherStore(this);
        if(!store.end().optString("session_id").isEmpty()||store.state().optString("id").isEmpty()){finish();return;}
        if(!restoring){
            restore();
        }else main.postDelayed(connect,500);
    }
    private void restore(){restoring=true;startActivity(new Intent(this,TogetherActivity.class).putExtra("mode","room_resume"));}
    private void history(){TogetherUi.browseHistory();finish();}
    @Override public void onBackPressed(){history();}
    @Override protected void onStop(){
        resumed=false;main.removeCallbacks(connect);
        if(service!=null){service.detachPage(body);service=null;}
        super.onStop();
    }
    @Override protected void onDestroy(){main.removeCallbacks(connect);if(current==this)current=null;super.onDestroy();}
}
