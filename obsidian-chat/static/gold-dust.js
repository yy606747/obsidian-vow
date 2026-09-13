/* Shared decorative layer. Does not own page content or navigation. */
(() => {
  'use strict';
  const preferenceKey = 'obsidian_vow_dust';
  const interval = 160; // At most 6.25 updates/second, independent of display refresh rate.
  const motion = matchMedia('(prefers-reduced-motion: reduce)');
  const compact = matchMedia('(max-width: 768px)');
  let timer, layer, host, observer, particles = [];
  let running = false, inView = false, away = false, lowBattery = false;
  let nativeForeground = true;
  let elapsed = 0, lastTime = 0, updates = 0;
  let area = { width: 0, top: 0, height: 0 };
  let home = false;
  let enabled = stored(preferenceKey);
  let savedGold = stored('obsidian_vow_gold');

  function stored(key) {
    try { return localStorage.getItem(key) !== 'off'; } catch (_) { return true; }
  }
  function reason() {
    const gold = document.body.classList.contains('vow-page') ? document.body.classList.contains('vow-gilded') : savedGold;
    if (!enabled || !gold) return 'off';
    if (motion.matches) return 'reduced-motion';
    if (lowBattery) return 'low-battery';
    if (!nativeForeground) return 'native-background';
    if (document.hidden || away || !inView) return 'hidden';
    if (document.body.classList.contains('vow-covered')) return 'covered';
    if (document.getElementById('subPageOverlay')?.classList.contains('show')) return 'covered';
    if (area.height < 70 || area.width < 100) return 'space';
    return '';
  }
  function controls() {
    document.querySelectorAll('[data-vow-dust]').forEach(button => {
      button.setAttribute('aria-pressed', String(enabled));
      button.querySelector('.vow-gold-state').textContent = enabled ? '已开启' : '已关闭';
    });
  }
  function paint(now) {
    elapsed += Math.min((now - lastTime) / 1000, .3);
    lastTime = now;
    for (const dot of particles) {
      const phase = (dot.phase + elapsed / dot.duration) % 1;
      const drift = Math.sin(phase * Math.PI * 2 + dot.phase * 5);
      const edge = compact.matches ? 7 : 18;
      const lane = edge + dot.lane * (compact.matches ? 4 : 22);
      const x = home
        ? area.width * dot.homeX + drift * 10
        : (dot.side ? area.width - lane - 8 : lane) + drift * 3;
      const y = area.top + area.height * (1 - phase);
      const fade = Math.min(1, phase / .17, (1 - phase) / .2);
      dot.node.style.transform = `translate3d(${x.toFixed(2)}px,${y.toFixed(2)}px,0) scale(${dot.scale})`;
      dot.node.style.opacity = (fade * dot.opacity).toFixed(3);
    }
    updates++;
  }
  function tick() {
    timer = undefined;
    if (!running) return;
    // A media/visibility change can precede delivery of its event.
    if (reason()) { sync(); return; }
    paint(performance.now());
    timer = setTimeout(tick, interval);
  }
  function sync() {
    controls();
    if (!layer) return;
    const stoppedBy = reason();
    layer.dataset.state = stoppedBy || 'running';
    layer.hidden = Boolean(stoppedBy);
    if (stoppedBy) {
      running = false;
      clearTimeout(timer);
      timer = undefined;
    } else if (!running) {
      running = true;
      lastTime = performance.now();
      tick();
    }
  }
  function setEnabled(value) {
    enabled = Boolean(value);
    try { localStorage.setItem(preferenceKey, enabled ? 'on' : 'off'); } catch (_) {}
    sync();
  }
  function setNativeForeground(value) {
    nativeForeground = Boolean(value);
    sync();
    for (const frame of document.querySelectorAll('iframe')) {
      try { frame.contentWindow?.VowDust?.setNativeForeground(nativeForeground); } catch (_) {}
    }
  }
  function measure() {
    const bounds = host.getBoundingClientRect();
    let top = Math.min(95, bounds.height * .18);
    let bottom = bounds.height - 95;
    if (home) {
      const clock = host.querySelector('.clock-widget')?.getBoundingClientRect();
      const firstIcon = host.querySelector('.app-grid .app-icon')?.getBoundingClientRect();
      top = clock ? clock.bottom - bounds.top + 25 : top;
      bottom = firstIcon ? firstIcon.top - bounds.top - 20 : bottom;
    }
    area = { width: bounds.width, top, height: bottom - top };
    const count = compact.matches ? 6 : 10;
    if (particles.length !== count) {
      particles = Array.from({ length: count }, (_, i) => {
        const node = document.createElement('i');
        node.className = 'gold-dust-dot';
        return { node, side: i % 2, lane: (i * .37) % 1,
          homeX: .07 + ((i * .618) % 1) * .86,
          phase: (.19 + i * .137) % 1, duration: 75 + (i * 13) % 39,
          opacity: .55 + (i % 3) * .13, scale: .8 + (i % 3) * .15 };
      });
      layer.replaceChildren(...particles.map(dot => dot.node));
    }
    sync();
  }
  function init() {
    host = document.querySelector('#screen, .chat-area, .sub-page');
    if (!host) return;
    home = host.id === 'screen';
    // Some Android ROMs keep visibilityState="visible" after the Activity pauses.
    // Preserve the existing chat callbacks and share their signal with subpages.
    if (typeof chatNativeForeground !== 'undefined') nativeForeground = chatNativeForeground;
    else if (window.parent !== window) {
      try { nativeForeground = window.parent.VowDust?.state().nativeForeground ?? true; } catch (_) {}
    }
    for (const [name, foreground] of [['onChatForeground', true], ['onChatBackground', false]]) {
      const original = window[name];
      window[name] = function (...args) {
        setNativeForeground(foreground);
        if (typeof original === 'function') return original.apply(this, args);
      };
    }
    layer = document.createElement('div');
    layer.className = 'gold-dust-layer';
    layer.setAttribute('aria-hidden', 'true');
    layer.hidden = true;
    host.append(layer);
    measure();
    if ('IntersectionObserver' in window) {
      observer = new IntersectionObserver(entries => {
        inView = entries[0].isIntersecting;
        sync();
      });
      observer.observe(host);
    } else { inView = true; sync(); }
    new ResizeObserver(measure).observe(host);
    compact.addEventListener('change', measure);
    // Chat's existing cover flag also pauses the layer underneath an iframe.
    const cover = new MutationObserver(sync);
    cover.observe(document.body, { attributes: true, attributeFilter: ['class'] });
    const overlay = document.getElementById('subPageOverlay');
    if (overlay) cover.observe(overlay, { attributes: true, attributeFilter: ['class'] });
    if (navigator.getBattery) {
      navigator.getBattery().then(battery => {
        const checkBattery = () => { lowBattery = !battery.charging && battery.level <= .2; sync(); };
        battery.addEventListener('levelchange', checkBattery);
        battery.addEventListener('chargingchange', checkBattery);
        checkBattery();
      }).catch(() => {});
    }
  }
  motion.addEventListener('change', sync);
  document.addEventListener('visibilitychange', sync);
  window.addEventListener('pagehide', () => { away = true; sync(); });
  window.addEventListener('pageshow', () => { away = false; sync(); });
  window.addEventListener('storage', e => {
    if (!e.key || e.key === preferenceKey) enabled = stored(preferenceKey);
    if (!e.key || e.key === 'obsidian_vow_gold') savedGold = stored('obsidian_vow_gold');
    if (!e.key || e.key === preferenceKey || e.key === 'obsidian_vow_gold') sync();
  });
  window.VowDust = { setEnabled, setNativeForeground, sync,
    state: () => ({ running, nativeForeground, reason: reason(), count: particles.length, updates, interval }) };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
  else init();
})();
