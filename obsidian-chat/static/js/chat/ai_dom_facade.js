// ── AI Dom 懒加载门面 ──
(function (global) {
  const AI_DOM_SCRIPT = "/static/js/chat/ai_dom.js?v=20260905-brand";
  let aiDomLoadPromise = null;

  function getAiDomModule() {
    return global.ChatApp?.getModule?.("aiDom") || null;
  }

  function ensureAiDomShell() {
    if (document.getElementById("aiDomModal")) return;
    const modal = document.createElement("div");
    modal.className = "modal-overlay";
    modal.id = "aiDomModal";
    modal.innerHTML = `
      <div class="modal ai-dom-modal">
        <h3>🔒 亲密控制</h3>
        <div class="ai-dom-note">
          AI 将根据对话自主掌控玩具节奏。进入后，用户界面不再出现停止/强度/预设按钮；
          对话期间不可切换会话或跳转子页面。<br>
          安全词在任意消息中出现即立刻熔断并进入安抚态。
        </div>
        <div class="dom-row"><label>安全词</label>
          <input class="ai-dom-input" type="text" id="aiDomSafewordInput" placeholder="必填，例如：红灯">
        </div>
        <div class="dom-row ai-dom-row"><label>CNC</label>
          <div class="ai-dom-cnc-options">
            <label class="ai-dom-cnc-label"><input type="checkbox" id="aiDomCncToggle"> <span>允许无视意愿</span></label>
            <span class="ai-dom-muted">不会提示触发</span>
          </div>
        </div>
        <div class="dom-row ai-dom-row ai-dom-row-top">
          <label>软肋</label>
          <textarea class="ai-dom-textarea" id="aiDomWeaknessInput" rows="3" placeholder="每行一个，会在 AI 无视意愿时被挑着用（可空）&#10;例如：&#10;被夸听话&#10;被追问具体感受"></textarea>
        </div>
        <div class="dom-row ai-dom-row ai-dom-row-top"><label>设备</label>
          <div class="ai-dom-device-grid">
            <label class="dom-dev"><input type="radio" name="aiDomDev" value="sosexy" checked> <span>SOSEXY</span></label>
            <label class="dom-dev"><input type="radio" name="aiDomDev" value="cx492b"> <span>CX492B</span></label>
            <label class="dom-dev"><input type="radio" name="aiDomDev" value="sk30"> <span>失控3.0</span></label>
            <label class="dom-dev"><input type="radio" name="aiDomDev" value="sk40"> <span>失控4.0</span></label>
          </div>
        </div>
        <div class="dom-row ai-dom-row">
          <label>连接</label>
          <div class="ai-dom-conn-row">
            <div class="whisper-dot off" id="aiDomDot"></div>
            <span class="ai-dom-conn-label" id="aiDomConnLabel">未连接</span>
            <button class="ai-dom-conn-btn" id="aiDomConnBtn" data-ai-dom-action="connect">连接</button>
          </div>
        </div>
        <div class="whisper-log ai-dom-log">
          <div class="whisper-log-area ai-dom-log-area" id="aiDomLogArea"></div>
        </div>
        <div class="btn-row ai-dom-actions">
          <button class="btn-cancel" data-ai-dom-action="close">关闭</button>
          <button class="btn-save" id="aiDomEnterBtn" data-ai-dom-action="enter">进入主控</button>
        </div>
      </div>`;
    document.body.appendChild(modal);
    bindAiDomShellEvents(modal);

    const pill = document.createElement("div");
    pill.id = "aiDomPill";
    pill.innerHTML = '<span>🔒 主控中 · <span id="aiDomPillDev"></span> · 安全词 <b id="aiDomPillSafe"></b></span><button data-ai-dom-action="exit">退出</button>';
    document.body.appendChild(pill);
    bindAiDomShellEvents(pill);
  }

  function bindAiDomShellEvents(root) {
    root.addEventListener("click", event => {
      const action = event.target.closest("[data-ai-dom-action]");
      if (!action || !root.contains(action)) return;
      event.preventDefault();
      switch (action.dataset.aiDomAction) {
        case "connect":
          global.aiDomToggleConnect();
          break;
        case "close":
          global.closeAiDom();
          break;
        case "enter":
          global.aiDomEnter();
          break;
        case "exit":
          global.aiDomExit();
          break;
      }
    });

    root.querySelector("#aiDomSafewordInput")?.addEventListener("input", () => global.onAiDomSafewordInput());
    root.querySelectorAll('input[name="aiDomDev"]').forEach(input => {
      input.addEventListener("change", () => global.onAiDomDeviceChange());
    });
  }

  function ensureAiDomLoaded() {
    const loaded = getAiDomModule();
    if (loaded) return Promise.resolve(loaded);
    if (aiDomLoadPromise) return aiDomLoadPromise;

    global.ChatPerf?.markOnce("ai_dom_lazy_load_start");
    aiDomLoadPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = AI_DOM_SCRIPT;
      script.async = true;
      script.onload = () => {
        const api = getAiDomModule();
        if (!api) {
          aiDomLoadPromise = null;
          reject(new Error("AI Dom module did not register"));
          return;
        }
        global.ChatPerf?.markOnce("ai_dom_lazy_load_done");
        resolve(api);
      };
      script.onerror = () => {
        aiDomLoadPromise = null;
        global.ChatPerf?.markOnce("ai_dom_lazy_load_error");
        reject(new Error("Failed to load AI Dom module"));
      };
      document.head.appendChild(script);
    });
    return aiDomLoadPromise;
  }

  function setDefault(name, value) {
    if (!(name in global)) global[name] = value;
  }

  setDefault("aiDomMode", false);
  setDefault("aiDomSafeword", "");
  setDefault("aiDomCncEnabled", false);
  setDefault("aiDomCncWeakness", []);
  setDefault("aiDomSessionStartAt", 0);
  setDefault("aiDomLastPunishAt", 0);
  setDefault("aiDomComplianceStreak", 0);
  setDefault("aiDomShortStreak", 0);
  setDefault("aiDomLastResistHits", 0);
  setDefault("aiDomLastReplyDelayMs", 0);
  setDefault("aiDomSendClickedAt", 0);
  setDefault("aiDomLastAiDoneAt", 0);
  setDefault("aiDomRatchetValley", 0);
  setDefault("aiDomDebt", 0);
  setDefault("aiDomStubbornStreak", 0);
  setDefault("aiDomScene", { name: null, startAt: 0, last: { v: 0, s: 0 } });
  setDefault("toyDriver", {
    active: null,
    isConnected() { return false; },
    async connect() { await ensureAiDomLoaded(); },
    async disconnect() {},
    async play() {},
    async stop() {},
    _isAdv() { return false; },
  });
  setDefault("aiDomGuard", () => false);
  setDefault("aiDomHistorySnapshot", () => []);
  setDefault("aiDomBuildSendContext", () => ({}));
  setDefault("aiDomBuildControlSnapshot", () => ({}));
  setDefault("aiDomCheckSafeword", () => false);
  setDefault("aiDomPanic", () => {});
  setDefault("aiDomInstantTap", () => {});
  setDefault("aiDomInitiativeReset", () => {});
  setDefault("aiDomSceneDispatch", () => {});
  setDefault("aiDomRefreshUI", () => {});
  setDefault("aiDomLog", () => {});

  async function runAiDom(method, args) {
    try {
      const api = await ensureAiDomLoaded();
      if (api && typeof api[method] === "function") return api[method](...(args || []));
    } catch (err) {
      console.error("[AI Dom] lazy load failed:", err);
      showToast?.("亲密控制加载失败", 2400);
    }
    return undefined;
  }

  global.openAiDom = () => {
    ensureAiDomShell();
    return runAiDom("open");
  };
  global.closeAiDom = () => {
    const api = getAiDomModule();
    if (api?.close) return api.close();
    document.getElementById("aiDomModal")?.classList.remove("show");
    return undefined;
  };
  global.onAiDomSafewordInput = () => runAiDom("onSafewordInput");
  global.onAiDomDeviceChange = () => runAiDom("onDeviceChange");
  global.aiDomToggleConnect = () => runAiDom("toggleConnect");
  global.aiDomEnter = () => runAiDom("enter");
  global.aiDomExit = (...args) => runAiDom("exit", args);

  ChatApp.registerModule("aiDomLoader", {
    ensureLoaded: ensureAiDomLoaded,
    ensureShell: ensureAiDomShell,
    isLoaded: () => !!getAiDomModule(),
    open: global.openAiDom,
  });
})(window);
