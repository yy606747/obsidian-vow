/* Math tokens stay separate from Markdown and its HTML sanitizer. */
(() => {
  'use strict';
  const base = '/static/vendor/katex/';
  const cache = new Map(), cacheLimit = 1024 * 1024, sourceLimit = 8000;
  let cacheSize = 0, ready = false, loading = null, frame = 0, retryAfter = 0;
  const escape = value => String(value).replace(/[&<>"']/g, char =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
  const keyFor = (source, display) => (display ? 'block:' : 'inline:') + source;

  function delimiter(source, pos) {
    const pair = source.slice(pos, pos + 2);
    if (pair === '$$') return { open: pair, close: pair, display: true };
    if (pair === '\\[') return { open: pair, close: '\\]', display: true };
    if (pair === '\\(') return { open: pair, close: '\\)', display: false };
    if (source[pos] === '$' && source[pos - 1] !== '$'
        && source[pos + 1] && !/\s/.test(source[pos + 1])) {
      return { open: '$', close: '$', display: false };
    }
    return null;
  }

  function closing(source, from, mark) {
    let pos = from;
    while ((pos = source.indexOf(mark, pos)) !== -1) {
      let slashes = 0;
      for (let i = pos - 1; i >= 0 && source[i] === '\\'; i--) slashes++;
      const currency = mark === '$' && (/\s/.test(source[pos - 1] || '')
        || /[\d$]/.test(source[pos + 1] || '') || source[pos - 1] === '$');
      if (slashes % 2 === 0 && !currency) return pos;
      pos += mark.length;
    }
    return -1;
  }

  function install(md) {
    md.inline.ruler.before('escape', 'math', (state, silent) => {
      const start = state.pos, mark = delimiter(state.src, start);
      if (!mark) return false;
      const from = start + mark.open.length;
      const end = closing(state.src, from, mark.close);
      if (end < 0 || (mark.open === '$' && state.src.slice(from, end).includes('\n'))) {
        // Explicit, unfinished TeX delimiters must survive Markdown's escapes.
        if (mark.open === '$') return false;
        if (!silent) state.push('text', '', 0).content = state.src.slice(start);
        state.pos = state.posMax;
        return true;
      }
      if (!silent) {
        const token = state.push('math_inline', '', 0);
        token.content = state.src.slice(from, end);
        token.meta = { display: mark.display, raw: state.src.slice(start, end + mark.close.length) };
        state.env.hasMath = true;
        if (mark.display) state.env.mathDisplay = true;
      }
      state.pos = end + mark.close.length;
      return true;
    });
    md.block.ruler.before('fence', 'math_block', (state, startLine, endLine, silent) => {
      if (state.sCount[startLine] - state.blkIndent >= 4) return false;
      const start = state.bMarks[startLine] + state.tShift[startLine];
      const mark = delimiter(state.src, start);
      if (!mark?.display) return false;
      if (silent) return true;
      let raw = '', end = -1, next = startLine;
      for (; next < endLine; next++) {
        const line = next === startLine ? state.src.slice(start, state.eMarks[next])
          : state.getLines(next, next + 1, state.blkIndent, false).replace(/\n$/, '');
        raw += (next === startLine ? '' : '\n') + line;
        end = closing(raw, mark.open.length, mark.close);
        if (end >= 0) {
          if (raw.slice(end + mark.close.length).trim()) return false;
          next++;
          break;
        }
        if (raw.length > sourceLimit) { next++; break; }
      }
      const token = state.push(end < 0 ? 'math_pending' : 'math_block', '', 0);
      token.block = true;
      token.map = [startLine, next];
      token.content = end < 0 ? raw : raw.slice(mark.open.length, end);
      token.meta = { display: true, raw };
      state.env.mathDisplay = true;
      if (end >= 0) state.env.hasMath = true;
      state.line = next;
      return true;
    }, { alt: ['paragraph', 'reference', 'blockquote', 'list'] });
    const render = (tokens, index) => {
      const token = tokens[index], display = token.meta.display;
      const tag = token.block ? 'div' : 'span';
      return `<${tag} class="md-math${display ? ' md-math-display' : ''}" data-math-source="${escape(token.content)}" data-math-display="${display ? '1' : '0'}" data-math-pending="1">${escape(token.meta.raw)}</${tag}>`;
    };
    md.renderer.rules.math_inline = render;
    md.renderer.rules.math_block = render;
    md.renderer.rules.math_pending = (tokens, index) =>
      `<div class="md-math-source">${escape(tokens[index].content)}</div>\n`;
  }

  function remember(key, value) {
    const size = key.length + value.html.length;
    if (size > cacheLimit) return;
    cache.set(key, { ...value, size }); cacheSize += size;
    while (cache.size > 128 || cacheSize > cacheLimit) {
      const oldest = cache.keys().next().value;
      cacheSize -= cache.get(oldest).size; cache.delete(oldest);
    }
  }

  function render(source, display) {
    const key = keyFor(source, display);
    if (cache.has(key)) {
      const hit = cache.get(key); cache.delete(key); cache.set(key, hit);
      return hit;
    }
    let value;
    try {
      if (source.length > sourceLimit) throw new Error('Formula too long');
      value = { html: katex.renderToString(source, { displayMode: display, throwOnError: true,
        trust: false, strict: 'ignore', maxExpand: 1000, maxSize: 20, macros: {} }), invalid: false };
    } catch (_) {
      value = { html: '', invalid: true };
    }
    remember(key, value);
    return value;
  }

  function apply(node, value) {
    if (!value.invalid) node.innerHTML = value.html;
    else node.classList.add('md-math-source'); // Retain the escaped original on errors.
    if (node.dataset.mathDisplay === '1') {
      node.tabIndex = 0;
      node.setAttribute('role', 'region');
      node.setAttribute('aria-label', '数学公式，可横向滚动');
    }
    node.removeAttribute('data-math-pending');
    node.classList.toggle('md-math-ready', !value.invalid);
  }

  function foreground() {
    return !document.hidden && (typeof isChatForeground !== 'function' || isChatForeground())
      && !document.body.classList.contains('vow-covered')
      && !document.getElementById('subPageOverlay')?.classList.contains('show');
  }

  function hydrate() {
    frame = 0;
    if (!ready || !foreground()) return;
    const scroller = document.getElementById('messages') || document.getElementById('hwList');
    const follow = scroller?.id === 'messages' && scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80
      && !document.querySelector('.heart-note-open');
    const top = scroller?.getBoundingClientRect().top;
    const anchor = scroller && [...scroller.querySelectorAll('.msg-row, .hw-card, .hw-reader > .md-document')]
      .find(node => node.getBoundingClientRect().bottom > top);
    const anchorTop = anchor?.getBoundingClientRect().top;
    const start = performance.now();
    const pending = [...document.querySelectorAll('.md-math[data-math-pending]')];
    let done = 0;
    for (const node of pending) {
      apply(node, render(node.dataset.mathSource, node.dataset.mathDisplay === '1'));
      done++;
      if (performance.now() - start >= 8) break;
    }
    if (follow) scroller.scrollTop = scroller.scrollHeight;
    else if (anchor?.isConnected) scroller.scrollTop += anchor.getBoundingClientRect().top - anchorTop;
    if (done < pending.length) schedule();
  }

  function ensureLoaded() {
    if (ready) return Promise.resolve(true);
    if (loading) return loading;
    if (Date.now() < retryAfter) return Promise.resolve(false);
    const css = document.createElement('link'), script = document.createElement('script');
    css.rel = 'stylesheet'; css.href = base + 'katex.min.css?v=0.19.0';
    script.src = base + 'katex.min.js?v=0.19.0'; script.async = true;
    loading = new Promise(resolve => {
      let loaded = 0, settled = false;
      const finish = success => {
        if (settled) return;
        settled = true; clearTimeout(timeout);
        ready = success && !!window.katex;
        if (!ready) { css.remove(); script.remove(); retryAfter = Date.now() + 15000; }
        loading = null; resolve(ready);
        if (ready) schedule();
      };
      const timeout = setTimeout(() => finish(false), 12000);
      css.onload = script.onload = () => { if (++loaded === 2) finish(true); };
      css.onerror = script.onerror = () => finish(false);
      document.head.append(css, script);
    });
    return loading;
  }

  function schedule() {
    if (!ready) { ensureLoaded(); return; }
    if (!frame && foreground()) frame = requestAnimationFrame(hydrate);
  }

  function finishHtml(html) {
    const template = document.createElement('template');
    template.innerHTML = html; // Only called after Markdown's DOMPurify pass.
    let pending = false;
    for (const node of template.content.querySelectorAll('.md-math[data-math-pending]')) {
      const hit = cache.get(keyFor(node.dataset.mathSource, node.dataset.mathDisplay === '1'));
      if (hit) apply(node, hit);
      else pending = true;
    }
    if (pending) schedule();
    return { html: template.innerHTML, pending };
  }

  document.addEventListener('visibilitychange', () => { if (!document.hidden && document.querySelector('.md-math[data-math-pending]')) schedule(); });
  window.addEventListener('pageshow', () => { if (document.querySelector('.md-math[data-math-pending]')) schedule(); });
  window.ChatMath = { install, finishHtml, resume: () => {
    if (document.querySelector('.md-math[data-math-pending]')) schedule();
  } };
})();
