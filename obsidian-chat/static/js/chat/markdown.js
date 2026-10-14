/* Shared message presentation. Stored messages always retain their Markdown source. */
(() => {
  'use strict';
  const cache = new Map(), streams = new Map();
  const interval = 100, cacheLimit = 2 * 1024 * 1024;
  let parser = null, cacheSize = 0, timer = null, lastPaint = -interval, away = false;
  const blocks = new Set(['heading_open', 'bullet_list_open', 'ordered_list_open',
    'blockquote_open', 'fence', 'code_block', 'table_open', 'hr']);
  const tags = ['p', 'br', 'strong', 'em', 's', 'a', 'code', 'pre', 'blockquote',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li', 'hr', 'table', 'thead',
    'tbody', 'tr', 'th', 'td', 'div', 'span', 'button', 'img'];
  const attributes = ['class', 'href', 'title', 'target', 'rel', 'type', 'data-md-copy',
    'role', 'aria-label', 'tabindex', 'align', 'start', 'src', 'alt', 'loading',
    'decoding', 'referrerpolicy', 'data-math-source', 'data-math-display', 'data-math-pending'];

  function getParser() {
    if (parser) return parser;
    if (!window.markdownit || !window.DOMPurify) return null;
    parser = window.markdownit({ html: false, breaks: true, linkify: false, typographer: false });
    window.ChatMath?.install(parser);
    const originalValidate = parser.validateLink;
    parser.validateLink = url => originalValidate(url) && !/^data:/i.test(url);
    const code = (tokens, index) => {
      const token = tokens[index];
      const language = (token.info || '').trim().split(/\s+/, 1)[0].slice(0, 32);
      return '<div class="md-code-block"><div class="md-code-heading"><span>'
        + escHtml(language) + '</span><button type="button" data-md-copy aria-label="复制代码">复制</button>'
        + '</div><pre><code>' + escHtml(token.content) + '</code></pre></div>\n';
    };
    parser.renderer.rules.fence = code;
    parser.renderer.rules.code_block = code;
    parser.renderer.rules.table_open = () => '<div class="md-table-wrap" tabindex="0" role="region" aria-label="表格"><table>\n';
    parser.renderer.rules.table_close = () => '</table></div>\n';
    for (const type of ['th_open', 'td_open']) parser.renderer.rules[type] = (tokens, index, options, env, renderer) => {
      const token = tokens[index], style = token.attrGet('style') || '';
      const align = style.match(/^text-align:(left|right|center)$/)?.[1];
      if (align) token.attrSet('align', align);
      token.attrs = (token.attrs || []).filter(([name]) => name !== 'style');
      return renderer.renderToken(tokens, index, options);
    };
    parser.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
      tokens[index].attrSet('target', '_blank');
      tokens[index].attrSet('rel', 'noopener noreferrer');
      return renderer.renderToken(tokens, index, options);
    };
    const originalImage = parser.renderer.rules.image;
    parser.renderer.rules.image = (tokens, index, options, env, renderer) => {
      const token = tokens[index];
      token.attrSet('loading', 'lazy'); token.attrSet('decoding', 'async');
      token.attrSet('referrerpolicy', 'no-referrer');
      return originalImage(tokens, index, options, env, renderer);
    };
    return parser;
  }

  function textFromTokens(tokens) {
    let text = '';
    for (const token of tokens) {
      if (token.type === 'inline') text += textFromTokens(token.children || []);
      else if (['text', 'code_inline', 'html_inline'].includes(token.type)) text += token.content;
      else if (token.type === 'image') text += textFromTokens(token.children || []) || token.content;
      else if (token.type === 'fence' || token.type === 'code_block') text += '\n' + token.content + '\n';
      else if (token.type === 'math_inline') text += '公式';
      else if (token.type === 'math_block' || token.type === 'math_pending') text += '\n公式\n';
      else if (['softbreak', 'hardbreak', 'paragraph_close', 'heading_close', 'tr_close', 'hr'].includes(token.type)) text += '\n';
      else if (token.type === 'td_close' || token.type === 'th_close') text += ' ';
    }
    return text;
  }

  function compile(source, plain, useCache) {
    const key = (plain ? 'plain:' : 'md:') + source;
    if (useCache && cache.has(key)) {
      const hit = cache.get(key); cache.delete(key); cache.set(key, hit);
      return hit.result;
    }
    let result, mathPending = false;
    const md = plain ? null : getParser();
    if (!md) {
      result = { expanded: false, body: escHtml(source).replace(/\n/g, '<br>'),
        paragraphs: [escHtml(source).replace(/\n/g, '<br>')], text: source };
    } else {
      const env = {}, tokens = md.parse(source, env);
      const expanded = !!env.mathDisplay || tokens.some(token => blocks.has(token.type));
      const purify = html => {
        const clean = DOMPurify.sanitize(html, { ALLOWED_TAGS: tags, ALLOWED_ATTR: attributes,
          ALLOW_DATA_ATTR: false, ALLOW_ARIA_ATTR: false });
        if (!env.hasMath) return clean;
        // KaTeX fills only parser-owned placeholders after untrusted Markdown is sanitized.
        const math = ChatMath.finishHtml(clean);
        mathPending ||= math.pending;
        return math.html;
      };
      const body = purify(md.renderer.render(tokens, md.options, env));
      const paragraphs = [];
      if (!expanded) {
        for (let start = 0; start < tokens.length; start++) {
          if (tokens[start].type !== 'paragraph_open' || tokens[start].level !== 0) continue;
          let end = start + 1;
          while (end < tokens.length && !(tokens[end].type === 'paragraph_close' && tokens[end].level === 0)) end++;
          paragraphs.push(purify(md.renderer.render(tokens.slice(start, end + 1), md.options, env)));
          start = end;
        }
      }
      result = { expanded, body, paragraphs: paragraphs.length ? paragraphs : [body],
        text: textFromTokens(tokens).replace(/\n{3,}/g, '\n\n').trim() };
    }
    const size = key.length + result.body.length + result.text.length + result.paragraphs.join('').length;
    if (useCache && !mathPending && size < cacheLimit) {
      cache.set(key, { result, size }); cacheSize += size;
      while (cache.size > 256 || cacheSize > cacheLimit) {
        const oldest = cache.keys().next().value;
        cacheSize -= cache.get(oldest).size; cache.delete(oldest);
      }
    }
    return result;
  }

  function prepare(value, { plain = false, msgId = null } = {}) {
    const source = String(value || '');
    const stream = streams.get(msgId);
    const result = compile(source, plain, !stream);
    if (stream && result.expanded) stream.expanded = true;
    const expanded = !plain && (result.expanded || !!stream?.expanded);
    const html = expanded ? '<div class="md-document">' + result.body + '</div>'
      : '<div class="msg-bubbles">' + result.paragraphs.map(p => '<div class="msg-bubble">' + p + '</div>').join('') + '</div>';
    return { expanded, html };
  }

  function foreground() {
    return !away && !document.hidden
      && (typeof isChatForeground !== 'function' || isChatForeground())
      && !document.body.classList.contains('vow-covered')
      && !document.getElementById('subPageOverlay')?.classList.contains('show');
  }
  function suspend() { clearTimeout(timer); timer = null; }
  function paint(id) {
    const message = currentMessages.find(m => m.id === id);
    const row = document.getElementById('m_' + id);
    const content = row?.querySelector('[data-msg-content]');
    if (!message || !content) return;
    const scroller = document.getElementById('messages');
    const follow = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80
      && !document.querySelector('.heart-note-open');
    const plain = isErrorLikeAssistantMessage(message);
    const source = plain ? chatErrorDisplay(message.error_text || message.content) : cleanAssistantContent(message.content);
    const result = prepare(source, { plain, msgId: id });
    content.innerHTML = result.html;
    row.classList.toggle('md-expanded', result.expanded);
    if (follow) scroller.scrollTop = scroller.scrollHeight;
  }
  function flush() {
    suspend();
    if (!foreground()) return;
    for (const [id, state] of streams) {
      if (!state.dirty) continue;
      state.dirty = false;
      if (state.finished) streams.delete(id);
      paint(id);
    }
    lastPaint = performance.now();
  }
  function queue(id) {
    if (!streams.has(id)) streams.set(id, { expanded: false, dirty: false });
    streams.get(id).dirty = true;
    if (!foreground() || timer !== null) return;
    const remaining = interval - (performance.now() - lastPaint);
    if (remaining <= 0) flush();
    else timer = setTimeout(flush, remaining);
  }
  function finish(id) {
    const stream = streams.get(id);
    if (!stream) return;
    stream.finished = true;
    stream.dirty = true;
    if (foreground()) {
      // Final HTML uses the same layout as a later reload.
      streams.delete(id); paint(id);
      if (![...streams.values()].some(s => s.dirty)) suspend();
    }
  }
  function reset(id) {
    streams.delete(id);
    if (foreground()) paint(id);
  }
  function didRender() {
    for (const [id, stream] of streams) {
      const message = currentMessages.find(m => m.id === id);
      if (!message || message.persisted === true || stream.finished) streams.delete(id);
      else stream.dirty = false;
    }
    if (![...streams.values()].some(s => s.dirty)) suspend();
  }
  function resume() {
    if (foreground() && [...streams.values()].some(s => s.dirty)) flush();
    window.ChatMath?.resume();
  }
  function plainText(source) { return compile(String(source || ''), false, true).text; }

  document.addEventListener('visibilitychange', () => document.hidden ? suspend() : resume());
  window.addEventListener('pagehide', () => { away = true; suspend(); });
  window.addEventListener('pageshow', () => { away = false; resume(); });
  document.addEventListener('DOMContentLoaded', () => {
    const cover = new MutationObserver(() => foreground() ? resume() : suspend());
    cover.observe(document.body, { attributes: true, attributeFilter: ['class'] });
    const overlay = document.getElementById('subPageOverlay');
    if (overlay) cover.observe(overlay, { attributes: true, attributeFilter: ['class'] });
    document.getElementById('messages')?.addEventListener('click', async event => {
      const copy = event.target.closest('[data-md-copy]');
      if (!copy) return;
      const code = copy.closest('.md-code-block')?.querySelector('code');
      if (!code) return;
      try { await navigator.clipboard.writeText(code.textContent); showToast('代码已复制'); }
      catch (_) { showToast('复制失败，可以长按选择代码'); }
    });
  }, { once: true });
  window.ChatMarkdown = { prepare, queue, finish, reset, didRender, suspend, resume, plainText,
    renderDocument: source => compile(String(source || ''), false, true).body };
  window.ChatApp?.registerModule('markdown', window.ChatMarkdown);
})();
