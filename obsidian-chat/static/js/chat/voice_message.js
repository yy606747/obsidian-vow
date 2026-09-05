// ── 语音消息：按住录音，原始音频直接交给支持音频输入的主模型 ──
const VOICE_MESSAGE_MAX_MS = 60 * 1000;
const VOICE_MESSAGE_MIN_MS = 300;

function setVoiceMessageMode(enabled) {
  if (enabled && !currentModelSupportsAudioInput()) {
    showAudioInputUnavailable();
    return false;
  }
  const area = document.querySelector(".input-area");
  const row = $("voiceRecordRow");
  area?.classList.toggle("voice-mode", !!enabled);
  row?.setAttribute("aria-hidden", enabled ? "false" : "true");
  return true;
}

function _writeAscii(view, offset, text) {
  for (let i = 0; i < text.length; i += 1) view.setUint8(offset + i, text.charCodeAt(i));
}

function _wavBlobFromPcmBytes(chunks, sampleRate) {
  const pcmBytes = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  const buffer = new ArrayBuffer(44 + pcmBytes);
  const view = new DataView(buffer);
  _writeAscii(view, 0, "RIFF");
  view.setUint32(4, 36 + pcmBytes, true);
  _writeAscii(view, 8, "WAVE");
  _writeAscii(view, 12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  _writeAscii(view, 36, "data");
  view.setUint32(40, pcmBytes, true);
  const output = new Uint8Array(buffer, 44);
  let offset = 0;
  chunks.forEach(chunk => {
    output.set(chunk, offset);
    offset += chunk.length;
  });
  return new Blob([buffer], { type: "audio/wav" });
}

function _wavBlobFromFloatFrames(frames, sampleRate) {
  const sampleCount = frames.reduce((sum, frame) => sum + frame.length, 0);
  const buffer = new ArrayBuffer(44 + sampleCount * 2);
  const view = new DataView(buffer);
  _writeAscii(view, 0, "RIFF");
  view.setUint32(4, 36 + sampleCount * 2, true);
  _writeAscii(view, 8, "WAVE");
  _writeAscii(view, 12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  _writeAscii(view, 36, "data");
  view.setUint32(40, sampleCount * 2, true);
  let offset = 44;
  frames.forEach(frame => {
    for (let i = 0; i < frame.length; i += 1) {
      const sample = Math.max(-1, Math.min(1, frame[i]));
      view.setInt16(offset, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
      offset += 2;
    }
  });
  return new Blob([buffer], { type: "audio/wav" });
}

const voiceMessageRecorder = {
  starting: false,
  active: false,
  processing: false,
  releaseRequested: false,
  cancelRequested: false,
  sourceConvId: null,
  sourceModelKey: null,
  useNative: false,
  ownsNative: false,
  nativeChunks: [],
  floatFrames: [],
  sampleRate: 16000,
  stream: null,
  context: null,
  source: null,
  processor: null,
  startedAt: 0,
  ticker: 0,
  maxTimer: 0,

  async start() {
    if (this.starting || this.active || this.processing || sending) return;
    if (!currentConvId) {
      showToast("请先选择或创建一个对话", 2400);
      return;
    }
    if (!currentModelSupportsAudioInput()) {
      showAudioInputUnavailable();
      return;
    }

    this.starting = true;
    this.releaseRequested = false;
    this.cancelRequested = false;
    this.sourceConvId = currentConvId;
    this.sourceModelKey = $("modelSelect")?.value || "";
    this.nativeChunks = [];
    this.floatFrames = [];
    this.useNative = false;
    this.ownsNative = false;

    try {
      if (window.ObsidianAudio) {
        let alreadyRecording = false;
        try { alreadyRecording = !!window.ObsidianAudio.isRecording(); } catch (_) {}
        let nativeReady = alreadyRecording;
        if (!nativeReady) {
          try { nativeReady = !!window.ObsidianAudio.start(); } catch (_) {}
          this.ownsNative = nativeReady;
        }
        if (nativeReady) {
          this.useNative = true;
          this.sampleRate = 16000;
        }
      }

      if (!this.useNative) {
        if (!navigator.mediaDevices?.getUserMedia) throw new Error("当前环境不能访问麦克风");
        this.stream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        });
        this.context = new (window.AudioContext || window.webkitAudioContext)();
        this.sampleRate = this.context.sampleRate;
        this.source = this.context.createMediaStreamSource(this.stream);
        this.processor = this.context.createScriptProcessor(2048, 1, 1);
        this.processor.onaudioprocess = event => {
          if (this.active) this.floatFrames.push(new Float32Array(event.inputBuffer.getChannelData(0)));
        };
        this.source.connect(this.processor);
        this.processor.connect(this.context.destination);
      }

      this.starting = false;
      if (this.releaseRequested) {
        this._cleanupCapture();
        if (!this.cancelRequested) showToast("按住时间太短", 1600);
        return;
      }
      this.active = true;
      this.startedAt = performance.now();
      this._setRecordingUi(true);
      this.ticker = window.setInterval(() => this._updateElapsed(), 100);
      this.maxTimer = window.setTimeout(() => {
        showToast("语音最长 60 秒，已自动发送", 2200);
        this.stop(false);
      }, VOICE_MESSAGE_MAX_MS);
    } catch (error) {
      this.starting = false;
      this._cleanupCapture();
      console.error("[VoiceMessage] microphone start failed:", error);
      showToast(error?.message || "无法访问麦克风", 3000);
    }
  },

  requestStop(cancel) {
    if (this.starting) {
      this.releaseRequested = true;
      this.cancelRequested = !!cancel;
      return;
    }
    if (this.active) this.stop(!!cancel);
  },

  async stop(cancel) {
    if (!this.active) return;
    this.active = false;
    const wallDurationMs = Math.max(0, performance.now() - this.startedAt);
    window.clearInterval(this.ticker);
    window.clearTimeout(this.maxTimer);
    this.ticker = 0;
    this.maxTimer = 0;
    this._setRecordingUi(false);

    let blob = null;
    let durationMs = wallDurationMs;
    if (this.useNative) {
      const byteCount = this.nativeChunks.reduce((sum, chunk) => sum + chunk.length, 0);
      durationMs = byteCount / (this.sampleRate * 2) * 1000;
      if (byteCount) blob = _wavBlobFromPcmBytes(this.nativeChunks, this.sampleRate);
    } else {
      const sampleCount = this.floatFrames.reduce((sum, frame) => sum + frame.length, 0);
      durationMs = sampleCount / this.sampleRate * 1000;
      if (sampleCount) blob = _wavBlobFromFloatFrames(this.floatFrames, this.sampleRate);
    }
    this._cleanupCapture();

    if (cancel) return;
    if (!blob || durationMs < VOICE_MESSAGE_MIN_MS) {
      showToast("说话时间太短", 1800);
      return;
    }
    if (this.sourceConvId !== currentConvId || this.sourceModelKey !== ($("modelSelect")?.value || "")) {
      showToast("对话或模型已经切换，请重新录制", 2600);
      return;
    }
    if (!currentModelSupportsAudioInput()) {
      showAudioInputUnavailable();
      return;
    }

    this.processing = true;
    this._setProcessingUi(true);
    try {
      const filename = `voice-${Date.now()}.wav`;
      const uploadForm = new FormData();
      uploadForm.append("file", blob, filename);
      const asrForm = new FormData();
      asrForm.append("file", blob, filename);
      const uploadPromise = fetch("/api/upload", { method: "POST", body: uploadForm })
        .then(response => response.json());
      const asrPromise = fetch("/api/voice/remote-asr", { method: "POST", body: asrForm })
        .then(response => response.json())
        .catch(() => ({ text: "" }));
      const [uploaded, recognized] = await Promise.all([uploadPromise, asrPromise]);
      if (!uploaded?.url || uploaded.error) throw new Error(uploaded?.error || "语音上传失败");
      pendingAttachments.push({
        type: "voice",
        url: uploaded.url,
        mime_type: uploaded.type || "audio/wav",
        name: uploaded.name || filename,
        duration_ms: Math.round(durationMs),
        transcript: String(recognized?.text || "").trim(),
      });
      renderPreview();
      if (!currentModelSupportsAudioInput()) {
        showAudioInputUnavailable();
        return;
      }
      await send();
    } catch (error) {
      console.error("[VoiceMessage] upload failed:", error);
      showToast(error?.message || "语音上传失败", 3000);
    } finally {
      this.processing = false;
      this._setProcessingUi(false);
    }
  },

  onNativeChunk(base64Pcm) {
    if (!this.active || !this.useNative) return;
    try {
      const binary = atob(base64Pcm);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
      this.nativeChunks.push(bytes);
    } catch (_) {}
  },

  _cleanupCapture() {
    if (this.ownsNative && window.ObsidianAudio) {
      try { window.ObsidianAudio.stop(); } catch (_) {}
    }
    this.ownsNative = false;
    this.useNative = false;
    if (this.processor) {
      try { this.processor.disconnect(); } catch (_) {}
      this.processor.onaudioprocess = null;
    }
    if (this.source) {
      try { this.source.disconnect(); } catch (_) {}
    }
    if (this.context) this.context.close().catch(() => {});
    if (this.stream) this.stream.getTracks().forEach(track => track.stop());
    this.processor = null;
    this.source = null;
    this.context = null;
    this.stream = null;
    this.nativeChunks = [];
    this.floatFrames = [];
  },

  _setRecordingUi(recording) {
    const button = $("voiceHoldBtn");
    if (!button) return;
    button.classList.toggle("recording", recording);
    button.textContent = recording ? "松开 发送" : "按住 说话";
    if (!recording && !this.processing) $("voiceRecordTime").textContent = "0:00";
  },

  _setProcessingUi(processing) {
    const button = $("voiceHoldBtn");
    if (!button) return;
    button.classList.toggle("processing", processing);
    button.disabled = processing;
    button.textContent = processing ? "正在处理语音…" : "按住 说话";
    if (!processing) $("voiceRecordTime").textContent = "0:00";
  },

  _updateElapsed() {
    if (!this.active) return;
    $("voiceRecordTime").textContent = formatVoiceDuration(performance.now() - this.startedAt);
  },
};

window._voiceNativeOnChunk = base64Pcm => voiceMessageRecorder.onNativeChunk(base64Pcm);

function bindVoiceMessageControls() {
  let pointerId = null;
  const hold = $("voiceHoldBtn");
  hold?.addEventListener("pointerdown", event => {
    if (event.button != null && event.button !== 0) return;
    event.preventDefault();
    pointerId = event.pointerId;
    try { hold.setPointerCapture(pointerId); } catch (_) {}
    voiceMessageRecorder.start();
  });
  hold?.addEventListener("pointerup", event => {
    if (pointerId !== event.pointerId) return;
    event.preventDefault();
    pointerId = null;
    voiceMessageRecorder.requestStop(false);
  });
  hold?.addEventListener("pointercancel", event => {
    if (pointerId !== event.pointerId) return;
    pointerId = null;
    voiceMessageRecorder.requestStop(true);
  });
}

bindVoiceMessageControls();

ChatApp.registerModule("voiceMessage", {
  isAttachment: isVoiceAttachment,
  hasAttachments: hasVoiceAttachments,
  supportsCurrentModel: currentModelSupportsAudioInput,
  serializeAttachment: serializeChatAttachment,
  renderAttachment: renderVoiceAttachment,
  renderPreview: renderVoicePreview,
  setMode: setVoiceMessageMode,
  recorder: voiceMessageRecorder,
});
