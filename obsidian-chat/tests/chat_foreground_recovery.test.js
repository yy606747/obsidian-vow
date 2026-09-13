const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const scripts = path.join(__dirname, '../static/js/chat');
const message = (id, created_at, conv_id = 'c1') => ({
  id, created_at, conv_id, role: 'assistant', content: id, attachments: [],
});
const conversation = id => ({ id, title: id, model: 'test', message_count: 2 });
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

function harness({ daily = false } = {}) {
  let now = 1_000_000;
  let nextTimer = 1;
  const timers = new Map();
  const requests = [], acknowledgements = [], sockets = [], notices = [];
  const storage = new Map();
  const docEvents = new Map(), windowEvents = new Map();
  const nodes = new Map(), rows = new Map();
  let pendingConvId = '';
  let responseHandler = url => {
    if (url === '/api/models') return [{ key: 'test' }];
    if (url === '/api/worldbook') return { ai_name: 'Companion', user_name: 'Owner' };
    if (url === '/api/conversations') return [conversation('c1')];
    if (url.startsWith('/api/conversations/c1/messages?')) return [message('old', 100), message('latest', 200)];
    throw new Error(`Unexpected request: ${url}`);
  };
  const addListener = (map, name, fn) => map.set(name, [...(map.get(name) || []), fn]);
  function node(id) {
    if (nodes.has(id)) return nodes.get(id);
    const classes = new Set();
    let html = '';
    const element = {
      value: '', textContent: '', disabled: false, scrollHeight: 1000, scrollTop: 0,
      classList: { contains: c => classes.has(c), add: c => classes.add(c), remove: c => classes.delete(c) },
      getBoundingClientRect: () => ({ top: 0, bottom: 600, width: 400, height: 600 }),
      addEventListener() {}, contains: () => true,
      querySelector: () => null,
      querySelectorAll: () => [...rows.values()],
      get innerHTML() { return html; },
      set innerHTML(value) {
        html = value;
        if (id === 'messages') {
          rows.clear();
          for (const match of value.matchAll(/id="(m_[^"]+)"/g)) {
            rows.set(match[1], { getBoundingClientRect: () => ({ top: -500, bottom: -400 }) });
          }
        }
      },
    };
    nodes.set(id, element);
    return element;
  }
  const schedule = (fn, delay, interval = 0) => {
    const id = nextTimer++;
    timers.set(id, { fn, due: now + (delay || 0), interval });
    return id;
  };
  const context = {
    console: { log() {}, warn() {}, error() {} },
    Date: class extends Date { static now() { return now; } },
    AbortController,
    location: { protocol: 'https:', host: 'example.invalid', origin: 'https://example.invalid' },
    localStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, String(value)), removeItem: key => storage.delete(key) },
    ChatApp: { registerModule() {} },
    document: {
      visibilityState: 'visible',
      addEventListener: (name, fn) => addListener(docEvents, name, fn),
      getElementById: id => id.startsWith('m_') ? rows.get(id) || null : node(id),
    },
    addEventListener: (name, fn) => addListener(windowEvents, name, fn),
    $: node,
    escHtml: text => String(text), cleanAssistantContent: text => text,
    formatMsg: text => text, renderAttachments: () => '', avatarUrl: () => '/avatar',
    _restoreMoodGlows() {}, voiceInCall: false, ttsEnabled: false,
    activityCheckMsgId: null,
    showToast: text => notices.push(text), closeSidebar() {}, autoResize() {},
    refreshAvatarVersions: async () => {},
    setTimeout: (fn, delay) => schedule(fn, delay), clearTimeout: id => timers.delete(id),
    setInterval: (fn, delay) => schedule(fn, delay, delay), clearInterval: id => timers.delete(id),
    requestAnimationFrame: fn => schedule(fn, 0),
    WebSocket: class {
      constructor() { this.readyState = 0; this.sent = []; sockets.push(this); }
      send(data) { this.sent.push(data); }
      close() { this.readyState = 3; this.onclose?.(); }
    },
    ObsidianNotifications: {
      syncStarted() {},
      pendingConversationId: () => pendingConvId,
      synced: at => acknowledgements.push(at),
    },
    fetch: async (url, options) => {
      requests.push(url);
      const value = await responseHandler(url, options);
      if (value && value.httpStatus) return { ok: false, status: value.httpStatus };
      return { ok: true, status: 200, json: async () => value };
    },
  };
  context.window = context;
  context.api = async (method, url, body) => responseHandler(url, { method, body });
  vm.createContext(context);
  for (const name of ['state', 'render', 'lifecycle', 'realtime', 'conversations', 'boot']) {
    vm.runInContext(fs.readFileSync(path.join(scripts, `${name}.js`), 'utf8'), context, { filename: `${name}.js` });
  }
  if (daily) vm.runInContext(fs.readFileSync(path.join(scripts, 'daily.js'), 'utf8'), context, { filename: 'daily.js' });
  const run = program => vm.runInContext(program, context);
  const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
  async function advance(milliseconds) {
    const target = now + milliseconds;
    await flush();
    for (let count = 0; count < 1000; count++) {
      const first = [...timers].filter(([, timer]) => timer.due <= target)
        .sort((a, b) => a[1].due - b[1].due)[0];
      if (!first) { now = target; await flush(); return; }
      const [id, timer] = first;
      now = timer.due;
      if (timer.interval) timer.due += timer.interval;
      else timers.delete(id);
      timer.fn();
      await flush();
    }
    throw new Error('Timer loop did not settle');
  }
  function warm() {
    run("currentConvId = 'c1'; conversations = [{id:'c1',title:'c1',model:'test'}]; currentMessages = [{id:'old',conv_id:'c1',role:'assistant',content:'old',created_at:100}];");
    run('connectWS()');
    sockets[0].readyState = 1;
    sockets[0].onopen();
    run('chatInitialized = true');
  }
  return {
    run, context, requests, acknowledgements, sockets, notices, storage, node, warm, advance, flush,
    respond: fn => { responseHandler = fn; }, pending: id => { pendingConvId = id; },
    emitDocument: name => (docEvents.get(name) || []).forEach(fn => fn()),
    emitWindow: name => (windowEvents.get(name) || []).forEach(fn => fn()),
    ids: () => Array.from(run('currentMessages.map(m => m.id)')),
  };
}

test('a websocket error before debug stays transient, is not spoken, and does not increase the saved message count', () => {
  const h = harness(); h.warm();
  const spoken = [];
  h.context.ttsSpeak = text => spoken.push(text);
  h.run('voiceInCall = true; ttsEnabled = true; conversations[0].message_count = 2;');
  const detail = '[错误] upstream empty_response req_test';
  h.context.handleSync({ type: 'msg_created', data: {
    id: 'failed', conv_id: 'c1', role: 'assistant', content: detail,
    transient_error: true, persisted: false, error_text: detail,
  } });
  assert.equal(h.run("currentMessages.find(m => m.id === 'failed').persisted"), false);
  assert.equal(h.run("currentMessages.find(m => m.id === 'failed').error_text"), detail);
  assert.equal(h.run("conversations[0].message_count"), 2);
  assert.deepEqual(spoken, []);
  assert.match(h.node('messages').innerHTML, /这次没有收到回复/);
  assert.doesNotMatch(h.node('messages').innerHTML, /empty_response|req_test/);
});

test('returning with an OPEN socket fetches missed messages and acknowledges latest plus earlier messages', async () => {
  const h = harness(); h.warm();
  await h.run('onChatForeground()');
  assert.deepEqual(h.ids(), ['old', 'latest']);
  assert.equal(h.acknowledgements.length, 0, 'no acknowledgement before render frame');
  await h.advance(0);
  assert.deepEqual(h.acknowledgements, [200]);
  assert.equal(h.sockets.length, 1);
  assert.equal(h.requests.filter(url => url.includes('/messages?')).length, 1);
});

test('cold start with null socket selects latest conversation and restores saved draft', async () => {
  const h = harness();
  h.storage.set('obsidian_chat_draft:c1', 'unsent draft');
  await h.run('onChatForeground()'); await h.advance(0);
  assert.equal(h.run('chatInitialized'), true);
  assert.deepEqual(h.ids(), ['old', 'latest']);
  assert.equal(h.node('input').value, 'unsent draft');
  assert.deepEqual(h.acknowledgements, [200]);
});

test('all visible entry events share an in-flight refresh', async () => {
  const h = harness(); h.warm();
  const held = deferred();
  h.respond(url => url === '/api/conversations' ? held.promise : [message('latest', 200)]);
  const first = h.run('onChatForeground()');
  h.emitWindow('pageshow'); h.emitWindow('focus'); h.emitWindow('online');
  h.emitDocument('visibilitychange');
  assert.equal(h.requests.length, 1);
  held.resolve([conversation('c1')]);
  await first; await h.advance(0);
  assert.equal(h.requests.length, 2);
  assert.deepEqual(h.acknowledgements, [200]);
});

test('an old reconnect timer cannot create a second live socket after native resume', async () => {
  const h = harness(); h.warm();
  h.sockets[0].close();
  await h.run('onChatForeground()');
  h.sockets[1].readyState = 1;
  h.sockets[1].onopen();
  await h.advance(2000);
  assert.equal(h.sockets.length, 2);
  assert.equal(h.sockets.filter(socket => socket.readyState === 1).length, 1);
});

test('a half-open socket is replaced when its foreground heartbeat receives no pong', async () => {
  const h = harness(); h.warm();
  await h.run('onChatForeground()');
  await h.advance(10000);
  assert.equal(h.sockets.length, 2);
  assert.equal(h.sockets[0].readyState, 3);
  assert.equal(h.acknowledgements[0], 200, 'HTTP recovery works independently of the socket');
});

test('pong keeps a healthy socket and stale socket callbacks cannot replace it', async () => {
  const h = harness(); h.warm();
  await h.run('onChatForeground()');
  h.sockets[0].onmessage({ data: '{"type":"pong"}' });
  await h.advance(10000);
  assert.equal(h.sockets.length, 1);
});

test('a connection stalled in the background gets a new connection timeout on resume', async () => {
  const h = harness();
  h.run('connectWS(); onChatBackground();');
  await h.advance(10000);
  assert.equal(h.sockets.length, 1);
  await h.run('onChatForeground()');
  await h.advance(10000);
  assert.equal(h.sockets.length, 2);
  assert.equal(h.sockets[0].readyState, 3);
});

test('failed refresh retains notifications and old messages, then retries without restarting the app', async () => {
  const h = harness(); h.warm();
  h.respond(() => ({ httpStatus: 503 }));
  assert.equal(await h.run('onChatForeground()'), false);
  await h.advance(0);
  assert.deepEqual(h.ids(), ['old']);
  assert.deepEqual(h.acknowledgements, []);
  h.respond(url => url === '/api/conversations' ? [conversation('c1')] : [message('latest', 200)]);
  await h.advance(3000);
  assert.deepEqual(h.ids(), ['latest']);
  assert.deepEqual(h.acknowledgements, [200]);
});

test('a hanging HTTP request times out and is retried', async () => {
  const h = harness(); h.warm();
  h.respond((_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new Error('aborted')));
  }));
  const refresh = h.run('onChatForeground()');
  await h.advance(15000);
  assert.equal(await refresh, false);
  assert.deepEqual(h.acknowledgements, []);
  assert.equal(h.run('chatSyncPending'), true);
});

test('backgrounding during fetch prevents read acknowledgement and forces a fresh snapshot on return', async () => {
  const h = harness(); h.warm();
  const held = deferred();
  h.respond(url => url === '/api/conversations' ? [conversation('c1')] : held.promise);
  const refresh = h.run('onChatForeground()'); await h.flush();
  h.run('onChatBackground()');
  held.resolve([message('latest', 200)]);
  await refresh; await h.advance(0);
  assert.deepEqual(h.acknowledgements, []);
  h.respond(url => url === '/api/conversations' ? [conversation('c1')] : [message('newer', 300)]);
  await h.run('onChatForeground()'); await h.advance(0);
  assert.deepEqual(h.acknowledgements, [300]);
});

test('latest unread conversation is opened even when the previously selected conversation is older', async () => {
  const h = harness(); h.warm(); h.pending('c2');
  h.respond(url => url === '/api/conversations'
    ? [conversation('c2'), conversation('c1')] : [message('latest', 300, 'c2')]);
  await h.run('onChatForeground()'); await h.advance(0);
  assert.equal(h.run('currentConvId'), 'c2');
  assert.deepEqual(h.acknowledgements, [300]);
});

test('messages created, edited and deleted during HTTP refresh survive the snapshot merge', async () => {
  const h = harness(); h.warm();
  const held = deferred();
  h.respond(url => url === '/api/conversations' ? [conversation('c1')] : held.promise);
  const refresh = h.run('onChatForeground()'); await h.flush();
  for (const event of [
    { type: 'msg_created', data: message('newest', 300) },
    { type: 'msg_updated', data: { ...message('edited', 150), content: 'new version' } },
    { type: 'msg_deleted', data: { id: 'old', conv_id: 'c1' } },
  ]) {
    h.context.event = event;
    h.run('handleSync(event)');
  }
  held.resolve([message('old', 100), message('edited', 150), message('latest', 200)]);
  await refresh; await h.advance(0);
  assert.deepEqual(h.ids(), ['edited', 'latest', 'newest']);
  assert.equal(h.run("currentMessages[0].content"), 'new version');
  assert.deepEqual(h.acknowledgements, [300]);
});

test('pending send and streamed reply survive refresh and never advance read time to local placeholders', async () => {
  const h = harness(); h.warm();
  h.run("streamingAiId = 'stream'; currentMessages.push({id:'temp_user',conv_id:'c1',role:'user',content:'draft',created_at:400}, {id:'stream',conv_id:'c1',role:'assistant',content:'partial',created_at:500});");
  await h.run('onChatForeground()'); await h.advance(0);
  assert.deepEqual(h.ids(), ['old', 'latest', 'temp_user', 'stream']);
  assert.deepEqual(h.acknowledgements, [200]);
});

test('switching conversation while refresh is pending cannot apply the old conversation response', async () => {
  const h = harness(); h.warm();
  const held = deferred();
  h.respond(url => {
    if (url === '/api/conversations') return [conversation('c1'), conversation('c2')];
    if (url.includes('/c1/')) return held.promise;
    return [message('c2-message', 400, 'c2')];
  });
  const refresh = h.run('onChatForeground()'); await h.flush();
  await h.run("selectConv('c2')");
  held.resolve([message('c1-late', 600)]);
  await refresh; await h.advance(0);
  assert.equal(h.run('currentConvId'), 'c2');
  assert.deepEqual(h.ids(), ['c2-message']);
  assert.deepEqual(h.acknowledgements, [400]);
});

test('a settings overlay and a hidden document cannot acknowledge unseen chat', async () => {
  const h = harness(); h.warm();
  h.node('subPageOverlay').classList.add('show');
  await h.run('onChatForeground()'); await h.advance(0);
  assert.deepEqual(h.acknowledgements, []);
  h.node('subPageOverlay').classList.remove('show');
  h.context.document.visibilityState = 'hidden';
  h.run('scheduleVisibleMessagesSeen()'); await h.advance(0);
  assert.deepEqual(h.acknowledgements, []);
});

test('bootstrap network failure can recover through the same foreground entry', async () => {
  const h = harness();
  h.respond(() => ({ httpStatus: 503 }));
  await h.run('onChatForeground()');
  assert.equal(h.run('chatInitialized'), false);
  h.respond(url => {
    if (url === '/api/models') return [];
    if (url === '/api/worldbook') return {};
    if (url === '/api/conversations') return [conversation('c1')];
    return [message('recovered', 200)];
  });
  await h.advance(3000);
  assert.equal(h.run('chatInitialized'), true);
  assert.deepEqual(h.ids(), ['recovered']);
  assert.deepEqual(h.acknowledgements, [200]);
});

function dailyFixture() {
  const h = harness({ daily: true });
  let activeModel = 'test';
  let state = { enabled: true, count_initialized: true, inherit_count: 30, timezone: 'Asia/Shanghai',
    day_key: '2026-09-08', revision: 1, active_conv_id: 'c2', redirects: { c1: 'c2' },
    next_rollover_at: 90000, latest_assistant_at: 200 };
  let messages = [{ ...message('old', 100, 'c2'), inherited: true, origin_conv_id: 'c1' }, message('new-day', 200, 'c2')];
  let messageHandler = () => messages;
  h.respond((url, options) => {
    if (url === '/api/chat/daily') return state;
    if (url === '/api/models') return [{ key: 'gemini-3-flash' }, { key: 'test' }, { key: 'chosen-model' }];
    if (url === '/api/worldbook') return { user_name: '青禾', ai_name: '临川' };
    if (url === '/api/conversations') return [{ ...conversation('c2'), model: activeModel }, conversation('c1')];
    if (url.includes('/messages?')) return messageHandler(url, options);
    throw new Error(url);
  });
  return { h, state: () => state, messages: () => messages,
    setModel: model => { activeModel = model; },
    setState: next => { state = next; }, setMessages: next => { messages = next; },
    respondMessages: fn => { messageHandler = fn; },
    event() { h.context.rollover = { state, conv_id: 'c2', previous_conv_id: 'c1', conversation: { ...conversation('c2'), model: activeModel } }; h.run('receiveDailyRollover(rollover)'); },
  };
}

test('midnight carries an active stream, text, attachment, recording and control session before backfill', async () => {
  const f = dailyFixture(), h = f.h; h.warm();
  const held = deferred(); f.respondMessages(() => held.promise);
  const recorder = { sourceConvId: 'c1', sourceModelKey: 'test', active: true };
  const moves = [];
  h.context.ChatApp.getModule = name => name === 'voiceMessage' ? { recorder } : null;
  h.context.ControlRuntime = { rolloverConversation: (...args) => moves.push(args) };
  h.node('input').value = '还没发完的草稿';
  h.run("sending=true; streamingAiId='stream'; pendingAttachments=[{url:'/uploads/photo.png'}]; currentMessages.push({id:'stream',conv_id:'c1',role:'assistant',content:'前半句',created_at:300});");
  f.event(); await h.flush(); await h.advance(0);
  assert.equal(h.run('currentConvId'), 'c2');
  assert.equal(h.run('sending'), true);
  assert.equal(h.run('streamingAiId'), 'stream');
  assert.equal(h.run("currentMessages[0].content"), '前半句');
  assert.equal(recorder.sourceConvId, 'c2'); assert.equal(recorder.active, true);
  assert.deepEqual(moves, [['c1', 'c2']]);
  assert.equal(h.storage.get('obsidian_chat_draft:c2'), '还没发完的草稿');
  assert.equal(h.run('pendingAttachments[0].url'), '/uploads/photo.png');
  assert.equal(h.acknowledgements.length, 0, 'rollover itself is not a read receipt');
  h.node('input').value += '，切窗后又写了一句'; h.run('persistChatDraft()');
  held.resolve(f.messages()); await h.flush(); await h.advance(0);
  assert.deepEqual(h.ids(), ['new-day', 'stream']);
  assert.equal(h.node('input').value, '还没发完的草稿，切窗后又写了一句');
  assert.equal(h.run('pendingAttachments.length'), 1);
  assert.deepEqual(h.acknowledgements, [200]);
  f.event(); await h.flush();
  assert.equal(moves.length, 1, 'duplicate WS event must not migrate twice');
});

test('cold entry follows today despite an old notification and migrates saved draft and attachments', async () => {
  const f = dailyFixture(), h = f.h;
  h.pending('c1'); h.storage.set('obsidian_last_conv', 'c1');
  h.storage.set('obsidian_chat_draft:c1', '进程重建前的草稿');
  h.storage.set('obsidian_chat_attachments:c1', '[{"url":"/uploads/saved.jpg"}]');
  await h.run('onChatForeground()'); await h.advance(0);
  assert.equal(h.run('currentConvId'), 'c2');
  assert.equal(h.node('input').value, '进程重建前的草稿');
  assert.equal(h.run('pendingAttachments[0].url'), '/uploads/saved.jpg');
  assert.equal(h.storage.has('obsidian_chat_draft:c1'), false);
  assert.deepEqual(h.acknowledgements, [200]);
});

test('background midnight clears notifications after successful entry even when today has no visible messages', async () => {
  const f = dailyFixture(), h = f.h; h.warm();
  h.run('onChatBackground()');
  f.setMessages([{ ...message('last-input', 250, 'c2'), role: 'user', inherited: true }]);
  f.event(); await h.advance(0);
  assert.equal(h.acknowledgements.length, 0);
  f.respondMessages(() => ({ httpStatus: 503 }));
  await h.run('onChatForeground()'); await h.advance(0);
  assert.equal(h.acknowledgements.length, 0);
  f.respondMessages(() => f.messages());
  await h.run('refreshChat()'); await h.advance(0);
  assert.deepEqual(h.ids(), []);
  assert.deepEqual(h.acknowledgements, [200]);
});

test('late old-window HTTP snapshot cannot overwrite a rollover or resurrect its draft', async () => {
  const f = dailyFixture(), h = f.h; h.warm();
  const next = f.state(), held = deferred();
  f.setState({ ...next, revision: 0, active_conv_id: 'c1', redirects: {} });
  f.respondMessages(url => url.includes('/c1/') ? held.promise : f.messages());
  const oldFetch = h.run('onChatForeground()'); await h.flush();
  h.node('input').value = '写到一半';
  f.setState(next); f.event();
  held.resolve([message('late-old-snapshot', 999)]);
  await oldFetch; await h.advance(3000);
  assert.equal(h.run('currentConvId'), 'c2');
  assert.deepEqual(h.ids(), ['new-day']);
  assert.equal(h.node('input').value, '写到一半');
  assert.equal(h.acknowledgements.includes(999), false);
});

test('legacy inherited rows never display or consume current-day pagination', async () => {
  const f = dailyFixture(), h = f.h;
  const inherited = Array.from({ length: 80 }, (_, i) => ({ ...message('past'+i, i, 'c2'), inherited: true }));
  f.setMessages([...inherited, message('today', 500, 'c2')]);
  await h.run('onChatForeground()');
  assert.deepEqual(h.ids(), ['today']);
  assert.doesNotMatch(h.node('messages').innerHTML, /past0|跨日背景/);
  assert.equal(h.run('hasMoreMessages'), false);
  f.setMessages([...inherited, ...Array.from({ length: 50 }, (_, i) => message('today'+i, 500+i, 'c2'))]);
  await h.run('refreshChat()');
  assert.equal(h.run('hasMoreMessages'), true);
  let before = null;
  f.respondMessages(url => { before = url; return [message('older-today', 499, 'c2')]; });
  await h.run('loadOlderMessages()');
  assert.match(before, /before=500$/);
  assert.equal(new Set(h.ids()).size, h.ids().length);
});

test('cold entry and a missed midnight event synchronize the inherited non-default model', async () => {
  for (const warm of [false, true]) {
    const f = dailyFixture(), h = f.h;
    if (warm) h.warm();
    h.node('modelSelect').value = 'gemini-3-flash';
    f.setModel('chosen-model');
    await h.run('onChatForeground()');
    assert.equal(h.run('currentConvId'), 'c2');
    assert.equal(h.node('modelSelect').value, 'chosen-model');
    assert.deepEqual(h.ids(), ['new-day']);
  }
});

test('live midnight and remote model updates keep the selector in sync', async () => {
  const f = dailyFixture(), h = f.h; h.warm();
  f.setModel('chosen-model');
  f.event(); await h.flush();
  assert.equal(h.node('modelSelect').value, 'chosen-model');
  h.run("handleSync({type:'conv_updated',data:{id:'c2',model:'another-selected-model'}})");
  assert.equal(h.node('modelSelect').value, 'another-selected-model');
});
