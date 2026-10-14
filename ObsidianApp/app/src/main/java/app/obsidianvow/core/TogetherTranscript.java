package app.obsidianvow.core;

import android.annotation.SuppressLint;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;
import androidx.webkit.WebViewAssetLoader;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;
import java.io.ByteArrayInputStream;
import java.util.Collections;
import java.util.function.Consumer;

/** One offline message renderer; composing, capture and session ownership stay native. */
final class TogetherTranscript extends WebView {
    interface Listener { void following(); void retry(String id); }
    private static final String PAGE="https://appassets.androidplatform.net/assets/together/index.html";
    private final Handler main=new Handler(Looper.getMainLooper());
    private final Listener listener;
    private JSONObject pending=new JSONObject();
    private boolean ready,closed,following=true,jumpPending,paintScheduled;
    private Runnable pendingPlainText;
    private final Runnable paint=this::flush;

    @SuppressLint("SetJavaScriptEnabled")
    TogetherTranscript(Context context,Listener listener){
        super(context);this.listener=listener;
        setBackgroundColor(Color.TRANSPARENT);
        setContentDescription("陪伴对话记录");
        setVerticalScrollBarEnabled(false);setHorizontalScrollBarEnabled(false);
        setOverScrollMode(View.OVER_SCROLL_NEVER);
        setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS);
        WebSettings settings=getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setAllowFileAccess(false);settings.setAllowContentAccess(false);
        settings.setDomStorageEnabled(false);settings.setGeolocationEnabled(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setTextZoom(Math.round(context.getResources().getConfiguration().fontScale*100));
        WebViewAssetLoader assets=new WebViewAssetLoader.Builder()
            .addPathHandler("/assets/",new WebViewAssetLoader.AssetsPathHandler(context)).build();
        setWebViewClient(new WebViewClient(){
            @Override public WebResourceResponse shouldInterceptRequest(WebView view,WebResourceRequest request){
                WebResourceResponse local=assets.shouldInterceptRequest(request.getUrl());
                // Never fall back to the network, including Markdown images.
                return local!=null?local:new WebResourceResponse("text/plain","UTF-8",403,"Offline transcript",
                    Collections.emptyMap(),new ByteArrayInputStream(new byte[0]));
            }
            @Override public boolean shouldOverrideUrlLoading(WebView view,WebResourceRequest request){
                if(request.isForMainFrame()&&request.hasGesture())openExternal(request.getUrl().toString());
                return true;
            }
            @Override public void onPageFinished(WebView view,String url){
                if(closed||!PAGE.equals(url))return;
                ready=true;flush();
                if(pendingPlainText!=null){Runnable next=pendingPlainText;pendingPlainText=null;next.run();}
            }
        });
        addJavascriptInterface(new Bridge(),"TogetherNative");
        loadUrl(PAGE);
    }
    private void put(String key,Object value){
        if(closed)return;
        try{pending.put(key,value);}catch(JSONException impossible){throw new IllegalArgumentException(impossible);}
        // One bounded paint per burst; a finishing delta and its snapshot can share it.
        if(!paintScheduled){paintScheduled=true;main.postDelayed(paint,80);}
    }
    void identity(String aiName,String userName){put("aiName",aiName);put("userName",userName);}
    void messages(JSONArray messages){
        JSONArray recent=new JSONArray();
        if(messages!=null)for(int i=Math.max(0,messages.length()-40);i<messages.length();i++){
            JSONObject message=messages.optJSONObject(i);if(message!=null)recent.put(message);
        }
        put("messages",recent);
    }
    void outbox(JSONArray messages){put("outbox",messages);}
    void delta(String text){put("stream",text);}
    boolean following(){return following;}
    void bottom(){following=true;jumpPending=true;if(ready)flush();}
    private static String js(String json){return json.replace("\u2028","\\u2028").replace("\u2029","\\u2029");}
    private void flush(){
        main.removeCallbacks(paint);
        paintScheduled=false;
        if(closed||!ready)return;
        if(pending.length()>0){String data=js(pending.toString());pending=new JSONObject();evaluateJavascript("TogetherTranscript.update("+data+")",null);}
        if(jumpPending){jumpPending=false;evaluateJavascript("TogetherTranscript.bottom()",null);}
    }
    void plainText(String source,Consumer<String> callback){
        if(closed)return;
        if(!ready){pendingPlainText=()->plainText(source,callback);return;}
        evaluateJavascript("TogetherTranscript.plainText("+js(JSONObject.quote(source))+")",result->{
            if(closed)return;
            try{callback.accept(new JSONArray("["+result+"]").getString(0));}catch(JSONException ignored){callback.accept("");}
        });
    }
    private void openExternal(String url){
        if(closed)return;
        Uri uri=Uri.parse(url);String scheme=uri.getScheme();
        if(!"https".equalsIgnoreCase(scheme)&&!"http".equalsIgnoreCase(scheme))return;
        try{getContext().startActivity(new Intent(Intent.ACTION_VIEW,uri).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));}
        catch(RuntimeException ignored){Toast.makeText(getContext(),"暂时无法打开链接",Toast.LENGTH_SHORT).show();}
    }
    private final class Bridge {
        @JavascriptInterface public void following(boolean value){main.post(()->{if(closed)return;following=value;if(value)listener.following();});}
        @JavascriptInterface public void retry(String id){main.post(()->{if(!closed)listener.retry(id);});}
        @JavascriptInterface public void openLink(String url){main.post(()->openExternal(url));}
        @JavascriptInterface public void copy(String text){main.post(()->{
            if(closed)return;
            getContext().getSystemService(ClipboardManager.class).setPrimaryClip(ClipData.newPlainText("相伴消息",text));
            if(android.os.Build.VERSION.SDK_INT<33)Toast.makeText(getContext(),"已复制",Toast.LENGTH_SHORT).show();
        });}
    }
    void close(){
        if(closed)return;closed=true;main.removeCallbacksAndMessages(null);pendingPlainText=null;
        pending=new JSONObject();stopLoading();removeJavascriptInterface("TogetherNative");destroy();
    }
}
