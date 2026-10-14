// ── WebSocket 同步 ──
let wsReconnectTimer = null;
let wsHeartbeatTimer = null;
let wsPongTimer = null;

function clearChatSocketTimers() {
  clearTimeout(wsReconnectTimer);
  clearInterval(wsHeartbeatTimer);
  clearTimeout(wsPongTimer);
  wsReconnectTimer = wsHeartbeatTimer = wsPongTimer = null;
}

function reconnectChatSocket() {
  clearChatSocketTimers();
  const previous = ws;
  ws = null;
  if (previous) {
    previous.onopen = previous.onmessage = previous.onclose = previous.onerror = null;
    try { previous.close(); } catch (_) {}
  }
  if (isChatForeground()) connectWS();
}

function pingChatSocket() {
  if (!isChatForeground() || !ws || ws.readyState !== 1 || wsPongTimer) return;
  const socket = ws;
  try { socket.send(JSON.stringify({ type: 'ping' })); }
  catch (_) { reconnectChatSocket(); return; }
  wsPongTimer = setTimeout(() => {
    wsPongTimer = null;
    if (ws === socket && isChatForeground()) reconnectChatSocket();
  }, 10000);
}

function connectWS() {
  if (ws && ws.readyState === 0) {
    if (!wsPongTimer) watchChatSocketConnection(ws);
    return;
  }
  if (ws && ws.readyState === 1) return;
  clearChatSocketTimers();
  if (ws) {
    ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
    try { ws.close(); } catch (_) {}
  }
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${proto}//${location.host}/ws`);
  ws = socket;
  socket.onmessage = e => {
    if (ws !== socket) return;
    try {
      const message = JSON.parse(e.data);
      if (message.type === 'pong') {
        clearTimeout(wsPongTimer);
        wsPongTimer = null;
        return;
      }
      handleSync(message);
    } catch (error) { console.warn('[chat] WebSocket event failed', error); }
  };
  socket.onopen = () => {
    if (ws !== socket) return;
    clearChatSocketTimers();
    wsHeartbeatTimer = setInterval(pingChatSocket, 20000);
    window.ChatPerf?.markOnce("ws_connected");
    recoverInterruptedStream();
    if (chatInitialized && isChatForeground()) refreshChat();
    if (window.ControlRuntime) ControlRuntime.recover().catch(() => {});
    window.SceneRuntime?.connectionChanged(true);
  };
  socket.onclose = () => {
    if (ws !== socket) return;
    window.SceneRuntime?.connectionChanged(false);
    clearChatSocketTimers();
    wsReconnectTimer = setTimeout(() => {
      wsReconnectTimer = null;
      if (isChatForeground()) connectWS();
    }, 2000);
  };
  socket.onerror = () => {
    if (ws === socket) socket.close();
  };
  watchChatSocketConnection(socket);
}

function watchChatSocketConnection(socket) {
  // CONNECTING 状态也可能在后台恢复后卡住，返回前台时必须重新安排检测。
  wsPongTimer = setTimeout(() => {
    wsPongTimer = null;
    if (ws === socket && socket.readyState === 0 && isChatForeground()) reconnectChatSocket();
  }, 10000);
}

// 流式中断恢复：WS 重连后把服务器上已完成的消息内容覆盖回当前页
async function recoverInterruptedStream() {
  if (!_recoverStreamMsgId) return;
  const id = _recoverStreamMsgId;
  _recoverStreamMsgId = null;
  try {
    const msg = await api("GET", `/api/messages/${id}`);
    if (!msg || msg.error) return;
    if (msg.conv_id !== currentConvId) return;
    const mi = currentMessages.findIndex(m => m.id === id);
    if (mi >= 0) currentMessages[mi] = msg;
    else currentMessages.push(msg);
    renderMessages();
    showToast("已恢复中断的回复", 2000);
  } catch (e) {}
}
window.onWsReopen = recoverInterruptedStream;

// 把 WS 事件转发给子页面 iframe：iframe 内不建 WS（common.js 统一约定），
// 记忆库誓约 tab、心语等子页靠这个保持实时。chat 页不加载 common.js，故内联。
function _forwardWsToSubPage(msg) {
  const frame = document.getElementById('subPageFrame');
  if (!frame || !frame.contentWindow) return;
  if (!frame.src || frame.src === 'about:blank') return;
  try { frame.contentWindow.postMessage({ __obsidianWs: true, payload: msg }, location.origin); } catch (e) {}
}

function handleSync(msg) {
  const { type, data } = msg;
  if (type === 'scene_state' || type === 'scene_playback') {
    window.SceneRuntime?.handleEvent(msg);
    _forwardWsToSubPage(msg);
    return;
  }

  if (chatRefreshEvents && ['msg_created', 'msg_updated', 'msg_deleted'].includes(type) && data) {
    chatRefreshEvents.push({ type, data: { ...data } });
  }

  _forwardWsToSubPage(msg);

  if (type === 'daily_chat_rollover') {
    receiveDailyRollover(data);
  } else if (type === "conv_created") {
    if (!conversations.find(c => c.id === data.id)) {
      conversations.unshift(data);
      renderConvList();
    }
  } else if (type === "conv_updated") {
    const c = conversations.find(c => c.id === data.id);
    if (c) { Object.assign(c, data); renderConvList(); }
    if (data.id === currentConvId && data.title) $("chatTitle").textContent = data.title;
    if (data.id === currentConvId && data.model) $("modelSelect").value = data.model;
  } else if (type === "conv_deleted") {
    conversations = conversations.filter(c => c.id !== data.id);
    renderConvList();
    if (data.id === currentConvId) {
      if (window.ControlRuntime) ControlRuntime.clear("conversation_deleted");
      currentConvId = null; currentMessages = []; localStorage.removeItem('obsidian_last_conv'); renderMessages();
    }
  } else if (type === "msg_created") {
    if (data.conv_id === currentConvId) {
      const incoming = data.role === "assistant" && (data.transient_error || data.persisted === false || msgDebugData[data.id]?.has_error)
        ? { ...data, transient_error: true, persisted: false, error_text: data.error_text || msgDebugData[data.id]?.error_text || data.content }
        : { ...data, persisted: true };
      // 正在流式的 AI 消息 → 用完整内容替换
      if (data.id === streamingAiId) {
        const mi = currentMessages.findIndex(m => m.id === data.id);
        if (mi >= 0) currentMessages[mi] = incoming;
        else currentMessages.push(incoming);
        streamingAiId = null;
        renderMessages();
      }
      // 临时用户消息被真实消息替换
      else if (currentMessages.find(m => m.id === "temp_user") && data.role === "user") {
        const ti = currentMessages.findIndex(m => m.id === "temp_user");
        if (ti >= 0) currentMessages[ti] = data;
        renderMessages();
      }
      // 其他端发来的新消息（含 Core 主动发言 / 语音唤醒）
      else if (!currentMessages.find(m => m.id === data.id)) {
        currentMessages.push(incoming);
        // CAM_CHECK 响应到达：收到 assistant 消息时关闭「正在查看监控」提示
        if (data.role === 'assistant' && camCheckMsgId) dismissCamCheckIndicator();
        if (data.role === 'assistant' && screenCheckMsgId) dismissScreenCheckIndicator();
        if (data.role === 'assistant' && poiSearchMsgId) dismissPoiSearchIndicator();
        if (data.role === 'assistant' && activityCheckMsgId) dismissActivityCheckIndicator();
        renderMessages();
        // TTS：语音通话中自动播报 AI 回复 / Core 主动发言（带 tts 标记）
        if (data.role === 'assistant' && !incoming.transient_error && (voiceInCall || msg.tts)) {
          ttsSpeak(data.content, data.id);
        } else if (data.role === 'assistant' && voiceInCall && !ttsEnabled) {
          // TTS 未启用但在通话中，通知语音模块 AI 说完了
          notifyVoiceAiSpeaking(false);
        }
      }
      scrollBottom();
    }
    const ci = conversations.findIndex(c => c.id === data.conv_id);
    if (ci >= 0 && !data.transient_error && data.persisted !== false) {
      if (conversations[ci].message_count != null) conversations[ci].message_count++;
      if (ci > 0) { const [c] = conversations.splice(ci, 1); conversations.unshift(c); }
      renderConvList();
    }
  } else if (type === "msg_updated") {
    if (data.conv_id === currentConvId) {
      const mi = currentMessages.findIndex(m => m.id === data.id);
      if (mi >= 0) { currentMessages[mi] = { ...currentMessages[mi], ...data, conv_id: currentConvId }; renderMessages(); }
    }
  } else if (type === "msg_deleted") {
    if (data.conv_id === currentConvId) {
      currentMessages = currentMessages.filter(m => m.id !== data.id);
      renderMessages();
    }
    const dc = conversations.find(c => c.id === data.conv_id);
    if (dc && dc.message_count != null && dc.message_count > 0) { dc.message_count--; renderConvList(); }
  } else if (type === "file_synced") {
    if (data.conv_id === currentConvId) {
      refreshChat();
    }
  } else if (type === "voice_state") {
    // 远程模式下忽略后端的语音状态广播（PC sounddevice 的状态不应覆盖手机麦克风的状态）
    if (!isRemoteVoice()) updateVoiceUI(data);
  } else if (type === "cam_check") {
    // 通过 WebSocket 收到 cam_check（语音发送时前端没有 SSE 流）
    if (data.conv_id === currentConvId && !streamingAiId) {
      handleCamCheck(data.conv_id, data.model_key, data.msg_id);
    }
  } else if (type === "poi_search") {
    // 通过 WebSocket 收到 poi_search
    if (data.conv_id === currentConvId && !streamingAiId) {
      handlePoiSearch(data.categories, data.msg_id);
    }
  } else if (type === "activity_check") {
    // 通过 WebSocket 收到 activity_check（语音发送时前端没有 SSE 流）
    if (data.conv_id === currentConvId && !streamingAiId) {
      handleActivityCheck(data.conv_id, data.n, data.msg_id);
    }
  } else if (type === "screen_check_pending") {
    if (data.conv_id === currentConvId && !streamingAiId) {
      handleScreenCheck(data, data.msg_id);
    }
  } else if (type === "screen_check_complete" || type === "screen_check_rejected") {
    if (data.conv_id === currentConvId) {
      dismissScreenCheckIndicator();
    }
  } else if (type === "debug") {
    // 通过 WebSocket 收到 debug 信息（语音发送时前端没有 SSE 流）
    if (data.has_error && data.msg_id) {
      markTransientErrorMessage(data.msg_id, data.error_text);
    }
    if (data.msg_id && !streamingAiId) {
      msgDebugData[data.msg_id] = data;
      renderDebugBar(data.msg_id);
    }
  } else if (type === "music") {
    // 通过 WebSocket 收到音乐卡片（语音发送 / 闹铃触发 / 定时监控）
    if (data.msg_id && !streamingAiId) {
      msgMusicCards[data.msg_id] = data.cards;
      renderMusicCards(data.msg_id);
      scrollBottom();
      // autoplay：闹铃/定时监控触发的音乐自动播放第一首
      if (data.autoplay && data.cards && data.cards.length) {
        playMusicOnline(data.cards[0].id);
      }
    }
  } else if (type === "schedule_alarm") {
    showAlarmPopup(data);
  } else if (type === "monitor_alert") {
    // 定时监控即将触发，播放提示音
    const audio = new Audio('/public/monitor-alert.mp3');
    audio.play().catch(() => {});
    sendSystemNotification('📷 监控提醒', data.content || '哨兵监控即将分析');
  } else if (type === "schedule_changed") {
    // 日程管理已拆分为独立页面
  } else if (type === "toy_command") {
    ControlToyRouter.execute(data || {});
  } else if (type === "toy_command_rejected") {
    ControlToyRouter.reportRejected(data || {});
  } else if (type === "tide_toy_frame") {
    TideToyRouter.execute(data || {});
  } else if (type === "control_stop_request") {
    if ((data || {}).kind === "tide") {
      TideToyRouter.execute({ ...(data || {}), global_stop: true });
      const current = window.ControlRuntime?.currentSession?.();
      if (current && current.session_id === data.control_session_id) {
        window.ControlRuntime.clear("control_stop_request");
        window.tideMode = false;
        document.getElementById("tidePill")?.classList.remove("show");
      }
    }
  } else if (type === "heart_whisper") {
    // 通过 WebSocket 收到心语（语音发送时前端没有 SSE 流）
    if (data.msg_id) {
      showHeartWhisperHint(data.msg_id, data.content, data);
    }
  }
}
