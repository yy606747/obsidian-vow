(() => {
  'use strict';
  const scroller = document.getElementById('transcript'), rows = document.getElementById('rows');
  const state = { messages: [], outbox: [], stream: '', aiName: '', userName: '' };
  const entries = new Map();
  let following = true, anchor = null, notifiedFollow = null;
  let press = null, pressTimer = null, suppressClickUntil = 0;

  function native(method, ...args) { window.TogetherNative?.[method]?.(...args); }
  function report() {
    if (notifiedFollow !== following) { notifiedFollow = following; native('following', following); }
  }
  function rememberPosition() {
    if (!scroller.clientHeight) return;
    const top = scroller.getBoundingClientRect().top;
    const first = [...rows.children].find(row => row.getBoundingClientRect().bottom > top);
    anchor = first ? { key: first.dataset.key, offset: first.getBoundingClientRect().top - top, y: scroller.scrollTop } : null;
  }
  function restorePosition() {
    if (!scroller.clientHeight) return;
    if (following) scroller.scrollTop = scroller.scrollHeight;
    else if (anchor) {
      const row = entries.get(anchor.key)?.element;
      scroller.scrollTop = row?.isConnected
        ? scroller.scrollTop + row.getBoundingClientRect().top - scroller.getBoundingClientRect().top - anchor.offset
        : anchor.y;
    }
    rememberPosition();
    report();
  }
  function bottom() { following = true; restorePosition(); report(); }
  scroller.addEventListener('scroll', () => {
    if (!scroller.clientHeight) return;
    following = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 40;
    rememberPosition(); report();
  }, { passive: true });
  new ResizeObserver(restorePosition).observe(scroller);

  function render(entry, message) {
    const owner = message.role === 'user';
    const source = String(message.content || '');
    const person = owner ? state.userName : state.aiName;
    const signature = JSON.stringify([message, person]);
    if (entry.signature === signature) return;
    entry.signature = signature;
    entry.source = source;
    const element = entry.element;
    element.className = 'message' + (owner ? ' owner' : '') + (message.streaming ? ' streaming' : '');
    element.setAttribute('role', 'group');
    element.setAttribute('aria-label', person + '：' + ChatMarkdown.plainText(source));
    element.innerHTML = source ? ChatMarkdown.prepare(source).html : '';
    // Linked Markdown images are labels here. Pictures shared in this session have
    // their own capture lifecycle; rendering text must never fetch a remote image.
    for (const img of element.querySelectorAll('img')) img.replaceWith(document.createTextNode(img.alt || '图片'));
    if (message.picture) {
      let first = element.querySelector('.msg-bubble, .md-document');
      if (!first) { first = document.createElement('div'); first.className = 'picture-only'; element.append(first); }
      const tag = document.createElement('div'); tag.className = 'picture-tag'; tag.textContent = '分享了一张画面'; first.prepend(tag);
    }
    if (message.hint) {
      const hint = document.createElement(message.retry ? 'button' : 'div');
      hint.className = 'message-status'; hint.textContent = message.hint;
      if (message.retry) { hint.type = 'button'; hint.dataset.retry = message.retry; }
      element.append(hint);
    }
  }
  function update(patch) {
    rememberPosition();
    Object.assign(state, patch);
    const messages = state.messages.map(item => ({
      key: 'message:' + item.id, role: item.role, content: item.content,
      picture: !!item.capture_id,
      hint: item.status === 'failed' ? '未完成 · 点此重试' : item.status === 'interrupted' ? '已中断' : '',
      retry: item.status === 'failed' ? item.id : ''
    }));
    for (const item of state.outbox) messages.push({
      key: 'outbox:' + item.client_id, role: 'user', content: item.text, picture: !!item.capture_id,
      hint: item.error ? item.error + ' · 点此重新编辑' : '等待连接后发送', retry: item.error ? 'outbox:' + item.client_id : ''
    });
    if (state.stream) messages.push({ key: 'stream', role: 'assistant', content: state.stream, streaming: true });
    const keep = new Set();
    let previous = null;
    for (const message of messages) {
      keep.add(message.key);
      let entry = entries.get(message.key);
      if (!entry) { entry = { element: document.createElement('article') }; entry.element.dataset.key = message.key; entries.set(message.key, entry); }
      render(entry, message);
      const expected = previous ? previous.nextSibling : rows.firstChild;
      if (entry.element !== expected) rows.insertBefore(entry.element, expected);
      previous = entry.element;
    }
    for (const [key, entry] of entries) if (!keep.has(key)) { entry.element.remove(); entries.delete(key); }
    restorePosition();
  }

  function cancelPress() { clearTimeout(pressTimer); pressTimer = null; press = null; }
  function copyMessage(element) {
    const source = entries.get(element?.closest('.message')?.dataset.key)?.source;
    if (source) native('copy', source);
  }
  // One message can have several bubbles. Long-press still copies the complete
  // original Markdown once; it neither creates extra messages nor extra alerts.
  rows.addEventListener('pointerdown', event => {
    if (event.button !== 0 || event.target.closest('button, a')) return;
    cancelPress(); press = { x: event.clientX, y: event.clientY, target: event.target };
    pressTimer = setTimeout(() => { copyMessage(press?.target); suppressClickUntil = Date.now() + 800; cancelPress(); }, 550);
  });
  rows.addEventListener('pointermove', event => {
    if (press && Math.hypot(event.clientX - press.x, event.clientY - press.y) > 8) cancelPress();
  }, { passive: true });
  for (const name of ['pointerup', 'pointercancel', 'pointerleave']) rows.addEventListener(name, cancelPress);
  rows.addEventListener('contextmenu', event => {
    event.preventDefault(); if (Date.now() >= suppressClickUntil) copyMessage(event.target); cancelPress();
  });
  rows.addEventListener('click', event => {
    const target = event.target;
    const copy = target.closest('[data-md-copy]'), retry = target.closest('[data-retry]'), link = target.closest('a');
    if (Date.now() < suppressClickUntil || copy || retry || link) { event.preventDefault(); event.stopPropagation(); }
    if (Date.now() < suppressClickUntil) return;
    if (copy) native('copy', copy.closest('.md-code-block').querySelector('code').textContent);
    else if (retry) native('retry', retry.dataset.retry);
    else if (link) native('openLink', link.href);
  }, true);
  window.addEventListener('blur', cancelPress);
  document.addEventListener('visibilitychange', () => { if (document.hidden) cancelPress(); });
  window.TogetherTranscript = { update, bottom, plainText: ChatMarkdown.plainText };
  report();
})();
