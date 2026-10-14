/* A timestamp-only hint; private work contents never enter localStorage. */
(() => {
  let latest = 0;
  let socket = null, retry = null;
  function paint() {
    const entry = document.querySelector('[data-heart-archive]');
    if (!entry) return;
    const unread = latest > (Number(localStorage.getItem('heartWorksSeenAt')) || 0);
    entry.classList.toggle('heart-work-unread', unread);
    entry.setAttribute('aria-label', unread ? '心语，有新作品' : '心语');
  }
  async function refresh() {
    try {
      const response = await fetch('/api/heart-whispers/works-status');
      if (response.ok) { latest = Number((await response.json()).latest) || 0; paint(); }
    }
    catch (_) { /* Retry on the next foreground visit. */ }
  }
  function receive(message) {
    if (message.type === 'heart_work_published') { latest = Math.max(latest, Number(message.data?.created_at) || 0); paint(); }
  }
  // home.html has no common.js. Nested home pages receive the chat socket;
  // a top-level home page opens only a lightweight listener for this hint.
  window.addEventListener('message', event => {
    if (event.origin === location.origin && event.source === window.parent && event.data?.__obsidianWs) receive(event.data.payload);
  });
  function connect() {
    if (window.parent !== window || document.hidden || socket) return;
    socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`);
    socket.onopen = refresh;
    socket.onmessage = event => { try { receive(JSON.parse(event.data)); } catch (_) {} };
    socket.onclose = () => { socket = null; if (!document.hidden) retry = setTimeout(connect, 10000); };
  }
  function disconnect() {
    clearTimeout(retry);
    if (socket) { socket.onclose = null; socket.close(); socket = null; }
  }
  window.addEventListener('storage', paint);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) disconnect(); else { refresh(); connect(); }
  });
  window.addEventListener('pagehide', disconnect);
  window.addEventListener('pageshow', () => { refresh(); connect(); });
  refresh();
  connect();
})();
