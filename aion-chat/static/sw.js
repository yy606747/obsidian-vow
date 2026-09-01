// Obsidian Vow Service Worker — PWA lifecycle + alarm Web Push.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));

self.addEventListener('push', event => {
  event.waitUntil((async () => {
    let payload = {};
    try {
      payload = event.data ? event.data.json() : {};
    } catch (_) {
      payload = { body: event.data ? event.data.text() : '日程提醒' };
    }

    const alarmData = payload.data || {};
    const windows = await self.clients.matchAll({
      type: 'window',
      includeUncontrolled: true,
    });
    const focused = windows.filter(client => client.focused);
    if (focused.length) {
      focused.forEach(client => client.postMessage({
        type: 'schedule_alarm',
        data: alarmData,
      }));
      return;
    }

    const ids = Array.isArray(alarmData.ids) ? alarmData.ids : [];
    const scheduleId = alarmData.id || ids.join('-') || alarmData.trigger_at || 'unknown';
    await self.registration.showNotification(payload.title || '⏰ 闹铃', {
      body: payload.body || alarmData.content || '日程提醒',
      icon: '/public/icon-192.png',
      badge: '/public/icon-192.png',
      tag: `aion-alarm-${scheduleId}`,
      data: { url: payload.url || '/chat' },
    });
  })());
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil((async () => {
    let target = new URL('/chat', self.location.origin);
    try {
      const requested = new URL(event.notification.data?.url || '/chat', self.location.origin);
      if (requested.origin === self.location.origin) target = requested;
    } catch (_) {}

    const windows = await self.clients.matchAll({
      type: 'window',
      includeUncontrolled: true,
    });
    const existing = windows.find(client => client.url.startsWith(self.location.origin));
    if (existing) {
      await existing.focus();
      return;
    }
    await self.clients.openWindow(target.href);
  })());
});
