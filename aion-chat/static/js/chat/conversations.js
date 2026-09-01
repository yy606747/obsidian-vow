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

async function selectConv(id) {
  if (typeof aiDomGuard === 'function' && id !== currentConvId && aiDomGuard('切换对话')) return;
  currentConvId = id;
  localStorage.setItem('aion_last_conv', id);
  msgDebugData = {};
  const conv = conversations.find(c => c.id === id);
  if (conv) {
    $("chatTitle").textContent = conv.title;
    $("modelSelect").value = conv.model;
  }
  currentMessages = await api("GET", `/api/conversations/${id}/messages?limit=${MSG_PAGE_SIZE}`);
  hasMoreMessages = currentMessages.length >= MSG_PAGE_SIZE;
  window.ChatPerf?.markOnce("first_message_batch_loaded", { count: currentMessages.length });
  renderConvList();
  renderMessages();
  window.ChatPerf?.markOnce("first_messages_rendered", { count: currentMessages.length });
  $("sendBtn").disabled = false;
  if (window.ControlRuntime) ControlRuntime.recover(id).catch(e => console.warn("[ControlRuntime] recover failed", e));
  closeSidebar();
}

async function loadOlderMessages() {
  if (!currentConvId || !hasMoreMessages || loadingMore) return;
  loadingMore = true;
  const oldest = currentMessages[0];
  if (!oldest) { loadingMore = false; return; }
  const el = $("messages");
  const prevHeight = el.scrollHeight;
  try {
    const older = await api("GET", `/api/conversations/${currentConvId}/messages?limit=${MSG_PAGE_SIZE}&before=${oldest.created_at}`);
    if (older.length === 0) { hasMoreMessages = false; return; }
    hasMoreMessages = older.length >= MSG_PAGE_SIZE;
    currentMessages = [...older, ...currentMessages];
    renderMessages();
    // 保持滚动位置
    requestAnimationFrame(() => el.scrollTop = el.scrollHeight - prevHeight);
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
  await api("PUT", `/api/conversations/${currentConvId}`, { model: $("modelSelect").value });
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
