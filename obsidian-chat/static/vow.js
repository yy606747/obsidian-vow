/* Shared navigation and visual helpers. Page scripts keep ownership of their data. */
(() => {
  'use strict';
  const embedded = window.parent !== window;
  const path = location.pathname.replace(/\/$/, '') || '/';
  document.documentElement.classList.toggle('vow-embedded', embedded);
  const icons = {
    home: '<path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z"/>',
    chat: '<path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5H5l-3 2 1.5-5A8.5 8.5 0 1 1 21 11.5Z"/><path d="M7 10h9M7 14h5"/>',
    memory: '<path d="M12 5c-3-2-6-2-10-1v16c4-1 7-1 10 1 3-2 6-2 10-1V4c-4-1-7-1-10 1Zm0 0v16"/>',
    heart: '<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8Z"/>',
    calendar: '<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M7 3v4m10-4v4M3 11h18m-13 5h2m4 0h2"/>',
    settings: '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="15" cy="17" r="3"/>',
    more: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
    arrow: '<path d="M4 12h16m-6-6 6 6-6 6"/>',
    back: '<path d="M20 12H4m6-6-6 6 6 6"/>',
    close: '<path d="m6 6 12 12M6 18 18 6"/>',
    menu: '<path d="M4 7h16M4 12h12M4 17h16"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    mic: '<rect x="9" y="2" width="6" height="13" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2m-7 9v3m-4 0h8"/>',
    smile: '<circle cx="12" cy="12" r="9"/><path d="M8 14a4 4 0 0 0 8 0M8 9h.01M16 9h.01"/>',
    send: '<path d="m21 3-7 18-4-7-7-4 18-7ZM10 14 21 3"/>',
    volume: '<path d="m11 4-6 5H2v6h3l6 5V4Zm4 4a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14"/>',
    folder: '<path d="M3 7V5a2 2 0 0 1 2-2h5l3 4h6a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/>',
    devices: '<rect x="2" y="3" width="14" height="12" rx="2"/><path d="M9 15v5m-4 0h8"/><rect x="17" y="10" width="5" height="12" rx="1"/>',
    location: '<path d="M20 10c0 6-8 12-8 12S4 16 4 10a8 8 0 1 1 16 0Z"/><circle cx="12" cy="10" r="2.5"/>',
    activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    log: '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 7h6m-6 5h6m-6 5h4"/>',
    lock: '<rect x="4" y="10" width="16" height="11" rx="3"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 5v2"/>',
    wave: '<path d="M2 8c4-8 6 8 10 0s6 8 10 0M2 16c4-8 6 8 10 0s6 8 10 0"/>',
    moon: '<path d="M21 13a9 9 0 0 1-10-10 9 9 0 1 0 10 10Z"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',
    bookmark: '<path d="M6 3h12v19l-6-4-6 4V3Z"/>',
    edit: '<path d="m15 4 5 5M4 15 16 3a2 2 0 0 1 5 5L9 20l-6 1 1-6Z"/>',
    trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    sparkle: '<path d="m12 2 2.6 7.4L22 12l-7.4 2.6L12 22l-2.6-7.4L2 12l7.4-2.6L12 2Z"/>',
  };
  function icon(name, className = '') {
    return `<svg class="vow-icon ${className}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.55" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.more}</svg>`;
  }
  function engraving() {
    // A fixed line engraving: no filters, bitmap downloads, timers or animation.
    return `<svg class="vow-engraving" viewBox="0 0 280 160" fill="none" aria-hidden="true" focusable="false">
      <g stroke="currentColor" stroke-width=".8">
        <path class="vow-leaf" d="M150 148C81 138 30 101 15 44C73 50 115 74 150 148Z"/>
        <path class="vow-leaf" d="M150 148C150 75 185 27 250 17C245 81 220 120 150 148Z"/>
        <path class="vow-leaf" d="M150 148C115 90 105 43 136 5C177 39 184 91 150 148Z"/>
        <path d="M28 55C73 73 107 101 150 148C190 100 216 62 241 27M136 17C143 56 147 102 150 148"/>
        <path d="m50 66 4 33 32-12m-8-4 4 40 31-12m-7-5 3 28 27-9M136 43l-15 16 29 8m-8 6-13 16 25 6m-8 7-7 16 14 7m78-84-35 10 17 13m6-6-40 12 23 15m4-7-42 13 24 13m-2-1-25 10 9 13"/>
        <path d="M146 148H27m0-3v6m129-3h109m0-3v6" opacity=".4"/>
      </g></svg>`;
  }
  const primary = [
    ['/', 'home', '小屋'], ['/chat', 'chat', '聊天'],
    ['/memory', 'memory', '记忆库'], ['/heart-whispers', 'heart', '心语'],
  ];
  const secondary = [
    ['/schedule', 'calendar', '日程与提醒'], ['/memory#vows', 'bookmark', '共同誓约'],
    ['/worldbook', 'memory', '世界书'], ['/chat?whisper=1', 'heart', '密语'],
    ['/devices', 'devices', '设备'], ['/location', 'location', '定位地图'],
    ['/activity-logs', 'activity', '活动日志'], ['/monitor-logs', 'log', '监控日志'],
    ['/settings', 'settings', '设置'],
  ];
  const pages = {
    '/memory': ['记忆库', 'memory'], '/heart-whispers': ['心语', 'heart'], '/schedule': ['日程', 'calendar'],
    '/worldbook': ['世界书', 'memory'], '/settings': ['设置', 'settings'], '/devices': ['设备', 'devices'],
    '/location': ['定位追踪', 'location'], '/activity-logs': ['活动日志', 'activity'],
    '/monitor-logs': ['监控日志', 'log'], '/camera': ['摄像头', 'devices'],
  };
  function links(items) {
    return items.map(([href, glyph, label]) => `<a href="${href}" data-vow-link>${icon(glyph)}<span>${label}</span></a>`).join('');
  }
  function brand() {
    return `<a href="/" class="vow-brand" data-vow-link aria-label="Obsidian Vow 小屋"><span class="vow-mark"><svg viewBox="0 0 36 42" fill="none" aria-hidden="true"><path d="M4 37V18a14 14 0 0 1 28 0v19H4Z" stroke="currentColor" stroke-width="1.3"/><path d="M10 21v7l8 7 8-7v-7M18 35V15" stroke="currentColor" stroke-width="1.3"/><circle cx="18" cy="10" r="1.5" fill="currentColor"/></svg></span><span>Obsidian <em>Vow</em></span></a>`;
  }
  function navigate(url) {
    const target = new URL(url, location.origin);
    if (target.origin !== location.origin) return;
    closeTools();
    if (embedded && window.parent.VowUI) {
      window.parent.VowUI.navigate(target.pathname + target.search + target.hash);
      return;
    }
    if (path === '/chat' && typeof window.openSubPage === 'function') {
      if (target.pathname === '/chat') {
        window.closeSubPage();
        if (target.searchParams.get('whisper') === '1') window.openWhisper?.();
      } else {
        window.openSubPage(target.pathname + target.search + target.hash);
      }
      return;
    }
    location.assign(target.href);
  }
  function setActive(url) {
    const active = new URL(url, location.origin).pathname;
    document.querySelectorAll('.vow-primary a').forEach(a => {
      const selected = new URL(a.href).pathname === active;
      a.classList.toggle('active', selected);
      if (selected) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    });
  }
  function setNames(book) {
    const ai = String(book?.ai_name || '').trim();
    const user = String(book?.user_name || '').trim();
    document.querySelectorAll('[data-vow-names]').forEach(el => { el.textContent = [user, ai].filter(Boolean).join(' · '); });
    const name = document.getElementById('chatPersonName');
    if (name && ai) name.textContent = ai;
    const avatar = document.getElementById('chatPersonAvatar');
    if (avatar && ai) avatar.alt = ai;
  }
  function openTools() {
    const dialog = document.getElementById('vowToolsDialog');
    if (dialog && !dialog.open) dialog.showModal();
  }
  function closeTools() { document.getElementById('vowToolsDialog')?.close(); }
  const goldKey = 'obsidian_vow_gold';
  function goldEnabled() {
    try { return localStorage.getItem(goldKey) !== 'off'; } catch (_) { return true; }
  }
  function setGold(enabled, persist = true) {
    document.body.classList.toggle('vow-gilded', enabled);
    document.querySelectorAll('[data-vow-gold]').forEach(button => {
      button.setAttribute('aria-pressed', String(enabled));
      button.querySelector('.vow-gold-state').textContent = enabled ? '已开启' : '已关闭';
    });
    if (persist) {
      try { localStorage.setItem(goldKey, enabled ? 'on' : 'off'); } catch (_) {}
    }
    window.VowDust?.sync();
  }
  function initGold() {
    // Fixed gold material and a separately switchable, bounded dust layer.
    setGold(goldEnabled(), false);
    window.addEventListener('storage', e => { if (e.key === goldKey) setGold(goldEnabled(), false); });
    const syncVisibility = () => document.body.classList.toggle('vow-paused', document.hidden);
    document.addEventListener('visibilitychange', syncVisibility);
    window.addEventListener('pagehide', () => document.body.classList.add('vow-paused'));
    window.addEventListener('pageshow', syncVisibility);
    syncVisibility();
  }
  function arrangeSettings() {
    const panel = document.querySelector('.tab-panel[data-panel="basic"]');
    if (!panel) return;
    const cards = [...panel.querySelectorAll(':scope > .card')];
    if (!cards.length) return;
    const grid = document.createElement('div');
    grid.className = 'vow-settings-grid';
    cards[0].before(grid);
    cards.forEach(card => grid.append(card));
    panel.querySelector('#imageHistoryTurns')?.closest('.card')?.classList.add('vow-settings-wide');
    const symbols = {
      screenCaptureToggle: 'devices', mobileScreenCaptureToggle: 'devices', lookAtMeToggle: 'heart',
      ringTouchToggle: 'wave', webSearchToggle: 'log', deviceBaselineToggle: 'activity',
      opportunityToggle: 'sun', imageHistoryTurns: 'chat',
    };
    Object.entries(symbols).forEach(([id, glyph]) => {
      const title = panel.querySelector(`#${id}`)?.closest('.card')?.querySelector('.card-title');
      title?.insertAdjacentHTML('afterbegin', `<span class="vow-setting-symbol">${icon(glyph)}</span>`);
    });
  }
  function init() {
    document.body.classList.add('vow-page');
    document.body.dataset.vowPage = path === '/' ? 'home' : path.slice(1);
    if (!embedded) {
      const nav = document.createElement('div');
      nav.className = 'vow-navigation';
      nav.innerHTML = `${brand()}<nav class="vow-primary" aria-label="主导航">${links(primary)}</nav>`;
      const sidebar = path === '/chat' && document.getElementById('sidebar');
      if (sidebar) {
        sidebar.prepend(nav);
      } else {
        const rail = document.createElement('aside');
        rail.className = 'vow-shell-nav';
        rail.append(nav);
        rail.insertAdjacentHTML('beforeend', `<div class="vow-nav-foot"><a href="/schedule" data-vow-link>${icon('calendar')}日程与提醒</a><button type="button" data-vow-tools>${icon('more')}工具与设置</button></div>`);
        document.body.prepend(rail);
      }
    }
    const dialog = document.createElement('dialog');
    dialog.id = 'vowToolsDialog';
    dialog.className = 'vow-tools-dialog';
    dialog.innerHTML = `<div class="vow-dialog-heading"><div><h2>工具与设置</h2></div><button type="button" data-vow-close-tools aria-label="关闭">${icon('close')}</button></div><nav class="vow-tools-grid" aria-label="工具与设置">${links(secondary)}</nav><button class="vow-gold-toggle" type="button" data-vow-gold aria-pressed="true">${icon('sparkle')}<span>鎏金细节</span><span class="vow-gold-state">已开启</span></button><button class="vow-gold-toggle" type="button" data-vow-dust aria-pressed="true">${icon('wave')}<span>流金光点</span><span class="vow-gold-state">已开启</span></button>`;
    dialog.addEventListener('click', e => { if (e.target === dialog) closeTools(); });
    document.body.append(dialog);
    document.querySelectorAll('[data-vow-icon]').forEach(el => { el.innerHTML = icon(el.dataset.vowIcon); });
    const config = pages[path];
    const content = document.querySelector('.sub-page > .page-content');
    if (config && content) {
      const heading = document.createElement('header');
      heading.className = 'vow-page-heading';
      heading.innerHTML = `<span class="vow-heading-seal">${icon(config[1])}</span><h1>${config[0]}</h1>${engraving()}`;
      content.prepend(heading);
      const bar = document.querySelector('.top-bar');
      if (bar) {
        const back = bar.querySelector('.back-btn');
        if (back) {
          back.innerHTML = icon('back'); back.setAttribute('aria-label', '返回小屋');
          back.onclick = () => navigate('/');
        }
        const title = bar.querySelector('h2');
        if (title?.firstChild?.nodeType === Node.TEXT_NODE) title.firstChild.textContent = config[0] + ' ';
        const more = document.createElement('button');
        more.className = 'vow-icon-button'; more.type = 'button'; more.dataset.vowTools = '';
        more.setAttribute('aria-label', '工具与设置'); more.innerHTML = icon('more'); bar.append(more);
      }
    }
    if (path === '/chat') document.querySelector('.chat-header')?.insertAdjacentHTML('afterbegin', engraving());
    if (path === '/settings') arrangeSettings();
    setActive(location.href);
    initGold();
    document.addEventListener('click', e => {
      const link = e.target.closest('[data-vow-link]');
      if (link && !e.defaultPrevented && e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey) {
        e.preventDefault(); navigate(link.getAttribute('href'));
      }
      if (e.target.closest('[data-vow-tools]')) openTools();
      if (e.target.closest('[data-vow-close-tools]')) closeTools();
      if (e.target.closest('[data-vow-home]')) navigate('/');
      if (e.target.closest('[data-vow-gold]')) setGold(!document.body.classList.contains('vow-gilded'));
      const dust = e.target.closest('[data-vow-dust]');
      if (dust) window.VowDust?.setEnabled(dust.getAttribute('aria-pressed') !== 'true');
    });
    document.addEventListener('keydown', e => {
      if (e.target.id === 'chatTitle' && (e.key === 'Enter' || e.key === ' ')) {
        e.preventDefault(); e.target.click();
      }
    });
  }
  window.VowUI = { icon, navigate, setActive, setNames, openTools, closeTools, setGold };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
  else init();
})();
