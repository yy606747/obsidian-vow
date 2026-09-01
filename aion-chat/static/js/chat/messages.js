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
  // 编辑时合并多气泡为单气泡
  const bubbles = row.querySelector('.msg-bubbles');
  if (bubbles) { const single = document.createElement('div'); single.className = 'msg-bubble'; bubbles.replaceWith(single); }
  const bubble = row.querySelector('.msg-bubble');
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
  const newText = ta.value.trim();
  const msg = currentMessages.find(m => m.id === id);
  if (!msg) return;
  if (newText && newText !== msg.content) {
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
          } else if (d.type === "chunk") {
            _stopTypingAnim();
            if (d.content.includes('\x00RETRY\x00')) {
              aiContent = '';
              const mi2 = currentMessages.findIndex(m => m.id === newId);
              if (mi2 >= 0) currentMessages[mi2].content = '...';
              const b2 = document.querySelector(`#m_${newId} .msg-bubble`);
              if (b2) b2.textContent = '...';
              _startTypingAnim(newId);
              continue;
            }
            aiContent += d.content;
            if (assistantErrorText(aiContent)) {
              hasError = true;
              markTransientErrorMessage(newId, aiContent);
            }
            const display = cleanAssistantContent(aiContent);
            const mi = currentMessages.findIndex(m => m.id === newId);
            if (mi >= 0) currentMessages[mi].content = display;
            const b = document.querySelector(`#m_${newId} .msg-bubble`);
            if (b) b.textContent = display;
            scrollBottom();
          } else if (d.type === "debug" && newId) {
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
            showHeartWhisperHint(d.msg_id, d.content);
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
    sending = false;
    streamingAiId = null;
    $("sendBtn").disabled = false;
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
