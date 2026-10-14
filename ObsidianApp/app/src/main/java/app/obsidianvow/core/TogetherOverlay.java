package app.obsidianvow.core;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.graphics.PixelFormat;
import android.graphics.Typeface;
import android.text.Editable;
import android.text.TextUtils;
import android.text.TextWatcher;
import android.view.*;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputMethodManager;
import android.widget.*;
import org.json.JSONArray;
import org.json.JSONObject;
import java.util.ArrayList;
import java.util.List;
import static app.obsidianvow.core.TogetherChrome.*;

/** Compact native controls with an offline Markdown transcript, independent of picture source. */
final class TogetherOverlay {
    interface Actions {
        void send(String text); void look(); void record(); void cancelVoice(); void retryVoice();
        void pause(); void settings(); void end(); void draft(String text,boolean editing);
        void removeImage(); void preview(Surface surface); void retry(String messageId);
    }
    private final Context context;
    private final WindowManager windows;
    private final Actions actions;
    private final FrameLayout root,conversation;
    private final LinearLayout panel,photoRow,cameraRow,menu;
    private final TextView title,subtitle,notice,sourceChip,photoLabel,look,voiceHint,pauseItem,newMessages;
    private final EditText input;
    private final ImageView photo;
    private final ImageButton record,send,cancelVoice,more,minimize;
    private final TogetherChrome.Seal seal,bubble;
    private final TextureView preview;
    private Surface previewSurface;
    private final TogetherTranscript transcript;
    private final WindowManager.LayoutParams params;
    private final TogetherReplies replies=new TogetherReplies();
    private final LinearLayout peek;
    private final TextView peekTitle,peekText;
    private final WindowManager.LayoutParams peekParams;
    private final Runnable dismissPeek=this::hidePeek;
    private boolean showing,collapsed,rendering,capturing,cameraVisible,recording,transcribing,retryVoice,hasImage;
    private boolean peekShowing,unread,dragging;
    private FrameLayout pageHost;
    private String aiName="",userName="",renderedMessages="",renderedOutbox="";
    private int cameraRotation,peekVersion;
    private float dragX,dragY;
    private int initialX,initialY;

    TogetherOverlay(Context context,Actions actions){
        this.context=context;this.actions=actions;windows=context.getSystemService(WindowManager.class);
        root=new FrameLayout(context);root.setPadding(dp(8),dp(8),dp(8),dp(8));root.setClipChildren(false);root.setFocusableInTouchMode(true);
        panel=column();panel.setPadding(dp(12),dp(10),dp(12),dp(10));panel.setBackground(card(context));panel.setClipToOutline(true);panel.setElevation(dp(7));root.addView(panel,new FrameLayout.LayoutParams(-1,-1));
        bubble=new TogetherChrome.Seal(context);bubble.setVisibility(View.GONE);root.addView(bubble,new FrameLayout.LayoutParams(-1,-1));

        LinearLayout header=row();seal=new TogetherChrome.Seal(context);seal.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO);header.addView(seal,new LinearLayout.LayoutParams(dp(34),dp(34)));
        LinearLayout identity=column();LinearLayout.LayoutParams identityParams=new LinearLayout.LayoutParams(0,-2,1);identityParams.leftMargin=dp(8);header.addView(identity,identityParams);
        title=label("相伴",17,TEXT);title.setTypeface(Typeface.create("serif",Typeface.NORMAL));title.setLetterSpacing(.025f);title.setSingleLine(true);title.setEllipsize(TextUtils.TruncateAt.END);identity.addView(title);
        subtitle=label("正在连接…",10,MUTED);subtitle.setSingleLine(true);subtitle.setEllipsize(TextUtils.TruncateAt.END);LinearLayout.LayoutParams subParams=new LinearLayout.LayoutParams(-1,-2);subParams.topMargin=dp(3);identity.addView(subtitle,subParams);
        minimize=icon("collapse","收起",MUTED,()->collapse(true));header.addView(minimize,new LinearLayout.LayoutParams(dp(36),dp(40)));
        TextView end=label("结束",11.5f,ROSE);end.setGravity(Gravity.CENTER);end.setContentDescription("结束陪伴");end.setBackground(touch(context,0x223B2435,10,0));end.setOnClickListener(v->actions.end());header.addView(end,new LinearLayout.LayoutParams(dp(38),dp(40)));panel.addView(header,new LinearLayout.LayoutParams(-1,dp(44)));

        notice=label("",11,GOLD);notice.setPadding(dp(8),dp(4),dp(8),dp(4));notice.setMaxLines(2);notice.setEllipsize(TextUtils.TruncateAt.END);notice.setOnClickListener(v->Toast.makeText(context,notice.getText(),Toast.LENGTH_LONG).show());notice.setBackground(shape(context,0xFF30383B,10,0));notice.setVisibility(View.GONE);panel.addView(notice,new LinearLayout.LayoutParams(-1,-2));
        cameraRow=row();cameraRow.setPadding(dp(7),dp(5),dp(7),dp(5));cameraRow.setVisibility(View.GONE);cameraRow.setBackground(shape(context,SOFT,12,0));
        FrameLayout cameraFrame=new FrameLayout(context);cameraFrame.setBackground(shape(context,Color.BLACK,8,0));cameraFrame.setClipToOutline(true);preview=new TextureView(context);cameraFrame.addView(preview,new FrameLayout.LayoutParams(-1,-1));cameraRow.addView(cameraFrame,new LinearLayout.LayoutParams(dp(51),dp(34)));
        LinearLayout cameraInfo=column();cameraInfo.setPadding(dp(10),0,0,0);cameraInfo.addView(label("后摄预览",11,TEXT));TextView cameraHint=label("点「看这个」分享",10,MUTED);cameraHint.setPadding(0,dp(3),0,0);cameraInfo.addView(cameraHint);cameraRow.addView(cameraInfo);panel.addView(cameraRow,new LinearLayout.LayoutParams(-1,dp(44)));
        preview.setSurfaceTextureListener(new TextureView.SurfaceTextureListener(){
            public void onSurfaceTextureAvailable(android.graphics.SurfaceTexture texture,int width,int height){texture.setDefaultBufferSize(640,480);previewSurface=new Surface(texture);actions.preview(previewSurface);previewRotation(cameraRotation);}
            public void onSurfaceTextureSizeChanged(android.graphics.SurfaceTexture texture,int width,int height){previewRotation(cameraRotation);}
            public boolean onSurfaceTextureDestroyed(android.graphics.SurfaceTexture texture){actions.preview(null);if(previewSurface!=null)previewSurface.release();previewSurface=null;return true;}
            public void onSurfaceTextureUpdated(android.graphics.SurfaceTexture texture){}
        });

        conversation=new FrameLayout(context);LinearLayout.LayoutParams conversationParams=new LinearLayout.LayoutParams(-1,0,1);conversationParams.topMargin=dp(10);panel.addView(conversation,conversationParams);
        transcript=new TogetherTranscript(context,new TogetherTranscript.Listener(){
            public void following(){if(!collapsed)markRead();}
            public void retry(String id){actions.retry(id);}
        });conversation.addView(transcript,new FrameLayout.LayoutParams(-1,-1));
        newMessages=label("新消息 ↓",11,GOLD);newMessages.setGravity(Gravity.CENTER);newMessages.setPadding(dp(12),dp(7),dp(12),dp(7));newMessages.setBackground(touch(context,0xFF253746,14,LINE));newMessages.setVisibility(View.GONE);newMessages.setOnClickListener(v->{markRead();bottom();});FrameLayout.LayoutParams jump=new FrameLayout.LayoutParams(-2,-2,Gravity.BOTTOM|Gravity.END);jump.bottomMargin=dp(3);conversation.addView(newMessages,jump);
        menu=column();menu.setPadding(dp(5),dp(5),dp(5),dp(5));menu.setBackground(shape(context,0xFF243743,15,0xFFB29565));menu.setElevation(dp(16));menu.setVisibility(View.GONE);
        menuItem("切换画面","switch",TEXT,actions::settings);
        pauseItem=menuItem("暂停画面","pause",TEXT,actions::pause);
        FrameLayout.LayoutParams menuParams=new FrameLayout.LayoutParams(dp(168),-2,Gravity.BOTTOM|Gravity.END);menuParams.rightMargin=dp(20);menuParams.bottomMargin=dp(86);root.addView(menu,menuParams);

        photoRow=row();photoRow.setPadding(dp(6),dp(5),dp(4),dp(5));photoRow.setBackground(shape(context,SOFT,12,0));
        photo=new ImageView(context);photo.setScaleType(ImageView.ScaleType.CENTER_CROP);photo.setBackground(shape(context,INK,7,0));photo.setClipToOutline(true);photo.setContentDescription("待发送图片");photoRow.addView(photo,new LinearLayout.LayoutParams(dp(38),dp(34)));
        photoLabel=label("",10.5f,TEXT);photoLabel.setPadding(dp(8),0,0,0);photoLabel.setMaxLines(2);photoRow.addView(photoLabel,new LinearLayout.LayoutParams(0,-2,1));photoRow.addView(icon("close","移除图片",MUTED,actions::removeImage),new LinearLayout.LayoutParams(dp(32),dp(34)));photoRow.setVisibility(View.GONE);panel.addView(photoRow,new LinearLayout.LayoutParams(-1,dp(44)));

        LinearLayout tools=row();look=chip("看这个","camera",GOLD,actions::look);look.setBackground(touch(context,0xFF334348,10,0x555E7874));tools.addView(look,new LinearLayout.LayoutParams(dp(86),dp(30)));
        voiceHint=label("",11,ROSE);voiceHint.setPadding(dp(4),0,0,0);voiceHint.setVisibility(View.GONE);tools.addView(voiceHint,new LinearLayout.LayoutParams(0,-2,1));
        View spacer=new View(context);tools.addView(spacer,new LinearLayout.LayoutParams(0,1,1));
        sourceChip=chip("画面暂停","pause",MUTED,actions::settings);sourceChip.setPadding(dp(6),0,dp(2),0);tools.addView(sourceChip,new LinearLayout.LayoutParams(-2,dp(32)));
        cancelVoice=icon("close","取消录音",ROSE,actions::cancelVoice);cancelVoice.setVisibility(View.GONE);tools.addView(cancelVoice,new LinearLayout.LayoutParams(dp(32),dp(32)));
        more=icon("more","更多",MUTED,this::toggleMenu);tools.addView(more,new LinearLayout.LayoutParams(dp(32),dp(34)));panel.addView(tools,new LinearLayout.LayoutParams(-1,dp(36)));

        LinearLayout composer=row();composer.setPadding(dp(3),dp(3),dp(5),dp(3));composer.setBackground(shape(context,0xFF14222F,15,LINE));
        record=icon("mic","录音",MUTED,()->{if(retryVoice)actions.retryVoice();else actions.record();});composer.addView(record,new LinearLayout.LayoutParams(dp(34),dp(36)));
        input=new EditText(context);input.setTextColor(TEXT);input.setHintTextColor(0xFF98A7AB);input.setHint("说点什么…");input.setTextSize(13.5f);input.setPadding(dp(2),dp(8),dp(5),dp(8));input.setBackgroundColor(Color.TRANSPARENT);input.setMinHeight(dp(36));input.setMinLines(1);input.setMaxLines(2);input.setIncludeFontPadding(false);
        input.setInputType(android.text.InputType.TYPE_CLASS_TEXT|android.text.InputType.TYPE_TEXT_FLAG_MULTI_LINE|android.text.InputType.TYPE_TEXT_FLAG_CAP_SENTENCES);input.setImeOptions(EditorInfo.IME_FLAG_NO_EXTRACT_UI);input.setFilters(new android.text.InputFilter[]{new android.text.InputFilter.LengthFilter(8000)});input.setContentDescription("陪伴消息");composer.addView(input,new LinearLayout.LayoutParams(0,-2,1));
        send=icon("send","发送",INK,()->actions.send(input.getText().toString()));send.setBackground(touch(context,GOLD,12,0));composer.addView(send,new LinearLayout.LayoutParams(dp(34),dp(34)));panel.addView(composer,new LinearLayout.LayoutParams(-1,-2));

        params=new WindowManager.LayoutParams(expandedWidth(),expandedHeight(),WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,PixelFormat.TRANSLUCENT);
        params.setTitle("Together chat");
        params.gravity=Gravity.TOP|Gravity.START;params.x=Math.max(0,screenWidth()-params.width-dp(4));params.y=dp(82);params.softInputMode=WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE;
        if(android.os.Build.VERSION.SDK_INT>=30)params.setFitInsetsTypes(WindowInsets.Type.systemBars()|WindowInsets.Type.displayCutout()|WindowInsets.Type.ime());
        root.setOnApplyWindowInsetsListener((view,insets)->{root.post(this::fitVisibleFrame);return insets;});root.getViewTreeObserver().addOnGlobalLayoutListener(this::fitVisibleFrame);
        input.addTextChangedListener(new TextWatcher(){public void beforeTextChanged(CharSequence s,int start,int count,int after){}public void onTextChanged(CharSequence s,int start,int before,int count){updateSend();if(!rendering)actions.draft(s.toString(),input.hasFocus());}public void afterTextChanged(Editable e){}});
        input.setOnFocusChangeListener((view,focus)->{actions.draft(input.getText().toString(),focus);updatePreviewVisibility();if(focus)menu.setVisibility(View.GONE);});
        View.OnTouchListener drag=(view,event)->{
            if(embedded())return false;
            switch(event.getActionMasked()){
                case MotionEvent.ACTION_DOWN:dragging=false;dragX=event.getRawX();dragY=event.getRawY();initialX=params.x;initialY=params.y;return true;
                case MotionEvent.ACTION_MOVE:
                    if(!dragging&&Math.hypot(event.getRawX()-dragX,event.getRawY()-dragY)<ViewConfiguration.get(context).getScaledTouchSlop())return true;
                    dragging=true;hidePeek();params.x=Math.max(0,Math.min(screenWidth()-params.width,initialX+(int)(event.getRawX()-dragX)));params.y=Math.max(0,initialY+(int)(event.getRawY()-dragY));fitVisibleFrame();update();return true;
                case MotionEvent.ACTION_UP:
                    if(view==bubble&&!dragging)view.performClick();
                    else if(collapsed){params.x=params.x+params.width/2<screenWidth()/2?0:screenWidth()-params.width;update();}
                    return true;
                case MotionEvent.ACTION_CANCEL:dragging=false;return true;
                default:return false;
            }
        };
        title.setOnTouchListener(drag);subtitle.setOnTouchListener(drag);seal.setOnTouchListener(drag);bubble.setOnTouchListener(drag);bubble.setOnClickListener(v->collapse(false));
        peek=column();peek.setPadding(dp(12),dp(8),dp(10),dp(9));peek.setBackground(shape(context,0xFF223541,17,0xFFB79B6F));
        LinearLayout peekHeader=row();peekTitle=label("",10,GOLD);peekHeader.addView(peekTitle,new LinearLayout.LayoutParams(0,-2,1));peekHeader.addView(icon("close","收起消息提示",MUTED,this::hidePeek),new LinearLayout.LayoutParams(dp(24),dp(22)));peek.addView(peekHeader);
        peekText=label("",12.5f,TEXT);peekText.setMaxLines(2);peekText.setEllipsize(TextUtils.TruncateAt.END);peekText.setLineSpacing(dp(2),1);peek.addView(peekText);peek.setContentDescription("查看新消息");peek.setOnClickListener(v->collapse(false));
        peekParams=new WindowManager.LayoutParams(dp(202),dp(76),WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE|WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,PixelFormat.TRANSLUCENT);peekParams.gravity=Gravity.TOP|Gravity.START;peekParams.setTitle("Together reply");
        avatar(BitmapFactory.decodeResource(context.getResources(),R.drawable.together_avatar_default));
        updateSend();
    }
    private int dp(float value){return TogetherChrome.dp(context,value);}
    private int screenWidth(){return context.getResources().getDisplayMetrics().widthPixels;}
    private int expandedWidth(){return Math.min(dp(296),screenWidth()-dp(8));}
    private int expandedHeight(){return Math.min(dp(274),context.getResources().getDisplayMetrics().heightPixels-dp(32));}
    private LinearLayout column(){LinearLayout view=new LinearLayout(context);view.setOrientation(LinearLayout.VERTICAL);return view;}
    private LinearLayout row(){LinearLayout view=new LinearLayout(context);view.setGravity(Gravity.CENTER_VERTICAL);return view;}
    private TextView label(String text,float size,int color){TextView view=new TextView(context);view.setText(text);view.setTextSize(size);view.setTextColor(color);view.setIncludeFontPadding(false);return view;}
    private ImageButton icon(String kind,String description,int color,Runnable action){ImageButton view=new ImageButton(context);view.setImageDrawable(new Icon(kind,color));view.setScaleType(ImageView.ScaleType.FIT_CENTER);view.setPadding(dp(8),dp(8),dp(8),dp(8));view.setBackground(touch(context,Color.TRANSPARENT,12,0));view.setContentDescription(description);view.setOnClickListener(v->action.run());return view;}
    private void glyph(TextView view,String kind,int color){Icon icon=new Icon(kind,color);icon.setBounds(0,0,dp(14),dp(14));view.setCompoundDrawables(icon,null,null,null);view.setCompoundDrawablePadding(dp(6));}
    private TextView chip(String text,String kind,int color,Runnable action){TextView view=label(text,11.5f,color);view.setGravity(Gravity.CENTER);view.setPadding(dp(9),0,dp(9),0);glyph(view,kind,color);view.setBackground(touch(context,Color.TRANSPARENT,10,0));view.setOnClickListener(v->action.run());return view;}
    private TextView menuItem(String text,String kind,int color,Runnable action){TextView item=chip(text,kind,color,()->{menu.setVisibility(View.GONE);action.run();});item.setGravity(Gravity.CENTER_VERTICAL);item.setTextSize(12);menu.addView(item,new LinearLayout.LayoutParams(-1,dp(40)));return item;}
    private void toggleMenu(){blur();menu.setVisibility(menu.getVisibility()==View.VISIBLE?View.GONE:View.VISIBLE);more.setSelected(menu.getVisibility()==View.VISIBLE);}
    private boolean following(){return transcript.following();}
    private void bottom(){transcript.bottom();}
    private void updateSend(){send.setEnabled(!recording&&(!input.getText().toString().trim().isEmpty()||hasImage));send.setAlpha(send.isEnabled()?1f:.4f);}
    private void fitVisibleFrame(){
        if(!showing||embedded())return;
        android.graphics.Rect visible=new android.graphics.Rect();root.getWindowVisibleDisplayFrame(visible);if(visible.height()<=0)return;
        int bottom=visible.bottom;
        if(android.os.Build.VERSION.SDK_INT>=30){WindowMetrics metrics=windows.getCurrentWindowMetrics();int keyboard=metrics.getWindowInsets().getInsets(WindowInsets.Type.ime()).bottom;if(keyboard>0)bottom=Math.min(bottom,metrics.getBounds().bottom-keyboard);}
        int oldY=params.y,oldHeight=params.height,available=bottom-visible.top-dp(8);
        if(collapsed)params.y=Math.min(params.y,Math.max(0,available-params.height));
        else{params.height=Math.max(1,Math.min(expandedHeight(),available));params.y=Math.min(params.y,Math.max(0,available-params.height));}
        if(params.height!=oldHeight||params.y!=oldY)update();
    }
    void show(){if(!showing&&!embedded()){windows.addView(root,params);showing=true;root.requestFocus();}root.setVisibility(View.VISIBLE);}
    boolean embedded(){return pageHost!=null;}
    void attachTo(FrameLayout host){
        if(pageHost==host)return;
        hidePeek();blur();if(showing){windows.removeViewImmediate(root);showing=false;}
        if(pageHost!=null)pageHost.removeView(root);
        pageHost=host;collapsed=false;menu.setVisibility(View.GONE);panel.setVisibility(View.VISIBLE);bubble.setVisibility(View.GONE);minimize.setVisibility(View.GONE);
        root.setPadding(dp(10),dp(6),dp(10),dp(10));root.setVisibility(View.VISIBLE);markRead();
        host.addView(root,new FrameLayout.LayoutParams(-1,-1));root.requestFocus();
    }
    void detachFromPage(){
        if(pageHost==null)return;
        blur();pageHost.removeView(root);pageHost=null;minimize.setVisibility(View.VISIBLE);collapsed=false;
        params.width=expandedWidth();params.height=expandedHeight();collapse(true);show();
    }
    void close(){hidePeek();bubble.stopPulse();if(pageHost!=null){pageHost.removeView(root);pageHost=null;}if(showing){windows.removeViewImmediate(root);showing=false;}conversation.removeView(transcript);transcript.close();}
    private void update(){if(showing)try{windows.updateViewLayout(root,params);}catch(RuntimeException ignored){}}
    void relayout(){params.x=Math.min(params.x,Math.max(0,screenWidth()-params.width));fitVisibleFrame();update();}
    void collapse(boolean value){
        if(embedded()||collapsed==value)return;
        int previousWidth=params.width;collapsed=value;menu.setVisibility(View.GONE);hidePeek();if(value)blur();else{boolean jump=unread;markRead();if(jump)bottom();}
        panel.setVisibility(value?View.GONE:View.VISIBLE);bubble.setVisibility(value?View.VISIBLE:View.GONE);root.setPadding(value?0:dp(8),value?0:dp(8),value?0:dp(8),value?0:dp(8));
        params.width=value?dp(52):expandedWidth();params.height=value?dp(52):expandedHeight();params.x=Math.max(0,Math.min(screenWidth()-params.width,params.x+previousWidth-params.width));
        params.flags=value?WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE|WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL:WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL;update();root.post(this::fitVisibleFrame);
    }
    boolean editing(){return !collapsed&&input.hasFocus();}
    void blur(){context.getSystemService(InputMethodManager.class).hideSoftInputFromWindow(input.getWindowToken(),0);input.clearFocus();root.requestFocus();actions.draft(input.getText().toString(),false);}
    void hideForFrame(){capturing=true;hidePeek();blur();menu.setVisibility(View.GONE);root.setVisibility(View.INVISIBLE);}
    void frameDone(){capturing=false;if(showing||embedded())root.setVisibility(View.VISIBLE);}
    void visibility(boolean visible){if(!visible){hidePeek();bubble.stopPulse();}if((showing||embedded())&&!capturing)root.setVisibility(visible?View.VISIBLE:View.INVISIBLE);}
    void avatar(Bitmap bitmap){if(bitmap!=null){seal.avatar(bitmap);bubble.avatar(bitmap);}}
    private void markRead(){unread=false;bubble.unread(false);newMessages.setVisibility(View.GONE);}
    private void hidePeek(){peekVersion++;root.removeCallbacks(dismissPeek);if(peekShowing){windows.removeViewImmediate(peek);peekShowing=false;}}
    private void showPeek(String text){
        int version=++peekVersion;
        transcript.plainText(text,plain->{if(version==peekVersion)showPlainPeek(plain);});
    }
    private void showPlainPeek(String text){
        if(!showing||!collapsed||capturing||root.getVisibility()!=View.VISIBLE)return;
        peekTitle.setText(aiName);peekText.setText(text.replace('\n',' '));
        int width=screenWidth();peekParams.width=Math.min(dp(202),width-dp(64));
        int right=params.x+params.width+dp(6);peekParams.x=right+peekParams.width<=width?right:Math.max(0,params.x-peekParams.width-dp(6));
        peekParams.y=Math.max(0,Math.min(params.y,context.getResources().getDisplayMetrics().heightPixels-peekParams.height-dp(40)));
        try{if(peekShowing)windows.updateViewLayout(peek,peekParams);else{windows.addView(peek,peekParams);peekShowing=true;}}catch(RuntimeException ignored){return;}
        bubble.pulse();root.removeCallbacks(dismissPeek);root.postDelayed(dismissPeek,6000);
    }
    void text(String value){if(input.getText().toString().equals(value))return;rendering=true;input.setText(value);input.setSelection(input.length());rendering=false;}
    String text(){return input.getText().toString();}
    void image(Bitmap bitmap,String caption){hasImage=bitmap!=null;photo.setImageBitmap(bitmap);photoLabel.setText(caption);photoRow.setVisibility(hasImage?View.VISIBLE:View.GONE);updatePreviewVisibility();updateSend();}
    void status(String value){notice.setText(value);notice.setVisibility(value.isEmpty()?View.GONE:View.VISIBLE);}
    void presence(long seconds,boolean online,String captureState,String source){
        long minutes=Math.max(0,seconds)/60;
        String elapsed=minutes==0?"刚刚开始":minutes<60?"已一起待了 "+minutes+" 分钟":"已一起待了 "+(minutes/60)+" 小时"+(minutes%60==0?"":" "+(minutes%60)+" 分钟");
        notice.setVisibility(View.GONE);subtitle.setText(online?elapsed:"暂时没连上");
        sourceState(captureState,source);
    }
    private void sourceState(String captureState,String source){
        boolean ready="ready".equals(captureState);sourceChip.setText(ready?(source.equals("screen")?"屏幕":"后摄"):"画面暂停");glyph(sourceChip,ready?source:"pause",MUTED);
        sourceChip.setContentDescription("来源 / 设置");pauseItem.setText(ready?"暂停画面":"恢复画面");glyph(pauseItem,ready?"pause":"play",TEXT);
    }
    void voice(boolean active,boolean retry){recording=active;retryVoice=retry;updateVoice();}
    void transcribing(boolean active){transcribing=active;updateVoice();}
    private void updateVoice(){
        record.setImageDrawable(new Icon(recording?"stop":retryVoice?"retry":"mic",recording||retryVoice?ROSE:MUTED));record.setContentDescription(recording?"停止转写":retryVoice?"重试转写":"录音");record.setEnabled(!transcribing);record.setAlpha(transcribing?.4f:1f);
        boolean active=recording||transcribing||retryVoice;look.setVisibility(active?View.GONE:View.VISIBLE);voiceHint.setVisibility(active?View.VISIBLE:View.GONE);voiceHint.setText(recording?"●  正在录音":transcribing?"正在转写…":"转写未完成");
        cancelVoice.setVisibility(active?View.VISIBLE:View.GONE);sourceChip.setVisibility(active?View.GONE:View.VISIBLE);updatePreviewVisibility();updateSend();
    }
    private void updatePreviewVisibility(){cameraRow.setVisibility(cameraVisible&&!input.hasFocus()&&!hasImage&&!recording&&!transcribing?View.VISIBLE:View.GONE);}
    void delta(String value){transcript.delta(value);}
    void previewRotation(int rotation){
        cameraRotation=rotation;float w=preview.getWidth(),h=preview.getHeight();if(w<=0||h<=0)return;
        android.graphics.Matrix matrix=new android.graphics.Matrix();matrix.setScale(640f/w,480f/h);matrix.postTranslate(-320,-240);matrix.postRotate(rotation);
        boolean swapped=rotation==90||rotation==270;float scale=Math.min(w/(swapped?480:640),h/(swapped?640:480));matrix.postScale(scale,scale);matrix.postTranslate(w/2,h/2);preview.setTransform(matrix);
    }
    void render(JSONObject state){
        aiName=state.optString("ai_name");userName=state.optString("user_name");title.setText(aiName);seal.name(aiName);bubble.name(aiName);
        transcript.identity(aiName,userName);
        boolean imageSupported=state.optBoolean("image_supported");look.setEnabled(imageSupported);look.setAlpha(imageSupported?1f:.45f);look.setContentDescription(imageSupported?"看这个":"当前模型不支持看图");
        cameraVisible="camera".equals(state.optString("source"))&&"ready".equals(state.optString("capture_state"));updatePreviewVisibility();
        sourceState(state.optString("capture_state"),state.optString("source"));
        JSONArray messages=state.optJSONArray("messages");String fingerprint=messages==null?"":messages.toString();if(fingerprint.equals(renderedMessages))return;
        boolean follow=following()||renderedMessages.isEmpty();renderedMessages=fingerprint;
        List<TogetherReplies.Message> observed=new ArrayList<>();
        if(messages!=null)for(int i=0;i<messages.length();i++){JSONObject item=messages.optJSONObject(i);if(item!=null)observed.add(new TogetherReplies.Message(item.optString("id"),item.optString("role"),item.optString("status"),item.optString("content")));}
        String fresh=replies.observe(observed);transcript.messages(messages);
        if(!fresh.isEmpty()&&(collapsed||!follow)){unread=true;bubble.unread(true);if(collapsed)showPeek(fresh);else newMessages.setVisibility(View.VISIBLE);}
    }
    void outbox(JSONArray queue){
        String fingerprint=queue.toString();if(fingerprint.equals(renderedOutbox))return;renderedOutbox=fingerprint;transcript.outbox(queue);
    }
}
