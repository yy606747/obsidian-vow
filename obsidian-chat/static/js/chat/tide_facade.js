(function (global) {
  if (!("tideMode" in global)) global.tideMode = false;

  function ensureTideShell() {
    if (document.getElementById("tideModal")) return;
    const modal = document.createElement("div");
    modal.className = "modal-overlay";
    modal.id = "tideModal";
    modal.innerHTML = `
      <div class="modal whisper-modal">
        <h3>🌊 潮汐控制</h3>
        <div class="whisper-conn-bar">
          <div class="whisper-conn-left">
            <div class="whisper-dot off" id="tideDot"></div>
            <span class="whisper-conn-label" id="tideConnLabel">未检测</span>
          </div>
          <button class="whisper-btn-conn" data-tide-action="refresh">检测</button>
        </div>
        <div class="whisper-toggle-row">
          <span class="whisper-toggle-label">🌊 潮汐模式（开启后 AI 可控制玩具）</span>
          <label class="toggle-switch">
            <input type="checkbox" id="tideModeToggle">
            <span class="toggle-slider"></span>
          </label>
        </div>
        <div class="whisper-stop-row">
          <button class="whisper-btn-stop" data-tide-action="stop">停止</button>
        </div>
        <div class="btn-row whisper-actions">
          <button class="btn-cancel" data-tide-action="close">关闭</button>
        </div>
      </div>`;
    document.body.appendChild(modal);
    bind(modal);

    const pill = document.createElement("div");
    pill.id = "tidePill";
    pill.innerHTML = '<span>🌊 潮汐中 · <span id="tidePillDev">Muse</span></span><button data-tide-action="stop">退出</button>';
    document.body.appendChild(pill);
    bind(pill);
  }

  function bind(root) {
    root.addEventListener("click", event => {
      const action = event.target.closest("[data-tide-action]");
      if (!action || !root.contains(action)) return;
      event.preventDefault();
      if (action.dataset.tideAction === "close") close();
      if (action.dataset.tideAction === "refresh") refreshUi();
      if (action.dataset.tideAction === "stop") stop();
    });
    root.querySelector("#tideModeToggle")?.addEventListener("change", event => {
      if (event.target.checked) start();
      else stop();
    });
  }

  function refreshUi() {
    ensureTideShell();
    const current = global.ControlRuntime?.currentSession?.();
    global.tideMode = !!(current && current.kind === "tide" && current.status !== "ended");
    const supported = !!global.ObsidianMuse?.isSupported?.();
    const dot = document.getElementById("tideDot");
    const label = document.getElementById("tideConnLabel");
    if (dot) dot.classList.toggle("off", !supported);
    if (dot) dot.classList.toggle("on", supported);
    if (label) label.textContent = supported ? "Muse 已就绪" : "需要 Android 广播桥";
    const toggle = document.getElementById("tideModeToggle");
    if (toggle) toggle.checked = !!global.tideMode;
  }

  async function refreshFromBackend() {
    await global.ControlRuntime?.recover?.().catch(() => null);
    refreshUi();
  }

  async function start() {
    ensureTideShell();
    if (!currentConvId) {
      showToast?.("先打开一个对话", 1800);
      refreshUi();
      return null;
    }
    if (!global.ObsidianMuse?.isSupported?.()) {
      showToast?.("需要 Obsidian Vow Android App 的 Muse 广播桥", 2400);
      refreshUi();
      return null;
    }
    const session = await global.ControlRuntime?.start?.("tide", {
      convId: currentConvId,
      deviceId: "muse",
      controlResourceId: "toy:muse",
      snapshot: { tide_mode: true, toy_connected: true },
    });
    global.tideMode = !!(session && session.kind === "tide" && session.status !== "ended");
    refreshUi();
    return session;
  }

  async function stop() {
    global.tideMode = false;
    try { global.ObsidianMuse?.stop?.(); } catch (e) {}
    const current = global.ControlRuntime?.currentSession?.();
    if (current && current.kind === "tide") {
      await global.ControlRuntime.end("normal").catch(() => null);
    }
    document.getElementById("tidePill")?.classList.remove("show");
    refreshUi();
  }

  function open() {
    ensureTideShell();
    document.getElementById("tideModal")?.classList.add("show");
    refreshUi();
    refreshFromBackend();
  }

  function close() {
    document.getElementById("tideModal")?.classList.remove("show");
  }

  document.addEventListener("click", event => {
    const action = event.target.closest('[data-action="openTide"]');
    if (!action) return;
    event.preventDefault();
    open();
  });

  global.openTide = open;
  global.closeTide = close;
  global.tideStart = start;
  global.tideStop = stop;
  global.tideBuildControlSnapshot = () => ({ tide_mode: !!global.tideMode, toy_connected: !!global.ObsidianMuse?.isSupported?.() });

  global.ChatApp?.registerModule?.("tideFacade", { open, close, start, stop, refreshUi, refreshFromBackend });
})(window);
