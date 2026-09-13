// 每日窗口由后端决定；前端只接续显示和本机尚未发送的输入。
let dailyChatState = null;
let dailyChatTimer = null;

function resolveDailyConversation(id) {
  return dailyChatState?.enabled ? (dailyChatState.redirects?.[id] || id) : id;
}

function dailyEntryTarget(fallback) {
  return dailyChatState?.enabled ? (dailyChatState.active_conv_id || fallback) : fallback;
}

function acceptDailyChatState(state) {
  if (!state || typeof state.revision !== 'number') throw new Error('每日对话状态无效');
  if (dailyChatState && state.revision < dailyChatState.revision) return;
  dailyChatState = state;
  if ($('dailyChatEnabled')) $('dailyChatEnabled').checked = state.enabled;
  if ($('dailyContextSlider')) $('dailyContextSlider').value = state.inherit_count;
  if ($('dailyContextValue')) $('dailyContextValue').textContent = state.inherit_count + '条';
  if ($('dailyChatTime')) $('dailyChatTime').textContent = `${state.timezone} · 每天 00:00`;
  clearTimeout(dailyChatTimer);
  if (state.enabled) {
    // WS 提供即时切换；定时校准补上断线、休眠和漏推送。
    const delay = Math.max(1000, Math.min(2147483647, state.next_rollover_at * 1000 - Date.now() + 100));
    dailyChatTimer = setTimeout(() => {
      dailyChatTimer = null;
      if (isChatForeground()) refreshChat();
    }, delay);
  }
}

async function syncDailyChatState() {
  let state = await readChatJson('/api/chat/daily');
  if (!state.count_initialized) {
    const initial = Number(localStorage.getItem('obsidian_context_limit')) || 30;
    state = await api('PUT', '/api/chat/daily', { initial_count: initial });
  }
  acceptDailyChatState(state);
  return dailyChatState;
}

async function saveDailyChatSettings(update) {
  try {
    acceptDailyChatState(await api('PUT', '/api/chat/daily', update));
    if ('inherit_count' in update) showToast('已保存，下次凌晨切窗生效', 2000);
    if (dailyChatState.enabled) refreshChat();
  } catch (error) {
    showToast('每日对话设置保存失败', 3000);
    console.warn('[daily chat] save failed', error);
  }
}

function migrateDailyDrafts(target, sources) {
  try {
    const textKey = 'obsidian_chat_draft:';
    const filesKey = 'obsidian_chat_attachments:';
    let text = localStorage.getItem(textKey + target) || '';
    let files = JSON.parse(localStorage.getItem(filesKey + target) || '[]');
    for (const source of new Set(sources.filter(id => id && id !== target))) {
      const previous = localStorage.getItem(textKey + source) || '';
      if (previous && previous !== text) text = [text, previous].filter(Boolean).join('\n');
      const previousFiles = JSON.parse(localStorage.getItem(filesKey + source) || '[]');
      files.push(...previousFiles);
      localStorage.removeItem(textKey + source);
      localStorage.removeItem(filesKey + source);
    }
    if (text) localStorage.setItem(textKey + target, text);
    const unique = [...new Map(files.map(file => [file.url || JSON.stringify(file), file])).values()];
    if (unique.length) localStorage.setItem(filesKey + target, JSON.stringify(unique));
  } catch (error) { console.warn('[daily chat] draft migration failed', error); }
}

function prepareDailyEntryDraft(target, previous) {
  if (!dailyChatState?.enabled) return;
  const sources = Object.keys(dailyChatState.redirects || {}).filter(id => resolveDailyConversation(id) === target);
  if (previous && resolveDailyConversation(previous) === target) sources.push(previous);
  migrateDailyDrafts(target, sources);
}

function handoverDailyWindow(target) {
  if (!target || currentConvId === target) return false;
  const previous = currentConvId;
  persistChatDraft();
  prepareDailyEntryDraft(target, previous);
  if (previous) migrateDailyDrafts(target, [previous]);
  currentConvId = target;
  localStorage.setItem('obsidian_last_conv', target);
  // 保留流对象、消息 ID、录音器和上传 Promise；HTTP 回填后继续用同一条流。
  currentMessages = currentMessages.filter(message =>
    message.id === 'temp_user' || message.id === streamingAiId
    || message.id === _recoverStreamMsgId || message.persisted === false
  ).map(message => ({ ...message, conv_id: target }));
  const recorder = window.ChatApp?.getModule?.('voiceMessage')?.recorder;
  if (recorder?.sourceConvId === previous) recorder.sourceConvId = target;
  window.ControlRuntime?.rolloverConversation?.(previous, target);
  restoreChatDraft();
  const conv = conversations.find(item => item.id === target);
  $('chatTitle').textContent = conv?.title || dailyChatState?.day_key || '';
  if (conv?.model) $('modelSelect').value = conv.model;
  if (recorder?.sourceConvId === target) recorder.sourceModelKey = $('modelSelect').value;
  hasMoreMessages = false;
  renderConvList();
  renderMessages();
  return true;
}

function receiveDailyRollover(data) {
  if (!data?.state?.enabled) return;
  const previousRevision = dailyChatState?.revision ?? -1;
  acceptDailyChatState(data.state);
  if (data.state.revision <= previousRevision) return;
  if (data.conversation && !conversations.some(item => item.id === data.conv_id)) {
    conversations.unshift(data.conversation);
  }
  beginChatSync();
  handoverDailyWindow(data.conv_id);
  if (isChatForeground()) refreshChat();
}
