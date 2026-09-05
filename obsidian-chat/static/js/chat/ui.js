// ── UI ──
function handleKey(e) { if (e.key === "Enter" && e.ctrlKey) { e.preventDefault(); send(); } }

const AUDIO_INPUT_UNAVAILABLE_MESSAGE = "当前主模型不支持直接听取音频，请切换到支持音频的模型后再发送。";
const VOICE_MESSAGE_SCRIPT = "/static/js/chat/voice_message.js?v=20260905-brand";
let voiceMessageLoadPromise = null;

function isVoiceAttachment(item) {
  if (!item) return false;
  if (typeof item === "string") return /\.(wav|mp3)(?:$|[?#])/i.test(item);
  if (typeof item !== "object") return false;
  const type = String(item.type || "").toLowerCase();
  const mime = String(item.mime_type || item.mimeType || item.content_type || "").toLowerCase();
  return type === "voice" || type.startsWith("audio/") || mime.startsWith("audio/");
}

function hasVoiceAttachments(items) {
  return Array.isArray(items) && items.some(isVoiceAttachment);
}

function currentModelConfig() {
  const key = $("modelSelect")?.value || "";
  return models.find(item => item.key === key) || null;
}

function currentModelSupportsAudioInput() {
  return currentModelConfig()?.audio_input === true;
}

function showAudioInputUnavailable() {
  showToast(AUDIO_INPUT_UNAVAILABLE_MESSAGE, 3600);
}

function serializeChatAttachment(item) {
  if (isVoiceAttachment(item) && typeof item === "object") {
    return {
      type: "voice",
      url: String(item.url || ""),
      mime_type: String(item.mime_type || item.mimeType || "audio/wav"),
      name: String(item.name || "voice-message.wav"),
      duration_ms: Math.max(0, Math.round(Number(item.duration_ms) || 0)),
      transcript: String(item.transcript || ""),
    };
  }
  return typeof item === "string" ? item : String(item?.url || "");
}

function voiceDurationMs(item) {
  if (!item || typeof item !== "object") return 0;
  if (item.duration_ms != null) return Math.max(0, Number(item.duration_ms) || 0);
  return Math.max(0, (Number(item.duration) || 0) * 1000);
}

function formatVoiceDuration(ms) {
  const total = Math.max(0, Math.round((Number(ms) || 0) / 1000));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

function renderVoiceAttachment(item) {
  const url = escHtml(typeof item === "string" ? item : item.url || "");
  const duration = formatVoiceDuration(voiceDurationMs(item));
  return `
    <div class="voice-message-bar" data-voice-message>
      <button class="voice-message-play" data-voice-play type="button" aria-label="播放语音">▶</button>
      <div class="voice-message-track" data-voice-seek><span class="voice-message-progress"></span></div>
      <span class="voice-message-duration">${duration}</span>
      <audio src="${url}" preload="metadata"></audio>
    </div>`;
}

function renderVoicePreview(item) {
  const duration = formatVoiceDuration(voiceDurationMs(item));
  const transcript = String(item?.transcript || "").trim();
  return `
    <span class="voice-preview-icon">🎙</span>
    <span class="voice-preview-meta">
      <strong>语音 ${duration}</strong>
      <span>${transcript ? escHtml(transcript) : "原始音频"}</span>
    </span>`;
}

function _syncVoicePlaybackUi(bar, audio) {
  const button = bar.querySelector("[data-voice-play]");
  const progress = bar.querySelector(".voice-message-progress");
  const duration = bar.querySelector(".voice-message-duration");
  if (button) button.textContent = audio.paused ? "▶" : "❚❚";
  const total = Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : 0;
  if (progress) progress.style.width = total ? `${Math.min(100, (audio.currentTime / total) * 100)}%` : "0";
  if (duration && total) duration.textContent = formatVoiceDuration((audio.paused ? total : audio.currentTime) * 1000);
}

function bindVoiceMessagePlayback() {
  document.addEventListener("click", event => {
    const play = event.target.closest("[data-voice-play]");
    if (play) {
      event.preventDefault();
      const bar = play.closest("[data-voice-message]");
      const audio = bar?.querySelector("audio");
      if (!audio) return;
      document.querySelectorAll("[data-voice-message] audio").forEach(other => {
        if (other !== audio) other.pause();
      });
      audio.ontimeupdate = () => _syncVoicePlaybackUi(bar, audio);
      audio.onplay = () => _syncVoicePlaybackUi(bar, audio);
      audio.onpause = () => _syncVoicePlaybackUi(bar, audio);
      audio.onended = () => {
        audio.currentTime = 0;
        _syncVoicePlaybackUi(bar, audio);
      };
      if (audio.paused) audio.play().catch(() => showToast("语音播放失败", 2200));
      else audio.pause();
      _syncVoicePlaybackUi(bar, audio);
      return;
    }
    const seek = event.target.closest("[data-voice-seek]");
    if (!seek) return;
    const bar = seek.closest("[data-voice-message]");
    const audio = bar?.querySelector("audio");
    if (!audio || !Number.isFinite(audio.duration) || audio.duration <= 0) return;
    const rect = seek.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
    audio.currentTime = audio.duration * ratio;
    _syncVoicePlaybackUi(bar, audio);
  });
}

function ensureVoiceMessageLoaded() {
  const loaded = ChatApp.getModule("voiceMessage");
  if (loaded) return Promise.resolve(loaded);
  if (voiceMessageLoadPromise) return voiceMessageLoadPromise;
  voiceMessageLoadPromise = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = VOICE_MESSAGE_SCRIPT;
    script.async = true;
    script.onload = () => {
      const module = ChatApp.getModule("voiceMessage");
      if (module) resolve(module);
      else reject(new Error("语音消息模块加载失败"));
    };
    script.onerror = () => reject(new Error("语音消息模块加载失败"));
    document.head.appendChild(script);
  }).catch(error => {
    voiceMessageLoadPromise = null;
    throw error;
  });
  return voiceMessageLoadPromise;
}

async function handleVoiceModeAction(action) {
  if (action === "voice-mode") {
    if (!currentModelSupportsAudioInput()) {
      showAudioInputUnavailable();
      return;
    }
    try {
      const module = await ensureVoiceMessageLoaded();
      module.setMode(true);
    } catch (error) {
      showToast(error.message || "语音消息模块加载失败", 2600);
    }
  } else if (action === "text-mode") {
    ChatApp.getModule("voiceMessage")?.setMode(false);
    $("input")?.focus();
  }
}

function renderAttachments(atts) {
  if (!atts || !atts.length) return '';
  let mediaHtml = '';
  let capsuleHtml = '';
  let voiceHtml = '';
  const aiName = worldBook.ai_name || 'AI';
  atts.forEach(item => {
    if (typeof item === 'object' && item.type === 'music') {
      capsuleHtml += `<div class="music-capsule" data-music-action="open" data-song-id="${escHtml(item.id)}">🎵 ${escHtml(aiName)}给你点播歌曲《${escHtml(item.name)}》</div>`;
    } else if (typeof isVoiceAttachment === 'function' && isVoiceAttachment(item)) {
      voiceHtml += renderVoiceAttachment(item);
    } else {
      const url = typeof item === 'string' ? item : '';
      if (/\.(mp4|webm|mov)$/i.test(url)) mediaHtml += `<video src="${escHtml(url)}" controls preload="metadata"></video>`;
      else if (url) mediaHtml += `<img src="${escHtml(url)}" data-attachment-preview>`;
    }
  });
  let html = '';
  if (mediaHtml) html += '<div class="msg-media">' + mediaHtml + '</div>';
  if (voiceHtml) html += '<div class="voice-message-list">' + voiceHtml + '</div>';
  if (capsuleHtml) html += capsuleHtml;
  return html;
}

async function handleFileSelect(input) {
  for (const file of input.files) {
    const fd = new FormData();
    fd.append('file', file);
    const res = await fetch('/api/upload', {method:'POST', body: fd});
    const data = await res.json();
    if (data.error) { alert(data.error); continue; }
    pendingAttachments.push(data);
  }
  input.value = '';
  renderPreview();
}

// 粘贴图片到输入框
document.addEventListener('DOMContentLoaded', () => {
  const input = $('input');
  if (input) input.addEventListener('paste', async (e) => {
    const items = e.clipboardData && e.clipboardData.items;
    if (!items) return;
    for (const item of items) {
      if (!item.type.startsWith('image/')) continue;
      e.preventDefault();
      const file = item.getAsFile();
      if (!file) continue;
      const fd = new FormData();
      fd.append('file', file);
      const res = await fetch('/api/upload', {method:'POST', body: fd});
      const data = await res.json();
      if (data.error) { alert(data.error); continue; }
      pendingAttachments.push(data);
      renderPreview();
    }
  });
});

function renderPreview() {
  const area = $('previewArea');
  if (!pendingAttachments.length) { area.className = 'preview-area'; area.innerHTML = ''; return; }
  area.className = 'preview-area has-files';
  area.innerHTML = pendingAttachments.map((a, i) => {
    if (typeof isVoiceAttachment === 'function' && isVoiceAttachment(a)) {
      return `<div class="preview-item voice-preview">${renderVoicePreview(a)}<button class="preview-remove" data-preview-remove="${i}">✕</button></div>`;
    }
    const isVid = a.type && a.type.startsWith('video/');
    const media = isVid ? `<video src="${a.url}" muted></video>` : `<img src="${a.url}">`;
    return `<div class="preview-item">${media}<button class="preview-remove" data-preview-remove="${i}">✕</button></div>`;
  }).join('');
}

function removeAttachment(i) {
  pendingAttachments.splice(i, 1);
  renderPreview();
}

let attachmentEventsBound = false;

function bindAttachmentEvents() {
  if (attachmentEventsBound) return;
  attachmentEventsBound = true;

  document.addEventListener('click', event => {
    const previewImg = event.target.closest('[data-attachment-preview]');
    if (previewImg) {
      event.preventDefault();
      window.open(previewImg.src);
      return;
    }

    const removeBtn = event.target.closest('[data-preview-remove]');
    if (removeBtn) {
      event.preventDefault();
      removeAttachment(parseInt(removeBtn.dataset.previewRemove, 10));
    }
  });
}

function toggleMsgMenu(id) {
  const menu = document.getElementById('menu_' + id);
  const wasOpen = menu && menu.classList.contains('show');
  closeMsgMenus();
  if (!wasOpen && menu) menu.classList.add('show');
}
function closeMsgMenus() {
  document.querySelectorAll('.msg-menu.show').forEach(m => m.classList.remove('show'));
}
document.addEventListener('click', closeMsgMenus);

function autoResize(el) {
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 200) + "px";
}

function openSidebar() { $("sidebar").classList.add("open"); $("overlay").classList.add("show"); }
function closeSidebar() { $("sidebar").classList.remove("open"); $("overlay").classList.remove("show"); }

let chatShellEventsBound = false;
let tideFacadeLoadPromise = null;

function openTideControl() {
  if (typeof window.openTide === "function") return window.openTide();
  if (!tideFacadeLoadPromise) {
    tideFacadeLoadPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "/static/js/chat/tide_facade.js?v=20260905-brand";
      script.async = true;
      script.onload = resolve;
      script.onerror = () => reject(new Error("Failed to load tide facade"));
      document.head.appendChild(script);
    });
  }
  return tideFacadeLoadPromise
    .then(() => {
      if (typeof window.openTide === "function") return window.openTide();
      throw new Error("Tide facade did not register openTide");
    })
    .catch(err => {
      console.error("[Tide] open failed:", err);
      showToast?.("潮汐控制加载失败", 2400);
    });
}

function bindChatShellEvents() {
  if (chatShellEventsBound) return;
  chatShellEventsBound = true;

  document.addEventListener("click", event => {
    const el = event.target.closest("[data-action]");
    if (!el) return;
    const action = el.dataset.action;
    const actions = {
      newConversation: () => newConversation(),
      openFileManager: () => openFileManager(),
      openSubPage: () => openSubPage(el.dataset.subpage || "/"),
      closeSubPage: () => closeSubPage(),
      openSystemLog: () => openSystemLog(),
      closeSystemLog: () => closeSystemLog(),
      clearSystemLog: () => clearSystemLog(),
      openWhisper: () => openWhisper(),
      openAiDom: () => openAiDom(),
      openTide: () => openTideControl(),
      closeSidebar: () => closeSidebar(),
      openSidebar: () => openSidebar(),
      renameCurrent: () => renameCurrent(),
      dismissAlarm: () => dismissAlarm(),
      chooseUpload: () => $("fileInput")?.click(),
      send: () => send(),
    };
    if (!actions[action]) return;
    event.preventDefault();
    event.stopPropagation();
    actions[action]();
  }, true);

  document.addEventListener("click", event => {
    const el = event.target.closest("[data-voice-action]");
    if (!el) return;
    event.preventDefault();
    handleVoiceModeAction(el.dataset.voiceAction);
  });

  const bind = (id, type, handler) => {
    const el = $(id);
    if (el) el.addEventListener(type, handler);
  };

  bind("fileInput", "change", event => handleFileSelect(event.target));
  bind("input", "keydown", handleKey);
  bind("input", "input", event => autoResize(event.target));
}

bindChatShellEvents();
bindAttachmentEvents();
bindVoiceMessagePlayback();

// ── 设置/世界书/定位 → 已拆分为独立页面 ──

// ── 文件管理 ──
const FILE_MANAGER_SCRIPT = "/static/js/chat/file_manager.js?v=20260905-brand";
let fileManagerLoadPromise = null;

function getFileManagerModule() {
  return ChatApp.getModule("fileManager");
}

function ensureFileManagerShell() {
  let modal = $("fileModal");
  if (modal) return modal;
  modal = document.createElement("div");
  modal.className = "modal-overlay";
  modal.id = "fileModal";
  modal.innerHTML = `
    <div class="modal fm-modal">
      <div id="fmList">
        <div class="fm-header">
          <h3>📁 聊天记录文件</h3>
          <button class="btn-cancel fm-close" data-file-manager-action="close">✕ 关闭</button>
        </div>
        <div class="fm-file-list" id="fmFileList"></div>
      </div>
      <div id="fmEditor" class="fm-editor">
        <h3 id="fmEditorTitle">编辑文件</h3>
        <textarea class="fm-textarea" id="fmContent"></textarea>
        <div class="btn-row">
          <button class="btn-cancel" data-file-manager-action="back">返回</button>
          <button class="btn-save" data-file-manager-action="save">保存并同步</button>
        </div>
      </div>
    </div>`;
  document.body.appendChild(modal);
  return modal;
}

let fileManagerShellEventsBound = false;

function bindFileManagerShellEvents() {
  if (fileManagerShellEventsBound) return;
  fileManagerShellEventsBound = true;
  document.addEventListener("click", event => {
    const action = event.target.closest("[data-file-manager-action]");
    if (!action || !$("fileModal")?.contains(action)) return;
    event.preventDefault();
    switch (action.dataset.fileManagerAction) {
      case "close":
        closeFileManager();
        break;
      case "back":
        fmBack();
        break;
      case "save":
        fmSave();
        break;
    }
  });
}

bindFileManagerShellEvents();

function ensureFileManagerLoaded() {
  const loaded = getFileManagerModule();
  if (loaded) return Promise.resolve(loaded);
  if (fileManagerLoadPromise) return fileManagerLoadPromise;

  ChatPerf?.markOnce("file_manager_lazy_load_start");
  fileManagerLoadPromise = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = FILE_MANAGER_SCRIPT;
    script.async = true;
    script.onload = () => {
      const api = getFileManagerModule();
      if (!api) {
        fileManagerLoadPromise = null;
        reject(new Error("File manager module did not register"));
        return;
      }
      ChatPerf?.markOnce("file_manager_lazy_load_done");
      resolve(api);
    };
    script.onerror = () => {
      fileManagerLoadPromise = null;
      ChatPerf?.markOnce("file_manager_lazy_load_error");
      reject(new Error("Failed to load file manager module"));
    };
    document.head.appendChild(script);
  });
  return fileManagerLoadPromise;
}

function showFileManagerLoading() {
  ensureFileManagerShell();
  $("fmList").style.display = "";
  $("fmEditor").style.display = "none";
  $("fmFileList").innerHTML = '<div class="fm-empty">正在加载文件管理器...</div>';
  $("fileModal").classList.add("show");
}

async function openFileManager() {
  showFileManagerLoading();
  try {
    const api = await ensureFileManagerLoaded();
    return api.open();
  } catch (err) {
    console.error("[FileManager] lazy load failed:", err);
    showToast?.("文件管理器加载失败", 2400);
    return undefined;
  }
}

function closeFileManager() {
  const api = getFileManagerModule();
  if (api?.close) return api.close();
  $("fileModal")?.classList.remove("show");
  return undefined;
}

function fmOpen(convId) {
  return ensureFileManagerLoaded().then(api => api.openConversationFile(convId));
}

function fmBack() {
  const api = getFileManagerModule();
  if (api?.back) return api.back();
  $("fmList").style.display = "";
  $("fmEditor").style.display = "none";
  return undefined;
}

function fmSave() {
  return ensureFileManagerLoaded().then(api => api.save());
}

ChatApp.registerModule("attachments", {
  render: renderAttachments,
  handleFileSelect,
  renderPreview,
  remove: removeAttachment,
  bindEvents: bindAttachmentEvents,
});

ChatApp.registerModule("messageMenu", {
  toggle: toggleMsgMenu,
  close: closeMsgMenus,
});

ChatApp.registerModule("sidebar", {
  open: openSidebar,
  close: closeSidebar,
});

ChatApp.registerModule("fileManagerLoader", {
  ensureLoaded: ensureFileManagerLoaded,
  isLoaded: () => !!getFileManagerModule(),
  open: openFileManager,
  close: closeFileManager,
});
