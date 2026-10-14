// ── 消息操作 ──
async function delMsg(id) {
  const msg = currentMessages.find(m => m.id === id);
  if (isTransientErrorMessage(msg)) {
    removeLocalMessage(id);
    return;
  }
  if (msg && msg.role === 'user') _pendingRetract = true;
  const res = await api("DELETE", `/api/messages/${id}`);
  if (res && res.ok === false) {
    showToast(`删除失败：${res.error || "未知错误"}`, 3000);
    return;
  }
  removeLocalMessage(id);
}

function editMsg(id) {
  closeMsgMenus();
  const msg = currentMessages.find(m => m.id === id);
  if (!msg) return;
  const row = document.getElementById(`m_${id}`);
  if (!row) return;
  const content = row.querySelector('[data-msg-content]');
  if (!content) return;
  content.innerHTML = '<div class="msg-bubble"></div>';
  const bubble = content.querySelector('.msg-bubble');
  const safeId = escHtml(id);
  bubble.innerHTML = '<textarea class="edit-textarea" id="edit_' + safeId + '"></textarea>' +
    '<div class="edit-actions">' +
    '<button class="edit-cancel" data-edit-action="cancel" data-msg-id="' + safeId + '">取消</button>' +
    '<button class="edit-save" data-edit-action="save" data-msg-id="' + safeId + '">保存</button>' +
    '</div>';
  const ta = document.getElementById('edit_' + id);
  ta.value = msg.content;
  ta.style.height = 'auto';
  ta.style.height = ta.scrollHeight + 'px';
  ta.focus();
}

function cancelEdit(id) { renderMessages(); }

async function saveEdit(id) {
  const ta = document.getElementById('edit_' + id);
  if (!ta) return;
  const newText = ta.value;
  const msg = currentMessages.find(m => m.id === id);
  if (!msg) return;
  if (newText.trim() && newText !== msg.content) {
    const res = await api("PUT", `/api/messages/${id}`, { content: newText });
    // 后端只允许编辑 user 消息（誓约可见性不变量）；被拒时不在本地假装改成功
    if (!res || res.ok !== false) msg.content = newText;
  }
  renderMessages();
}

function copyMsg(id) {
  const msg = currentMessages.find(m => m.id === id);
  if (msg) navigator.clipboard.writeText(msg.content);
}

async function regenerateMsg(aiMsgId) {
  if (await window.ChatTogether?.guard?.()) return;
  if (sending || !currentConvId) return;
  const latestUserMessage = [...currentMessages].reverse().find(message => message.role === "user");
  if (hasVoiceAttachments(latestUserMessage?.attachments) && !currentModelSupportsAudioInput()) {
    showAudioInputUnavailable();
    return;
  }
  const replacedMessage = currentMessages.find(m => m.id === aiMsgId);
  const replaceOnBackend = !isTransientErrorMessage(replacedMessage);
  // 删旧消息不再单独 DELETE：replaced_message_id 交给后端在单一事务里
  // 撤约→删消息→冻结誓约 snapshot（誓约设计 §4.5）。事务失败时旧消息保留，
  // 本地也保留——只有收到 start（事务已提交、开始生成）才移除旧消息。
  sending = true;
  $("sendBtn").disabled = true;

  try {
    const cl = parseInt($("contextSlider").value) || 30;
    const temperature = parseFloat($("tempSlider").value);
    let domQs = '';
    if (aiDomMode) {
      const now = Date.now();
      const sessionElapsed = aiDomSessionStartAt ? Math.floor((now - aiDomSessionStartAt) / 1000) : 0;
      const sceneElapsed = aiDomScene.startAt ? Math.floor((now - aiDomScene.startAt) / 1000) : 0;
      const sinceLastPunish = aiDomLastPunishAt ? Math.floor((now - aiDomLastPunishAt) / 1000) : '';
      const wk = (aiDomCncWeakness || []).join('|');
      domQs = `&cnc_enabled=${aiDomCncEnabled}` +
              `&cnc_weakness=${encodeURIComponent(wk)}` +
              `&resist_hits=${aiDomLastResistHits}` +
              `&short_streak=${aiDomShortStreak}` +
              `&reply_delay_ms=${aiDomLastReplyDelayMs}` +
              `&compliance_streak=${aiDomComplianceStreak}` +
              `&session_elapsed=${sessionElapsed}` +
              `&scene_name=${encodeURIComponent(aiDomScene.name || '')}` +
              `&scene_elapsed=${sceneElapsed}` +
              (sinceLastPunish === '' ? '' : `&since_last_punish=${sinceLastPunish}`) +
              `&ratchet_valley=${aiDomRatchetValley}` +
              `&debt=${aiDomDebt}` +
              `&stubborn_streak=${aiDomStubbornStreak}`;
    }
    if (window.ControlRuntime && (aiDomMode || whisperMode)) {
      await ControlRuntime.updateSnapshot();
    }
    const replaceQs = replaceOnBackend ? `&replaced_message_id=${encodeURIComponent(aiMsgId)}` : "";
    const res = await fetch(`/api/conversations/${currentConvId}/regenerate?context_limit=${cl}&whisper_mode=${whisperMode}&temperature=${temperature}&ai_dom_mode=${aiDomMode}&safeword=${encodeURIComponent(aiDomSafeword)}&dom_history=${encodeURIComponent(aiDomMode ? aiDomHistorySnapshot().join(',') : '')}${replaceQs}${domQs}`, {
      method: "POST", headers: {"Content-Type": "application/json"}
    });
    await ensureEventStreamResponse(res, "重新生成");
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let newId = null, aiContent = "", buf = "", hasError = false;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        try {
          const d = JSON.parse(line.slice(6));
          if (d.type === "start") {
            newId = d.id;
            streamingAiId = newId;
            // 事务已提交：旧消息确定被删（WS msg_deleted 也会到，这里先行移除保证顺序）
            currentMessages = currentMessages.filter(m => m.id !== aiMsgId);
            currentMessages.push({ id: newId, conv_id: currentConvId, role: "assistant", content: "...", created_at: Date.now()/1000 });
            renderMessages();
            _startTypingAnim(newId);
          } else if (d.type === "retry" && newId) {
            aiContent = '';
            hasError = false;
            resetStreamingAssistantMessage(newId);
            _startTypingAnim(newId, d.reason === "timeout" ? "连接有点慢，正在重试…" : "");
          } else if (d.type === "query") {
            showQueryProgress(newId, d, aiContent);
          } else if (d.type === "chunk") {
            clearQueryProgress(newId);
            _stopTypingAnim();
            if (d.content.includes('\x00RETRY\x00')) {
              aiContent = '';
              hasError = false;
              resetStreamingAssistantMessage(newId);
              _startTypingAnim(newId);
              continue;
            }
            aiContent += d.content;
            if (assistantErrorText(aiContent)) {
              hasError = true;
              markTransientErrorMessage(newId, aiContent);
            }
            updateStreamingAssistantMessage(newId, aiContent);
          } else if (d.type === "tool_delivery") {
            _stopTypingAnim();
            currentMessages = currentMessages.filter(message => message.id !== d.id);
            renderMessages();
          } else if (d.type === "look_at_me") {
            showToast('陪伴邀请已发往手机');
          } else if (d.type === "message" && d.message) {
            _stopTypingAnim();
            aiContent = String(d.message.content || "");
            upsertSystemMessage(d.message);
          } else if (d.type === "debug" && newId) {
            clearQueryProgress(newId);
            msgDebugData[newId] = d;
            if (d.has_error) {
              hasError = true;
              markTransientErrorMessage(newId, d.error_text);
            }
            renderDebugBar(newId);
          } else if (d.type === "cam_check") {
            handleCamCheck(d.conv_id, d.model_key, newId);
          } else if (d.type === "cam_offline") {
            showCamOfflineNotice();
          } else if (d.type === "activity_check") {
            handleActivityCheck(d.conv_id, d.n, newId);
          } else if (d.type === "screen_check_pending") {
            handleScreenCheck(d, newId);
          } else if (d.type === "screen_check_complete" || d.type === "screen_check_rejected") {
            dismissScreenCheckIndicator();
          } else if (d.type === "poi_search") {
            handlePoiSearch(d.categories, newId);
          } else if (d.type === "music") {
            msgMusicCards[d.msg_id] = d.cards;
            renderMusicCards(d.msg_id);
            scrollBottom();
            if (d.cards && d.cards.length) playMusicOnline(d.cards[0].id);
          } else if (d.type === "toy_command") {
            ControlToyRouter.execute(d);
          } else if (d.type === "toy_command_rejected") {
            ControlToyRouter.reportRejected(d);
          } else if (d.type === "heart_whisper") {
            showHeartWhisperHint(d.msg_id, d.content, d);
          } else if (d.type === "generation_blocked") {
            hasError = true;
            handleGenerationBlocked(d);
          }
        } catch {}
      }
    }
    // 重新生成完成后：检测 AI 情绪
    if (!hasError && newId && aiContent) {
      _msgMoods[newId] = _detectAiMood(aiContent);
      _applyMoodGlow(newId);
    }
    // TTS：重新生成完成后自动播报
    if (!hasError && aiContent) ttsSpeak(cleanAssistantContent(aiContent), newId);
    if (voiceInCall && !ttsEnabled) {
      notifyVoiceAiSpeaking(false);
    }
  } catch (err) {
    console.error("重新生成失败:", err);
    _stopTypingAnim();
    showToast(err.message || "重新生成失败", 3600);
    addErrorToSystemLog(`重新生成失败: ${err.message || err}`, $("modelSelect")?.value);
  } finally {
    _stopTypingAnim();
    window.ChatMarkdown?.finish(streamingAiId);
    sending = false;
    streamingAiId = null;
    $("sendBtn").disabled = false;
    window.ChatHeartNotes?.sync();
  }
}

let messageEditEventsBound = false;

function bindMessageEditEvents() {
  if (messageEditEventsBound) return;
  messageEditEventsBound = true;

  document.addEventListener('click', event => {
    const action = event.target.closest('[data-edit-action]');
    if (!action || !$('messages')?.contains(action)) return;
    event.preventDefault();
    const id = action.dataset.msgId;
    if (action.dataset.editAction === 'cancel') cancelEdit(id);
    if (action.dataset.editAction === 'save') saveEdit(id);
  });

  document.addEventListener('input', event => {
    const ta = event.target.closest('.edit-textarea');
    if (!ta) return;
    ta.style.height = 'auto';
    ta.style.height = ta.scrollHeight + 'px';
  });
}

bindMessageEditEvents();

ChatApp.registerModule("messageActions", {
  delete: delMsg,
  edit: editMsg,
  cancelEdit,
  saveEdit,
  copy: copyMsg,
  regenerate: regenerateMsg,
  bindEditEvents: bindMessageEditEvents,
});
