// ── 时间 ──
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0');
}

// ── 渲染 ──
function renderModelSelect() {
  $("modelSelect").innerHTML = models.map(m =>
    `<option value="${escHtml(m.key)}">${escHtml(m.key)}${m.audio_input === true ? ' · 🎙' : ''}</option>`
  ).join("");
}

function renderConvList() {
  $("convList").innerHTML = conversations.map(c => {
    const count = c.message_count != null ? c.message_count : '';
    const id = escHtml(c.id);
    return `
    <div class="conv-item ${c.id === currentConvId ? 'active' : ''}" data-conv-id="${id}">
      <span class="title">${escHtml(c.title)}</span>
      ${count !== '' ? `<span class="conv-count">${count}</span>` : ''}
      <button class="rename-btn" data-conv-action="rename" data-conv-id="${id}" title="重命名">✎</button>
      <button class="del-btn" data-conv-action="delete" data-conv-id="${id}" title="删除">✕</button>
    </div>`;
  }).join("");
}

function renderMessages() {
  const el = $("messages");

  if (!currentConvId) {
    el.innerHTML = '<div class="empty-state"><div class="icon">💬</div><div class="hint">选择或创建一个对话开始聊天</div></div>';
    return;
  }

  if (currentMessages.length === 0) {
    el.innerHTML = '<div class="empty-state"><div class="icon">✨</div><div class="hint">发送第一条消息开始对话</div></div>';
    return;
  }

  const loadMoreBtn = hasMoreMessages ? '<button class="load-more-bar" data-render-action="loadOlderMessages">⬆ 加载更早的消息</button>' : '';
  el.innerHTML = loadMoreBtn + currentMessages.map(m => {
    const isUser = m.role === "user";
    const id = escHtml(m.id);
    const createdAt = Number(m.created_at) || 0;
    const isErrorMsg = typeof isErrorLikeAssistantMessage === "function"
      ? isErrorLikeAssistantMessage(m)
      : Boolean(m.transient_error);

    // 隐藏监控相关消息（日志已独立存储）
    if (m.role === "cam_user" || m.role === "cam_log" || m.role === "cam_trigger" || m.role === "trigger") {
      return '';
    }

    // 系统提示消息（居中显示）
    if (m.role === "system") {
      return `<div class="msg-row system" id="m_${id}" data-created-at="${createdAt}"><div class="system-notice">${escHtml(m.content)}</div><button class="system-del" data-msg-action="delete" data-msg-id="${id}" title="删除">✕</button></div>`;
    }

    const roleLabel = isErrorMsg ? '请求错误' : (isUser ? (worldBook.user_name || '你') : (worldBook.ai_name || 'AI'));
    const time = m.created_at ? fmtTime(m.created_at) : "";
    const primaryAction = isUser
      ? `<button data-msg-action="edit" data-msg-id="${id}">编辑</button>`
      : `<button data-msg-action="regenerate" data-msg-id="${id}">${isErrorMsg ? '重试' : '重新生成'}</button>`;
    const actionsHtml = `${primaryAction}<button data-msg-action="delete" data-msg-id="${id}">删除</button><button data-msg-action="copy" data-msg-id="${id}">复制</button>`;
    const dotsLeft = isUser ? `<button class="msg-dots" data-msg-action="toggleMenu" data-msg-id="${id}">&#8943;</button>` : '';
    const dotsRight = !isUser ? `<button class="msg-dots" data-msg-action="toggleMenu" data-msg-id="${id}">&#8943;</button>` : '';
    const displayContent = isUser ? String(m.content || "") : cleanAssistantContent(m.content);
    const parts = isUser ? displayContent.split(/\n+/).filter(p => p.trim()) : displayContent.split(/\n{2,}/).filter(p => p.trim());
    const bubblesHtml = parts.length > 1
      ? '<div class="msg-bubbles">' + parts.map(p => `<div class="msg-bubble">${formatMsg(p)}</div>`).join('') + renderAttachments(m.attachments) + '</div>'
      : `<div class="msg-bubble">${formatMsg(displayContent)}${renderAttachments(m.attachments)}</div>`;
    const avatarSrc = avatarUrl(isUser ? 'user' : 'ai');
    const ttsBtn = !isUser && !isErrorMsg ? `<button class="tts-replay-btn" data-msg-action="replayTTS" data-msg-id="${id}" title="重听语音">🔊</button>` : '';
    return `
    <div class="msg-row ${m.role}${isErrorMsg ? ' transient-error' : ''}" id="m_${id}" data-created-at="${createdAt}">
      <div class="msg-avatar-col">
        <img class="msg-avatar" src="${avatarSrc}" alt="">
        ${ttsBtn}
      </div>
      <div class="msg-body">
        <div class="msg-role-row">
          ${dotsLeft}<span class="msg-role-name">${roleLabel}</span><span class="msg-time">${time}</span>${dotsRight}
          <div class="msg-menu" id="menu_${m.id}">${actionsHtml}</div>
        </div>
        ${bubblesHtml}
      </div>
    </div>`;
  }).join("");
  // 恢复音乐卡片
  for (const mid of Object.keys(msgMusicCards)) {
    renderMusicCards(mid);
  }
  // 恢复 [CAM_CHECK] 加载指示器
  if (camCheckMsgId) {
    const row = document.getElementById('m_' + camCheckMsgId);
    if (row && !row.querySelector('.cam-check-indicator')) {
      const aiName = worldBook.ai_name || 'AI';
      const indicator = document.createElement('div');
      indicator.className = 'cam-check-indicator';
      indicator.id = 'cam_check_loading';
      indicator.innerHTML = `\uD83D\uDCF7 ${escHtml(aiName)} \u6B63\u5728\u67E5\u770B\u76D1\u63A7<span class="cam-dots"><span></span><span></span><span></span></span>`;
      const msgBody = row.querySelector('.msg-body');
      (msgBody || row).appendChild(indicator);
    }
  }
  // 恢复 [POI_SEARCH] 加载指示器
  if (poiSearchMsgId) {
    const row = document.getElementById('m_' + poiSearchMsgId);
    if (row && !row.querySelector('.poi-search-indicator')) {
      const aiName = worldBook.ai_name || 'AI';
      const catText = (poiSearchCategories || []).join('\u3001');
      const indicator = document.createElement('div');
      indicator.className = 'poi-search-indicator';
      indicator.id = 'poi_search_loading';
      indicator.innerHTML = `\uD83D\uDCCD ${escHtml(aiName)} \u6B63\u5728\u641C\u7D22\u9644\u8FD1${escHtml(catText)}<span class="poi-dots"><span></span><span></span><span></span></span>`;
      const msgBody = row.querySelector('.msg-body');
      (msgBody || row).appendChild(indicator);
    }
  }
  // 恢复 [查看动态] 加载指示器
  if (activityCheckMsgId) {
    const row = document.getElementById('m_' + activityCheckMsgId);
    if (row && !row.querySelector('.activity-check-indicator')) {
      const aiName = worldBook.ai_name || 'AI';
      const minutes = (activityCheckN || 6) * 10;
      const indicator = document.createElement('div');
      indicator.className = 'activity-check-indicator';
      indicator.id = 'activity_check_loading';
      indicator.innerHTML = `📊 ${escHtml(aiName)} 正在查看过去${minutes}分钟的动态<span class="activity-dots"><span></span><span></span><span></span></span>`;
      const msgBody = row.querySelector('.msg-body');
      (msgBody || row).appendChild(indicator);
    }
  }
  // 恢复 [SCREEN_CHECK] 加载指示器
  if (screenCheckMsgId) {
    const row = document.getElementById('m_' + screenCheckMsgId);
    if (row && !row.querySelector('.screen-check-indicator')) {
      const aiName = worldBook.ai_name || 'AI';
      const indicator = document.createElement('div');
      indicator.className = 'screen-check-indicator';
      indicator.id = 'screen_check_loading';
      indicator.innerHTML = `🖥️ ${escHtml(aiName)} 正在等待屏幕确认<span class="screen-dots"><span></span><span></span><span></span></span>`;
      const msgBody = row.querySelector('.msg-body');
      (msgBody || row).appendChild(indicator);
    }
  }
  // 恢复 [HEART] 心语气泡
  for (const hwMsgId of _heartWhisperMsgIds) {
    _applyHeartHint(hwMsgId);
  }
  // 恢复 AI 情绪光圈
  for (const mid of Object.keys(_msgMoods)) {
    _applyMoodGlow(mid);
  }
  scrollBottom();
  scheduleVisibleMessagesSeen();
}

let notificationSeenFrame = 0;

function scheduleVisibleMessagesSeen() {
  if (notificationSeenFrame) return;
  notificationSeenFrame = requestAnimationFrame(() => {
    notificationSeenFrame = 0;
    reportVisibleMessagesSeen();
  });
}

function reportVisibleMessagesSeen() {
  if (document.visibilityState !== "visible") return;
  if (!window.AionNotifications || typeof window.AionNotifications.seenThrough !== "function") return;
  const container = $("messages");
  if (!container) return;
  const viewport = container.getBoundingClientRect();
  let seenThrough = 0;
  for (const row of container.querySelectorAll(".msg-row.assistant[data-created-at]")) {
    const rect = row.getBoundingClientRect();
    if (rect.bottom <= viewport.top || rect.top >= viewport.bottom) continue;
    seenThrough = Math.max(seenThrough, Number(row.dataset.createdAt) || 0);
  }
  if (seenThrough > 0) {
    try { window.AionNotifications.seenThrough(seenThrough); } catch (e) {}
  }
}

let renderEventsBound = false;

function bindRenderEvents() {
  if (renderEventsBound) return;
  renderEventsBound = true;

  $("messages")?.addEventListener("scroll", scheduleVisibleMessagesSeen, { passive: true });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") scheduleVisibleMessagesSeen();
  });

  document.addEventListener("click", event => {
    const convAction = event.target.closest("[data-conv-action]");
    if (convAction) {
      event.preventDefault();
      event.stopPropagation();
      const id = convAction.dataset.convId;
      if (convAction.dataset.convAction === "rename") renameConv(id);
      if (convAction.dataset.convAction === "delete") delConv(id);
      return;
    }

    const convItem = event.target.closest("[data-conv-id]");
    if (convItem && $("convList")?.contains(convItem)) {
      event.preventDefault();
      selectConv(convItem.dataset.convId);
      return;
    }

    const renderAction = event.target.closest("[data-render-action]");
    if (renderAction?.dataset.renderAction === "loadOlderMessages") {
      event.preventDefault();
      loadOlderMessages();
      return;
    }

    const msgAction = event.target.closest("[data-msg-action]");
    if (!msgAction || !$("messages")?.contains(msgAction)) return;
    event.preventDefault();
    const id = msgAction.dataset.msgId;
    switch (msgAction.dataset.msgAction) {
      case "toggleMenu":
        event.stopImmediatePropagation();
        toggleMsgMenu(id);
        break;
      case "edit":
        editMsg(id);
        closeMsgMenus();
        break;
      case "regenerate":
        regenerateMsg(id);
        closeMsgMenus();
        break;
      case "delete":
        delMsg(id);
        closeMsgMenus();
        break;
      case "copy":
        copyMsg(id);
        closeMsgMenus();
        break;
      case "replayTTS":
        replayTTS(id);
        break;
    }
  });
}

bindRenderEvents();

ChatApp.registerModule("render", {
  renderModelSelect,
  renderConvList,
  renderMessages,
  scrollBottom,
  renderDebugBar,
  bindEvents: bindRenderEvents,
});

function scrollBottom() {
  const el = $("messages");
  requestAnimationFrame(() => el.scrollTop = el.scrollHeight);
}

function renderDebugBar(msgId) {
  // 不再在聊天气泡下方渲染，改为写入系统日志
  const d = msgDebugData[msgId];
  if (!d) return;
  addSystemLog(d);
}
