package app.obsidianvow.core;

import android.annotation.SuppressLint;
import android.content.Context;
import android.content.Intent;
import android.graphics.*;
import android.hardware.camera2.*;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.media.Image;
import android.media.ImageReader;
import android.media.projection.MediaProjection;
import android.media.projection.MediaProjectionManager;
import android.os.Build;
import android.os.Handler;
import android.util.DisplayMetrics;
import android.util.Range;
import android.util.Size;
import android.view.Surface;
import android.view.WindowManager;
import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.util.*;

/** A local channel consumes frames continuously; only an explicit request encodes one. */
final class TogetherFrames {
    interface Frame { void done(byte[] jpeg, String error); }
    interface Status { void changed(boolean ready, String reason); }
    private TogetherFrames() { }
    static byte[] jpeg(Bitmap image) {
        Bitmap scaled=image;
        int edge=Math.max(image.getWidth(),image.getHeight());
        if(edge>1280) scaled=Bitmap.createScaledBitmap(image, image.getWidth()*1280/edge,image.getHeight()*1280/edge,true);
        ByteArrayOutputStream out=new ByteArrayOutputStream();
        scaled.compress(Bitmap.CompressFormat.JPEG,75,out);
        if(scaled!=image) scaled.recycle();
        return out.toByteArray();
    }
    static Bitmap rgba(Image image) {
        Image.Plane plane=image.getPlanes()[0];
        int width=image.getWidth(), height=image.getHeight();
        int paddedWidth=plane.getRowStride()/plane.getPixelStride();
        Bitmap padded=Bitmap.createBitmap(paddedWidth,height,Bitmap.Config.ARGB_8888);
        padded.copyPixelsFromBuffer(plane.getBuffer());
        if(paddedWidth==width) return padded;
        Bitmap result=Bitmap.createBitmap(padded,0,0,width,height); padded.recycle(); return result;
    }

    static final class Screen {
        private final Context context;
        private final Handler handler;
        private final Status status;
        private MediaProjection projection;
        private volatile VirtualDisplay display;
        private ImageReader reader;
        private Frame pending;
        private int width,height,density;
        private volatile boolean closed;
        private boolean parked;
        boolean active(){return !closed&&display!=null;}

        Screen(Context context,Handler handler,Status status) { this.context=context;this.handler=handler;this.status=status; }
        void start(int code,Intent data) {
            try {
                projection=context.getSystemService(MediaProjectionManager.class).getMediaProjection(code,data);
                projection.registerCallback(new MediaProjection.Callback() {
                    @Override public void onStop() { close(); status.changed(false,"screen_stopped"); }
                    @Override public void onCapturedContentResize(int w,int h) {
                        if(closed||display==null||w<=0||h<=0) return;
                        dimensions(w,h); resetSurface();
                    }
                },handler);
                WindowManager wm=context.getSystemService(WindowManager.class);
                DisplayMetrics dm=new DisplayMetrics(); wm.getDefaultDisplay().getRealMetrics(dm);
                if(Build.VERSION.SDK_INT>=30) {
                    Rect bounds=wm.getMaximumWindowMetrics().getBounds(); dimensions(bounds.width(),bounds.height());
                } else dimensions(dm.widthPixels,dm.heightPixels);
                density=context.getResources().getConfiguration().densityDpi;
                newReader();
                // This is the only createVirtualDisplay call for this grant.
                display=projection.createVirtualDisplay("Together",width,height,density,
                    DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,reader.getSurface(),null,handler);
                status.changed(true,"");
            } catch(RuntimeException error) { close(); status.changed(false,"permission_denied"); }
        }
        private void dimensions(int w,int h) {
            float scale=Math.min(1f,1280f/Math.max(w,h));
            width=Math.max(2,Math.round(w*scale));height=Math.max(2,Math.round(h*scale));
        }
        private void newReader() {
            reader=ImageReader.newInstance(width,height,PixelFormat.RGBA_8888,2);
            reader.setOnImageAvailableListener(source -> {
                if(closed||source!=reader) return;
                try(Image image=source.acquireLatestImage()) {
                    if(image==null||pending==null||parked) return;
                    Frame callback=pending;pending=null;
                    try {
                        Bitmap bitmap=rgba(image);
                        byte[] bytes=jpeg(bitmap);bitmap.recycle();callback.done(bytes,"");
                    } catch(RuntimeException error) { callback.done(null,"unavailable"); }
                } catch(RuntimeException ignored) { fail("unavailable"); }
            },handler);
        }
        private void resetSurface() {
            if(closed||display==null) return;
            display.setSurface(null);
            if(reader!=null) reader.close();
            newReader(); display.resize(width,height,density);
            if(!parked) display.setSurface(reader.getSurface());
        }
        void park(boolean value) {
            parked=value;
            if(display!=null) display.setSurface(value?null:reader.getSurface());
            if(value) fail("unavailable");
        }
        void capture(Frame frame) {
            if(closed||display==null||parked) {frame.done(null,"screen_stopped");return;}
            fail("capture_timeout"); pending=frame;
            // Reattach a fresh surface after the overlay is hidden. This forces
            // a fresh composition even when the shared app displays a still image.
            // Resize/setSurface reuse the SAME VirtualDisplay and consent token.
            resetSurface();
            handler.postDelayed(()-> { if(pending==frame) fail("capture_timeout"); },7000);
        }
        private void fail(String reason) { Frame previous=pending;pending=null;if(previous!=null)previous.done(null,reason); }
        void close() {
            if(closed)return;closed=true;fail("screen_stopped");
            if(display!=null){display.release();display=null;}
            if(reader!=null){reader.close();reader=null;}
            if(projection!=null){projection.stop();projection=null;}
        }
    }

    static final class Camera {
        private final Context context;
        private final Handler handler;
        private final Status status;
        private CameraDevice device;
        private CameraCaptureSession session;
        private ImageReader reader;
        private Surface preview;
        private Frame pending;
        private int sensorOrientation;
        private long requestTime;
        private Range<Integer> fps;
        private boolean closed;
        private int configuration;
        Camera(Context context,Handler handler,Status status) {this.context=context;this.handler=handler;this.status=status;}
        int rotation(){return (sensorOrientation-context.getSystemService(WindowManager.class).getDefaultDisplay().getRotation()*90+360)%360;}
        @SuppressLint("MissingPermission") void start() {
            try {
                CameraManager manager=context.getSystemService(CameraManager.class);
                String selected=null;CameraCharacteristics traits=null;
                for(String id:manager.getCameraIdList()) {
                    CameraCharacteristics item=manager.getCameraCharacteristics(id);
                    if(Objects.equals(item.get(CameraCharacteristics.LENS_FACING),CameraCharacteristics.LENS_FACING_BACK)) {selected=id;traits=item;break;}
                }
                if(selected==null) throw new IllegalStateException("rear_camera_missing");
                sensorOrientation=Objects.requireNonNull(traits.get(CameraCharacteristics.SENSOR_ORIENTATION));
                StreamConfigurationMap map=traits.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
                List<Size> sizes=new ArrayList<>(Arrays.asList(map.getOutputSizes(ImageFormat.YUV_420_888)));
                sizes.sort(Comparator.comparingInt(s -> Math.abs(Math.max(s.getWidth(),s.getHeight())-1280)));
                Size size=sizes.get(0);
                for(Size candidate:sizes) if(Math.max(candidate.getWidth(),candidate.getHeight())<=1280){size=candidate;break;}
                Range<Integer>[] ranges=traits.get(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES);
                if(ranges!=null) for(Range<Integer> range:ranges)
                    if(range.getUpper()>=10 && (fps==null||range.getUpper()<fps.getUpper())) fps=range;
                reader=ImageReader.newInstance(size.getWidth(),size.getHeight(),ImageFormat.YUV_420_888,3);
                reader.setOnImageAvailableListener(this::frame,handler);
                manager.openCamera(selected,new CameraDevice.StateCallback() {
                    @Override public void onOpened(CameraDevice camera) {if(closed){camera.close();return;}device=camera;configure();}
                    @Override public void onDisconnected(CameraDevice camera) {camera.close();close();status.changed(false,"camera_busy");}
                    @Override public void onError(CameraDevice camera,int error) {camera.close();close();status.changed(false,"camera_busy");}
                },handler);
            } catch(Exception error) {close();status.changed(false,"camera_busy");}
        }
        void preview(Surface surface) {preview=surface;if(device!=null) configure();}
        private void configure() {
            if(closed||device==null)return;
            final int version=++configuration;
            if(session!=null){session.close();session=null;}
            try {
                List<Surface> targets=new ArrayList<>();targets.add(reader.getSurface());
                if(preview!=null&&preview.isValid()) targets.add(preview);
                device.createCaptureSession(targets,new CameraCaptureSession.StateCallback() {
                    @Override public void onConfigured(CameraCaptureSession next) {
                        if(closed||version!=configuration){next.close();return;}
                        session=next;
                        try {
                            CaptureRequest.Builder request=device.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW);
                            for(Surface target:targets) request.addTarget(target);
                            request.set(CaptureRequest.CONTROL_AF_MODE,CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE);
                            if(fps!=null)request.set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE,fps);
                            session.setRepeatingRequest(request.build(),null,handler);status.changed(true,"");
                        } catch(Exception error){status.changed(false,"camera_busy");}
                    }
                    @Override public void onConfigureFailed(CameraCaptureSession failed) {if(version==configuration)status.changed(false,"camera_busy");}
                },handler);
            } catch(Exception error) {status.changed(false,"camera_busy");}
        }
        void capture(Frame callback) {
            if(closed||session==null){callback.done(null,"camera_busy");return;}
            fail("capture_timeout");pending=callback;requestTime=System.nanoTime();
            handler.postDelayed(()->{if(pending==callback)fail("capture_timeout");},7000);
        }
        private void frame(ImageReader source) {
            if(closed||source!=reader)return;
            try(Image image=source.acquireLatestImage()) {
                if(image==null||pending==null)return;
                // Camera timestamps can use an unknown timebase. Draining the
                // latest queued image before arming avoids relying on wall time.
                if(System.nanoTime()-requestTime<100_000_000L)return;
                Frame callback=pending;pending=null;
                try {callback.done(encode(image),"");} catch(RuntimeException error){callback.done(null,"unavailable");}
            } catch(RuntimeException ignored){fail("unavailable");}
        }
        private byte[] encode(Image image) {
            Rect crop=image.getCropRect();int w=crop.width(),h=crop.height();
            byte[] nv21=new byte[w*h*3/2];Image.Plane[] planes=image.getPlanes();
            for(int plane=0;plane<3;plane++) {
                int scale=plane==0?1:2, pw=w/scale, ph=h/scale;
                Image.Plane p=planes[plane];ByteBuffer buffer=p.getBuffer();
                int start=buffer.position()+(crop.top/scale)*p.getRowStride()+(crop.left/scale)*p.getPixelStride();
                for(int y=0;y<ph;y++) for(int x=0;x<pw;x++) {
                    int dest=plane==0?y*w+x:w*h+(y*pw+x)*2+(plane==1?1:0);
                    nv21[dest]=buffer.get(start+y*p.getRowStride()+x*p.getPixelStride());
                }
            }
            ByteArrayOutputStream out=new ByteArrayOutputStream();
            new YuvImage(nv21,ImageFormat.NV21,w,h,null).compressToJpeg(new Rect(0,0,w,h),75,out);
            byte[] bytes=out.toByteArray();
            int angle=rotation();
            if(angle==0)return bytes;
            Bitmap original=BitmapFactory.decodeByteArray(bytes,0,bytes.length);Matrix matrix=new Matrix();matrix.postRotate(angle);
            Bitmap rotated=Bitmap.createBitmap(original,0,0,w,h,matrix,true);
            byte[] result=jpeg(rotated);if(rotated!=original)rotated.recycle();original.recycle();return result;
        }
        private void fail(String reason){Frame callback=pending;pending=null;if(callback!=null)callback.done(null,reason);}
        void close(){closed=true;configuration++;fail("camera_busy");if(session!=null)session.close();session=null;if(device!=null)device.close();device=null;if(reader!=null)reader.close();reader=null;}
    }
}
