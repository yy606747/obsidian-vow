// ── Whisper Toy 懒加载门面 ──
(function (global) {
  const WHISPER_TOY_SCRIPT = "/static/js/chat/whisper_toy.js?v=20260531-control-trace4";
  let whisperToyLoadPromise = null;

  function getWhisperToyModule() {
    return global.ChatApp?.getModule?.("whisperToy") || null;
  }

  function ensureWhisperToyShell() {
    if ($("whisperModal")) return;
    const modal = document.createElement("div");
    modal.className = "modal-overlay";
    modal.id = "whisperModal";
    modal.innerHTML = `
      <div class="modal whisper-modal">
        <h3>💗 密语时刻</h3>
        <div class="whisper-conn-bar">
          <div class="whisper-conn-left">
            <div class="whisper-dot off" id="toyDot"></div>
            <span class="whisper-conn-label" id="toyConnLabel">未连接</span>
          </div>
          <button class="whisper-btn-conn" id="toyConnBtn" data-whisper-action="connect">连接</button>
        </div>
        <div class="whisper-toggle-row">
          <span class="whisper-toggle-label">🔮 密语模式（开启后AI可控制玩具）</span>
          <label class="toggle-switch">
            <input type="checkbox" id="whisperModeToggle">
            <span class="toggle-slider"></span>
          </label>
        </div>
        <div class="whisper-toggle-row">
          <span class="whisper-toggle-label">🎯 AI 随机突袭（户外模式）</span>
          <label class="toggle-switch">
            <input type="checkbox" id="whisperInitToggle">
            <span class="toggle-slider"></span>
          </label>
        </div>
        <div class="whisper-preset-grid" id="toyPresetGrid"></div>
        <div class="whisper-stop-row">
          <button class="whisper-btn-stop" data-whisper-action="stop">⏹ 停止</button>
        </div>
        <div class="whisper-log">
          <div class="whisper-log-area" id="toyLogArea"></div>
        </div>
        <div class="btn-row whisper-actions">
          <button class="btn-cancel" data-whisper-action="close">关闭</button>
        </div>
      </div>`;
    document.body.appendChild(modal);
    bindWhisperShellEvents(modal);

    const editor = document.createElement("div");
    editor.className = "toy-overlay";
    editor.id = "toyEditorOverlay";
    editor.addEventListener("click", event => { if (event.target === editor) global.toyCloseEditor(); });
    editor.innerHTML = `
      <div class="toy-edit-sheet">
        <div class="toy-sheet-handle"></div>
        <div id="toyEditContent"></div>
      </div>`;
    document.body.appendChild(editor);
  }

  function bindWhisperShellEvents(modal) {
    const actionHandlers = {
      connect: () => global.toyToggleConnect(),
      stop: () => global.toyStopAll(),
      close: () => global.closeWhisper(),
    };

    modal.addEventListener("click", event => {
      const action = event.target.closest("[data-whisper-action]");
      if (!action || !modal.contains(action)) return;
      event.preventDefault();
      actionHandlers[action.dataset.whisperAction]?.();
    });

    $("whisperModeToggle")?.addEventListener("change", () => global.onWhisperModeChange());
    $("whisperInitToggle")?.addEventListener("change", () => global.onWhisperInitChange());
  }

  function ensureWhisperToyLoaded() {
    const loaded = getWhisperToyModule();
    if (loaded) return Promise.resolve(loaded);
    if (whisperToyLoadPromise) return whisperToyLoadPromise;

    global.ChatPerf?.markOnce("whisper_toy_lazy_load_start");
    whisperToyLoadPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = WHISPER_TOY_SCRIPT;
      script.async = true;
      script.onload = () => {
        const api = getWhisperToyModule();
        if (!api) {
          whisperToyLoadPromise = null;
          reject(new Error("Whisper toy module did not register"));
          return;
        }
        global.ChatPerf?.markOnce("whisper_toy_lazy_load_done");
        resolve(api);
      };
      script.onerror = () => {
        whisperToyLoadPromise = null;
        global.ChatPerf?.markOnce("whisper_toy_lazy_load_error");
        reject(new Error("Failed to load whisper toy module"));
      };
      document.head.appendChild(script);
    });
    return whisperToyLoadPromise;
  }

  async function runWhisperToy(method, args) {
    try {
      const api = await ensureWhisperToyLoaded();
      if (api && typeof api[method] === "function") return api[method](...(args || []));
    } catch (err) {
      console.error("[WhisperToy] lazy load failed:", err);
      showToast?.("密语时刻加载失败", 2400);
    }
    return undefined;
  }

  if (!("whisperMode" in global)) global.whisperMode = false;
  global.openWhisper = () => {
    ensureWhisperToyShell();
    return runWhisperToy("open");
  };
  global.closeWhisper = () => {
    const api = getWhisperToyModule();
    if (api?.close) return api.close();
    $("whisperModal")?.classList.remove("show");
    return undefined;
  };
  global.toyToggleConnect = () => runWhisperToy("toggleConnect");
  global.onWhisperModeChange = () => runWhisperToy("onModeChange");
  global.onWhisperInitChange = () => runWhisperToy("onInitChange");
  global.toyStopAll = () => {
    const api = getWhisperToyModule();
    if (api?.stopAll) return api.stopAll();
    if (toyConnected) return toySendData2(toyBuildStopCmd());
    return undefined;
  };
  global.toyExecCmd = (cmd) => {
    if (typeof aiDomMode !== "undefined" && aiDomMode && typeof aiDomSceneDispatch === "function") {
      return aiDomSceneDispatch(cmd);
    }
    return runWhisperToy("execCommand", [cmd]);
  };
  global.whisperAutoRetreatCancel = () => {
    const api = getWhisperToyModule();
    if (api?.cancelAutoRetreat) return api.cancelAutoRetreat();
    return undefined;
  };
  global.whisperInitStart = () => runWhisperToy("startInitiative");
  global.whisperInitStop = () => {
    const api = getWhisperToyModule();
    if (api?.stopInitiative) return api.stopInitiative();
    return undefined;
  };
  global.whisperBuildControlSnapshot = () => ({ whisper_mode: !!global.whisperMode });
  global.toyCloseEditor = () => $("toyEditorOverlay")?.classList.remove("show");

  ChatApp.registerModule("whisperToyLoader", {
    ensureLoaded: ensureWhisperToyLoaded,
    ensureShell: ensureWhisperToyShell,
    isLoaded: () => !!getWhisperToyModule(),
    open: global.openWhisper,
    execCommand: global.toyExecCmd,
  });

  const params = new URLSearchParams(location.search);
  if (params.get("whisper") === "1") {
    setTimeout(() => global.openWhisper(), 500);
    history.replaceState(null, "", "/chat");
  }
})(window);
