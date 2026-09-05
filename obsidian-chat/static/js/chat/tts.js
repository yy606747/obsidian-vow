// ── TTS 语音合成 ──
let ttsEnabled = localStorage.getItem('obsidian_tts_enabled') === 'true';
let ttsVoiceId = localStorage.getItem('obsidian_tts_voice') || '';
let ttsAudio = new Audio();
let ttsQueue = [];
let ttsPlaying = false;
let ttsVoicesLoaded = false;
let ttsVoicesLoading = null;
let ttsControlsBound = false;

function initTTSControls() {
  const toggle = $('ttsToggle');
  if (toggle) toggle.checked = ttsEnabled;
  const sel = $('ttsVoiceSelect');
  if (sel && ttsVoiceId) {
    sel.innerHTML = `<option value="${escHtml(ttsVoiceId)}" selected>${escHtml(ttsVoiceId)}</option>`;
  }
  bindTTSControls();
}

function bindTTSControls() {
  if (ttsControlsBound) return;
  ttsControlsBound = true;

  const toggle = $('ttsToggle');
  if (toggle) toggle.addEventListener('change', () => toggleTTS());
  const sel = $('ttsVoiceSelect');
  if (sel) sel.addEventListener('change', () => changeTTSVoice());
  const refreshBtn = document.querySelector('[data-action="refreshTTSVoices"]');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', event => {
      event.preventDefault();
      refreshTTSVoices();
    });
  }
}

async function ensureTTSVoicesLoaded() {
  if (ttsVoicesLoaded) return;
  return refreshTTSVoices();
}

function toggleTTS() {
  ttsEnabled = $('ttsToggle').checked;
  localStorage.setItem('obsidian_tts_enabled', ttsEnabled);
  if (!ttsEnabled) {
    ttsAudio.pause();
    ttsAudio.src = '';
    ttsQueue = [];
    ttsPlaying = false;
  } else if (!ttsVoiceId) {
    ensureTTSVoicesLoaded();
  }
}

function changeTTSVoice() {
  ttsVoiceId = $('ttsVoiceSelect').value;
  localStorage.setItem('obsidian_tts_voice', ttsVoiceId);
}

async function refreshTTSVoices() {
  if (ttsVoicesLoading) return ttsVoicesLoading;
  ttsVoicesLoading = (async () => {
  try {
    const data = await api("GET", "/api/tts/voices");
    const sel = $('ttsVoiceSelect');
    if (data.voices && data.voices.length > 0) {
      sel.innerHTML = data.voices.map(v => {
        const name = v.customName || v.uri || 'Unknown';
        return `<option value="${v.uri}" ${v.uri === ttsVoiceId ? 'selected' : ''}>${name}</option>`;
      }).join('');
      // 如果没有选中的音色，默认选第一个
      if (!ttsVoiceId || !data.voices.find(v => v.uri === ttsVoiceId)) {
        ttsVoiceId = data.voices[0].uri;
        localStorage.setItem('obsidian_tts_voice', ttsVoiceId);
        sel.value = ttsVoiceId;
      }
    } else {
      sel.innerHTML = '<option value="">无可用音色</option>';
    }
    ttsVoicesLoaded = true;
  } catch(e) {
    console.error('刷新TTS音色失败:', e);
  } finally {
    ttsVoicesLoading = null;
  }
  })();
  return ttsVoicesLoading;
}

function ttsSpeak(text, msgId) {
  if (!ttsEnabled || !ttsVoiceId || !text || !text.trim()) return;
  // 通话中时通知语音模块 AI 开始说话
  if (voiceInCall) {
    notifyVoiceAiSpeaking(true);
  }
  ttsQueue.push({text: text.trim(), msgId: msgId || null});
  if (!ttsPlaying) playNextTTS();
}

async function playNextTTS() {
  if (ttsQueue.length === 0 || !ttsEnabled) {
    ttsPlaying = false;
    // TTS 队列空了 → AI 说完了，通知语音模块
    if (voiceInCall) {
      notifyVoiceAiSpeaking(false);
    }
    return;
  }
  ttsPlaying = true;
  const item = ttsQueue.shift();
  const text = item.text;
  const msgId = item.msgId;
  try {
    const reqBody = { text: text, voice: ttsVoiceId };
    if (msgId) reqBody.msg_id = msgId;
    const resp = await fetch('/api/tts', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(reqBody)
    });
    if (!resp.ok || !ttsEnabled) { ttsPlaying = false; playNextTTS(); return; }
    const blob = await resp.blob();
    if (blob.type && blob.type.includes('json')) {
      // 返回了错误 JSON 而非音频
      ttsPlaying = false; playNextTTS(); return;
    }
    const url = URL.createObjectURL(blob);
    ttsAudio.src = url;
    ttsAudio.onended = () => { URL.revokeObjectURL(url); ttsPlaying = false; playNextTTS(); };
    ttsAudio.onerror = () => { URL.revokeObjectURL(url); ttsPlaying = false; playNextTTS(); };
    await ttsAudio.play().catch(() => { ttsPlaying = false; playNextTTS(); });
  } catch(e) {
    console.error('TTS失败:', e);
    ttsPlaying = false;
    playNextTTS();
  }
}

// 重听 TTS 音频（从服务器缓存播放）
let replayAudio = new Audio();
async function replayTTS(msgId) {
  try {
    const btn = document.querySelector(`#m_${msgId} .tts-replay-btn`);
    // 如果正在播放同一条，停止
    if (btn && btn.classList.contains('playing')) {
      replayAudio.pause();
      replayAudio.src = '';
      btn.classList.remove('playing');
      return;
    }
    // 停止之前的播放
    replayAudio.pause();
    document.querySelectorAll('.tts-replay-btn.playing').forEach(b => b.classList.remove('playing'));

    const resp = await fetch(`/api/tts/audio/${msgId}`);
    if (!resp.ok) return;
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    replayAudio.src = url;
    if (btn) btn.classList.add('playing');
    replayAudio.onended = () => { URL.revokeObjectURL(url); if (btn) btn.classList.remove('playing'); };
    replayAudio.onerror = () => { URL.revokeObjectURL(url); if (btn) btn.classList.remove('playing'); };
    await replayAudio.play().catch(() => { if (btn) btn.classList.remove('playing'); });
  } catch(e) {
    console.error('重听TTS失败:', e);
  }
}

ChatApp.registerModule("tts", {
  initControls: initTTSControls,
  bindControls: bindTTSControls,
  ensureVoicesLoaded: ensureTTSVoicesLoaded,
  refreshVoices: refreshTTSVoices,
  toggle: toggleTTS,
  changeVoice: changeTTSVoice,
  speak: ttsSpeak,
  replay: replayTTS,
  isEnabled: () => ttsEnabled,
  isVoiceListLoaded: () => ttsVoicesLoaded,
  getSelectedVoice: () => ttsVoiceId,
});
