package app.obsidianvow.core;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.media.projection.MediaProjectionManager;
import android.os.Bundle;
import android.os.CountDownTimer;
import android.text.TextUtils;
import android.util.Log;
import android.widget.TextView;

import androidx.core.content.ContextCompat;

import java.util.concurrent.TimeUnit;

import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

/**
 * 轻量截图确认页。展示请求来源/原因，用户点「允许」后拉起系统 MediaProjection
 * 授权弹窗；真正的屏幕授权由系统完成，本页只负责展示与触发。授权成功后把
 * token 交给 {@link MobileScreenCaptureService}；拒绝或系统授权失败则直接回报后端。
 */
public class ScreenCaptureRequestActivity extends Activity {

    private static final String TAG = "ObsidianScreenReq";
    private static final int REQ_PROJECTION = 9201;
    private static final int CONFIRM_TIMEOUT_SEC = 30;  // 与 PC 弹窗一致，超时按拒绝

    static final String EXTRA_REQUEST_ID = "request_id";
    static final String EXTRA_REASON     = "reason";
    static final String EXTRA_AI_NAME    = "ai_name";
    static final String EXTRA_HTTP_BASE  = "http_base";
    static final String EXTRA_TOKEN      = "token";

    private String requestId;
    private String httpBase;
    private String authToken;
    private CountDownTimer timer;
    private TextView tvTimer;
    private boolean decided = false;

    private final OkHttpClient http = new OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .writeTimeout(15, TimeUnit.SECONDS)
            .readTimeout(15, TimeUnit.SECONDS)
            .build();

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        Intent in = getIntent();
        requestId = in.getStringExtra(EXTRA_REQUEST_ID);
        httpBase = in.getStringExtra(EXTRA_HTTP_BASE);
        authToken = in.getStringExtra(EXTRA_TOKEN);
        String reason = in.getStringExtra(EXTRA_REASON);
        String aiName = in.getStringExtra(EXTRA_AI_NAME);
        if (TextUtils.isEmpty(aiName)) aiName = "AI";
        if (TextUtils.isEmpty(reason)) reason = "想确认你当前在做什么";

        if (TextUtils.isEmpty(requestId) || TextUtils.isEmpty(httpBase)) {
            finish();
            return;
        }

        setContentView(R.layout.dialog_screen_capture);
        setFinishOnTouchOutside(false);

        ((TextView) findViewById(R.id.tvCaptureTitle)).setText(aiName + " 请求查看你的屏幕");
        ((TextView) findViewById(R.id.tvCaptureReason)).setText("原因：" + reason);
        tvTimer = findViewById(R.id.tvCaptureTimer);

        findViewById(R.id.btnCaptureAllow).setOnClickListener(v -> {
            if (decided) return;
            decided = true;
            cancelTimer();
            requestProjection();
        });
        findViewById(R.id.btnCaptureDeny).setOnClickListener(v -> decline("denied"));

        startCountdown();
    }

    private void startCountdown() {
        timer = new CountDownTimer(CONFIRM_TIMEOUT_SEC * 1000L, 1000L) {
            @Override public void onTick(long msLeft) {
                int s = (int) Math.ceil(msLeft / 1000.0);
                if (tvTimer != null) {
                    tvTimer.setText("⏱ " + s + "s");
                    tvTimer.setTextColor(s <= 8 ? Color.parseColor("#e8715a") : Color.parseColor("#b5a898"));
                }
            }
            @Override public void onFinish() {
                decline("confirm_timeout");  // 超时未确认 → 当作拒绝
            }
        };
        timer.start();
    }

    private void decline(String reason) {
        if (decided) return;
        decided = true;
        cancelTimer();
        postDecisionAsync("rejected", reason);
        finish();
    }

    private void cancelTimer() {
        if (timer != null) { timer.cancel(); timer = null; }
    }

    @Override
    public void onBackPressed() {
        decline("denied");  // 返回键视为拒绝
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        cancelTimer();
    }

    private void requestProjection() {
        try {
            MediaProjectionManager mpm =
                    (MediaProjectionManager) getSystemService(Context.MEDIA_PROJECTION_SERVICE);
            startActivityForResult(mpm.createScreenCaptureIntent(), REQ_PROJECTION);
        } catch (Exception e) {
            Log.e(TAG, "createScreenCaptureIntent failed: " + e.getMessage());
            postDecisionAsync("rejected", "projection_failed");
            finish();
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQ_PROJECTION) return;

        if (resultCode == RESULT_OK && data != null) {
            Intent svc = new Intent(this, MobileScreenCaptureService.class);
            svc.putExtra(MobileScreenCaptureService.EXTRA_RESULT_CODE, resultCode);
            svc.putExtra(MobileScreenCaptureService.EXTRA_RESULT_DATA, data);
            svc.putExtra(MobileScreenCaptureService.EXTRA_REQUEST_ID, requestId);
            svc.putExtra(MobileScreenCaptureService.EXTRA_HTTP_BASE, httpBase);
            svc.putExtra(MobileScreenCaptureService.EXTRA_TOKEN, authToken);
            ContextCompat.startForegroundService(this, svc);
        } else {
            // 用户在系统弹窗里取消/拒绝了录屏授权。
            postDecisionAsync("rejected", "permission_denied");
        }
        finish();
    }

    private void postDecisionAsync(String decision, String rejectReason) {
        final String base = httpBase, rid = requestId;
        new Thread(() -> {
            try {
                String json = "{\"decision\":\"" + decision + "\",\"reject_reason\":\""
                        + (rejectReason == null ? "" : rejectReason) + "\"}";
                RequestBody body = RequestBody.create(
                        json, MediaType.get("application/json; charset=utf-8"));
                Request.Builder rb = new Request.Builder()
                        .url(base + "/api/mobile-screen/" + rid + "/decision")
                        .post(body);
                if (authToken != null && !authToken.isEmpty()) {
                    rb.header("Authorization", "Bearer " + authToken);
                }
                Request req = rb.build();
                try (Response resp = http.newCall(req).execute()) {
                    Log.d(TAG, "decision " + decision + " → " + resp.code());
                }
            } catch (Exception e) {
                Log.e(TAG, "decision post failed: " + e.getMessage());
            }
        }, "ScreenDecision").start();
    }
}
