package app.obsidianvow.core;

import android.app.Activity;
import android.graphics.*;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.view.*;
import android.widget.*;
import org.json.JSONArray;
import org.json.JSONObject;

/** Debug-only local UI gallery. No networking, recording, camera or session writes. */
public final class TogetherPreviewActivity extends Activity implements TogetherOverlay.Actions {
    private TogetherOverlay overlay;
    private final JSONArray messages=new JSONArray();
    private Bitmap picture;
    private boolean recording,paused;
    private String source="screen";
    private String aiName,userName;
    private TextView result;
    private final Handler main=new Handler(Looper.getMainLooper());
    private long nextMessage;
    @Override public void onCreate(Bundle saved){
        super.onCreate(saved);aiName=getIntent().getStringExtra("ai_name");userName=getIntent().getStringExtra("user_name");
        if(aiName==null)aiName="Alaric";if(userName==null)userName="Yang";
        getWindow().setStatusBarColor(0xFFEDE7DE);getWindow().setNavigationBarColor(0xFFEDE7DE);getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR|View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
        LinearLayout page=new LinearLayout(this);page.setOrientation(LinearLayout.VERTICAL);page.setPadding(dp(26),dp(25),dp(26),dp(22));page.setBackgroundColor(0xFFEDE7DE);
        TextView kicker=label("OBSIDIAN VOW",11,0xFF8D7859);kicker.setLetterSpacing(.18f);page.addView(kicker);
        TextView heading=label("相伴 · 排版预览",23,0xFF35342E);heading.setPadding(0,dp(12),0,dp(6));page.addView(heading);
        TextView sub=label("固定小窗 · 本地预览",12,0xFF928576);page.addView(sub);
        View space=new View(this);page.addView(space,new LinearLayout.LayoutParams(-1,0,1));
        result=label("拖动名字移动，点头像展开。",12,0xFF776C5E);result.setPadding(0,0,0,dp(16));page.addView(result);
        LinearLayout controls=new LinearLayout(this);controls.setGravity(Gravity.CENTER_VERTICAL);
        option(controls,"对话",()->reset());option(controls,"长对话",this::longChat);option(controls,"图文",()->{reset();look();overlay.text("看，今天的晚霞。");});option(controls,"语音",()->{overlay.collapse(false);record();});page.addView(controls);
        LinearLayout secondary=new LinearLayout(this);secondary.setPadding(0,dp(8),0,0);
        option(secondary,"收起",()->overlay.collapse(true));option(secondary,"新消息",this::newReply);option(secondary,"关闭预览",this::end);page.addView(secondary);
        LinearLayout markdown=new LinearLayout(this);markdown.setPadding(0,dp(8),0,0);
        option(markdown,"段落",this::paragraphs);option(markdown,"引用 / 列表",this::richText);option(markdown,"代码 / 表格",this::technical);option(markdown,"流式",this::streaming);page.addView(markdown);
        TextView foot=label("示例对话 · 不录音、不取图、不调用模型",10,0xFF928576);foot.setPadding(0,dp(14),0,0);page.addView(foot);setContentView(page);
        if(!Settings.canDrawOverlays(this)){result.setText("需要已授予的悬浮窗权限才能预览。");return;}
        if(TogetherMediaService.instance!=null){result.setText("请先结束正在进行的陪伴，再预览界面。");return;}
        page.post(()->{if(isFinishing())return;overlay=new TogetherOverlay(this,this);overlay.show();reset();});
    }
    private int dp(int value){return TogetherChrome.dp(this,value);}
    private TextView label(String text,int size,int color){TextView view=new TextView(this);view.setText(text);view.setTextSize(size);view.setTextColor(color);view.setIncludeFontPadding(false);return view;}
    private void option(LinearLayout row,String text,Runnable action){TextView view=label(text,12,0xFF615644);view.setGravity(Gravity.CENTER);view.setBackground(TogetherChrome.touch(this,0xFFF6F1E9,12,0xFFDAD0C1));view.setOnClickListener(v->{if(overlay!=null)action.run();});LinearLayout.LayoutParams p=new LinearLayout.LayoutParams(0,dp(40),1);p.rightMargin=dp(6);row.addView(view,p);}
    private JSONObject object(Object... values){return TogetherApi.object(values);}
    private void add(String role,String text,boolean image){messages.put(object("id","preview_"+(++nextMessage),"role",role,"content",text,"status","complete","capture_id",image?"preview_image":JSONObject.NULL));}
    private void refresh(){if(overlay==null)return;overlay.render(object("ai_name",aiName,"user_name",userName,"image_supported",true,"source",source,"capture_state",paused?"paused":"ready","messages",messages));overlay.presence(728,true,paused?"paused":"ready",source);overlay.outbox(new JSONArray());}
    private void reset(){
        main.removeCallbacksAndMessages(null);while(messages.length()>0)messages.remove(messages.length()-1);paused=false;source="screen";cancelVoice();removeImage();overlay.text("");overlay.delta("");overlay.collapse(false);
        add("user","下课了，陪我走一会儿吧。",false);add("assistant","好啊，**慢慢走**。\n\n想吃什么？我们路上慢慢挑。",false);refresh();result.setText("空行分成小气泡；长按任意一段，复制整条原文。");
    }
    private void paragraphs(){reset();result.setText("普通换行留在同一气泡；空行另起一段。窗口保持固定大小。");}
    private void richText(){reset();add("assistant","你刚才说：\n\n> 下课之后，想在外面多待一会儿。\n\n那我们先不急着回去。\n\n- 走有树荫的那一侧\n- 想喝东西了就停一下\n\n*今天的风应该挺舒服。*",false);refresh();result.setText("引用和列表保留结构，粗斜体直接显示，不露出 Markdown 符号。");}
    private void technical(){reset();add("assistant","### 放在这里的小记录\n\n```text\n今晚的安排：散步 → 买杯热茶 → 沿着河边慢慢走回家\n```\n\n| 时刻 | 地点 | 记一下 |\n| --- | --- | --- |\n| 傍晚 | 街角的咖啡店 | 隔着玻璃看见一只猫 |",false);refresh();result.setText("代码和宽表格可以横向滑动，不会撑宽窗口。");}
    private void streaming(){
        reset();String text="嗯，**我听着呢**。\n\n不用把事情都整理好再说。\n今天想到哪里，就聊到哪里。";
        for(int i=1;i<=text.length();i++){final int end=i;main.postDelayed(()->{if(overlay!=null)overlay.delta(text.substring(0,end));},i*75L);}
        main.postDelayed(()->{if(overlay==null)return;overlay.delta("");add("assistant",text,false);refresh();},text.length()*75L+150);
        result.setText("边输出边排版；可以往上翻，新的文字不会抢走阅读位置。");
    }
    private void longChat(){reset();for(int i=1;i<=8;i++){add("user","第 "+i+" 段：今天路上看到了不少有意思的东西。",false);add("assistant","那就慢慢看。\n这段话稍微长一点，试试往上翻之前的消息。\n窗口会保持原来的大小。",false);}refresh();result.setText("在聊天区上下滑动，整个框保持固定大小。");}
    private void newReply(){
        overlay.collapse(true);result.setText("稍后出现两行新消息提示；六秒后收回，留下未读点。");
        main.postDelayed(()->{if(overlay==null)return;overlay.delta("刚才那张图");},350);
        main.postDelayed(()->{if(overlay==null)return;overlay.delta("");add("assistant","刚才那张图我**还挺喜欢的**。\n\n走到哪儿了？",false);refresh();},1200);
    }
    @Override public void send(String text){if(text.trim().isEmpty()&&picture==null)return;add("user",text,picture!=null);overlay.text("");removeImage();overlay.blur();add("assistant","好，我陪着。",false);refresh();result.setText("已验证发送按钮（仅本地示例）。");}
    @Override public void look(){
        if(picture!=null)picture.recycle();picture=Bitmap.createBitmap(240,180,Bitmap.Config.ARGB_8888);Canvas c=new Canvas(picture);Paint p=new Paint(Paint.ANTI_ALIAS_FLAG);p.setShader(new LinearGradient(0,0,0,180,new int[]{0xFF768D96,0xFFE4BE97,0xFFDC9E7C},null,Shader.TileMode.CLAMP));c.drawRect(0,0,240,180,p);p.setShader(null);p.setColor(0xFFF6DDB2);c.drawCircle(172,92,19,p);p.setColor(0xFF59675E);Path hills=new Path();hills.moveTo(0,150);hills.cubicTo(50,110,90,175,140,133);hills.cubicTo(190,92,212,151,240,131);hills.lineTo(240,180);hills.lineTo(0,180);hills.close();c.drawPath(hills,p);overlay.image(picture,"这张画面会和文字一起发送");result.setText("图片为本地绘制的示例，没有开启采集。");
    }
    @Override public void record(){recording=!recording;overlay.voice(recording,false);if(!recording){overlay.text("这里是可编辑的语音转写示例。");result.setText("没有实际录音；转写文字可修改再发送。");}}
    @Override public void cancelVoice(){recording=false;if(overlay!=null){overlay.voice(false,false);overlay.transcribing(false);}}
    @Override public void retryVoice(){record();}
    @Override public void pause(){paused=!paused;refresh();}
    @Override public void settings(){source=source.equals("screen")?"camera":"screen";refresh();result.setText("切换了预览样式；没有开启相机或屏幕采集。");}
    @Override public void end(){main.removeCallbacksAndMessages(null);if(overlay!=null){overlay.close();overlay=null;}finish();}
    @Override public void draft(String text,boolean editing){}
    @Override public void removeImage(){if(overlay!=null)overlay.image(null,"");if(picture!=null){picture.recycle();picture=null;}}
    @Override public void preview(Surface surface){}
    @Override public void retry(String id){}
    @Override public void onBackPressed(){end();}
    @Override protected void onDestroy(){main.removeCallbacksAndMessages(null);if(overlay!=null){overlay.close();overlay=null;}if(picture!=null)picture.recycle();super.onDestroy();}
}
