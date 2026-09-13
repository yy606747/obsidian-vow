// ── 发送消息 ──

function createSendStreamState() {
  return { aiMsgId: null, aiContent: "", hasError: false };
}

// generation_blocked（誓约读取失败 fail-closed）：事件携带已持久化的 system 消息，
// 按 id upsert——SSE 与 WebSocket msg_created 任一先到，结果一致，无重复。
function upsertSystemMessage(message) {
  if (!message || !message.id || message.conv_id !== currentConvId) return;
  const mi = currentMessages.findIndex(m => m.id === message.id);
  if (mi >= 0) currentMessages[mi] = message;
  else currentMessages.push(message);
  window.ChatMarkdown?.finish(message.id);
  renderMessages();
  scrollBottom();
}

function handleGenerationBlocked(data) {
  _stopTypingAnim();
  upsertSystemMessage(data.message);
}

function assistantErrorText(text) {
  const value = String(text || "").trim();
  return Boolean(
    value.includes("[请求出错:")
    || value.startsWith("[Gemini错误")
    || value.startsWith("[硅基流动错误")
    || value.startsWith("[中转站错误")
    || value.startsWith("[错误]")
  );
}

function isTransientErrorMessage(msg) {
  return Boolean(
    msg
    && (msg.transient_error || msg.persisted === false)
  );
}

function isErrorLikeAssistantMessage(msg) {
  return Boolean(
    msg
    && msg.role === "assistant"
    && (isTransientErrorMessage(msg) || assistantErrorText(msg.content))
  );
}

function removeLocalMessage(id) {
  const before = currentMessages.length;
  currentMessages = currentMessages.filter(m => m.id !== id);
  if (currentMessages.length !== before) {
    delete msgDebugData[id];
    delete msgMusicCards[id];
    renderMessages();
  }
}

function markTransientErrorMessage(msgId, errorText) {
  if (!msgId) return;
  const mi = currentMessages.findIndex(m => m.id === msgId);
  if (mi < 0) return;
  const detail = errorText || currentMessages[mi].error_text || currentMessages[mi].content || "";
  currentMessages[mi] = {
    ...currentMessages[mi],
    transient_error: true,
    persisted: false,
    error_text: detail,
    content: chatErrorDisplay(detail),
  };
  renderMessages();
}

async function ensureEventStreamResponse(res, actionLabel) {
  const contentType = res.headers.get("content-type") || "";
  if (res.ok && contentType.includes("text/event-stream")) return;

  let message = `${actionLabel || "请求"}失败`;
  try {
    const raw = await res.text();
    if (raw) {
      try {
        const data = JSON.parse(raw);
        message = data.error || data.message || raw;
      } catch {
        message = raw.slice(0, 300);
      }
    }
  } catch {}
  throw new Error(message);
}

function resetStreamingAssistantMessage(msgId) {
  const mi = currentMessages.findIndex(m => m.id === msgId);
  if (mi >= 0) currentMessages[mi].content = "...";
  if (window.ChatMarkdown) { ChatMarkdown.reset(msgId); return; }
  const container = document.getElementById(`m_${msgId}`);
  if (!container) return;
  const bubble = container.querySelector(".msg-bubbles") || container.querySelector(".msg-bubble");
  if (bubble) bubble.innerHTML = "...";
}

function updateStreamingAssistantMessage(msgId, raw) {
  const mi = currentMessages.findIndex(m => m.id === msgId);
  if (mi >= 0 && currentMessages[mi].persisted === true) return;
  const isError = mi >= 0 && isErrorLikeAssistantMessage(currentMessages[mi]);
  if (mi >= 0 && !isError) currentMessages[mi].content = String(raw || '');
  if (window.ChatMarkdown) { ChatMarkdown.queue(msgId); return; }
  const display = isError ? chatErrorDisplay(currentMessages[mi].error_text || raw) : cleanAssistantContent(raw);
  const container = document.getElementById(`m_${msgId}`);
  if (!container) return;
  const parts = display.split(/\n{2,}/).filter(p => p.trim());
  const target = container.querySelector(".msg-bubbles") || container.querySelector(".msg-bubble");
  if (!target) return;
  if (parts.length > 1) {
    const wrapper = document.createElement("div");
    wrapper.className = "msg-bubbles";
    wrapper.innerHTML = parts.map(p => `<div class="msg-bubble">${formatMsg(p)}</div>`).join("");
    target.replaceWith(wrapper);
  } else if (target.classList.contains("msg-bubbles")) {
    const single = document.createElement("div");
    single.className = "msg-bubble";
    single.innerHTML = formatMsg(display);
    target.replaceWith(single);
  } else {
    target.innerHTML = formatMsg(display);
  }
}

async function handleSendStreamEvent(data, streamState) {
  if (data.type === "start") {
    if (data.conv_id && data.conv_id !== currentConvId && typeof handoverDailyWindow === 'function') {
      beginChatSync();
      handoverDailyWindow(data.conv_id);
      refreshChat();
    }
    const pendingUser = currentMessages.find(message => message.id === 'temp_user');
    if (pendingUser && data.user_message_id) {
      if (currentMessages.some(message => message.id === data.user_message_id)) {
        currentMessages = currentMessages.filter(message => message !== pendingUser);
      } else {
        pendingUser.id = data.user_message_id;
        pendingUser.persisted = true;
      }
    }
    streamState.aiMsgId = data.id;
    streamingAiId = data.id;
    currentMessages.push({ id: data.id, conv_id: currentConvId, role: "assistant", content: "...", created_at: Date.now()/1000 });
    renderMessages();
    _startTypingAnim(data.id);
  } else if (data.type === "chunk") {
    if (!streamState.aiMsgId) return;
    await _awaitTypingFloor();
    _stopTypingAnim();
    if (data.content.includes("\x00RETRY\x00")) {
      streamState.aiContent = "";
      resetStreamingAssistantMessage(streamState.aiMsgId);
      _startTypingAnim(streamState.aiMsgId);
      return;
    }
    streamState.aiContent += data.content;
    if (assistantErrorText(streamState.aiContent)) {
      streamState.hasError = true;
      markTransientErrorMessage(streamState.aiMsgId, streamState.aiContent);
    }
    updateStreamingAssistantMessage(streamState.aiMsgId, streamState.aiContent);
  } else if (data.type === "tool_delivery") {
    _stopTypingAnim();
    currentMessages = currentMessages.filter(message => message.id !== data.id);
    renderMessages();
  } else if (data.type === "look_at_me") {
    showToast('陪伴邀请已发往手机');
  } else if (data.type === "message" && data.message) {
    _stopTypingAnim();
    streamState.aiContent = String(data.message.content || "");
    upsertSystemMessage(data.message);
  } else if (data.type === "debug" && streamState.aiMsgId) {
    msgDebugData[streamState.aiMsgId] = data;
    if (data.has_error) {
      streamState.hasError = true;
      markTransientErrorMessage(streamState.aiMsgId, data.error_text);
    }
    renderDebugBar(streamState.aiMsgId);
  } else if (data.type === "cam_check") {
    handleCamCheck(data.conv_id, data.model_key, streamState.aiMsgId);
  } else if (data.type === "cam_offline") {
    showCamOfflineNotice();
  } else if (data.type === "activity_check") {
    handleActivityCheck(data.conv_id, data.n, streamState.aiMsgId);
  } else if (data.type === "screen_check_pending") {
    handleScreenCheck(data, streamState.aiMsgId);
  } else if (data.type === "screen_check_complete" || data.type === "screen_check_rejected") {
    dismissScreenCheckIndicator();
  } else if (data.type === "poi_search") {
    handlePoiSearch(data.categories, streamState.aiMsgId);
  } else if (data.type === "music") {
    msgMusicCards[data.msg_id] = data.cards;
    renderMusicCards(data.msg_id);
    scrollBottom();
    if (data.cards && data.cards.length) playMusicOnline(data.cards[0].id);
  } else if (data.type === "toy_command") {
    ControlToyRouter.execute(data);
  } else if (data.type === "toy_command_rejected") {
    ControlToyRouter.reportRejected(data);
  } else if (data.type === "heart_whisper") {
    showHeartWhisperHint(data.msg_id, data.content, data);
  } else if (data.type === "generation_blocked") {
    streamState.hasError = true;
    handleGenerationBlocked(data);
  }
}

async function consumeSendStream(reader, streamState) {
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const lines = buf.split("\n");
    buf = lines.pop();
    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      try {
        await handleSendStreamEvent(JSON.parse(line.slice(6)), streamState);
      } catch {}
    }
  }
}

function finalizeSendStream(streamState) {
  window.ChatMarkdown?.finish(streamState.aiMsgId);
  if (!streamState.hasError && streamState.aiMsgId && streamState.aiContent) {
    _msgMoods[streamState.aiMsgId] = _detectAiMood(streamState.aiContent);
    _applyMoodGlow(streamState.aiMsgId);
  }
  if (aiDomMode) aiDomLastAiDoneAt = Date.now();
  const cleanText = cleanAssistantContent(streamState.aiContent);
  if (!streamState.hasError && cleanText) ttsSpeak(cleanText, streamState.aiMsgId);
  if (voiceInCall && !ttsEnabled) {
    notifyVoiceAiSpeaking(false);
  }
  return cleanText;
}

async function send() {
  if (window.ChatDocuments?.busy()) { showToast('文件还在读取，稍等一下。'); return; }
  if (typeof resolveDailyConversation === 'function' && resolveDailyConversation(currentConvId) !== currentConvId) {
    beginChatSync();
    handoverDailyWindow(resolveDailyConversation(currentConvId));
    refreshChat();
  }
  const input = $("input");
  const text = input.value.trim() ? input.value : '';
  if ((!text && !pendingAttachments.length) || !currentConvId || sending) return;
  if (pendingAttachments.filter(item => item?.type === 'sticker').length > 1) {
    showToast("每条消息最多发送一张表情，请移除多余表情", 2600);
    return;
  }
  if (hasVoiceAttachments(pendingAttachments) && !currentModelSupportsAudioInput()) {
    showAudioInputUnavailable();
    return;
  }

  // AI Dom 模式：safeword 在用户消息里出现 → 立即熔断（前端先落地，后端也会再读一次兜底）
  // 先快照再 panic：panic 会把 aiDomMode 置 false，但本次消息仍需带 dom 上下文让 AI 知道安全词触发
  const _domModeSnapshot = aiDomMode;
  const _domSafewordSnapshot = aiDomSafeword;
  const _safewordTriggered = window.ControlEmergencyStop
    ? ControlEmergencyStop.triggerSafeword(text, {
        active: () => aiDomMode,
        matches: aiDomCheckSafeword,
        panic: () => aiDomPanic("safeword"),
      })
    : (aiDomMode && text && aiDomCheckSafeword(text) && (aiDomPanic("safeword"), true));
  if (_safewordTriggered) {
    // 继续把消息发出去，让 AI 感知到 safeword 从而切回温柔态
  }

  sending = true;
  if (typeof whisperAutoRetreatCancel === 'function') whisperAutoRetreatCancel();
  $("sendBtn").disabled = true;
  input.value = "";
  persistChatDraft();
  autoResize(input);
  const pendingSnapshot = pendingAttachments.slice();
  const attachments = pendingSnapshot.map(serializeChatAttachment).filter(Boolean);
  pendingAttachments = [];
  renderPreview();
  let requestAccepted = false;

  // 立即显示用户消息（乐观更新）
  const tempUserMsg = { id: "temp_user", conv_id: currentConvId, role: "user", content: text, created_at: Date.now()/1000, attachments };
  currentMessages.push(tempUserMsg);
  renderMessages();

  try {
    _lastUserMood = _detectUserMood(text);
    // AI Dom 延迟补偿：用户话一出就给身体一次"听见了"的微反馈，掩盖后端响应时延
    if (_domModeSnapshot) aiDomInstantTap();
    const contextLimit = parseInt($("contextSlider").value) || 30;
    const temperature = parseFloat($("tempSlider").value);
    const sendBody = { content: text, context_limit: contextLimit, attachments, whisper_mode: whisperMode, temperature,
                       ai_dom_mode: _domModeSnapshot, safeword: _domSafewordSnapshot,
                       dom_history: _domModeSnapshot ? aiDomHistorySnapshot() : [] };
    if (_domModeSnapshot) {
      const ctx = aiDomBuildSendContext(text);
      Object.assign(sendBody, ctx);
    }
    if (window.ControlRuntime && (_domModeSnapshot || whisperMode)) {
      await ControlRuntime.updateSnapshot(
        _domModeSnapshot ? aiDomBuildControlSnapshot() : whisperBuildControlSnapshot()
      );
    }
    aiDomSendClickedAt = Date.now();
    if (_domModeSnapshot) aiDomInitiativeReset();
    if (_pendingRetract) { sendBody.retracted = true; _pendingRetract = false; }
    const res = await fetch(`/api/conversations/${currentConvId}/send`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(sendBody)
    });
    await ensureEventStreamResponse(res, "发送");
    requestAccepted = true;

    const streamState = createSendStreamState();
    await consumeSendStream(res.body.getReader(), streamState);
    finalizeSendStream(streamState);

  } catch (err) {
    console.error("发送失败:", err);
    _stopTypingAnim();
    showToast(err.message || "发送失败", 3600);
    if (!requestAccepted && currentMessages.some(m => m.id === "temp_user")) {
      currentMessages = currentMessages.filter(m => m.id !== "temp_user");
      pendingAttachments = [...pendingSnapshot, ...pendingAttachments];
      if (text) input.value = input.value.trim() ? `${text}\n${input.value}` : text;
      persistChatDraft();
      autoResize(input);
      renderPreview();
      renderMessages();
    }
    // 记录到系统日志并闪烁按钮
    addErrorToSystemLog(`发送失败: ${err.message || err}`, $("modelSelect")?.value);
    // 移除未完成的 AI 消息占位；若已有部分内容，保留并标记等待 WS 恢复
    if (streamingAiId) {
      const mi = currentMessages.findIndex(m => m.id === streamingAiId);
      if (mi >= 0 && currentMessages[mi].content === '...') {
        currentMessages.splice(mi, 1);
        renderMessages();
      } else if (mi >= 0) {
        _recoverStreamMsgId = streamingAiId;
      }
    }
  } finally {
    window.ChatMarkdown?.finish(streamingAiId);
    sending = false;
    streamingAiId = null;
    $("sendBtn").disabled = false;
    window.ChatHeartNotes?.sync();
  }
}

ChatApp.registerModule("send", {
  send,
  createStreamState: createSendStreamState,
  handleStreamEvent: handleSendStreamEvent,
  consumeStream: consumeSendStream,
  finalizeStream: finalizeSendStream,
  isTransientErrorMessage,
  isErrorLikeAssistantMessage,
  markTransientErrorMessage,
  removeLocalMessage,
});
