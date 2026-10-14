// ── 时间 ──
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0');
}

// ── 渲染 ──
function chatErrorDisplay(errorText) {
  const detail = String(errorText || "");
  if (/timeout|超时/i.test(detail)) return '回复等得太久了，点“重试”再试一次。';
  if (/content_filter/.test(detail)) {
    return '回复被上游拦截，点“重试”再试一次。';
  }
  if (/empty_response|本轮没有可交付的文字或附件/.test(detail)) {
    return '这次没有收到回复，点“重试”再试一次。';
  }
  return '这次回复没有完成，点“重试”再试一次。';
}

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

// Keep unchanged rows (including selection, audio, menus and streaming UI) alive.
let renderedConversationId = null;
const renderedMessageRows = new Map();

function renderMessageRow(m) {
  if (m.inherited) return '';
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
  const collectAction = (isUser || m.role === 'assistant') && m.attachments?.some(item => item?.type === 'sticker' && item.id)
    ? `<button data-msg-action="collectSticker" data-msg-id="${id}">收藏表情</button>` : '';
  const actionsHtml = `${primaryAction}${collectAction}<button data-msg-action="delete" data-msg-id="${id}">删除</button><button data-msg-action="copy" data-msg-id="${id}">复制</button>`;
  const dotsLeft = isUser ? `<button class="msg-dots" data-msg-action="toggleMenu" data-msg-id="${id}">&#8943;</button>` : '';
  const dotsRight = !isUser ? `<button class="msg-dots" data-msg-action="toggleMenu" data-msg-id="${id}">&#8943;</button>` : '';
  const displayContent = isErrorMsg ? chatErrorDisplay(m.error_text || m.content)
    : isUser ? String(m.content || "") : cleanAssistantContent(m.content);
  const stickerOnly = !displayContent.trim() && m.attachments?.length && m.attachments.every(item => item?.type === 'sticker');
  const layout = window.ChatMarkdown?.prepare(displayContent, { plain: isErrorMsg, msgId: m.id })
    || { expanded: false, html: `<div class="msg-bubble">${formatMsg(displayContent)}</div>` };
  const attachmentsHtml = stickerOnly
    ? `<div class="msg-sticker-only">${renderAttachments(m.attachments)}</div>`
    : renderAttachments(m.attachments);
  const avatarSrc = avatarUrl(isUser ? 'user' : 'ai');
  const avatarHtml = `<img class="msg-avatar" src="${avatarSrc}" alt="">`;
  const ttsBtn = !isUser && !isErrorMsg && !stickerOnly ? `<button class="tts-replay-btn" data-msg-action="replayTTS" data-msg-id="${id}" title="重听语音">${window.VowUI?.icon('volume') || '♪'}</button>` : '';
  return `
  <div class="msg-row ${m.role}${isErrorMsg ? ' transient-error' : ''}${layout.expanded ? ' md-expanded' : ''}" id="m_${id}" data-created-at="${createdAt}">
    <div class="msg-avatar-col">
      ${m.role === 'assistant' ? `<span class="msg-avatar-frame">${avatarHtml}</span>` : avatarHtml}
      ${ttsBtn}
    </div>
    <div class="msg-body">
      <div class="msg-role-row">
        ${dotsLeft}<span class="msg-role-name">${roleLabel}</span><span class="msg-time">${time}</span>${dotsRight}
        <div class="msg-menu" id="menu_${m.id}">${actionsHtml}</div>
      </div>
      <div class="msg-content" data-msg-content>${displayContent.trim() ? layout.html : ''}</div>
      <div class="msg-attachments">${attachmentsHtml}</div>
      ${m.response_status === 'filtered_partial' ? `<div class="msg-response-notice" role="status">回复被上游截断，已保留收到的内容。<button data-msg-action="regenerate" data-msg-id="${id}">重新生成</button></div>` : ''}
    </div>
  </div>`;
}

function renderMessages({ scroll = 'auto' } = {}) {
  const el = $("messages");
  const changedConversation = renderedConversationId !== currentConvId;
  const follow = scroll === 'bottom' || (scroll === 'auto' && (changedConversation
    || (el.scrollHeight - el.scrollTop - el.clientHeight < 80 && !document.querySelector('.heart-note-open'))));
  const viewportTop = el.getBoundingClientRect().top;
  const anchor = follow ? null : [...el.children].find(row => row.classList.contains('msg-row')
    && row.getBoundingClientRect().bottom > viewportTop);
  const anchorTop = anchor?.getBoundingClientRect().top;
  window.ChatMarkdown?.suspend();
  window.ChatHeartNotes?.beforeRender();
  if (changedConversation) {
    el.replaceChildren();
    renderedMessageRows.clear();
    renderedConversationId = currentConvId;
  }
  if (!currentConvId || currentMessages.length === 0) {
    el.innerHTML = !currentConvId
      ? `<div class="empty-state"><div class="icon">${window.VowUI?.icon('chat') || '◇'}</div><div class="hint">选择对话或新建对话</div></div>`
      : '<div class="empty-state"><div class="icon">◇</div><div class="hint">暂无消息</div><div class="vow-empty-note">可以发送文字、语音或图片。</div></div>';
    renderedMessageRows.clear();
    _restoreMoodGlows();
    window.ChatMarkdown?.didRender();
    window.ChatHeartNotes?.restore();
    return;
  }
  el.querySelector(':scope > .empty-state')?.remove();
  let loadMore = el.querySelector(':scope > .load-more-bar');
  if (hasMoreMessages && !loadMore) {
    loadMore = document.createElement('button');
    loadMore.className = 'load-more-bar';
    loadMore.dataset.renderAction = 'loadOlderMessages';
    loadMore.textContent = '⬆ 加载更早的消息';
    el.prepend(loadMore);
  } else if (!hasMoreMessages && loadMore) { loadMore.remove(); loadMore = null; }
  const environment = JSON.stringify([worldBook.user_name, worldBook.ai_name, avatarUrl('user'), avatarUrl('ai')]);
  const wanted = new Set();
  let cursor = loadMore ? loadMore.nextElementSibling : el.firstElementChild;
  for (const message of currentMessages) {
    if (message.inherited || ['cam_user', 'cam_log', 'cam_trigger', 'trigger'].includes(message.role)) continue;
    wanted.add(message.id);
    const signature = environment + JSON.stringify(message);
    const previous = renderedMessageRows.get(message.id);
    let node = previous?.node;
    if (!node?.isConnected || previous.signature !== signature) {
      const template = document.createElement('template');
      template.innerHTML = renderMessageRow(message);
      const replacement = template.content.firstElementChild;
      if (node?.parentElement === el) {
        if (cursor === node) cursor = replacement;
        node.replaceWith(replacement);
      }
      node = replacement;
      renderedMessageRows.set(message.id, { signature, node });
    }
    if (node !== cursor) el.insertBefore(node, cursor);
    cursor = node.nextElementSibling;
  }
  for (const [id, row] of renderedMessageRows) {
    if (!wanted.has(id)) { row.node.remove(); renderedMessageRows.delete(id); }
  }
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
  if (follow) scrollBottom({ force: true });
  else if (anchor?.isConnected) el.scrollTop += anchor.getBoundingClientRect().top - anchorTop;
  // History, WebSocket updates and stream completion share the same mood state.
  _restoreMoodGlows();
  window.ChatMarkdown?.didRender();
  window.ChatHeartNotes?.restore();
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
  if (typeof chatSyncPending !== 'undefined' && (chatSyncPending || !isChatForeground())) return;
  const bridge = window.ObsidianNotifications;
  if (!bridge) return;
  if ($('subPageOverlay')?.classList.contains('show')) return;
  const container = $("messages");
  if (!container) return;
  const viewport = container.getBoundingClientRect();
  if (viewport.width <= 0 || viewport.height <= 0) return;
  let seenThrough = 0;
  // 打开聊天即确认最新已渲染的一批消息；流式占位和临时错误不算已读。
  for (const message of currentMessages) {
    if (message.role !== 'assistant' || message.persisted === false || message.transient_error) continue;
    if (message.id === streamingAiId && message.persisted !== true) continue;
    if (!document.getElementById('m_' + message.id)) continue;
    seenThrough = Math.max(seenThrough, Number(message.created_at) || 0);
  }
  // 日切可能只继承了最后一条文字输入；进入并成功刷新仍确认此前全部消息通知。
  if (typeof dailyChatState !== 'undefined' && dailyChatState?.enabled) {
    seenThrough = Math.max(seenThrough, Number(dailyChatState.latest_assistant_at) || 0);
  }
  try {
    if (typeof bridge.synced === 'function') bridge.synced(seenThrough);
    else if (seenThrough > 0 && typeof bridge.seenThrough === 'function') bridge.seenThrough(seenThrough);
  } catch (_) {}
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
      case "collectSticker":
        ChatApp.getModule("stickers")?.collectMessage(id);
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

function scrollBottom({ force = false } = {}) {
  const el = $("messages");
  if (!force && (el.scrollHeight - el.scrollTop - el.clientHeight >= 80
      || document.querySelector('.heart-note-open'))) return;
  const top = el.scrollTop;
  requestAnimationFrame(() => {
    // Do not override a swipe that happened while this frame was pending.
    if (el.scrollTop === top) el.scrollTop = el.scrollHeight;
  });
}

function renderDebugBar(msgId) {
  // 不再在聊天气泡下方渲染，改为写入系统日志
  const d = msgDebugData[msgId];
  if (!d) return;
  addSystemLog(d);
}
