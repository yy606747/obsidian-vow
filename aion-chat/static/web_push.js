/* Web Push bootstrap shared by the chat page and the standalone pages. */
(function () {
  if (window.__aionWebPushLoaded) return;
  window.__aionWebPushLoaded = true;

  const supported =
    'serviceWorker' in navigator &&
    'PushManager' in window &&
    'Notification' in window;
  if (!supported || window.parent !== window) return;

  let registration = null;
  let retryTimer = null;
  const fallbackAlarmSeen = new Map();

  function applicationServerKey(value) {
    const padding = '='.repeat((4 - value.length % 4) % 4);
    const base64 = (value + padding).replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(base64);
    return Uint8Array.from(raw, ch => ch.charCodeAt(0));
  }

  function scheduleRetry() {
    if (retryTimer || Notification.permission !== 'granted') return;
    retryTimer = setTimeout(() => {
      retryTimer = null;
      ensureSubscription().catch(scheduleRetry);
    }, 60_000);
  }

  async function sendSubscription(subscription) {
    const payload = subscription.toJSON();
    const response = await fetch('/api/push/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(payload),
    });
    if (!response.ok) throw new Error('push subscription registration failed');
  }

  async function ensureSubscription() {
    if (Notification.permission !== 'granted') return;
    registration = registration || await navigator.serviceWorker.ready;
    let subscription = await registration.pushManager.getSubscription();
    if (!subscription) {
      const response = await fetch('/api/push/public-key', {
        credentials: 'same-origin',
      });
      if (!response.ok) throw new Error('VAPID public key unavailable');
      const body = await response.json();
      subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: applicationServerKey(body.public_key),
      });
    }
    await sendSubscription(subscription);
  }

  function armPermissionRequest() {
    if (Notification.permission !== 'default') return;
    const requestOnce = async () => {
      document.removeEventListener('click', requestOnce, true);
      document.removeEventListener('keydown', requestOnce, true);
      try {
        const permission = await Notification.requestPermission();
        if (permission === 'granted') {
          await ensureSubscription();
        }
      } catch (_) {
        scheduleRetry();
      }
    };
    document.addEventListener('click', requestOnce, { once: true, capture: true });
    document.addEventListener('keydown', requestOnce, { once: true, capture: true });
  }

  function fallbackAlarmKey(data) {
    const ids = Array.isArray(data?.ids) ? [...data.ids].sort().join(',') : '';
    return ids || data?.id || `${data?.trigger_at || ''}|${data?.content || ''}`;
  }

  function showFallbackAlarm(data) {
    const now = Date.now();
    for (const [key, seenAt] of fallbackAlarmSeen) {
      if (now - seenAt > 10 * 60_000) fallbackAlarmSeen.delete(key);
    }
    const key = fallbackAlarmKey(data);
    if (key && fallbackAlarmSeen.has(key)) return;
    if (key) fallbackAlarmSeen.set(key, now);

    let overlay = document.getElementById('webPushAlarmOverlay');
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.id = 'webPushAlarmOverlay';
      overlay.style.cssText = 'position:fixed;inset:0;z-index:2147483647;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.78);padding:24px';
      const box = document.createElement('div');
      box.style.cssText = 'width:min(420px,100%);padding:28px;border-radius:20px;background:#241f1c;color:#f4ebe3;text-align:center;box-shadow:0 18px 60px rgba(0,0,0,.45)';
      box.innerHTML = '<div style="font-size:44px">⏰</div><h2 style="margin:8px 0 14px">日程提醒</h2><div data-alarm-content style="font-size:18px;white-space:pre-wrap"></div><div data-alarm-time style="margin-top:10px;opacity:.7"></div><button type="button" style="margin-top:22px;padding:10px 28px;border:0;border-radius:999px;background:#d4943a;color:#1a1714;font-weight:700">确认</button>';
      box.querySelector('button').addEventListener('click', () => {
        overlay.style.display = 'none';
      });
      overlay.appendChild(box);
      document.body.appendChild(overlay);
    }
    overlay.querySelector('[data-alarm-content]').textContent = data?.content || '日程提醒';
    overlay.querySelector('[data-alarm-time]').textContent = data?.trigger_at || '';
    overlay.style.display = 'flex';
  }

  function deliverToPage(message) {
    if (!message || message.type !== 'schedule_alarm') return;
    if (typeof window.showAlarmPopup === 'function') {
      window.showAlarmPopup(message.data || {});
    } else {
      showFallbackAlarm(message.data || {});
    }
  }

  async function init() {
    navigator.serviceWorker.addEventListener('message', event => {
      deliverToPage(event.data);
    });
    try {
      registration = await navigator.serviceWorker.register('/sw.js');
      if (Notification.permission === 'granted') {
        await ensureSubscription();
      } else {
        armPermissionRequest();
      }
    } catch (_) {
      scheduleRetry();
    }
  }

  window.addEventListener('online', () => {
    ensureSubscription().catch(scheduleRetry);
  });
  window.AionWebPush = { init, ensureSubscription };
  init();
})();
