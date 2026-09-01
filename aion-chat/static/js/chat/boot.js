// ── 初始化 ──
async function init() {
  window.ChatPerf?.markOnce("init_start");
  const [loadedModels, loadedWorldBook, loadedConversations] = await Promise.all([
    api("GET", "/api/models"),
    api("GET", "/api/worldbook"),
    api("GET", "/api/conversations"),
    refreshAvatarVersions(),
  ]).then(([m, wb, convs]) => [m || [], wb || {}, convs || []]);

  models = loadedModels;
  window.ChatPerf?.markOnce("models_loaded", { count: models.length });
  renderModelSelect();
  worldBook = loadedWorldBook;
  conversations = loadedConversations;
  window.ChatPerf?.markOnce("conversation_list_loaded", { count: conversations.length });
  const lastId = localStorage.getItem('aion_last_conv');
  if (lastId && conversations.find(c => c.id === lastId)) {
    await selectConv(lastId);
  } else {
    renderConvList();
    renderMessages();
  }
  connectWS();
  scheduleNonCriticalChatInit();
  // 滚动到顶部自动加载更早消息
  $("messages").addEventListener("scroll", function() {
    if (this.scrollTop < 80) loadOlderMessages();
    scheduleVisibleMessagesSeen();
  });
}
