package app.obsidianvow.core;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.os.Handler;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.MessageDigest;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;
import okhttp3.Call;
import okhttp3.Request;
import okhttp3.Response;

/** One small cached avatar per server. No work on the UI or heartbeat thread. */
final class TogetherAvatar {
    private static final int LIMIT=1024*1024;
    private final Context context;
    private final Handler main;
    private final Consumer<Bitmap> ready;
    private volatile boolean closed;
    private volatile Call call;

    TogetherAvatar(Context context,Handler main,Consumer<Bitmap> ready) {
        this.context=context.getApplicationContext();this.main=main;this.ready=ready;
    }
    void start() {
        new Thread(()->{
            try {
                TogetherApi api=new TogetherApi(context);
                byte[] hash=MessageDigest.getInstance("SHA-256").digest(api.base.getBytes(StandardCharsets.UTF_8));
                StringBuilder key=new StringBuilder();for(int i=0;i<8;i++)key.append(String.format(java.util.Locale.ROOT,"%02x",hash[i]));
                File cache=new File(context.getCacheDir(),"together-avatar-"+key+".png");
                if(cache.isFile()&&cache.length()<=LIMIT)deliver(decode(Files.readAllBytes(cache.toPath())));
                if(closed)return;
                call=api.http.newBuilder().callTimeout(8,TimeUnit.SECONDS).build()
                    .newCall(new Request.Builder().url(api.base+"/api/avatar/ai").get().build());
                if(closed){call.cancel();return;}
                try(Response response=call.execute()) {
                    if(!response.isSuccessful()||response.body()==null||response.body().contentLength()>LIMIT)return;
                    ByteArrayOutputStream bytes=new ByteArrayOutputStream();
                    try(InputStream stream=response.body().byteStream()) {
                        byte[] buffer=new byte[8192];int read;
                        while((read=stream.read(buffer))!=-1){if(bytes.size()+read>LIMIT)return;bytes.write(buffer,0,read);}
                    }
                    byte[] raw=bytes.toByteArray();Bitmap bitmap=decode(raw);if(bitmap==null||closed)return;
                    // Read the last valid cache before replacing it. A failed fetch keeps it.
                    File pending=new File(cache.getPath()+".tmp");Files.write(pending.toPath(),raw);
                    if(!pending.renameTo(cache))pending.delete();deliver(bitmap);
                }
            }catch(Exception ignored){ /* Existing avatar or bundled portrait remains visible. */ }
        },"TogetherAvatar").start();
    }
    private static Bitmap decode(byte[] bytes) {
        BitmapFactory.Options bounds=new BitmapFactory.Options();bounds.inJustDecodeBounds=true;
        BitmapFactory.decodeByteArray(bytes,0,bytes.length,bounds);
        if(bounds.outWidth<=0||bounds.outHeight<=0||bounds.outWidth>4096||bounds.outHeight>4096)return null;
        BitmapFactory.Options options=new BitmapFactory.Options();options.inSampleSize=1;
        while(Math.max(bounds.outWidth,bounds.outHeight)/options.inSampleSize>512)options.inSampleSize*=2;
        return BitmapFactory.decodeByteArray(bytes,0,bytes.length,options);
    }
    private void deliver(Bitmap bitmap){if(bitmap!=null)main.post(()->{if(!closed)ready.accept(bitmap);});}
    void close(){closed=true;Call pending=call;if(pending!=null)pending.cancel();}
}
