package app.obsidianvow.core;

import android.Manifest;
import android.app.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.media.projection.MediaProjectionConfig;
import android.media.projection.MediaProjectionManager;
import android.net.Uri;
import android.os.*;
import android.provider.Settings;
import android.content.res.ColorStateList;
import android.graphics.BitmapFactory;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.text.TextUtils;
import android.widget.*;
import android.view.*;
import androidx.core.content.ContextCompat;
import org.json.JSONArray;
import org.json.JSONObject;
import java.util.ArrayList;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import static app.obsidianvow.core.TogetherChrome.*;

/** Visible permission handoff for overlay, projection and while-in-use media types. */
public class TogetherActivity extends Activity {
    private static final int OVERLAY=7310, CAMERA=7311, MIC=7312, PROJECTION=7313;
    private final ExecutorService io=Executors.newSingleThreadExecutor();
    private final Handler main=new Handler(Looper.getMainLooper());
    private TogetherApi api;
    private TogetherStore store;
    private JSONObject config,current;
    private Spinner model;
    private SeekBar history;
    private TextView status,historyLabel,partner,choiceHint,settingsToggle,usageLabel,voiceLabel;
    private Button start,end;
    private Switch enabled;
    private LinearLayout details;
    private ScrollView scroll;
    private TogetherAvatar avatar;
    private TogetherChrome.Seal portrait;
    private final TextView[] choices=new TextView[3];
    private int choice;
    private final ArrayList<String> models=new ArrayList<>();
    private boolean loaded,working;
    private boolean activateSource,pauseCapture;
    private String wantedSource="screen",mode,requestId;
    private int selectedHistory;
    private String selectedModel;

    @Override public void onCreate(Bundle state){
        super.onCreate(state);mode=getIntent().getStringExtra("mode");requestId=state==null?UUID.randomUUID().toString():state.getString("request_id");
        if(TogetherMediaService.instance!=null)TogetherMediaService.instance.prepareSetup();
        buildPage();
        try{api=new TogetherApi(this);store=new TogetherStore(this);}catch(Exception error){notice(error.getMessage());return;}
        avatar=new TogetherAvatar(this,main,portrait::avatar);avatar.start();
        end.setVisibility(store.state().optString("id").isEmpty()?View.GONE:View.VISIBLE);
        io.execute(()->{
            try{
                TogetherClient.flushEnd(api,store);
                if(!store.end().optString("session_id").isEmpty())throw new IllegalStateException("上次已经结束，正在等网络恢复后同步。稍后再来就好。");
                JSONObject cfg=api.get("/api/together/config"), session=api.get("/api/together/sessions/current");
                main.post(()->loaded(cfg,session));
            }catch(Exception error){main.post(()->notice(error instanceof IllegalStateException?error.getMessage():TogetherClient.readable(error)));}
        });
    }
    private int dp(float value){return TogetherChrome.dp(this,value);}
    private TextView label(String text,int size){TextView view=new TextView(this);view.setText(text);view.setTextSize(size);view.setTextColor(TEXT);view.setIncludeFontPadding(false);return view;}
    private LinearLayout column(){LinearLayout view=new LinearLayout(this);view.setOrientation(LinearLayout.VERTICAL);return view;}
    private LinearLayout.LayoutParams box(int width,int height,int top){LinearLayout.LayoutParams p=new LinearLayout.LayoutParams(width<0?width:dp(width),height<0?height:dp(height));p.topMargin=dp(top);return p;}
    private Button button(String text,boolean primary){
        Button view=new Button(this);view.setText(text);view.setTextSize(15);view.setAllCaps(false);view.setTextColor(primary?INK:ROSE);view.setStateListAnimator(null);view.setBackgroundTintList(null);view.setPadding(dp(14),0,dp(14),0);
        if(primary){GradientDrawable fill=new GradientDrawable(GradientDrawable.Orientation.TL_BR,new int[]{0xFFF0D7A5,0xFFD4B780,0xFFE3C894});fill.setCornerRadius(dp(17));fill.setStroke(dp(1),0xFFF3DEB4);view.setBackground(new RippleDrawable(ColorStateList.valueOf(0x33715A30),fill,shape(this,android.graphics.Color.WHITE,17,0)));}
        else view.setBackground(touch(this,0x103C2539,14,0));
        return view;
    }
    private void buildPage(){
        getWindow().setStatusBarColor(INK);getWindow().setNavigationBarColor(INK);
        scroll=new ScrollView(this);scroll.setFillViewport(true);scroll.setClipToPadding(false);scroll.setBackground(entryBackground(this));
        LinearLayout content=column();content.setPadding(dp(24),dp(12),dp(24),dp(22));scroll.addView(content);setContentView(scroll);
        TextView back=label("‹ 返回",14);back.setTextColor(GOLD);back.setGravity(Gravity.CENTER_VERTICAL);back.setOnClickListener(v->finish());content.addView(back,box(-1,40,0));
        LinearLayout hero=column();hero.setGravity(Gravity.CENTER_HORIZONTAL);
        portrait=new TogetherChrome.Seal(this);portrait.setContentDescription("陪伴头像");portrait.avatar(BitmapFactory.decodeResource(getResources(),R.drawable.together_avatar_default));hero.addView(portrait,box(100,100,0));
        partner=label("",14);partner.setTextColor(GOLD);partner.setGravity(Gravity.CENTER);partner.setMaxLines(2);partner.setEllipsize(TextUtils.TruncateAt.END);hero.addView(partner,box(-1,-2,15));
        TextView title=label("相伴",29);title.setLetterSpacing(.06f);title.setGravity(Gravity.CENTER);hero.addView(title,box(-1,-2,10));
        TextView subtitle=label("聊聊天，也看看眼前的东西。",13);subtitle.setGravity(Gravity.CENTER);subtitle.setTextColor(MUTED);hero.addView(subtitle,box(-1,-2,17));
        content.addView(hero,box(-1,-2,34));
        View space=new View(this);space.setMinimumHeight(dp(38));content.addView(space,new LinearLayout.LayoutParams(-1,0,1));
        LinearLayout options=new LinearLayout(this);
        String[] labels={"先聊聊","看屏幕","看身边"},icons={"chat","screen","camera"};
        for(int i=0;i<3;i++){
            final int index=i;TextView option=label(labels[i],13);option.setGravity(Gravity.CENTER);option.setPadding(dp(4),dp(16),dp(4),dp(14));option.setCompoundDrawablePadding(dp(10));
            Icon icon=new Icon(icons[i],GOLD);icon.setBounds(0,0,dp(23),dp(23));option.setCompoundDrawables(null,icon,null,null);option.setOnClickListener(v->{if(!working)choose(index);});
            LinearLayout.LayoutParams p=new LinearLayout.LayoutParams(0,dp(92),1);if(i>0)p.leftMargin=dp(9);options.addView(option,p);choices[i]=option;
        }
        content.addView(options,box(-1,92,0));
        choiceHint=label("",12);choiceHint.setTextColor(MUTED);choiceHint.setGravity(Gravity.CENTER);choiceHint.setMinHeight(dp(44));choiceHint.setLineSpacing(dp(3),1);content.addView(choiceHint,box(-1,-2,9));choose(0);
        status=label("正在准备…",12);status.setTextColor(GOLD);status.setGravity(Gravity.CENTER);status.setLineSpacing(dp(3),1);content.addView(status,box(-1,-2,8));
        start=button("一起待会儿",true);start.setContentDescription("开始一起待会儿");start.setEnabled(false);start.setOnClickListener(v->begin());content.addView(start,box(-1,52,14));
        settingsToggle=label("聊天设置  ⌄",12);settingsToggle.setContentDescription("聊天设置");settingsToggle.setTextColor(MUTED);settingsToggle.setGravity(Gravity.CENTER);settingsToggle.setOnClickListener(v->showDetails(details.getVisibility()!=View.VISIBLE));content.addView(settingsToggle,box(-1,48,4));
        details=column();details.setPadding(dp(17),dp(16),dp(17),dp(18));details.setBackground(shape(this,0xAA1B2936,18,0x555C675F));details.setVisibility(View.GONE);content.addView(details,box(-1,-2,0));
        enabled=new Switch(this);enabled.setText("启用相伴");enabled.setTextSize(13);enabled.setTextColor(TEXT);enabled.setThumbTintList(ColorStateList.valueOf(GOLD));enabled.setTrackTintList(ColorStateList.valueOf(0xFF52645F));details.addView(enabled,box(-1,40,0));
        historyLabel=label("",13);details.addView(historyLabel,box(-1,-2,16));
        history=new SeekBar(this);history.setMax(100);history.setThumbTintList(ColorStateList.valueOf(GOLD));history.setProgressTintList(ColorStateList.valueOf(GOLD));history.setProgress(Math.max(0,Math.min(100,getIntent().getIntExtra("history_count",30))));details.addView(history,box(-1,40,4));
        history.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener(){public void onProgressChanged(SeekBar s,int progress,boolean user){historyText();}public void onStartTrackingTouch(SeekBar s){}public void onStopTrackingTouch(SeekBar s){}});historyText();
        TextView modelLabel=label("聊天模型",13);details.addView(modelLabel,box(-1,-2,12));model=new Spinner(this,Spinner.MODE_DROPDOWN);model.setBackground(touch(this,0xFF233542,12,0x446A624F));model.setPopupBackgroundDrawable(shape(this,0xFF233542,12,LINE));details.addView(model,box(-1,46,9));
        voiceLabel=label("",12);voiceLabel.setTextColor(MUTED);voiceLabel.setLineSpacing(dp(3),1);details.addView(voiceLabel,box(-1,-2,16));
        usageLabel=label("",12);usageLabel.setTextColor(MUTED);usageLabel.setLineSpacing(dp(4),1);details.addView(usageLabel,box(-1,-2,12));
        end=button("结束这次陪伴",false);end.setVisibility(View.GONE);end.setOnClickListener(v->end());content.addView(end,box(-1,44,4));
    }
    private void historyText(){historyLabel.setText(current!=null?"接着刚才聊":history.getProgress()==0?"从这里开始，不带入之前的聊天":"接上最近 "+history.getProgress()+" 条聊天");}
    private void showDetails(boolean show){details.setVisibility(show?View.VISIBLE:View.GONE);settingsToggle.setText(show?"收起设置  ⌃":"聊天设置  ⌄");settingsToggle.setContentDescription(show?"收起设置":"聊天设置");if(show)scroll.post(()->scroll.smoothScrollTo(0,details.getBottom()));}
    private void choose(int value){
        choice=value;for(int i=0;i<choices.length;i++){boolean selected=i==value;choices[i].setSelected(selected);choices[i].setTextColor(selected?GOLD:MUTED);choices[i].setBackground(touch(this,selected?0xFF2A3C46:0x991B2936,16,selected?0xFFB49E74:0x555F6A70));choices[i].setAlpha(selected?1f:.78f);}
        if(choiceHint!=null)choiceHint.setText(value==0?"先聊聊，想分享画面时再打开。":value==1?"屏幕按需分享，随时可以暂停。":"通过后置摄像头分享身边，随时可以暂停。");
    }
    private void notice(String text){status.setText(text);status.setVisibility(text==null||text.isEmpty()?View.GONE:View.VISIBLE);}
    private void loaded(JSONObject cfg,JSONObject session){
        if(isFinishing()||isDestroyed())return;config=cfg;current=session;loaded=true;enabled.setChecked(cfg.optBoolean("enabled"));
        String name=cfg.optString("ai_name",current==null?"":current.optString("ai_name")).trim();
        partner.setText(name.isEmpty()?"":"和 "+name);partner.setVisibility(name.isEmpty()?View.GONE:View.VISIBLE);
        portrait.setContentDescription(name.isEmpty()?"陪伴头像":name+"的头像");
        JSONArray items=cfg.optJSONArray("models");ArrayList<String> labels=new ArrayList<>();
        if(items!=null)for(int i=0;i<items.length();i++){JSONObject item=items.optJSONObject(i);if(item==null)continue;models.add(item.optString("key"));labels.add(item.optString("key")+(item.optBoolean("image_supported")?" · 可看图":" · 文字"));}
        model.setAdapter(new ArrayAdapter<String>(this,android.R.layout.simple_spinner_dropdown_item,labels){
            private View row(int position,boolean dropdown){TextView text=label(getItem(position)+(dropdown?"":"  ⌄"),12);text.setTextColor(TEXT);text.setGravity(Gravity.CENTER_VERTICAL);text.setPadding(dp(12),dp(10),dp(12),dp(10));text.setMinHeight(dp(46));text.setSingleLine(true);text.setEllipsize(TextUtils.TruncateAt.END);return text;}
            @Override public View getView(int position,View recycled,ViewGroup parent){return row(position,false);}
            @Override public View getDropDownView(int position,View recycled,ViewGroup parent){return row(position,true);}
        });
        String key=current==null?getIntent().getStringExtra("model_key"):current.optString("model_key");
        int selected=models.indexOf(key);if(selected>=0)model.setSelection(selected);
        if(current!=null){
            history.setEnabled(false);history.setAlpha(.45f);historyText();
            choose("ready".equals(current.optString("capture_state"))?("camera".equals(current.optString("source"))?2:1):0);start.setText("回到相伴");start.setContentDescription("返回相伴");end.setVisibility(View.VISIBLE);
            if(!DeviceIdentity.id(this).equals(current.optString("device_id"))){notice("已有另一台设备正在陪伴");loaded=false;return;}
        }
        String usage="";JSONArray totals=current==null?null:current.optJSONArray("usage");
        if(totals!=null)for(int i=0;i<totals.length();i++){JSONObject row=totals.optJSONObject(i);if(row!=null)usage+=(usage.isEmpty()?"调用记录\n":"\n")+row.optString("kind")+"："+row.optInt("calls")+" 次\n输入 "+token(row,"input_tokens")+" · 输出 "+token(row,"output_tokens")+" · 思考 "+token(row,"reasoning_tokens");}
        usageLabel.setText(usage);usageLabel.setVisibility(usage.isEmpty()?View.GONE:View.VISIBLE);
        voiceLabel.setText(cfg.optBoolean("asr_ready")?"说完后会先转成文字，可以改好再发。":"语音输入还没设置，可以先打字。");
        notice("");if(!enabled.isChecked())showDetails(true);
        start.setEnabled(true);
        if("room_resume".equals(mode)){
            if(current==null){store.clear();finish();return;}
            activateSource=false;startNative(null,false,false);return;
        }
        if("microphone".equals(mode)&&TogetherMediaService.instance!=null){
            if(ContextCompat.checkSelfPermission(this,Manifest.permission.RECORD_AUDIO)==PackageManager.PERMISSION_GRANTED)grantMicrophone();
            else requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO},MIC);
        }
    }
    private String token(JSONObject row,String key){return row.isNull(key)?"未提供":row.optString(key,"未提供");}
    private boolean allowed(){
        if(!loaded||working)return false;
        if(!Settings.canDrawOverlays(this)){startActivityForResult(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,Uri.parse("package:"+getPackageName())),OVERLAY);return false;}
        if(!enabled.isChecked()){showDetails(true);notice("在聊天设置里开启后，就可以一起待会儿了。");return false;}
        if(models.isEmpty()){notice("请先配置一个主模型");return false;}
        return true;
    }
    private void begin(){if(!allowed())return;commit(choice!=0);}
    private void commit(boolean withSource){
        if(!allowed())return;working=true;start.setEnabled(false);
        activateSource=withSource;pauseCapture=!withSource;
        wantedSource=choice==2?"camera":choice==1?"screen":current==null?"screen":current.optString("source","screen");selectedHistory=history.getProgress();selectedModel=models.get(model.getSelectedItemPosition());
        io.execute(()->{
            try{
                if(!config.optBoolean("enabled")){
                    RequestBuilder.enable(api); // Owner explicitly selected the feature switch.
                }
                JSONObject next=current;
                if(next==null){
                    String conv=getIntent().getStringExtra("conv_id");if(conv==null||conv.isEmpty())throw new IllegalStateException("请从正在聊天的页面进入");
                    next=api.post("/api/together/sessions",TogetherApi.object("request_id",requestId,"conv_id",conv,"device_id",DeviceIdentity.id(this),"source",wantedSource,"history_count",selectedHistory,"model_key",selectedModel));
                }else{
                    String path="/api/together/sessions/"+next.optString("id")+"/actions";
                    if(!selectedModel.equals(next.optString("model_key")))next=api.post(path,TogetherApi.object("device_id",DeviceIdentity.id(this),"action","set_model","model_key",selectedModel));
                    if(withSource)next=api.post(path,TogetherApi.object("device_id",DeviceIdentity.id(this),"action",wantedSource.equals(next.optString("source"))?"resume_capture":"switch_source","source",wantedSource));
                    else if("ready".equals(next.optString("capture_state")))next=api.post(path,TogetherApi.object("device_id",DeviceIdentity.id(this),"action","pause_capture"));
                }
                final JSONObject result=next;
                main.post(()->{if(isFinishing()||isDestroyed())return;current=result;if(withSource)permissions();else startNative(null,false,false);});
            }catch(Exception error){TogetherApi.diagnose(this,"setup",error);main.post(()->{
                if(isFinishing()||isDestroyed())return;
                working=false;start.setEnabled(true);
                if("together_chat_busy".equals(error.getMessage())){
                    notice("等这句回完，就一起待会儿。");start.setEnabled(false);
                    main.postDelayed(()->{if(!isFinishing()&&!isDestroyed())commit(withSource);},1000);
                }else notice(error instanceof IllegalStateException?error.getMessage():TogetherClient.readable(error));
            });}
        });
    }
    private void permissions(){
        if(wantedSource.equals("camera")){
            if(ContextCompat.checkSelfPermission(this,Manifest.permission.CAMERA)!=PackageManager.PERMISSION_GRANTED)requestPermissions(new String[]{Manifest.permission.CAMERA},CAMERA);
            else startNative(null,true,false);
        }else{
            TogetherMediaService existing=TogetherMediaService.instance;
            if(existing!=null&&existing.hasScreen())startNative(null,false,false);
            else {
                MediaProjectionManager manager=getSystemService(MediaProjectionManager.class);
                Intent request=Build.VERSION.SDK_INT>=34?manager.createScreenCaptureIntent(MediaProjectionConfig.createConfigForDefaultDisplay()):manager.createScreenCaptureIntent();
                startActivityForResult(request,PROJECTION);
            }
        }
    }
    private void startNative(Intent projection,boolean camera,boolean microphone){
        Intent intent=new Intent(this,TogetherMediaService.class).putExtra("snapshot",current.toString())
            .putExtra("camera",camera).putExtra("microphone",microphone).putExtra("pause_capture",pauseCapture);
        if(projection!=null)intent.putExtra("projection",projection).putExtra("result_code",RESULT_OK);
        if(activateSource)intent.putExtra("source",wantedSource);
        intent.putExtra("receiver",new ResultReceiver(main){
            @Override protected void onReceiveResult(int code,Bundle data){working=false;if(code==1)finish();else{start.setEnabled(true);notice(data.getString("error","未能启动，请重试"));}}
        });
        ContextCompat.startForegroundService(this,intent);
    }
    private void grantMicrophone(){
        Intent intent=new Intent(this,TogetherMediaService.class).putExtra("microphone",true).putExtra("record",true)
            .putExtra("receiver",new ResultReceiver(main){@Override protected void onReceiveResult(int code,Bundle data){if(code==1)finish();else notice(data.getString("error"));}});
        ContextCompat.startForegroundService(this,intent);
    }
    @Override public void onRequestPermissionsResult(int request,String[] names,int[] results){
        super.onRequestPermissionsResult(request,names,results);
        boolean granted=results.length>0&&results[0]==PackageManager.PERMISSION_GRANTED;
        if(request==CAMERA){if(granted)startNative(null,true,false);else{activateSource=false;notice("相机还没打开，先继续聊天。");startNative(null,false,false);}}
        if(request==MIC){if(granted)grantMicrophone();else{notice("麦克风还没打开，可以先打字。");working=false;}}
    }
    @Override protected void onActivityResult(int request,int result,Intent data){
        super.onActivityResult(request,result,data);
        if(request==PROJECTION){if(result==RESULT_OK&&data!=null)startNative(data,false,false);else{activateSource=false;notice("屏幕还没共享，先继续聊天。");startNative(null,false,false);}}
        if(request==OVERLAY)notice(Settings.canDrawOverlays(this)?"已经可以显示小窗，继续就好。":"允许显示小窗后，切到别的应用也能聊天。");
    }
    private void end(){
        if(store==null){notice("还没连上，稍后再试。");return;}
        if(TogetherMediaService.instance!=null){TogetherMediaService.instance.end();finish();return;}
        JSONObject state=current==null?store.state():current;
        String sid=state.optString("id");if(sid.isEmpty()){notice("现在没有正在进行的陪伴。");return;}
        // No server-time anchor after process death: omit ended_at rather than invent it.
        store.markEnd(TogetherApi.object("session_id",sid,"device_id",DeviceIdentity.id(this),"action","end"));
        io.execute(()->TogetherClient.flushEnd(api,store));finish();
    }
    @Override protected void onSaveInstanceState(Bundle state){super.onSaveInstanceState(state);state.putString("request_id",requestId);}
    @Override protected void onDestroy(){if(avatar!=null)avatar.close();main.removeCallbacksAndMessages(null);io.shutdown();super.onDestroy();}
    private static final class RequestBuilder {
        static void enable(TogetherApi api) throws Exception {api.put("/api/together/config",TogetherApi.object("enabled",true));}
    }
}
