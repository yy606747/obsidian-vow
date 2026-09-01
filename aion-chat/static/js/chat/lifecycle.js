// ── 摄像头/监控日志/记忆库 → 已拆分为独立页面 ──

// ── 静音音频保活（阻止浏览器后台节流） ──
let _keepAliveCtx = null;
function startSilentKeepAlive() {
  try {
    if (_keepAliveCtx) return;
    _keepAliveCtx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = _keepAliveCtx.createOscillator();
    const gain = _keepAliveCtx.createGain();
    gain.gain.value = 0;          // 完全静音
    osc.connect(gain);
    gain.connect(_keepAliveCtx.destination);
    osc.start();
    // 用户交互后 resume（Chrome 要求）
    const resume = () => { if (_keepAliveCtx.state === 'suspended') _keepAliveCtx.resume(); };
    document.addEventListener('click', resume, { once: true });
    document.addEventListener('keydown', resume, { once: true });
  } catch(e) { console.warn('keepalive audio failed:', e); }
}

// ── 系统通知（后台标签也能弹出） ──
function sendSystemNotification(title, body) {
  if (!('Notification' in window)) return;
  if (Notification.permission !== 'granted') return;
  try { new Notification(title, { body, icon: '/public/icon-192.png' }); } catch(e) {}
}

function requestNotificationPermissionAfterInteraction() {
  // web_push.js owns the user-gesture permission request and subscribes in the
  // same flow. Keeping this compatibility function avoids parallel prompts.
}

function scheduleNonCriticalChatInit() {
  window.ChatPerf?.markOnce("noncritical_init_scheduled");
  setTimeout(() => {
    const run = () => {
      if (typeof _initWhisperFloat === 'function') {
        window.ChatPerf?.markOnce("heart_whispers_init_start");
        _initWhisperFloat();
      }
    };
    if ('requestIdleCallback' in window) requestIdleCallback(run, { timeout: 8000 });
    else run();
  }, 5000);
}

ChatApp.registerModule("lifecycle", {
  startSilentKeepAlive,
  sendSystemNotification,
  requestNotificationPermissionAfterInteraction,
  scheduleNonCriticalChatInit,
});
