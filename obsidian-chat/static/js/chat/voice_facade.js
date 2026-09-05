// ── 语音模块懒加载门面 ──
(function (global) {
  const VOICE_SCRIPT = "/static/js/chat/voice.js?v=20260905-brand";
  let voiceLoadPromise = null;
  let voiceControlsBound = false;

  function getVoiceModule() {
    return global.ChatApp?.getModule?.("voice") || null;
  }

  function ensureVoiceLoaded() {
    const loaded = getVoiceModule();
    if (loaded) return Promise.resolve(loaded);
    if (voiceLoadPromise) return voiceLoadPromise;

    global.ChatPerf?.markOnce("voice_lazy_load_start");
    voiceLoadPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = VOICE_SCRIPT;
      script.async = true;
      script.onload = () => {
        const api = getVoiceModule();
        if (!api) {
          voiceLoadPromise = null;
          reject(new Error("Voice module did not register"));
          return;
        }
        global.ChatPerf?.markOnce("voice_lazy_load_done");
        resolve(api);
      };
      script.onerror = () => {
        voiceLoadPromise = null;
        global.ChatPerf?.markOnce("voice_lazy_load_error");
        reject(new Error("Failed to load voice module"));
      };
      document.head.appendChild(script);
    });
    return voiceLoadPromise;
  }

  function initVoiceControlsStub() {
    const ww = localStorage.getItem("obsidian_voice_wakeword") || "老公";
    const wakeInput = $("voiceWakeWord");
    if (wakeInput) wakeInput.value = ww;

    const ua = navigator.userAgent;
    const isApp = ua.includes("ObsidianVowApp");
    const isMobile = /Android|iPhone|iPad/i.test(ua);
    const savedSrc = localStorage.getItem("obsidian_voice_mic_source");
    global.voiceMicSource = savedSrc || ((isApp || isMobile) ? "remote" : "local");
    const micSelect = $("voiceMicSource");
    if (micSelect) micSelect.value = global.voiceMicSource;
    bindVoiceControls();
  }

  function bindVoiceControls() {
    if (voiceControlsBound) return;
    voiceControlsBound = true;

    const bind = (id, type, handler) => {
      const el = $(id);
      if (el) el.addEventListener(type, handler);
    };

    bind("voiceToggle", "change", () => toggleVoice());
    bind("voiceMicSource", "change", () => onMicSourceChange());
    bind("voiceWakeWord", "change", () => updateVoiceWakeWord());
    const hangupBtn = $("voiceHangupBtn");
    if (hangupBtn) {
      hangupBtn.addEventListener("click", event => {
        event.preventDefault();
        voiceHangup();
      });
    }
  }

  async function runVoice(method, args) {
    try {
      const api = await ensureVoiceLoaded();
      if (api && typeof api[method] === "function") return api[method](...(args || []));
    } catch (err) {
      console.error("[Voice] lazy load failed:", err);
      showToast?.("语音模块加载失败", 2400);
    }
    return undefined;
  }

  function isRemoteVoice() {
    return global.voiceMicSource === "remote";
  }

  function onMicSourceChange() {
    const next = $("voiceMicSource")?.value || "local";
    global.voiceMicSource = next;
    localStorage.setItem("obsidian_voice_mic_source", next);
    return runVoice("onMicSourceChange");
  }

  function toggleVoice() {
    return runVoice("toggle");
  }

  function updateVoiceWakeWord() {
    const ww = $("voiceWakeWord")?.value.trim() || "老公";
    localStorage.setItem("obsidian_voice_wakeword", ww);
    return runVoice("updateWakeWord");
  }

  function voiceHangup() {
    return runVoice("hangup");
  }

  function notifyVoiceAiSpeaking(speaking) {
    const api = getVoiceModule();
    if (api?.notifyAiSpeaking) return api.notifyAiSpeaking(speaking);
    return fetch("/api/voice/ai-speaking", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ speaking }),
    }).catch(() => {});
  }

  function notifyVoiceCamCheckStart() {
    const api = getVoiceModule();
    if (api?.notifyCamCheckStart) return api.notifyCamCheckStart();
    return fetch("/api/voice/cam-check-start", { method: "POST" }).catch(() => {});
  }

  function applyVoiceOffUI() {
    global.voiceEnabled = false;
    global.voiceInCall = false;
    const ind = $("voiceIndicator");
    const btn = $("voiceHangupBtn");
    const status = $("voiceStatus");
    if (ind) ind.className = "voice-indicator";
    if (btn) btn.style.display = "none";
    if (status) status.textContent = "未开启";
  }

  function updateVoiceUI(data) {
    const api = getVoiceModule();
    if (api?.updateUI) return api.updateUI(data);
    if (!data?.enabled) {
      applyVoiceOffUI();
      return undefined;
    }
    global.voiceEnabled = true;
    return runVoice("updateUI", [data]);
  }

  global.voiceEnabled = !!global.voiceEnabled;
  global.voiceInCall = !!global.voiceInCall;
  if (!("voiceMicSource" in global)) global.voiceMicSource = "local";
  global.ensureVoiceLoaded = ensureVoiceLoaded;
  global.isRemoteVoice = isRemoteVoice;
  global.onMicSourceChange = onMicSourceChange;
  global.toggleVoice = toggleVoice;
  global.updateVoiceWakeWord = updateVoiceWakeWord;
  global.voiceHangup = voiceHangup;
  global.notifyVoiceAiSpeaking = notifyVoiceAiSpeaking;
  global.notifyVoiceCamCheckStart = notifyVoiceCamCheckStart;
  global.updateVoiceUI = updateVoiceUI;

  initVoiceControlsStub();

  global.ChatApp?.registerModule?.("voiceLoader", {
    ensureLoaded: ensureVoiceLoaded,
    bindControls: bindVoiceControls,
    isLoaded: () => !!getVoiceModule(),
    isRemote: isRemoteVoice,
    toggle: toggleVoice,
    notifyAiSpeaking: notifyVoiceAiSpeaking,
    notifyCamCheckStart: notifyVoiceCamCheckStart,
    updateUI: updateVoiceUI,
  });
})(window);
