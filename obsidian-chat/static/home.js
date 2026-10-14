/* Visual lifecycle only. The original app registry and navigation stay in home.html. */
(() => {
  'use strict';
  let clockTimer;
  let wallFrame = 0;
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const screen = document.getElementById('screen');
  const clock = document.querySelector('.clock-widget');
  const dock = document.getElementById('dock');
  // Both day/night assets are 720 × 1279. These are image coordinates, not
  // percentages of the viewport: leave the sill and skirting board untouched.
  const wallpaper = { width: 720, height: 1279, wallTop: 672, wallBottom: 928 };
  function layoutWall() {
    const { width, height, top } = screen.getBoundingClientRect();
    if (!width || !height) return;
    const columns = width >= 560 && height < 560 ? 8 : 4;
    const rows = Math.ceil(document.getElementById('appGrid').children.length / columns);
    if (!rows) return;
    const gap = 12;
    const trim = 24; // 7 px hanging point + 4 px label gap + 13 px label.
    const roomTop = clock.getBoundingClientRect().bottom - top + 16;
    const roomBottom = dock.getBoundingClientRect().top - top - 12;
    const roomHeight = Math.max(0, roomBottom - roomTop);
    const minimum = Math.min(roomHeight, rows * (44 + trim) + (rows - 1) * gap);
    // Only zoom beyond cover on short portrait screens where the original
    // wall cannot hold two rows of readable, >= 44 px icons.
    const scale = Math.max(width / wallpaper.width, height / wallpaper.height,
      minimum / (wallpaper.wallBottom - wallpaper.wallTop));
    const imageWidth = wallpaper.width * scale;
    const imageHeight = wallpaper.height * scale;
    let imageY = (height - imageHeight) / 2;
    // Keep the normal centered crop unless part of the usable wall is hidden
    // behind the clock or Dock. Never move the image beyond its cover bounds.
    const earliest = roomTop + minimum - wallpaper.wallBottom * scale;
    const latest = roomBottom - minimum - wallpaper.wallTop * scale;
    imageY = Math.max(height - imageHeight, Math.min(0, Math.min(latest, Math.max(earliest, imageY))));
    const wallTop = Math.max(roomTop, imageY + wallpaper.wallTop * scale);
    const wallBottom = Math.min(roomBottom, imageY + wallpaper.wallBottom * scale);
    const wallHeight = Math.max(0, wallBottom - wallTop);
    const wallWidth = Math.max(0, Math.min(width - 32, columns === 8 ? 720 : 480));
    const iconSize = Math.max(44, Math.min(60,
      Math.floor((wallHeight - (rows - 1) * gap) / rows - trim),
      Math.floor((wallWidth - (columns - 1) * gap) / columns)));
    for (const [key, value] of Object.entries({
      '--home-background-size': `${imageWidth}px ${imageHeight}px`,
      '--home-background-y': `${imageY}px`,
      '--home-wall-top': `${wallTop}px`,
      '--home-wall-height': `${wallHeight}px`,
      '--home-wall-width': `${wallWidth}px`,
      '--home-wall-columns': String(columns),
      '--home-wall-icon-size': `${iconSize}px`,
    })) screen.style.setProperty(key, value);
  }
  function scheduleWallLayout() {
    if (wallFrame || document.hidden) return;
    wallFrame = requestAnimationFrame(() => { wallFrame = 0; layoutWall(); });
  }
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
    if (document.hidden) {
      stopClock();
      cancelAnimationFrame(wallFrame); wallFrame = 0;
    } else {
      updateVisibleTime();
      layoutWall();
    }
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
    cancelAnimationFrame(wallFrame); wallFrame = 0;
    document.body.classList.add('home-paused');
    document.body.classList.remove('home-arrival');
  });
  window.addEventListener('pageshow', syncVisibility);
  window.addEventListener('resize', scheduleWallLayout);
  if (typeof ResizeObserver !== 'undefined') {
    const observer = new ResizeObserver(scheduleWallLayout);
    for (const element of [screen, clock, dock]) observer.observe(element);
  }
  syncVisibility();
})();
