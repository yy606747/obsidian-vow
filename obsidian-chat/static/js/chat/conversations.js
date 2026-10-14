// ── 对话 ──
async function newConversation() {
  if (typeof aiDomGuard === 'function' && aiDomGuard('新建对话')) return;
  const model = $("modelSelect").value;
  const today = new Date();
  const title = today.getFullYear() + '-' + String(today.getMonth()+1).padStart(2,'0') + '-' + String(today.getDate()).padStart(2,'0');
  const conv = await api("POST", "/api/conversations", { title, model });
  await selectConv(conv.id);
  closeSidebar();
}

async function selectConv(id, { syncManaged = false } = {}) {
  if (typeof aiDomGuard === 'function' && id !== currentConvId && aiDomGuard('切换对话')) return;
  const generation = syncManaged ? chatSyncGeneration : beginChatSync();
  if (id !== currentConvId) persistChatDraft();
  currentConvId = id;
  restoreChatDraft();
  localStorage.setItem('obsidian_last_conv', id);
  msgDebugData = {};
  const conv = conversations.find(c => c.id === id);
  if (conv) {
    $("chatTitle").textContent = conv.title;
    $("modelSelect").value = conv.model;
  }
  const events = [];
  chatRefreshEvents = events;
  let messages;
  try {
    messages = await readChatJson(`/api/conversations/${encodeURIComponent(id)}/messages?limit=${MSG_PAGE_SIZE}`);
  } catch (error) {
    if (syncManaged) throw error;
    reportChatSyncFailure(error);
    return false;
  } finally {
    if (chatRefreshEvents === events) chatRefreshEvents = null;
  }
  if (currentConvId !== id || generation !== chatSyncGeneration) return false;
  if (!Array.isArray(messages)) throw new Error('消息同步返回了无效数据');
  currentMessages = mergeConversationSnapshot(id, messages, events);
  hasMoreMessages = messages.filter(message => !message.inherited).length >= MSG_PAGE_SIZE;
  window.ChatPerf?.markOnce("first_message_batch_loaded", { count: currentMessages.length });
  renderConvList();
  renderMessages({ scroll: 'bottom' });
  window.ChatPerf?.markOnce("first_messages_rendered", { count: currentMessages.length });
  $("sendBtn").disabled = sending;
  if (window.ControlRuntime) ControlRuntime.recover(id).catch(e => console.warn("[ControlRuntime] recover failed", e));
  closeSidebar();
  if (!syncManaged) completeChatSync(generation);
  window.ChatHeartWorks?.consumePending();
  return true;
}

async function refreshConversationMessages() {
  const generation = chatSyncGeneration;
  if (typeof syncDailyChatState === 'function') {
    await syncDailyChatState();
    if (generation !== chatSyncGeneration) return false;
    handoverDailyWindow(dailyEntryTarget(currentConvId));
  }
  const selectedBefore = currentConvId;
  const loaded = await readChatJson('/api/conversations');
  if (!Array.isArray(loaded)) throw new Error('对话列表同步返回了无效数据');
  if (currentConvId !== selectedBefore || generation !== chatSyncGeneration) return false;
  conversations = loaded;
  const pendingId = typeof dailyEntryTarget === 'function'
    ? dailyEntryTarget(pendingNotificationConversation()) : pendingNotificationConversation();
  const target = conversations.find(c => c.id === pendingId)
    || conversations.find(c => c.id === currentConvId) || conversations[0];
  if (!target) {
    currentConvId = null;
    currentMessages = [];
    renderConvList();
    renderMessages();
    return true;
  }
  if (target.id !== currentConvId) return selectConv(target.id, { syncManaged: true });

  const convId = currentConvId;
  // HTTP 快照期间收到的推送在快照之后重放，避免覆盖新消息或复活已删除消息。
  const events = [];
  chatRefreshEvents = events;
  try {
    const limit = Math.min(500, Math.max(MSG_PAGE_SIZE, currentMessages.filter(message => !message.inherited).length));
    const messages = await readChatJson(`/api/conversations/${encodeURIComponent(convId)}/messages?limit=${limit}`);
    if (currentConvId !== convId || generation !== chatSyncGeneration) return false;
    if (!Array.isArray(messages)) throw new Error('消息同步返回了无效数据');
    currentMessages = mergeConversationSnapshot(convId, messages, events);
    hasMoreMessages = messages.filter(message => !message.inherited).length >= limit;
    $('chatTitle').textContent = target.title;
    if (target.model) $('modelSelect').value = target.model;
    renderConvList();
    renderMessages();
    return true;
  } finally {
    if (chatRefreshEvents === events) chatRefreshEvents = null;
  }
}

function mergeConversationSnapshot(convId, messages, events) {
  // Cross-day records are prompt input only, including responses from an old
  // server during a release. Only this window's own messages belong in the UI.
  const merged = new Map(messages.filter(m => !m.inherited && m.conv_id === convId)
    .map(m => [m.id, { ...m, persisted: true }]));
  for (const message of currentMessages) {
    if (!message.inherited && message.conv_id === convId && !merged.has(message.id)
        && (message.id === 'temp_user' || message.id === streamingAiId
          || message.id === _recoverStreamMsgId || message.persisted === false)) {
      merged.set(message.id, message);
    }
  }
  for (const event of events) {
    const existing = merged.get(event.data.id);
    if (event.data.conv_id !== convId || event.data.inherited) continue;
    if (event.type === 'msg_deleted') merged.delete(event.data.id);
    else merged.set(event.data.id, { ...existing, ...event.data, conv_id: convId, persisted: true });
  }
  return [...merged.values()].sort((a, b) => a.created_at - b.created_at);
}

async function loadOlderMessages() {
  if (!currentConvId || !hasMoreMessages || loadingMore) return;
  loadingMore = true;
  const oldest = currentMessages.find(message => !message.inherited);
  if (!oldest) { loadingMore = false; return; }
  const el = $("messages");
  const convId = currentConvId;
  try {
    const page = await api("GET", `/api/conversations/${convId}/messages?limit=${MSG_PAGE_SIZE}&before=${oldest.created_at}`);
    const older = page.filter(message => !message.inherited && message.conv_id === convId);
    if (convId !== currentConvId) return;
    if (older.length === 0) { hasMoreMessages = false; renderMessages({ scroll: 'preserve' }); return; }
    hasMoreMessages = older.length >= MSG_PAGE_SIZE;
    currentMessages = [...new Map([...older, ...currentMessages].map(message => [message.id, message])).values()]
      .sort((a, b) => a.created_at - b.created_at);
    renderMessages({ scroll: 'preserve' });
  } finally {
    loadingMore = false;
  }
}

async function delConv(id) {
  if (!confirm("确定删除此对话？")) return;
  await api("DELETE", `/api/conversations/${id}`);
}

async function changeModel() {
  if (!currentConvId) return;
  const convId = currentConvId;
  const model = $("modelSelect").value;
  const result = await api("PUT", `/api/conversations/${convId}`, { model });
  const target = result.conv_id || (typeof resolveDailyConversation === 'function' ? resolveDailyConversation(convId) : convId);
  const conv = conversations.find(item => item.id === target);
  if (conv) conv.model = model;
  if (currentConvId === target) $("modelSelect").value = model;
  if (!currentModelSupportsAudioInput() && document.querySelector(".input-area")?.classList.contains("voice-mode")) {
    ChatApp.getModule("voiceMessage")?.setMode(false);
    showAudioInputUnavailable();
  }
}

async function renameConv(id) {
  const conv = conversations.find(c => c.id === id);
  if (!conv) return;
  const newTitle = prompt("重命名对话:", conv.title);
  if (newTitle !== null && newTitle.trim() && newTitle !== conv.title) {
    await api("PUT", `/api/conversations/${id}`, { title: newTitle.trim() });
  }
}

function renameCurrent() {
  if (currentConvId) renameConv(currentConvId);
}
