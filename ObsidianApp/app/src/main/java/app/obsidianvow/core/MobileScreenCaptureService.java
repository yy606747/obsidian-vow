package app.obsidianvow.core;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.graphics.Bitmap;
import android.graphics.PixelFormat;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.media.Image;
import android.media.ImageReader;
import android.media.projection.MediaProjection;
import android.media.projection.MediaProjectionManager;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.util.DisplayMetrics;
import android.util.Log;

import androidx.annotation.Nullable;

import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

import okhttp3.MediaType;
import okhttp3.MultipartBody;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

/**
 * 一次性屏幕截图前台服务（foregroundServiceType=mediaProjection）。
 *
 * 由 {@link ScreenCaptureRequestActivity} 在用户通过系统投屏授权后拉起，携带
 * MediaProjection 的 resultCode + data。流程：起前台 → 取 MediaProjection →
 * VirtualDisplay + ImageReader 截一帧 → 压缩 JPEG → 上传 →
 * 立即释放 projection。不长期持有 token（Android 14+ 不允许复用）。
 */
public class MobileScreenCaptureService extends Service {

    private static final String TAG = "ObsidianScreenCap";
    private static final String CH_CAPTURE = "obsidian_screen_capture";
    private static final int NOTIF_ID = 7001;

    private static final int MAX_EDGE = 1280;
    private static final int JPEG_QUALITY = 70;
    private static final long FRAME_TIMEOUT_MS = 4000;

    static final String EXTRA_RESULT_CODE = "result_code";
    static final String EXTRA_RESULT_DATA = "result_data";
    static final String EXTRA_REQUEST_ID  = "request_id";
    static final String EXTRA_HTTP_BASE   = "http_base";
    static final String EXTRA_TOKEN       = "token";

    private MediaProjection projection;
    private VirtualDisplay virtualDisplay;
    private ImageReader imageReader;
    private HandlerThread handlerThread;
    private Handler handler;
    private final AtomicBoolean captured = new AtomicBoolean(false);
    private final OkHttpClient http = new OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .writeTimeout(30, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .build();

    private String requestId;
    private String httpBase;
    private String authToken;

    @Override
    public void onCreate() {
        super.onCreate();
        createChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null) { stopSelf(); return START_NOT_STICKY; }

        final int resultCode = intent.getIntExtra(EXTRA_RESULT_CODE, 0);
        final Intent resultData = intent.getParcelableExtra(EXTRA_RESULT_DATA);
        requestId = intent.getStringExtra(EXTRA_REQUEST_ID);
        httpBase = intent.getStringExtra(EXTRA_HTTP_BASE);
        authToken = intent.getStringExtra(EXTRA_TOKEN);

        // 必须先起前台（mediaProjection 类型）再取 MediaProjection（Android 14+ 要求）。
        startAsForeground();

        handlerThread = new HandlerThread("ScreenCapture");
        handlerThread.start();
        handler = new Handler(handlerThread.getLooper());

        // 所有网络（decision/upload）与 projection 操作都放到后台线程，
        // 避免在主线程联网触发 NetworkOnMainThreadException。
        handler.post(() -> {
            if (resultData == null || requestId == null || httpBase == null) {
                Log.e(TAG, "missing extras, aborting");
                finishWithReject("capture_failed");
                return;
            }
            try {
                MediaProjectionManager mpm =
                        (MediaProjectionManager) getSystemService(Context.MEDIA_PROJECTION_SERVICE);
                projection = mpm.getMediaProjection(resultCode, resultData);
                if (projection == null) {
                    finishWithReject("projection_failed");
                    return;
                }
                // Android 14+ 要求在创建 VirtualDisplay 前注册回调。
                projection.registerCallback(new MediaProjection.Callback() {
                    @Override public void onStop() { Log.i(TAG, "projection stopped"); }
                }, handler);

                // 用户已同意，先把请求标记为 approved，后端才接受上传。
                // 若 approved 没被后端确认（token 失效/请求已过期/404），不要继续截屏。
                if (!postDecision("approved", "")) {
                    Log.e(TAG, "approved decision not accepted by server, aborting capture");
                    teardown();
                    return;
                }
                startCapture();
            } catch (Exception e) {
                Log.e(TAG, "projection setup failed: " + e.getMessage());
                finishWithReject("projection_failed");
            }
        });
        return START_NOT_STICKY;
    }

    private void startCapture() {
        DisplayMetrics dm = getResources().getDisplayMetrics();
        int width = dm.widthPixels;
        int height = dm.heightPixels;
        int density = dm.densityDpi;

        imageReader = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 2);
        imageReader.setOnImageAvailableListener(reader -> {
            if (!captured.compareAndSet(false, true)) return;  // 只处理第一帧
            Image image = null;
            try {
                image = reader.acquireLatestImage();
                if (image == null) { finishWithReject("capture_failed"); return; }
                Bitmap bmp = imageToBitmap(image, width, height);
                byte[] jpeg = compress(bmp);
                bmp.recycle();
                uploadAndFinish(jpeg);
            } catch (Exception e) {
                Log.e(TAG, "capture failed: " + e.getMessage());
                finishWithReject("capture_failed");
            } finally {
                if (image != null) image.close();
            }
        }, handler);

        virtualDisplay = projection.createVirtualDisplay(
                "ObsidianScreenCapture", width, height, density,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
                imageReader.getSurface(), null, handler);

        // 兜底：若超时仍未拿到帧，按 capture_failed 收尾。
        handler.postDelayed(() -> {
            if (!captured.get()) {
                Log.w(TAG, "frame timeout");
                if (captured.compareAndSet(false, true)) finishWithReject("capture_failed");
            }
        }, FRAME_TIMEOUT_MS);
    }

    private Bitmap imageToBitmap(Image image, int width, int height) {
        Image.Plane[] planes = image.getPlanes();
        ByteBuffer buffer = planes[0].getBuffer();
        int pixelStride = planes[0].getPixelStride();
        int rowStride = planes[0].getRowStride();
        int rowPadding = rowStride - pixelStride * width;

        // ImageReader 行有 padding，先按 padding 宽度建图再裁掉。
        Bitmap padded = Bitmap.createBitmap(
                width + rowPadding / pixelStride, height, Bitmap.Config.ARGB_8888);
        padded.copyPixelsFromBuffer(buffer);
        if (rowPadding == 0) return padded;
        Bitmap cropped = Bitmap.createBitmap(padded, 0, 0, width, height);
        padded.recycle();
        return cropped;
    }

    private byte[] compress(Bitmap src) {
        int w = src.getWidth(), h = src.getHeight();
        int longEdge = Math.max(w, h);
        Bitmap scaled = src;
        if (longEdge > MAX_EDGE) {
            float ratio = (float) MAX_EDGE / longEdge;
            scaled = Bitmap.createScaledBitmap(src, Math.round(w * ratio), Math.round(h * ratio), true);
        }
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        scaled.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, out);
        if (scaled != src) scaled.recycle();
        return out.toByteArray();
    }

    private void uploadAndFinish(byte[] jpeg) {
        try {
            RequestBody filePart = RequestBody.create(jpeg, MediaType.get("image/jpeg"));
            MultipartBody body = new MultipartBody.Builder()
                    .setType(MultipartBody.FORM)
                    .addFormDataPart("screenshot", requestId + ".jpg", filePart)
                    .build();
            Request.Builder rb = new Request.Builder()
                    .url(httpBase + "/api/mobile-screen/" + requestId + "/upload")
                    .post(body);
            applyAuth(rb);
            try (Response resp = http.newCall(rb.build()).execute()) {
                Log.i(TAG, "upload → " + resp.code());
                // 非 2xx（401/409/5xx）要主动回报，否则后端干等到 request_expired。
                if (!resp.isSuccessful()) {
                    postDecision("rejected", "upload_failed");
                }
            }
        } catch (Exception e) {
            Log.e(TAG, "upload failed: " + e.getMessage());
            postDecision("rejected", "upload_failed");
        } finally {
            teardown();
        }
    }

    /** 截图链路失败：通知后端拒绝原因并收尾。 */
    private void finishWithReject(String reason) {
        postDecision("rejected", reason);
        teardown();
    }

    /** @return true 仅当后端以 2xx 确认了该 decision。 */
    private boolean postDecision(String decision, String rejectReason) {
        if (httpBase == null || requestId == null) return false;
        try {
            String json = "{\"decision\":\"" + decision + "\",\"reject_reason\":\""
                    + (rejectReason == null ? "" : rejectReason) + "\"}";
            RequestBody reqBody = RequestBody.create(
                    json, MediaType.get("application/json; charset=utf-8"));
            Request.Builder rb = new Request.Builder()
                    .url(httpBase + "/api/mobile-screen/" + requestId + "/decision")
                    .post(reqBody);
            applyAuth(rb);
            try (Response resp = http.newCall(rb.build()).execute()) {
                Log.d(TAG, "decision " + decision + " → " + resp.code());
                return resp.isSuccessful();
            }
        } catch (Exception e) {
            Log.e(TAG, "decision post failed: " + e.getMessage());
            return false;
        }
    }

    /** 后端开启鉴权时为请求带上 Bearer（共享 client 的拦截器在本进程组件里没有）。 */
    private void applyAuth(Request.Builder rb) {
        if (authToken != null && !authToken.isEmpty()) {
            rb.header("Authorization", "Bearer " + authToken);
        }
    }

    private void teardown() {
        try { if (virtualDisplay != null) virtualDisplay.release(); } catch (Exception ignored) {}
        try { if (imageReader != null) imageReader.close(); } catch (Exception ignored) {}
        try { if (projection != null) projection.stop(); } catch (Exception ignored) {}
        virtualDisplay = null; imageReader = null; projection = null;
        if (handlerThread != null) handlerThread.quitSafely();
        stopForeground(true);
        stopSelf();
    }

    private void startAsForeground() {
        Notification n = new Notification.Builder(this, CH_CAPTURE)
                .setContentTitle("正在截取屏幕")
                .setContentText("一次性截图，完成后自动结束")
                .setSmallIcon(android.R.drawable.ic_menu_camera)
                .setOngoing(true)
                .build();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION);
        } else {
            startForeground(NOTIF_ID, n);
        }
    }

    private void createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationManager nm = getSystemService(NotificationManager.class);
            NotificationChannel ch = new NotificationChannel(
                    CH_CAPTURE, "屏幕截图", NotificationManager.IMPORTANCE_LOW);
            ch.setDescription("AI 请求的一次性屏幕截图");
            if (nm != null) BrandCompatibility.createNotificationChannel(nm, ch);
        }
    }

    @Override
    public void onDestroy() {
        super.onDestroy();
        teardown();
    }

    @Nullable @Override
    public IBinder onBind(Intent intent) { return null; }
}
