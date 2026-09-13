// ── 初始化 ──
async function init() {
  window.ChatPerf?.markOnce("init_start");
  if (typeof syncDailyChatState === 'function') await syncDailyChatState();
  const [loadedModels, loadedWorldBook, loadedConversations] = await Promise.all([
    readChatJson("/api/models"),
    readChatJson("/api/worldbook"),
    readChatJson("/api/conversations"),
  ]).then(([m, wb, convs]) => [m || [], wb || {}, convs || []]);

  if (!Array.isArray(loadedModels) || !Array.isArray(loadedConversations)) {
    throw new Error('消息同步返回了无效数据');
  }
  models = loadedModels;
  window.ChatPerf?.markOnce("models_loaded", { count: models.length });
  renderModelSelect();
  worldBook = loadedWorldBook;
  window.VowUI?.setNames(worldBook);
  conversations = loadedConversations;
  window.ChatPerf?.markOnce("conversation_list_loaded", { count: conversations.length });
  const savedId = localStorage.getItem('obsidian_last_conv');
  const fallbackId = pendingNotificationConversation() || savedId;
  const lastId = typeof dailyEntryTarget === 'function' ? dailyEntryTarget(fallbackId) : fallbackId;
  if (typeof prepareDailyEntryDraft === 'function') prepareDailyEntryDraft(lastId, savedId);
  const initial = conversations.find(c => c.id === lastId) || conversations[0];
  if (initial) {
    if (!await selectConv(initial.id, { syncManaged: true })) throw new Error('聊天页面已切换，重新同步');
  } else {
    renderConvList();
    renderMessages();
  }
  connectWS();
  refreshAvatarVersions().then(() => {
    const avatar = $('chatPersonAvatar');
    if (avatar) avatar.src = avatarUrl('ai');
    renderMessages();
  }).catch(() => {});
  scheduleNonCriticalChatInit();
  // 滚动到顶部自动加载更早消息
  $("messages").addEventListener("scroll", function() {
    if (this.scrollTop < 80) loadOlderMessages();
    scheduleVisibleMessagesSeen();
  });
}
