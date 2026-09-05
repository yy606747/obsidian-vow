// ── 语音唤醒通话模式 ──
var voiceEnabled = !!window.voiceEnabled;
var voiceInCall = !!window.voiceInCall;
var voiceMicSource = window.voiceMicSource || 'local'; // 'local' = PC后端 sounddevice, 'remote' = 手机 getUserMedia

function isRemoteVoice() { return voiceMicSource === 'remote'; }

function onMicSourceChange() {
  const newSrc = $('voiceMicSource').value;
  // 如果正在运行，先关闭
  if (voiceEnabled) {
    $('voiceToggle').checked = false;
    if (voiceMicSource === 'remote') remoteVoice.stop();
    else fetch('/api/voice/toggle', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({enabled:false}) });
    voiceEnabled = false;
  }
  voiceMicSource = newSrc;
  localStorage.setItem('obsidian_voice_mic_source', newSrc);
  $('voiceStatus').textContent = '未开启';
}

async function toggleVoice() {
  const enabled = $('voiceToggle').checked;
  const wakeWord = $('voiceWakeWord').value.trim() || '老公';
  localStorage.setItem('obsidian_voice_enabled', enabled);
  localStorage.setItem('obsidian_voice_wakeword', wakeWord);

  if (isRemoteVoice()) {
    // 手机麦克风模式 — 全部在前端处理
    if (enabled) {
      await remoteVoice.start(wakeWord);
    } else {
      remoteVoice.stop();
    }
  } else {
    // PC 后端模式 — 调后端 API
    try {
      await fetch('/api/voice/toggle', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ enabled, wake_word: wakeWord })
      });
      voiceEnabled = enabled;
      $('voiceStatus').textContent = enabled ? '正在校准...' : '未开启';
    } catch(e) { console.error('Voice toggle error:', e); }
  }
}

function updateVoiceWakeWord() {
  const ww = $('voiceWakeWord').value.trim();
  localStorage.setItem('obsidian_voice_wakeword', ww);
  if (voiceEnabled) toggleVoice();
}

function voiceHangup() {
  if (isRemoteVoice()) {
    remoteVoice.hangup();
  } else {
    fetch('/api/voice/toggle', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ enabled: true, wake_word: $('voiceWakeWord').value.trim() || '老公' })
    });
  }
}

// 通知语音模块 AI 说话状态（自动分发到 local 或 remote）
function notifyVoiceAiSpeaking(speaking) {
  if (isRemoteVoice() && remoteVoice.enabled) {
    remoteVoice.setAiSpeaking(speaking);
  } else {
    fetch('/api/voice/ai-speaking', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ speaking })
    });
  }
}

function notifyVoiceCamCheckStart() {
  if (isRemoteVoice() && remoteVoice.enabled) {
    remoteVoice.aiSpeaking = true;
  } else {
    fetch('/api/voice/cam-check-start', { method: 'POST' });
  }
}

function updateVoiceUI(data) {
  const ind = $('voiceIndicator');
  const txt = $('voiceIndicatorText');
  const btn = $('voiceHangupBtn');
  const status = $('voiceStatus');

  if (!data.enabled) {
    voiceEnabled = false;
    voiceInCall = false;
    ind.className = 'voice-indicator';
    btn.style.display = 'none';
    if (status) status.textContent = '未开启';
    return;
  }

  voiceEnabled = true;
  ind.classList.add('active');

  switch (data.status) {
    case 'calibrating':
      ind.className = 'voice-indicator active waiting';
      txt.textContent = '🔧 校准环境噪音...';
      btn.style.display = 'none';
      if (status) status.textContent = '校准中...';
      break;
    case 'waiting':
      ind.className = 'voice-indicator active waiting';
      txt.textContent = '🎙 等待唤醒词「' + (data.wake_word || '老公') + '」...';
      btn.style.display = 'none';
      voiceInCall = false;
      if (status) status.textContent = '监听中';
      break;
    case 'wakeup':
      ind.className = 'voice-indicator active in-call';
      txt.textContent = '📞 唤醒成功！通话开始';
      btn.style.display = 'inline-block';
      voiceInCall = true;
      if (status) status.textContent = '通话中';
      // 播放唤醒回复音频
      playWakeupReply();
      break;
    case 'listening_cmd':
      ind.className = 'voice-indicator active in-call';
      txt.textContent = '🎧 聆听中... (停顿3秒结束一句话)';
      btn.style.display = 'inline-block';
      voiceInCall = true;
      break;
    case 'recognizing':
      ind.className = 'voice-indicator active in-call';
      txt.textContent = '💬 识别中...';
      break;
    case 'ai_thinking':
      ind.className = 'voice-indicator active ai-speaking';
      txt.textContent = '🤖 AI 思考中...';
      break;
    case 'hangup':
      ind.className = 'voice-indicator active waiting';
      txt.textContent = '📞 ' + (data.message || '通话结束');
      btn.style.display = 'none';
      voiceInCall = false;
      if (status) status.textContent = '监听中';
      setTimeout(() => {
        if (voiceEnabled && !voiceInCall) {
          txt.textContent = '🎙 等待唤醒词...';
        }
      }, 2000);
      break;
    default:
      txt.textContent = data.message || '语音监听中...';
  }
}

async function playWakeupReply() {
  // 播放唤醒应答音频
  try {
    const audio = new Audio('/public/voice-response.mp3');
    notifyVoiceAiSpeaking(true);
    audio.onended = () => { notifyVoiceAiSpeaking(false); };
    audio.onerror = () => { notifyVoiceAiSpeaking(false); };
    await audio.play().catch(() => { notifyVoiceAiSpeaking(false); });
  } catch(e) {
    notifyVoiceAiSpeaking(false);
  }
}

// ══════════════════════════════════════════════════
// ── RemoteVoice: 手机麦克风录音 + VAD + ASR ──
// ══════════════════════════════════════════════════
const remoteVoice = {
  enabled: false,
  inCall: false,
  aiSpeaking: false,
  wakeWord: '老公',
  _stream: null,
  _ctx: null,
  _processor: null,
  _sampleRate: 48000,
  _useNative: false,  // true = Android App 原生桥接
  // VAD state
  _frames: [],
  _speechN: 0,
  _silenceN: 0,
  _isRecording: false,
  _waitN: 0,
  _processing: false,
  // 噪音基线（前 20 帧自动校准）
  _noiseFloor: 0.005,
  _calibFrames: [],
  _calibrated: false,

  async start(wakeWord) {
    if (this.enabled) return;
    this.wakeWord = wakeWord;
    this.inCall = false;
    this.aiSpeaking = false;
    this._processing = false;
    this._calibrated = false;
    this._calibFrames = [];
    this._useNative = false;

    // 优先用 Android 原生桥接（不需要 HTTPS）
    if (window.ObsidianAudio) {
      const ok = window.ObsidianAudio.start();
      if (ok) {
        this._useNative = true;
        this._sampleRate = 16000;
        this._resetVAD();
        this.enabled = true;
        voiceEnabled = true;
        updateVoiceUI({ enabled: true, status: 'calibrating' });
        console.log('[RemoteVoice] Started with native AudioBridge, 16kHz');
        return;
      }
      console.warn('[RemoteVoice] Native bridge start failed, trying getUserMedia...');
    }

    // 回退到 getUserMedia（需要 HTTPS 安全上下文）
    try {
      this._stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
      });
    } catch(e) {
      console.error('[RemoteVoice] getUserMedia failed:', e);
      alert('无法访问麦克风。如果在 App 外使用，需要 HTTPS 页面才能调用麦克风。');
      $('voiceToggle').checked = false;
      return;
    }

    this._ctx = new (window.AudioContext || window.webkitAudioContext)();
    this._sampleRate = this._ctx.sampleRate;
    const source = this._ctx.createMediaStreamSource(this._stream);
    this._processor = this._ctx.createScriptProcessor(2048, 1, 1);
    this._resetVAD();
    this.enabled = true;
    voiceEnabled = true;

    this._processor.onaudioprocess = (e) => this._onAudioFrame(e.inputBuffer.getChannelData(0));
    source.connect(this._processor);
    this._processor.connect(this._ctx.destination);

    updateVoiceUI({ enabled: true, status: 'calibrating' });
    console.log(`[RemoteVoice] Started with getUserMedia, sampleRate=${this._sampleRate}`);
  },

  stop() {
    this.enabled = false;
    this.inCall = false;
    this.aiSpeaking = false;
    voiceEnabled = false;
    voiceInCall = false;
    if (this._useNative && window.ObsidianAudio) {
      window.ObsidianAudio.stop();
      this._useNative = false;
    }
    if (this._processor) { this._processor.disconnect(); this._processor = null; }
    if (this._ctx) { this._ctx.close().catch(()=>{}); this._ctx = null; }
    if (this._stream) { this._stream.getTracks().forEach(t => t.stop()); this._stream = null; }
    updateVoiceUI({ enabled: false, status: 'off' });
    console.log('[RemoteVoice] Stopped');
  },

  hangup() {
    this.inCall = false;
    this.aiSpeaking = false;
    this._resetVAD();
    updateVoiceUI({ enabled: true, status: 'hangup', message: '手动挂断' });
    setTimeout(() => {
      if (this.enabled) updateVoiceUI({ enabled: true, status: 'waiting', wake_word: this.wakeWord });
    }, 2000);
  },

  setAiSpeaking(speaking) {
    this.aiSpeaking = speaking;
    if (!speaking && this.inCall && !this._processing) {
      this._resetVAD();
      updateVoiceUI({ enabled: true, status: 'listening_cmd', message: '聆听中...' });
    }
  },

  _resetVAD() {
    this._frames = [];
    this._speechN = 0;
    this._silenceN = 0;
    this._isRecording = false;
    this._waitN = 0;
  },

  // Android 原生桥接推送的音频帧（由 Java evaluateJavascript 调用）
  _onNativeChunk(b64) {
    if (!this.enabled || this._processing) return;
    // 解码 base64 → Int16 → Float32
    const binary = atob(b64);
    const len = binary.length / 2;
    const float32 = new Float32Array(len);
    for (let i = 0; i < len; i++) {
      const lo = binary.charCodeAt(i * 2);
      const hi = binary.charCodeAt(i * 2 + 1);
      const int16 = (hi << 8) | lo;
      float32[i] = int16 >= 32768 ? (int16 - 65536) / 32768 : int16 / 32768;
    }
    this._onAudioFrame(float32);
  },

  // 统一音频处理入口（getUserMedia 和原生桥接共用）
  _onAudioFrame(input) {
    if (!this.enabled || this._processing) return;
    const energy = input.reduce((s, v) => s + Math.abs(v), 0) / input.length;

    // 校准阶段：前 20 帧（约 0.85 秒）采集噪音基线
    if (!this._calibrated) {
      this._calibFrames.push(energy);
      if (this._calibFrames.length >= 20) {
        const avg = this._calibFrames.reduce((a, b) => a + b, 0) / this._calibFrames.length;
        this._noiseFloor = Math.max(avg * 2.5, 0.003);  // 噪音的 2.5 倍作为阈值，最低 0.003
        this._calibrated = true;
        console.log(`[RemoteVoice] Calibrated: noiseFloor=${this._noiseFloor.toFixed(5)}`);
        updateVoiceUI({ enabled: true, status: 'waiting', wake_word: this.wakeWord });
      }
      return;
    }

    // AI 在说话时跳过
    if (this.aiSpeaking) { this._resetVAD(); return; }

    const isSpeech = energy > this._noiseFloor;

    // 每帧约 2048/48000 = 42.7ms
    // silenceLimit: 唤醒 ~0.85s(20帧), 通话 ~1.5s(35帧)
    // waitLimit: 唤醒 ~15s(350帧), 通话 ~60s(1400帧)
    const silenceLimit = this.inCall ? 35 : 20;
    const waitLimit = this.inCall ? 1400 : 350;

    if (!this._isRecording) {
      if (isSpeech) {
        this._speechN++;
        this._frames.push(new Float32Array(input));
        if (this._speechN >= 8) {  // ~340ms 确认是语音
          this._isRecording = true;
          this._silenceN = 0;
        }
      } else {
        this._speechN = 0;
        this._frames = [];
        this._waitN++;
        if (this._waitN > waitLimit) {
          if (this.inCall) {
            // 通话超时
            this.inCall = false;
            updateVoiceUI({ enabled: true, status: 'hangup', message: '通话超时结束' });
            setTimeout(() => {
              if (this.enabled) updateVoiceUI({ enabled: true, status: 'waiting', wake_word: this.wakeWord });
            }, 2000);
          }
          this._resetVAD();
        }
      }
    } else {
      this._frames.push(new Float32Array(input));
      if (!isSpeech) {
        this._silenceN++;
        if (this._silenceN > silenceLimit) {
          this._processAudio();
        }
      } else {
        this._silenceN = 0;
      }
      // 最长 30 秒（帧大小: getUserMedia=2048, 原生=640）
      const frameSize = this._useNative ? 640 : 2048;
      if (this._frames.length > Math.ceil(30 * this._sampleRate / frameSize)) {
        this._processAudio();
      }
    }
  },

  async _processAudio() {
    if (this._processing) return;
    this._processing = true;

    const frames = this._frames;
    this._resetVAD();

    // 合并帧
    const total = frames.reduce((s, f) => s + f.length, 0);
    const audio = new Float32Array(total);
    let offset = 0;
    for (const f of frames) { audio.set(f, offset); offset += f.length; }

    const duration = total / this._sampleRate;
    // 计算最大振幅（调试用）
    let maxAmp = 0;
    for (let i = 0; i < audio.length; i++) maxAmp = Math.max(maxAmp, Math.abs(audio[i]));
    console.log(`[RemoteVoice] Recorded ${duration.toFixed(1)}s, ${total} samples, maxAmp=${maxAmp.toFixed(4)}, sr=${this._sampleRate}, native=${this._useNative}`);
    if (duration < 0.3) { this._processing = false; return; }

    // 转 WAV
    const wav = this._encodeWAV(audio);
    console.log(`[RemoteVoice] WAV size: ${wav.byteLength} bytes`);

    updateVoiceUI({ enabled: true, status: 'recognizing', message: '识别中...' });

    try {
      const form = new FormData();
      form.append('file', new Blob([wav], { type: 'audio/wav' }), 'audio.wav');
      const resp = await fetch('/api/voice/remote-asr', { method: 'POST', body: form });
      console.log(`[RemoteVoice] ASR response status: ${resp.status}`);
      const data = await resp.json();
      const text = (data.text || '').trim();
      console.log(`[RemoteVoice] ASR result: text="${text}", error=${data.error || 'none'}, inCall=${this.inCall}`);

      if (!text) {
        console.warn(`[RemoteVoice] ASR returned empty text. duration=${duration.toFixed(1)}s, maxAmp=${maxAmp.toFixed(4)}`);
        this._processing = false;
        this._resumeListening();
        return;
      }

      if (!this.inCall) {
        // 待命模式 — 检查唤醒词
        if (text.includes(this.wakeWord)) {
          console.log('[RemoteVoice] Wakeup!');
          this.inCall = true;
          this.aiSpeaking = true;
          updateVoiceUI({ enabled: true, status: 'wakeup', message: '唤醒成功！' });
        } else {
          updateVoiceUI({ enabled: true, status: 'waiting', wake_word: this.wakeWord });
        }
      } else {
        // 通话模式 — 检查挂断
        const hangupWords = ['再见', '拜拜', '挂断', '结束通话', '挂了'];
        if (hangupWords.some(kw => text.includes(kw))) {
          this.inCall = false;
          this.aiSpeaking = false;
          updateVoiceUI({ enabled: true, status: 'hangup', message: '通话结束' });
          await this._sendToChat(text);
          setTimeout(() => {
            if (this.enabled) updateVoiceUI({ enabled: true, status: 'waiting', wake_word: this.wakeWord });
          }, 2000);
        } else {
          // 发送到聊天
          console.log(`[RemoteVoice] Sending to chat: "${text}", currentConvId=${currentConvId}, sending=${sending}`);
          this.aiSpeaking = true;
          updateVoiceUI({ enabled: true, status: 'ai_thinking', message: 'AI 思考中...' });
          await this._sendToChat(text);
        }
      }
    } catch(e) {
      console.error('[RemoteVoice] ASR error:', e);
      // 出错时在状态栏显示错误信息（方便手机端看）
      updateVoiceUI({ enabled: true, status: 'listening_cmd', message: '⚠ ASR出错: ' + (e.message || e) });
    }

    this._processing = false;
  },

  _resumeListening() {
    if (this.inCall) {
      updateVoiceUI({ enabled: true, status: 'listening_cmd', message: '聆听中...' });
    } else {
      updateVoiceUI({ enabled: true, status: 'waiting', wake_word: this.wakeWord });
    }
  },

  async _sendToChat(text) {
    console.log(`[RemoteVoice] _sendToChat: text="${text}", currentConvId=${currentConvId}, sending=${sending}`);

    // 没有当前对话时自动创建一个
    if (!currentConvId) {
      try {
        const conv = await api("POST", "/api/conversations");
        conversations.unshift(conv);
        await selectConv(conv.id);
        console.log(`[RemoteVoice] Auto-created conversation: ${conv.id}`);
      } catch(e) {
        console.error('[RemoteVoice] Failed to create conversation:', e);
        this.aiSpeaking = false;
        this._resumeListening();
        return;
      }
    }

    // 如果上一条还在发送中，等最多 5 秒
    if (sending) {
      let waited = 0;
      while (sending && waited < 5000) {
        await new Promise(r => setTimeout(r, 200));
        waited += 200;
      }
      if (sending) {
        console.warn('[RemoteVoice] Still sending after 5s, skip');
        this.aiSpeaking = false;
        this._resumeListening();
        return;
      }
    }

    $('input').value = text;
    send();
  },

  _encodeWAV(samples) {
    const sr = this._sampleRate;
    const buf = new ArrayBuffer(44 + samples.length * 2);
    const v = new DataView(buf);
    const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    w(0, 'RIFF');
    v.setUint32(4, 36 + samples.length * 2, true);
    w(8, 'WAVE');
    w(12, 'fmt ');
    v.setUint32(16, 16, true);
    v.setUint16(20, 1, true);       // PCM
    v.setUint16(22, 1, true);       // mono
    v.setUint32(24, sr, true);
    v.setUint32(28, sr * 2, true);
    v.setUint16(32, 2, true);
    v.setUint16(34, 16, true);
    w(36, 'data');
    v.setUint32(40, samples.length * 2, true);
    for (let i = 0; i < samples.length; i++) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
    }
    return buf;
  }
};

// 初始化语音设置
(function initVoice() {
  const ww = localStorage.getItem('obsidian_voice_wakeword') || '老公';
  $('voiceWakeWord').value = ww;
  // 自动检测：App 内或移动端默认用手机麦克风
  const ua = navigator.userAgent;
  const isApp = ua.includes('ObsidianVowApp');
  const isMobile = /Android|iPhone|iPad/i.test(ua);
  const savedSrc = localStorage.getItem('obsidian_voice_mic_source');
  if (savedSrc) {
    voiceMicSource = savedSrc;
  } else if (isApp || isMobile) {
    voiceMicSource = 'remote';
  }
  $('voiceMicSource').value = voiceMicSource;
})();

ChatApp.registerModule("voice", {
  onMicSourceChange,
  toggle: toggleVoice,
  updateWakeWord: updateVoiceWakeWord,
  hangup: voiceHangup,
  notifyAiSpeaking: notifyVoiceAiSpeaking,
  notifyCamCheckStart: notifyVoiceCamCheckStart,
  updateUI: updateVoiceUI,
  isRemote: isRemoteVoice,
  isEnabled: () => voiceEnabled,
  isInCall: () => voiceInCall,
  getMicSource: () => voiceMicSource,
});
