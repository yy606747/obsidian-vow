// ── 前台恢复：原生、浏览器恢复、网络恢复共用同一条同步链路 ──
let chatInitialized = false;
let chatSyncPending = true;
let chatSyncGeneration = 0;
let chatRefreshPromise = null;
let chatRefreshRetry = null;
let chatNativeForeground = true;
let chatLastSyncErrorAt = 0;
let chatRefreshEvents = null;

function isChatForeground() {
  return chatNativeForeground && document.visibilityState === 'visible';
}

function callChatNative(method, ...args) {
  try {
    const bridge = window.ObsidianNotifications;
    if (bridge && typeof bridge[method] === 'function') return bridge[method](...args);
  } catch (e) { console.warn('[chat] native callback failed', method, e); }
}

async function readChatJson(url) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(url, { cache: 'no-store', signal: controller.signal });
    if (!response.ok) throw new Error(`消息同步失败 (${response.status})`);
    return await response.json();
  } finally {
    clearTimeout(timeout);
  }
}

function pendingNotificationConversation() {
  return callChatNative('pendingConversationId') || null;
}

function beginChatSync() {
  chatSyncPending = true;
  callChatNative('syncStarted');
  return ++chatSyncGeneration;
}

function completeChatSync(generation) {
  if (generation !== chatSyncGeneration || !isChatForeground()) return false;
  chatSyncPending = false;
  chatLastSyncErrorAt = 0;
  scheduleVisibleMessagesSeen();
  // Report the exact daily window only after sync; native additionally verifies drawing/focus.
  try {
    const bridge = window.ObsidianLookAtMe;
    const request = bridge?.requestId?.();
    if (request && dailyChatState?.enabled) {
      bridge.ready(request, currentConvId, dailyChatState.active_conv_id);
    }
  } catch (error) { console.warn('[look at me] ready deferred', error); }
  return true;
}

function retryChatSync() {
  if (!isChatForeground() || chatRefreshRetry) return;
  chatRefreshRetry = setTimeout(() => {
    chatRefreshRetry = null;
    refreshChat();
  }, 3000);
}

function reportChatSyncFailure(error) {
  console.warn('[chat] refresh failed', error);
  if (isChatForeground() && Date.now() - chatLastSyncErrorAt > 10000) {
    chatLastSyncErrorAt = Date.now();
    showToast('消息同步失败，正在重试', 3000);
  }
  retryChatSync();
}

function refreshChat() {
  if (!isChatForeground()) return Promise.resolve(false);
  if (chatRefreshPromise) return chatRefreshPromise;
  clearTimeout(chatRefreshRetry);
  chatRefreshRetry = null;
  const generation = beginChatSync();
  let stale = false;
  chatRefreshPromise = (async () => {
    try {
      if (!chatInitialized) {
        await init();
        chatInitialized = true;
      } else if (!await refreshConversationMessages()) {
        stale = true;
        return false;
      }
      stale = generation !== chatSyncGeneration;
      return completeChatSync(generation);
    } catch (error) {
      reportChatSyncFailure(error);
      return false;
    } finally {
      chatRefreshPromise = null;
      // 请求期间切过后台或切换过对话，旧响应不能算本次进入已经同步。
      if (stale && isChatForeground()) retryChatSync();
    }
  })();
  return chatRefreshPromise;
}

function onChatForeground() {
  chatNativeForeground = true;
  window.ChatMarkdown?.resume();
  window.ChatHeartNotes?.resume();
  if (typeof connectWS !== 'function' || typeof init !== 'function') return Promise.resolve(false);
  connectWS();
  pingChatSocket();
  return refreshChat();
}

function onChatBackground() {
  persistChatDraft();
  chatNativeForeground = false;
  window.ChatMarkdown?.suspend();
  if (typeof _stopMoodPulse === 'function') _stopMoodPulse();
  window.ChatHeartNotes?.suspend();
  beginChatSync();
  clearTimeout(chatRefreshRetry);
  chatRefreshRetry = null;
}

// 兼容尚未安装新 APK 的原生恢复入口。
function loadMessages() { return refreshChat(); }

function persistChatDraft() {
  if (!currentConvId) return;
  try {
    const value = $('input')?.value || '';
    const key = 'obsidian_chat_draft:' + currentConvId;
    if (value) localStorage.setItem(key, value);
    else localStorage.removeItem(key);
    const filesKey = 'obsidian_chat_attachments:' + currentConvId;
    if (pendingAttachments.length) localStorage.setItem(filesKey, JSON.stringify(pendingAttachments));
    else localStorage.removeItem(filesKey);
  } catch (_) {}
}

function restoreChatDraft() {
  const input = $('input');
  if (!input || !currentConvId) return;
  try { input.value = localStorage.getItem('obsidian_chat_draft:' + currentConvId) || ''; } catch (_) {}
  try { pendingAttachments = JSON.parse(localStorage.getItem('obsidian_chat_attachments:' + currentConvId) || '[]'); } catch (_) {}
  if (typeof renderPreview === 'function') renderPreview();
  if (typeof autoResize === 'function') autoResize(input);
}

$('input')?.addEventListener('input', persistChatDraft);
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    if (chatNativeForeground) onChatForeground();
  } else {
    persistChatDraft();
    beginChatSync();
  }
});
for (const event of ['pageshow', 'focus', 'online']) {
  window.addEventListener(event, () => {
    if (isChatForeground()) onChatForeground();
  });
}

// ── 摄像头/监控日志/记忆库 → 已拆分为独立页面 ──

// ── 静音音频保活（阻止浏览器后台节流） ──
let _keepAliveCtx = null;
function startSilentKeepAlive() {
  try {
    if (_keepAliveCtx) return;
    _keepAliveCtx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = _keepAliveCtx.createOscillator();
    const gain = _keepAliveCtx.createGain();
    gain.gain.value = 0;          // 完全静音
    osc.connect(gain);
    gain.connect(_keepAliveCtx.destination);
    osc.start();
    // 用户交互后 resume（Chrome 要求）
    const resume = () => { if (_keepAliveCtx.state === 'suspended') _keepAliveCtx.resume(); };
    document.addEventListener('click', resume, { once: true });
    document.addEventListener('keydown', resume, { once: true });
  } catch(e) { console.warn('keepalive audio failed:', e); }
}

// ── 系统通知（后台标签也能弹出） ──
function sendSystemNotification(title, body) {
  if (!('Notification' in window)) return;
  if (Notification.permission !== 'granted') return;
  try { new Notification(title, { body, icon: '/public/icon-192.png' }); } catch(e) {}
}

function requestNotificationPermissionAfterInteraction() {
  // web_push.js owns the user-gesture permission request and subscribes in the
  // same flow. Keeping this compatibility function avoids parallel prompts.
}

function scheduleNonCriticalChatInit() {
  window.ChatPerf?.markOnce("noncritical_init_scheduled");
  setTimeout(() => {
    const run = () => {
      if (window.ChatHeartNotes) {
        window.ChatPerf?.markOnce("heart_whispers_init_start");
        window.ChatHeartNotes.init();
      }
    };
    if ('requestIdleCallback' in window) requestIdleCallback(run, { timeout: 8000 });
    else run();
  }, 5000);
}

ChatApp.registerModule("lifecycle", {
  startSilentKeepAlive,
  sendSystemNotification,
  requestNotificationPermissionAfterInteraction,
  scheduleNonCriticalChatInit,
});
