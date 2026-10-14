package app.obsidianvow.core;

import android.content.Context;
import android.graphics.PixelFormat;
import android.view.Gravity;
import android.view.LayoutInflater;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

final class LookAtMeOverlay {
    interface Decision { void decide(boolean accept, String reason); }
    private final Context context;
    private final WindowManager windows;
    private View view;
    private TextView timer;
    private EditText reason;
    private String draft = "", requestId = "";

    LookAtMeOverlay(Context context) { this.context=context; windows=context.getSystemService(WindowManager.class); }
    boolean showing() { return view != null; }
    int dp(int v) { return (int)(v*context.getResources().getDisplayMetrics().density); }
    static String duration(long seconds) {
        long minutes=seconds/60, rest=seconds%60;
        return (minutes>0 ? minutes+"分钟" : "") + (rest>0 || minutes==0 ? rest+"秒" : "");
    }

    void invite(LookAtMeSession s, Decision decision, Runnable shown) {
        hide();
        if(!requestId.equals(s.id)) { draft="";requestId=s.id; }
        View card=LayoutInflater.from(context).inflate(R.layout.dialog_look_at_me,null);
        ((TextView)card.findViewById(R.id.lookName)).setText(s.aiName+" 想和你待一会儿");
        ((TextView)card.findViewById(R.id.lookExpression)).setText(s.expression);
        ((TextView)card.findViewById(R.id.lookDuration)).setText("这段时间 · "+duration(s.seconds));
        reason=card.findViewById(R.id.lookReason); reason.setText(draft);
        timer=card.findViewById(R.id.lookCountdown);
        card.findViewById(R.id.lookAccept).setOnClickListener(v -> decision.decide(true,""));
        card.findViewById(R.id.lookReject).setOnClickListener(v -> decision.decide(false,reason.getText().toString()));
        WindowManager.LayoutParams p=new WindowManager.LayoutParams(
            Math.min(dp(440),context.getResources().getDisplayMetrics().widthPixels-dp(32)),
            WindowManager.LayoutParams.WRAP_CONTENT,WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,PixelFormat.TRANSLUCENT);
        p.gravity=Gravity.CENTER; p.softInputMode=WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE;
        card.getViewTreeObserver().addOnWindowFocusChangeListener(new android.view.ViewTreeObserver.OnWindowFocusChangeListener() {
            boolean reported;
            @Override public void onWindowFocusChanged(boolean hasFocus) {
                if(hasFocus && !reported && view==card) {reported=true;shown.run();}
            }
        });
        windows.addView(card,p); view=card;
    }

    void residence(Runnable end) {
        hide(); draft="";
        LinearLayout bar=new LinearLayout(context); bar.setGravity(Gravity.CENTER_VERTICAL);bar.setPadding(dp(12),dp(4),dp(6),dp(4));
        bar.setBackgroundResource(R.drawable.screen_capture_card_bg);
        timer=new TextView(context);timer.setTextColor(0xfff3e7d5);timer.setTextSize(13);
        bar.addView(timer);
        Button button=new Button(context);button.setText("立即结束");button.setTextColor(0xffd4943a);
        button.setBackgroundColor(android.graphics.Color.TRANSPARENT);button.setOnClickListener(v->end.run());bar.addView(button);
        WindowManager.LayoutParams p=new WindowManager.LayoutParams(-2,-2,
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
            |WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,PixelFormat.TRANSLUCENT);
        p.gravity=Gravity.TOP|Gravity.CENTER_HORIZONTAL;p.y=dp(40);
        windows.addView(bar,p);view=bar;
    }

    void update(long remaining, boolean active) {
        if(timer!=null) timer.setText((active?"◆ 邀约 · ":"邀请剩余 ")+duration(Math.max(0,(remaining+999)/1000)));
    }

    void hide() {
        if(reason!=null) draft=reason.getText().toString();
        reason=null;timer=null;
        if(view!=null) { try{windows.removeViewImmediate(view);}catch(Exception ignored){} view=null; }
    }
}
