/* Visual lifecycle only. The original app registry and navigation stay in home.html. */
(() => {
  'use strict';
  let clockTimer;
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  function stopClock() {
    clearTimeout(clockTimer);
    clockTimer = undefined;
    document.body.dataset.homeClock = 'paused';
  }
  function updateVisibleTime() {
    stopClock();
    if (document.hidden) return;
    updateClock();
    updateBackground();
    document.body.dataset.homeClock = 'running';
    clockTimer = setTimeout(updateVisibleTime, 60000 - Date.now() % 60000);
  }
  function syncVisibility() {
    document.body.classList.toggle('home-paused', document.hidden);
    if (document.hidden) stopClock();
    else updateVisibleTime();
  }
  if (!document.hidden && !reducedMotion.matches) document.body.classList.add('home-arrival');
  document.addEventListener('animationend', event => {
    if (event.animationName === 'homeGoldPass' && event.target.closest('.app-icon') === document.querySelector('.dock .app-icon:last-child')) {
      document.body.classList.remove('home-arrival');
    }
  });
  reducedMotion.addEventListener('change', () => {
    if (reducedMotion.matches) document.body.classList.remove('home-arrival');
  });
  document.addEventListener('visibilitychange', syncVisibility);
  window.addEventListener('pagehide', () => {
    stopClock();
    document.body.classList.add('home-paused');
    document.body.classList.remove('home-arrival');
  });
  window.addEventListener('pageshow', syncVisibility);
  syncVisibility();
})();
