const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function harness() {
  const nodes = new Map();
  function node() {
    let text = '', html = '';
    return {
      get innerHTML() { return text ? text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;') : html; },
      set innerHTML(value) { html = value; text = ''; },
      set textContent(value) { text = String(value); },
      setAttribute(key, value) { this[key] = value; },
      style: {}, hidden: true, value: '',
      addEventListener() {}, querySelectorAll: () => [], querySelector: () => null,
    };
  }
  const context = {
    TextDecoder, FormData, sending: false, streamingAiId: null, whisperMode: false, aiDomSafeword: '', pendingAttachments: [],
    hasVoiceAttachments: () => false,
    document: {
      addEventListener() {}, createElement: node, querySelectorAll: () => [],
      querySelector: () => null,
      getElementById(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); },
    },
    currentConvId: 'c', currentMessages: [], worldBook: { user_name: '小满', ai_name: '知秋' },
    hasMoreMessages: false, msgDebugData: {}, msgMusicCards: {}, _msgMoods: {},
    _heartWhisperMsgIds: new Set(), camCheckMsgId: null, poiSearchMsgId: null,
    activityCheckMsgId: null, screenCheckMsgId: null,
    requestAnimationFrame: () => 1,
    _startTypingAnim() { context.typing = true; },
    _stopTypingAnim() { context.typing = false; },
    _awaitTypingFloor: async () => {},
    _restoreMoodGlows() {},
    aiDomMode: false, voiceInCall: false, ttsEnabled: false,
    ttsSpeak() { throw new Error('a sticker-only message has no speech'); },
  };
  context.window = context;
  vm.createContext(context);
  for (const file of ['core.js', 'ui.js', 'render.js', 'send.js', 'messages.js', 'stickers.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/js/chat', file), 'utf8'), context);
  }
  context.showToast = () => {};
  return { context, nodes };
}

const sticker = { type: 'sticker', id: 'st_1', url: '/uploads/sticker_1.gif', expression: '偷偷探头' };

test('empty response shows a retry notice in the live stream and after rerender, keeping diagnostic details out of the bubble', async () => {
  for (const detail of ['[请求出错: 本轮没有可交付的文字或附件]',
                        '[错误] upstream HTTP 200 / empty_response / req_test: frames=2']) {
    const { context, nodes } = harness();
    const state = context.createSendStreamState();
    await context.handleSendStreamEvent({ type: 'start', id: 'empty' }, state);
    await context.handleSendStreamEvent({ type: 'chunk', content: detail }, state);
    assert.equal(state.hasError, true);
    const message = context.currentMessages[0];
    assert.equal(message.transient_error, true);
    assert.equal(message.persisted, false);
    assert.equal(message.error_text, detail);
    assert.match(message.content, /这次没有收到回复/);
    context.finalizeSendStream(state); // The harness throws if TTS is called.
    context.renderMessages();
    const html = nodes.get('messages').innerHTML;
    assert.match(html, /这次没有收到回复/);
    assert.match(html, /data-msg-action="regenerate"[^>]*>重试/);
    assert.doesNotMatch(html, /本轮没有可交付|empty_response|req_test|tts-replay-btn/);
    assert.equal(context.typing, false);
  }
});

test('a failed regeneration shows a friendly error and retries without deleting a nonexistent message', async () => {
  const { context, nodes } = harness();
  const detail = '[错误] upstream / empty_response / req_test';
  context.currentMessages = [{ id: 'u', role: 'user', content: '晚安' },
    { id: 'previous_error', role: 'assistant', content: detail, transient_error: true, persisted: false }];
  let requestCount = 0;
  context.fetch = async url => {
    requestCount++;
    assert.doesNotMatch(url, /replaced_message_id/);
    const content = requestCount === 1 ? detail : '晚安。';
    const events = [{ type: 'start', id: 'reply_' + requestCount }, { type: 'chunk', content }];
    const bytes = new TextEncoder().encode(events.map(e => `data: ${JSON.stringify(e)}\n\n`).join(''));
    let done = false;
    return { ok: true, headers: { get: () => 'text/event-stream' }, body: { getReader: () => ({ read: async () => {
      if (done) return { done: true };
      done = true;
      return { done: false, value: bytes };
    } }) } };
  };
  await context.regenerateMsg('previous_error');
  assert.equal(context.currentMessages[1].error_text, detail);
  assert.equal(context.currentMessages[1].persisted, false);
  context.renderMessages();
  assert.match(nodes.get('messages').innerHTML, /这次没有收到回复/);
  assert.doesNotMatch(nodes.get('messages').innerHTML, /empty_response|req_test/);
  context._detectAiMood = () => null;
  context._applyMoodGlow = () => {};
  context.ttsSpeak = text => assert.equal(text, '晚安。');
  await context.regenerateMsg('reply_1');
  assert.equal(requestCount, 2);
  assert.equal(context.currentMessages[1].content, '晚安。');
  assert.equal(context.currentMessages[1].transient_error, undefined);
  assert.equal(context.typing, false);
});

test('picker keeps normal photos, stages one sticker, and preserves it through serialization and rendering', async () => {
  const { context, nodes } = harness();
  const second = { ...sticker, id: 'st_2', url: '/uploads/other.gif' };
  context.fetch = async () => ({ ok: true, json: async () => ({ stickers: [sticker, second] }) });
  context.pendingAttachments = [{ url: '/uploads/photo.png', type: 'image/png' }];
  context.persistChatDraft = () => { context.draft = JSON.stringify(context.pendingAttachments); };
  const picker = context.ChatApp.getModule('stickers');
  await picker.toggle();
  assert.equal(nodes.get('stickerPanel').hidden, false);
  assert.match(nodes.get('stickerGrid').innerHTML, /sticker_1.gif/);
  picker.select(sticker.id);
  picker.select(second.id);
  assert.equal(nodes.get('stickerPanel').hidden, true);
  assert.equal(context.pendingAttachments.length, 2);
  const serialized = context.pendingAttachments.map(context.serializeChatAttachment);
  assert.equal(serialized[0], '/uploads/photo.png');
  assert.equal(serialized[1].type, 'sticker');
  assert.equal(serialized[1].id, 'st_2');
  assert.match(nodes.get('previewArea').innerHTML, />图片</);
  assert.match(nodes.get('previewArea').innerHTML, />表情</);
  context.pendingAttachments = JSON.parse(context.draft);
  context.renderPreview();
  assert.equal(context.pendingAttachments[1].type, 'sticker');
  context.currentMessages = [{ id: 'u', conv_id: 'c', role: 'user', content: '', attachments: [serialized[1]] }];
  context.renderMessages();
  assert.match(nodes.get('messages').innerHTML, /msg-sticker-only/);
  assert.match(nodes.get('messages').innerHTML, /<figcaption>表情<\/figcaption>/);
  assert.doesNotMatch(nodes.get('messages').innerHTML, /class="msg-bubble"/);
});

test('picker import deduplicates and a late listing cannot erase an imported sticker', async () => {
  const { context, nodes } = harness();
  const calls = [];
  let finishListing;
  context.fetch = async (url, options) => {
    calls.push([url, options?.method || 'GET']);
    if (options?.method === 'POST') return { ok: true, json: async () => ({ sticker }) };
    return await new Promise(resolve => { finishListing = resolve; });
  };
  const picker = context.ChatApp.getModule('stickers');
  const loading = picker.toggle();
  const file = new File(['gif'], 'cat.gif', { type: 'image/gif' });
  await picker.addFiles({ files: [file, file], value: 'file' });
  finishListing({ ok: true, json: async () => ({ stickers: [] }) });
  await loading;
  assert.equal(nodes.get('stickerCount').innerHTML, '1');
  assert.equal((nodes.get('stickerGrid').innerHTML.match(/data-sticker-id=/g) || []).length, 1);
  assert.equal(nodes.get('stickerAddBtn').disabled, false);
  picker.select(sticker.id);
  assert.equal(context.pendingAttachments[0].type, 'sticker');
  assert.ok(calls.every(([url]) => url === '/api/stickers'));
});

test('chat offers collection for stickers only; collection does not send and survives a stale listing', async () => {
  const { context, nodes } = harness();
  context.currentMessages = [{ id: 'received', role: 'assistant', content: '', attachments: [sticker] }];
  context.renderMessages();
  assert.match(nodes.get('messages').innerHTML, /data-msg-action="collectSticker" data-msg-id="received">收藏表情/);
  context.currentMessages[0].attachments = ['/uploads/photo.png'];
  context.renderMessages();
  assert.doesNotMatch(nodes.get('messages').innerHTML, /collectSticker/);

  let finishListing, finishCollect, posts = 0;
  const toasts = [];
  context.showToast = text => toasts.push(text);
  context.fetch = async (url, options) => {
    if (url === '/api/stickers/collect') {
      posts++;
      assert.equal(options.method, 'POST');
      assert.deepEqual(JSON.parse(options.body), { message_id: 'received' });
      return new Promise(resolve => { finishCollect = resolve; });
    }
    assert.equal(url, '/api/stickers');
    return new Promise(resolve => { finishListing = resolve; });
  };
  const picker = context.ChatApp.getModule('stickers');
  const listing = picker.toggle();
  const collecting = picker.collectMessage('received');
  await picker.collectMessage('received');
  assert.equal(posts, 1);
  finishCollect({ ok: true, json: async () => ({ sticker }) });
  await collecting;
  finishListing({ ok: true, json: async () => ({ stickers: [] }) });
  await listing;
  assert.equal(context.pendingAttachments.length, 0);
  assert.match(nodes.get('stickerGrid').innerHTML, /sticker_1.gif/);
  assert.ok(toasts.includes('已收藏到表情面板'));
  picker.select(sticker.id);
  assert.equal(context.pendingAttachments[0].id, sticker.id);
});

test('failed collection keeps the picker unchanged and can be retried', async () => {
  const { context, nodes } = harness();
  const picker = context.ChatApp.getModule('stickers');
  const toasts = [];
  context.showToast = text => toasts.push(text);
  context.fetch = async () => ({ ok: false, json: async () => ({ detail: '这条消息中没有可收藏的表情' }) });
  await picker.collectMessage('gone');
  assert.ok(toasts.includes('这条消息中没有可收藏的表情'));
  picker.select(sticker.id);
  assert.equal(context.pendingAttachments.length, 0);
  context.fetch = async () => ({ ok: true, json: async () => ({ sticker }) });
  await picker.collectMessage('gone');
  assert.equal(nodes.get('stickerCount').innerHTML, '1');
});

test('failed sticker send restores the draft and does not become an ordinary image', async () => {
  const { context, nodes } = harness();
  context.pendingAttachments = [{ ...sticker }];
  context.persistChatDraft = () => {};
  context._detectUserMood = () => null;
  context._pendingRetract = false;
  context.addErrorToSystemLog = () => {};
  context.fetch = async (_url, options) => {
    assert.equal(JSON.parse(options.body).attachments[0].type, 'sticker');
    return { ok: false, headers: { get: () => 'application/json' }, json: async () => ({ error: '网络暂时不可用' }) };
  };
  await context.send();
  assert.equal(context.pendingAttachments[0].type, 'sticker');
  assert.equal(context.pendingAttachments[0].id, sticker.id);
  assert.equal(context.currentMessages.length, 0);
  assert.match(nodes.get('previewArea').innerHTML, />表情</);
});

test('all sticker controls are hidden, including unfinished streamed tags', () => {
  const { context } = harness();
  assert.equal(context.cleanAssistantContent('喵【偷图】【发图：猫猫】【做表情：写“收到”】'), '喵');
  assert.equal(context.cleanAssistantContent('喵【发图：还没写完'), '喵');
  for (const value of ['白猫举着[OK]的牌子', '白猫举着【牌子[OK]】']) {
    const raw = `喵【做表情：${value}】哦`;
    assert.equal(context.cleanAssistantContent(raw), '喵哦');
    for (let split = 1; split < raw.length - 1; split++) {
      assert.equal(context.cleanAssistantContent(raw.slice(0, split)), '喵');
    }
  }
});

test('sticker-only message renders its original GIF and no empty speech bubble', () => {
  const { context, nodes } = harness();
  context.currentMessages = [{ id: 'a', conv_id: 'c', role: 'assistant', content: '', attachments: [sticker] }];
  context.renderMessages();
  const html = nodes.get('messages').innerHTML;
  assert.match(html, /class="msg-sticker-only"/);
  assert.match(html, /class="chat-sticker" src="\/uploads\/sticker_1.gif"/);
  assert.doesNotMatch(html, /class="msg-bubble"|tts-replay-btn/);
  context.currentMessages[0].content = '偷偷看看。';
  context.renderMessages();
  assert.match(nodes.get('messages').innerHTML, /偷偷看看。/);
  assert.match(nodes.get('messages').innerHTML, /sticker_1.gif/);
});

test('SSE final message replaces a placeholder and deduplicates a WS copy', async () => {
  const { context, nodes } = harness();
  const state = { aiMsgId: 'a', aiContent: '', hasError: false };
  context.currentMessages = [{ id: 'a', conv_id: 'c', role: 'assistant', content: '...' }];
  const message = { id: 'a', conv_id: 'c', role: 'assistant', content: '', attachments: [sticker] };
  await context.handleSendStreamEvent({ type: 'message', message }, state);
  context.upsertSystemMessage(message);
  context.finalizeSendStream(state);
  assert.equal(context.currentMessages.length, 1);
  assert.equal(context.currentMessages[0].attachments[0].id, 'st_1');
  assert.match(nodes.get('messages').innerHTML, /sticker_1.gif/);
});

test('regeneration delivers SSE stickers with no WebSocket, then tolerates a late copy', async () => {
  for (const content of ['', '偷偷看看。']) {
    const { context, nodes } = harness();
    const message = { id: 'new', conv_id: 'c', role: 'assistant', content, attachments: [sticker] };
    context.currentMessages = [{ id: 'u', role: 'user', content: '继续' },
      { id: 'old', conv_id: 'c', role: 'assistant', content: '旧回复' }];
    const events = [{ type: 'start', id: 'new' }];
    if (content) events.push({ type: 'chunk', content });
    events.push({ type: 'message', message });
    const bytes = new TextEncoder().encode(events.map(event => `data: ${JSON.stringify(event)}\n\n`).join(''));
    let done = false;
    context.fetch = async () => ({ ok: true, headers: { get: () => 'text/event-stream' }, body: {
      getReader: () => ({ read: async () => {
        if (done) return { done: true };
        done = true;
        return { done: false, value: bytes };
      } }),
    } });
    context.ttsSpeak = text => assert.equal(text, content);
    context._detectAiMood = () => null;
    context._applyMoodGlow = () => {};
    await context.regenerateMsg('old');
    const rendered = () => nodes.get('messages').innerHTML;
    assert.equal(context.currentMessages.length, 2);
    assert.equal(context.currentMessages[1].content, content);
    assert.equal(context.currentMessages[1].attachments[0].id, 'st_1');
    assert.match(rendered(), /sticker_1.gif/);
    assert.doesNotMatch(rendered(), /\.\.\./);
    assert.equal(context.typing, false);
    assert.equal(context.sending, false);
    context.upsertSystemMessage(message);
    assert.equal(context.currentMessages.length, 2);
    assert.match(rendered(), /sticker_1.gif/);
  }
});
