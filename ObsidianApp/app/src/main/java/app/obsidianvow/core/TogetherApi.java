package app.obsidianvow.core;

import android.content.Context;
import android.content.SharedPreferences;
import org.json.JSONObject;
import okhttp3.*;
import java.io.IOException;
import java.util.concurrent.TimeUnit;

/** Requests deliberately have no HTTP retry: message/end retries use stored ids. */
final class TogetherApi {
    static final MediaType JSON = MediaType.get("application/json; charset=utf-8");
    final OkHttpClient http;
    final String base;
    private final SharedPreferences prefs;
    private final boolean debug;
    private final Context context;
    TogetherApi(Context context) {
        this.context=context.getApplicationContext();
        debug=(context.getApplicationInfo().flags&android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE)!=0;
        prefs = context.getSharedPreferences(LauncherActivity.PREFS, Context.MODE_PRIVATE);
        String configured = prefs.getString(LauncherActivity.KEY_URL, "");
        HttpUrl parsed = HttpUrl.parse(configured);
        if(parsed == null) throw new IllegalStateException("请先设置连接地址");
        base = parsed.newBuilder().encodedPath("/").query(null).fragment(null).build().toString().replaceAll("/$", "");
        http = new OkHttpClient.Builder().retryOnConnectionFailure(false)
            // Uvicorn closes idle HTTP connections after 5 seconds. USB reverse
            // can delay that close, leaving a stale pooled socket on Android.
            // Retire idle sockets sooner; never replay an uncertain mutation.
            .connectionPool(new ConnectionPool(5,3,TimeUnit.SECONDS))
            .connectTimeout(10, TimeUnit.SECONDS).readTimeout(70, TimeUnit.SECONDS).callTimeout(75, TimeUnit.SECONDS)
            .addInterceptor(chain -> {
                Request.Builder request = chain.request().newBuilder();
                String token = prefs.getString(LauncherActivity.KEY_TOKEN, "");
                if(!token.isEmpty()) request.header("Authorization", "Bearer "+token);
                return chain.proceed(request.build());
            }).build();
    }
    JSONObject get(String path) throws Exception { return perform(new Request.Builder().url(base+path).get().build()); }
    JSONObject post(String path, JSONObject body) throws Exception {
        return upload(path, RequestBody.create(body.toString(), JSON));
    }
    JSONObject put(String path, JSONObject body) throws Exception {
        return perform(new Request.Builder().url(base+path).put(RequestBody.create(body.toString(),JSON)).build());
    }
    JSONObject upload(String path, RequestBody body) throws Exception {
        return perform(new Request.Builder().url(base+path).post(body).build());
    }
    private JSONObject perform(Request request) throws Exception {
        try(Response response = http.newCall(request).execute()) {
            String raw = response.body() == null ? "" : response.body().string();
            if(!response.isSuccessful()) {
                String detail = "请求失败（"+response.code()+"）";
                try { detail = new JSONObject(raw).optString("detail",detail); } catch(Exception ignored) { }
                throw new ApiError(response.code(), detail);
            }
            return raw.equals("null") || raw.isEmpty() ? null : new JSONObject(raw);
        } catch(Exception error) {
            if(debug&&!(error instanceof ApiError))android.util.Log.w("TogetherApi",
                request.method()+" "+request.url().encodedPath()+" "+error.getClass().getSimpleName()+": "+error.getMessage());
            diagnose(context,request.method()+" "+request.url().encodedPath(),error);
            throw error;
        }
    }
    static void diagnose(Context context,String operation,Exception error){
        if((context.getApplicationInfo().flags&android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE)==0)return;
        try{
            Throwable cause=error;while(cause.getCause()!=null)cause=cause.getCause();
            JSONObject value=object("operation",operation,"type",error.getClass().getName(),"cause",cause.getClass().getName(),"at",System.currentTimeMillis(),
                "message",error instanceof IOException&&!(error instanceof ApiError)?error.getMessage():"");
            java.nio.file.Files.write(new java.io.File(context.getCacheDir(),"together-diagnostic.json").toPath(),value.toString().getBytes(java.nio.charset.StandardCharsets.UTF_8));
        }catch(Exception ignored){}
    }
    static JSONObject object(Object... pairs) {
        JSONObject result = new JSONObject();
        try { for(int i=0;i<pairs.length;i+=2) result.put((String)pairs[i], pairs[i+1]); }
        catch(Exception error) { throw new IllegalArgumentException(error); }
        return result;
    }
    static final class ApiError extends IOException {
        final int status;
        ApiError(int status,String message) { super(message); this.status=status; }
    }
}
