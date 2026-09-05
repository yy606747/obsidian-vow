package app.obsidianvow.core;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.TextUtils;
import android.view.WindowManager;
import android.widget.TextView;

/**
 * 闹钟、监控和哨兵提醒的轻量锁屏提示页。
 *
 * 普通 AI 消息不会启动本页；它们只交给系统通知设置决定是否点亮屏幕。
 * 本页短暂展示系统事件正文，随后自动退出，通知本身仍按各自 timeout 留在通知栏。
 */
public final class SystemAlertActivity extends Activity {

    private static final String EXTRA_TITLE = "system_alert_title";
    private static final String EXTRA_TEXT = "system_alert_text";
    private static final long VISIBLE_MS = 12_000L;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable finishTask = this::finish;

    static Intent createIntent(Context context, String title, String text) {
        return new Intent(context, SystemAlertActivity.class)
                .putExtra(EXTRA_TITLE, title)
                .putExtra(EXTRA_TEXT, text)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK
                        | Intent.FLAG_ACTIVITY_CLEAR_TOP
                        | Intent.FLAG_ACTIVITY_SINGLE_TOP);
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        configureLockscreenWindow();
        setContentView(R.layout.dialog_system_alert);
        findViewById(R.id.systemAlertRoot).setOnClickListener(v -> finish());
        render(getIntent());
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        render(intent);
    }

    private void configureLockscreenWindow() {
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true);
            setTurnScreenOn(true);
        } else {
            getWindow().addFlags(
                    WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED
                            | WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON);
        }
    }

    private void render(Intent intent) {
        String title = intent == null ? "" : intent.getStringExtra(EXTRA_TITLE);
        String text = intent == null ? "" : intent.getStringExtra(EXTRA_TEXT);
        if (TextUtils.isEmpty(title)) title = "Obsidian Vow 提醒";
        if (TextUtils.isEmpty(text)) text = "有一条新的系统提醒";

        ((TextView) findViewById(R.id.tvSystemAlertTitle)).setText(title);
        ((TextView) findViewById(R.id.tvSystemAlertText)).setText(text);
        handler.removeCallbacks(finishTask);
        handler.postDelayed(finishTask, VISIBLE_MS);
    }

    @Override
    protected void onDestroy() {
        handler.removeCallbacks(finishTask);
        super.onDestroy();
    }
}
