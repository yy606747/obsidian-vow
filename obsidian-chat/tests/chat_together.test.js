const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function harness({native = true, count = 0, conv = 'today', local = {}, fetcher} = {}) {
  const calls = [], toasts = [];
  const handlers = {}, classes = new Set(), timers = [];
  let resumed = 0;
  const nodes = {
    togetherOpen: {addEventListener(_, handler) {handlers.open = handler;}},
    togetherResume: {addEventListener(_, handler) {handlers.resume = handler;}},
    input: {value: '未发送的日常草稿', readOnly: false},
    togetherChatNotice: {hidden: true}, togetherChatStatus: {},
    modelSelect: {value: 'selected-model'}, contextSlider: {value: count},
  };
  const context = {
    currentConvId: conv, sending: false, closeSidebar() {},
    setTimeout: callback => timers.push(callback), setInterval() {},
    document: {getElementById: id => nodes[id] || null, addEventListener() {},
      body: {classList: {toggle(name, enabled) {if (enabled) classes.add(name); else classes.delete(name);}}}},
    window: {showToast: text => toasts.push(text), addEventListener() {},
      ObsidianTogether: native ? {open: (...args) => calls.push(args),
        state: () => JSON.stringify(local), resume: () => resumed++} : undefined},
  };
  if (fetcher) context.fetch = fetcher;
  vm.createContext(context);
  const load = name => vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/js/chat/', name), 'utf8'), context);
  load('together.js');
  return {click: handlers.open, calls, toasts, nodes, classes, timers, context, load,
    api: context.window.ChatTogether, get resumed() {return resumed;}};
}

test('native entry retains the selected conversation, model and zero history', () => {
  const h = harness(); h.click(); assert.deepEqual(h.calls, [['today', 'selected-model', 0]]);
});
test('native entry bounds history and rejects missing conversation', () => {
  const h = harness({count: 200}); h.click(); assert.equal(h.calls[0][2], 100);
  const empty = harness({conv: ''}); empty.click(); assert.equal(empty.calls.length, 0); assert.equal(empty.toasts.length, 1);
});
test('browser entry explains the native requirement without starting a server session', () => {
  const h = harness({native: false}); h.click(); assert.equal(h.calls.length, 0); assert.equal(h.toasts.length, 1);
});

test('active companionship pauses main chat without losing its draft, then restores it', async () => {
  let session = {id: 'room'};
  const h = harness({fetcher: async () => ({ok: true, json: async () => session})});
  assert.equal(await h.api.guard(), true);
  assert.equal(h.nodes.input.readOnly, true);
  assert.equal(h.nodes.togetherChatNotice.hidden, false);
  assert.equal(h.classes.has('together-active'), true);
  session = null;
  assert.equal(await h.api.guard(), false);
  assert.equal(h.nodes.input.readOnly, false);
  assert.equal(h.nodes.togetherChatNotice.hidden, true);
  assert.equal(h.nodes.input.value, '未发送的日常草稿');
});

test('an offline end receipt keeps ordinary chat held until the native receipt is flushed', async () => {
  const local = {id: 'room', ending: true};
  const h = harness({local, fetcher: async () => ({ok: true, json: async () => null})});
  assert.equal(await h.api.guard(), true);
  assert.match(h.nodes.togetherChatStatus.textContent, /已经结束.*同步/);
  delete local.id;
  assert.equal(await h.api.guard(), false);
});

test('relationship status uses the server name even when an older native bridge only reports an id', async () => {
  let name = '临川';
  const h = harness({local: {id: 'room'}, fetcher: async () => ({ok: true, json: async () => ({id: 'room', ai_name: name})})});
  await h.api.sync();
  assert.equal(h.nodes.togetherChatStatus.textContent, '和 临川 相伴中。');
  name = '青禾'; await h.api.sync();
  assert.equal(h.nodes.togetherChatStatus.textContent, '和 青禾 相伴中。');
  h.api.apply({id: 'another-room'});
  assert.equal(h.nodes.togetherChatStatus.textContent, '正在相伴中。');
});

test('returning to an existing room does not create another session', async () => {
  const h = harness({local: {id: 'room'}});
  await h.click();
  assert.equal(h.resumed, 1);
  assert.equal(h.calls.length, 0);
});

test('entry waits for the ordinary reply and coalesces repeated taps', async () => {
  const h = harness(); h.context.sending = true;
  const first = h.click(); await h.click();
  assert.equal(h.calls.length, 0);
  assert.equal(h.timers.length, 1);
  h.context.sending = false; h.timers.shift()(); await first;
  assert.equal(h.calls.length, 1);
});

test('send and regenerate both stop before touching messages during companionship', async () => {
  const h = harness({local: {id: 'room'}});
  h.context.ChatApp = {registerModule() {}};
  h.load('send.js'); h.load('messages.js');
  await vm.runInContext('send()', h.context);
  await vm.runInContext("regenerateMsg('old-answer')", h.context);
  assert.equal(h.nodes.input.value, '未发送的日常草稿');
  assert.equal(h.toasts.length, 2);
});
