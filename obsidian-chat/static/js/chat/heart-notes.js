/* Heart whispers belong to their reply. Historical notes never auto-play. */
(() => {
  'use strict';
  const notes = new Map();
  const byMessage = new Map();
  const pending = new Set();
  const motion = matchMedia('(prefers-reduced-motion: reduce)');
  const idleDelay = 1800;
  let active = null, idleTimer = null, dismissTimer = null;
  let lastActivity = performance.now(), composing = false, away = false;
  let initialized = false, historyRequest = null, historyFetchedAt = 0;
  let readingAnchor = null;

  function foreground() {
    return !away && !document.hidden
      && (typeof isChatForeground !== 'function' || isChatForeground())
      && !document.body.classList.contains('vow-covered')
      && !document.getElementById('subPageOverlay')?.classList.contains('show');
  }
  function rowFor(msgId) {
    return document.getElementById('m_' + msgId);
  }
  function itemsFor(msgId) {
    return [...(byMessage.get(msgId) || [])].map(key => notes.get(key)).filter(Boolean)
      .sort((a, b) => a.createdAt - b.createdAt);
  }
  function stopIdle() {
    clearTimeout(idleTimer);
    idleTimer = null;
  }
  function pauseDismiss() {
    clearTimeout(dismissTimer);
    dismissTimer = null;
    if (active?.deadline) {
      active.remaining = Math.max(0, active.deadline - performance.now());
      active.deadline = 0;
    }
  }
  function resumeDismiss() {
    if (!active || active.mode !== 'preview' || active.held || dismissTimer || !foreground()) return;
    active.deadline = performance.now() + active.remaining;
    dismissTimer = setTimeout(() => close(false), active.remaining);
  }
  function suspend() {
    stopIdle();
    pauseDismiss();
    document.querySelector('.heart-note-enter')?.classList.remove('heart-note-enter');
  }
  function markerState(msgId) {
    const marker = rowFor(msgId)?.querySelector('.heart-whisper-hint');
    if (marker) marker.setAttribute('aria-expanded', String(active?.msgId === msgId));
  }
  function removePaper() {
    const paper = document.querySelector('.heart-note');
    if (!paper) return;
    const scroller = document.getElementById('messages');
    const bottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
    const above = paper.getBoundingClientRect().bottom < scroller.getBoundingClientRect().top;
    const height = paper.getBoundingClientRect().height + 12;
    paper.remove();
    if (bottom) scroller.scrollTop = scroller.scrollHeight;
    else if (above) scroller.scrollTop -= height;
  }
  function close(returnFocus = false) {
    if (!active) return;
    const msgId = active.msgId;
    pauseDismiss();
    active = null;
    readingAnchor = null;
    removePaper();
    markerState(msgId);
    if (returnFocus) rowFor(msgId)?.querySelector('.heart-whisper-hint')?.focus({ preventScroll: true });
    sync();
  }
  function forgetKey(key) {
    const note = notes.get(key);
    if (!note) return;
    notes.delete(key);
    pending.delete(key);
    byMessage.get(note.msgId)?.delete(key);
    if (!byMessage.get(note.msgId)?.size) byMessage.delete(note.msgId);
  }
  function store(record, live) {
    if (!record?.msg_id || typeof record.content !== 'string' || !record.content.trim()) return;
    const content = record.content.trim(), msgId = String(record.msg_id);
    const key = JSON.stringify([msgId, content]);
    const existing = notes.get(key);
    if (existing) {
      if (record.id) existing.id = record.id;
      return;
    }
    const note = { key, msgId, content, id: record.id || '', createdAt: Number(record.created_at) || 0 };
    notes.set(key, note);
    if (!byMessage.has(msgId)) byMessage.set(msgId, new Set());
    byMessage.get(msgId).add(key);
    if (live) pending.add(key);
    // Recent history and live arrivals stay bounded without storing private text in localStorage.
    for (const [oldKey, old] of notes) {
      if (notes.size <= 400) break;
      if (old.msgId !== active?.msgId) forgetKey(oldKey);
    }
  }
  function renderMarker(msgId) {
    const row = rowFor(msgId);
    const avatar = row?.querySelector('.msg-avatar-col');
    if (!avatar || !row.classList.contains('assistant')) return;
    let marker = avatar.querySelector('.heart-whisper-hint');
    if (!itemsFor(msgId).length) { marker?.remove(); return; }
    if (!marker) {
      marker = document.createElement('button');
      marker.type = 'button';
      marker.className = 'heart-whisper-hint';
      marker.dataset.heartToggle = msgId;
      marker.title = '查看心语';
      marker.setAttribute('aria-label', '查看这条回复的心语');
      marker.setAttribute('aria-controls', 'chatHeartNote');
      marker.innerHTML = '<span aria-hidden="true">💭</span>';
      avatar.append(marker);
    }
    markerState(msgId);
  }
  function timestamp(note) {
    return note.createdAt ? new Date(note.createdAt * 1000).toLocaleString('zh-CN', {
      month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
    }) : '';
  }
  function paint(enter = false) {
    if (!active) return;
    const body = rowFor(active.msgId)?.querySelector('.msg-body');
    const items = itemsFor(active.msgId);
    if (!body || !items.length) { close(false); return; }
    const current = body.querySelector('.heart-note');
    const scroller = document.getElementById('messages');
    const bottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
    const paper = document.createElement('aside');
    paper.id = 'chatHeartNote';
    paper.className = 'heart-note' + (active.mode === 'open' ? ' heart-note-open' : '');
    paper.dataset.msgId = active.msgId;
    paper.setAttribute('aria-label', '心语');
    paper.tabIndex = -1;
    const closeButton = document.createElement('button');
    closeButton.type = 'button';
    closeButton.className = 'heart-note-close';
    closeButton.setAttribute('aria-label', '收起心语');
    closeButton.title = '收起心语';
    closeButton.innerHTML = window.VowUI?.icon('close') || '×';
    closeButton.addEventListener('click', () => close(true));
    paper.append(closeButton);
    if (active.mode === 'preview') {
      const note = notes.get(active.key) || items.at(-1);
      const expand = document.createElement('button');
      expand.type = 'button';
      expand.className = 'heart-note-preview';
      expand.setAttribute('aria-label', '展开阅读完整心语');
      const text = document.createElement('span');
      text.className = 'heart-note-excerpt';
      text.textContent = note.content;
      const more = document.createElement('span');
      more.className = 'heart-note-more';
      more.textContent = items.length > 1 ? '展开 · ' + items.length + ' 条' : '展开';
      expand.append(text, more);
      expand.addEventListener('click', () => open(active.msgId));
      paper.append(expand);
      const hold = () => { if (active?.mode === 'preview') { pauseDismiss(); active.held = true; } };
      const release = () => {
        if (active?.mode === 'preview' && !paper.contains(document.activeElement) && !paper.matches(':hover')) {
          active.held = false;
          resumeDismiss();
        }
      };
      paper.addEventListener('pointerenter', hold);
      paper.addEventListener('pointerleave', release);
      paper.addEventListener('focusin', hold);
      paper.addEventListener('focusout', release);
    } else {
      for (const note of items) {
        const entry = document.createElement('div');
        entry.className = 'heart-note-entry';
        const content = document.createElement('p');
        content.className = 'heart-note-content';
        content.textContent = note.content;
        const time = document.createElement('time');
        time.className = 'heart-note-time';
        time.textContent = timestamp(note);
        entry.append(content, time);
        paper.append(entry);
      }
    }
    if (enter && foreground() && !motion.matches) {
      paper.classList.add('heart-note-enter');
      paper.addEventListener('animationend', () => paper.classList.remove('heart-note-enter'), { once: true });
    }
    if (current) current.replaceWith(paper);
    else body.append(paper);
    markerState(active.msgId);
    if (bottom && active.mode === 'preview') scroller.scrollTop = scroller.scrollHeight;
  }
  function open(msgId) {
    if (!rowFor(msgId) || !itemsFor(msgId).length) return;
    const previous = active?.msgId;
    pauseDismiss();
    stopIdle();
    removePaper();
    for (const key of byMessage.get(msgId) || []) pending.delete(key);
    active = { msgId, mode: 'open' };
    if (previous) markerState(previous);
    paint(true);
    document.getElementById('chatHeartNote')?.focus({ preventScroll: true });
  }
  function canPreview(note) {
    const row = rowFor(note.msgId);
    if (!row || !row.classList.contains('assistant') || row.classList.contains('transient-error')) return false;
    const view = document.getElementById('messages').getBoundingClientRect();
    const bounds = row.getBoundingClientRect();
    return bounds.bottom > view.top && bounds.top < view.bottom;
  }
  function sync() {
    stopIdle();
    if (!foreground()) { suspend(); return; }
    if (active) {
      if (!rowFor(active.msgId)) { close(false); return; }
      resumeDismiss();
      return;
    }
    if (!pending.size || composing || sending || streamingAiId) return;
    const key = [...pending].find(key => notes.has(key) && canPreview(notes.get(key)));
    if (!key) return;
    idleTimer = setTimeout(() => {
      idleTimer = null;
      if (!foreground() || active || composing || sending || streamingAiId
          || performance.now() - lastActivity < idleDelay) { sync(); return; }
      const note = notes.get(key);
      if (!note || !canPreview(note)) return;
      pending.delete(key);
      active = { msgId: note.msgId, key, mode: 'preview', remaining: 8000, deadline: 0, held: false };
      paint(true);
      resumeDismiss();
    }, Math.max(250, idleDelay - (performance.now() - lastActivity)));
  }
  function beforeRender() {
    readingAnchor = active?.mode === 'open' ? document.getElementById('chatHeartNote')?.getBoundingClientRect().top : null;
    pauseDismiss();
  }
  function restore() {
    for (const message of currentMessages) {
      if (!message.inherited && message.role === 'assistant') renderMarker(String(message.id));
    }
    if (active) {
      if (!rowFor(active.msgId)) { close(false); return; }
      active.held = false;
      paint();
      if (readingAnchor != null && active.mode === 'open') {
        const top = readingAnchor, msgId = active.msgId;
        requestAnimationFrame(() => {
          const paper = document.getElementById('chatHeartNote');
          if (active?.msgId === msgId && paper) {
            document.getElementById('messages').scrollTop += paper.getBoundingClientRect().top - top;
          }
        });
      }
    }
    readingAnchor = null;
    sync();
  }
  function receive(record) {
    store(record, true);
    if (record?.msg_id) renderMarker(String(record.msg_id));
    // A second thought on an open note is readable without collapsing the first one.
    if (active?.msgId === String(record?.msg_id) && active.mode === 'open') {
      for (const key of byMessage.get(active.msgId) || []) pending.delete(key);
      paint();
    }
    sync();
  }
  function remove(id) {
    for (const [key, note] of notes) if (note.id === id) forgetKey(key);
    restore();
  }
  async function init() {
    initialized = true;
    if (!foreground() || historyRequest || (historyFetchedAt && Date.now() - historyFetchedAt < 60000)) return;
    historyRequest = (async () => {
      try {
        const result = await api('GET', '/api/heart-whispers?page=1&page_size=100');
        for (const record of [...(result.items || [])].sort((a, b) => a.created_at - b.created_at)) store(record, false);
        historyFetchedAt = Date.now();
        restore();
      } catch (_) { /* The archive and the next foreground sync can retry. */ }
      finally { historyRequest = null; }
    })();
    return historyRequest;
  }
  function resume() {
    sync();
    if (initialized) init();
  }
  function activity(event) {
    lastActivity = performance.now();
    if (event.type === 'input' && active?.mode === 'preview') close(false);
    sync();
  }
  document.addEventListener('DOMContentLoaded', () => {
    const input = document.getElementById('input');
    input?.addEventListener('input', activity);
    input?.addEventListener('compositionstart', () => { composing = true; stopIdle(); });
    input?.addEventListener('compositionend', event => { composing = false; activity(event); });
    document.getElementById('messages')?.addEventListener('scroll', activity, { passive: true });
    document.addEventListener('click', event => {
      const marker = event.target.closest('[data-heart-toggle]');
      if (!marker) return;
      event.preventDefault();
      const msgId = marker.dataset.heartToggle;
      if (active?.msgId === msgId && active.mode === 'open') close(true);
      else open(msgId);
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && active && foreground()) { event.preventDefault(); close(true); }
    });
    const cover = new MutationObserver(resume);
    cover.observe(document.body, { attributes: true, attributeFilter: ['class'] });
    const overlay = document.getElementById('subPageOverlay');
    if (overlay) cover.observe(overlay, { attributes: true, attributeFilter: ['class'] });
  }, { once: true });
  document.addEventListener('visibilitychange', () => document.hidden ? suspend() : resume());
  window.addEventListener('pagehide', () => { away = true; suspend(); });
  window.addEventListener('pageshow', () => { away = false; resume(); });
  motion.addEventListener('change', () => {
    if (motion.matches) document.querySelector('.heart-note-enter')?.classList.remove('heart-note-enter');
  });
  window.ChatHeartNotes = { receive, renderMarker, beforeRender, restore, sync, suspend, resume, init, remove };
})();
