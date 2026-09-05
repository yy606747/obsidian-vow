// ── Debug / 系统日志懒加载门面 ──
(function (global) {
  const DEBUG_LOG_SCRIPT = "/static/js/chat/debug_log.js?v=20260905-brand";
  let debugLogLoadPromise = null;

  function getDebugLogModule() {
    return global.ChatApp?.getModule?.("debugLog") || null;
  }

  function ensureDebugLogLoaded() {
    const loaded = getDebugLogModule();
    if (loaded) return Promise.resolve(loaded);
    if (debugLogLoadPromise) return debugLogLoadPromise;

    global.ChatPerf?.markOnce("debug_log_lazy_load_start");
    debugLogLoadPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = DEBUG_LOG_SCRIPT;
      script.async = true;
      script.onload = () => {
        const api = getDebugLogModule();
        if (!api) {
          debugLogLoadPromise = null;
          reject(new Error("Debug log module did not register"));
          return;
        }
        global.ChatPerf?.markOnce("debug_log_lazy_load_done");
        resolve(api);
      };
      script.onerror = () => {
        debugLogLoadPromise = null;
        global.ChatPerf?.markOnce("debug_log_lazy_load_error");
        reject(new Error("Failed to load debug log module"));
      };
      document.head.appendChild(script);
    });
    return debugLogLoadPromise;
  }

  function stampSystemLog(d) {
    const now = new Date();
    const ts = String(now.getHours()).padStart(2, "0") + ":" +
      String(now.getMinutes()).padStart(2, "0") + ":" +
      String(now.getSeconds()).padStart(2, "0");
    return {
      ...d,
      _ts: ts,
      _id: "slog_" + Date.now() + "_" + Math.random().toString(36).slice(2, 6),
    };
  }

  function flashSystemLogButton() {
    global.sysLogHasUnreadError = true;
    const btn = $("sysLogBtn");
    if (btn && !btn.classList.contains("syslog-btn-flash")) {
      btn.classList.add("syslog-btn-flash");
    }
  }

  function renderSystemLogListStub() {
    const el = $("sysLogList");
    const countEl = $("sysLogCount");
    if (countEl) countEl.textContent = `共 ${systemLogs.length} 条（刷新后清空）`;
    if (!el) return;
    el.innerHTML = systemLogs.length
      ? '<div class="syslog-empty">正在加载日志详情...</div>'
      : '<div class="syslog-empty">暂无日志</div>';
  }

  function addSystemLog(d) {
    if (d.msg_id && systemLogs.some(log => log.msg_id === d.msg_id)) return;
    systemLogs.unshift(stampSystemLog(d));
    if (d.has_error) flashSystemLogButton();
    if (getDebugLogModule()) {
      renderSystemLogList();
    } else if ($("sysLogModal")?.classList.contains("show")) {
      renderSystemLogListStub();
    }
  }

  function addErrorToSystemLog(errorMsg, model) {
    addSystemLog({
      type: "debug",
      model: model || "?",
      msg_id: null,
      has_error: true,
      error_text: errorMsg,
      usage: null,
      recalled_memories: null,
      prompt_messages: null,
    });
  }

  function openSystemLog() {
    global.sysLogHasUnreadError = false;
    $("sysLogBtn")?.classList.remove("syslog-btn-flash");
    renderSystemLogListStub();
    $("sysLogModal")?.classList.add("show");
    return ensureDebugLogLoaded()
      .then(() => {
        renderSystemLogList();
        $("sysLogModal")?.classList.add("show");
      })
      .catch(err => {
        console.error("[DebugLog] lazy load failed:", err);
        showToast?.("系统日志加载失败", 2400);
      });
  }

  function closeSystemLog() {
    $("sysLogModal")?.classList.remove("show");
  }

  function clearSystemLog() {
    systemLogs = [];
    global.sysLogHasUnreadError = false;
    $("sysLogBtn")?.classList.remove("syslog-btn-flash");
    if (getDebugLogModule()) {
      renderSystemLogList();
    } else {
      renderSystemLogListStub();
    }
  }

  function toggleSysLogDetail(id) {
    ensureDebugLogLoaded()
      .then(api => api.toggleDetail?.(id))
      .catch(err => {
        console.error("[DebugLog] lazy load failed:", err);
        showToast?.("系统日志详情加载失败", 2400);
      });
  }

  global.sysLogHasUnreadError = !!global.sysLogHasUnreadError;
  global.ensureDebugLogLoaded = ensureDebugLogLoaded;
  global.addSystemLog = addSystemLog;
  global.addErrorToSystemLog = addErrorToSystemLog;
  global.renderSystemLogList = renderSystemLogListStub;
  global.openSystemLog = openSystemLog;
  global.closeSystemLog = closeSystemLog;
  global.clearSystemLog = clearSystemLog;
  global.toggleSysLogDetail = toggleSysLogDetail;

  global.ChatApp?.registerModule?.("debugLogLoader", {
    ensureLoaded: ensureDebugLogLoaded,
    isLoaded: () => !!getDebugLogModule(),
    open: openSystemLog,
    addSystemLog,
    addErrorToSystemLog,
  });
})(window);
