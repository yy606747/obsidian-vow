(function (global) {
  function _commands(payload) {
    if (!payload) return [];
    if (Array.isArray(payload.commands)) return payload.commands;
    if (typeof payload.commands === "string") return [payload.commands];
    if (typeof payload.command === "string") return [payload.command];
    return [];
  }

  function _isAiDomMode() {
    try { if (typeof aiDomMode !== "undefined") return !!aiDomMode; } catch (e) {}
    return !!global.aiDomMode;
  }

  function _isConnected() {
    try {
      if (typeof toyBridgeIsConnected === "function" && toyBridgeIsConnected()) return true;
    } catch (e) {}
    if (_isAiDomMode()) {
      try {
        if (typeof toyDriver !== "undefined" && toyDriver?.isConnected) return !!toyDriver.isConnected();
      } catch (e) {}
      return !!global.toyDriver?.isConnected?.();
    }
    try { if (typeof toyConnected !== "undefined") return !!toyConnected; } catch (e) {}
    try { if (typeof cxConnected !== "undefined" && cxConnected) return true; } catch (e) {}
    try { if (typeof skConnected !== "undefined" && skConnected) return true; } catch (e) {}
    try { if (global.ObsidianBle?.isConnected?.()) return true; } catch (e) {}
    return !!global.toyConnected;
  }

  function _executor() {
    try { if (typeof toyExecCmd === "function") return toyExecCmd; } catch (e) {}
    return typeof global.toyExecCmd === "function" ? global.toyExecCmd : null;
  }

  function _isStopCommand(command) {
    let c = String(command || "").trim().toUpperCase();
    c = c.replace(/^\[?TOY:/, "").replace(/\]?$/, "").trim();
    return c === "STOP" || c === "0" || c === "OFF" || c === "HALT" || c === "EMERGENCY_STOP";
  }

  function _modelKey() {
    try { return document.getElementById("modelSelect")?.value || "?"; } catch (e) {}
    return "?";
  }

  function reportRejected(payload) {
    payload = payload || {};
    const commands = _commands(payload);
    const reason = payload.reason || payload.message || payload.status || "unknown";
    const label = commands.length ? commands.join(", ") : "unknown";
    const session = payload.control_session_id ? ` · session ${payload.control_session_id}` : "";
    const message = `玩具指令未执行: ${label} · ${reason}${session}`;
    try {
      if (typeof addErrorToSystemLog === "function") {
        addErrorToSystemLog(message, _modelKey());
      } else {
        console.warn("[ControlToyRouter] rejected", payload);
      }
    } catch (e) {
      console.warn("[ControlToyRouter] rejected", payload);
    }
    return false;
  }

  function _forceStop(reason) {
    try {
      if (global.ControlEmergencyStop?.stopDevice) {
        return !!global.ControlEmergencyStop.stopDevice(reason || "toy_stop_command");
      }
    } catch (e) {
      console.warn("[ControlToyRouter] force stop failed", e);
    }
    return false;
  }

  function decision(payload) {
    if (global.ControlRuntime?.toyCommandDecision) {
      return ControlRuntime.toyCommandDecision(payload || {});
    }
    if (global.ControlRuntime?.shouldExecuteToyCommand) {
      return {
        ok: !!ControlRuntime.shouldExecuteToyCommand(payload || {}),
        reason: "legacy_runtime_decision",
      };
    }
    return { ok: false, reason: "control_runtime_missing" };
  }

  function shouldExecute(payload) {
    return !!decision(payload).ok;
  }

  function execute(payload, options) {
    const commands = _commands(payload);
    if (!commands.length) return false;

    const hasStopCommand = commands.some(_isStopCommand);
    const runtimeDecision = decision(payload);
    const runtimeAllowed = !!runtimeDecision.ok;
    if (!runtimeAllowed && !hasStopCommand) {
      return reportRejected({ ...(payload || {}), reason: runtimeDecision.reason || "frontend_runtime_rejected" });
    }

    const connected = typeof options?.connected === "function" ? options.connected : _isConnected;
    const isConnected = !!connected();
    if (!hasStopCommand && !isConnected) {
      return reportRejected({ ...(payload || {}), reason: "frontend_device_disconnected" });
    }

    const exec = typeof options?.exec === "function" ? options.exec : _executor();
    if (!exec && !hasStopCommand) {
      return reportRejected({ ...(payload || {}), reason: "frontend_executor_missing" });
    }

    let attempted = false;
    commands.forEach(command => {
      const stopCommand = _isStopCommand(command);
      if (!runtimeAllowed && !stopCommand) return;
      if (!stopCommand && !isConnected) return;
      try {
        if (typeof exec === "function") {
          const result = exec(command);
          if (result && typeof result.catch === "function") {
            result
              .then(ok => {
                if (ok === false) {
                  reportRejected({ ...(payload || {}), commands: [command], reason: "frontend_command_failed" });
                }
              })
              .catch(e => reportRejected({ ...(payload || {}), commands: [command], reason: e?.message || "frontend_command_failed" }));
          } else if (result === false) {
            reportRejected({ ...(payload || {}), commands: [command], reason: "frontend_command_failed" });
            return;
          }
          attempted = true;
        }
        if (stopCommand && _forceStop("toy_stop_command")) {
          attempted = true;
        }
      } catch (e) {
        reportRejected({ ...(payload || {}), commands: [command], reason: e?.message || "frontend_command_failed" });
      }
    });
    return attempted;
  }

  global.ControlToyRouter = { commands: _commands, decision, shouldExecute, execute, reportRejected, isStopCommand: _isStopCommand };
  global.ChatApp?.registerModule?.("controlToyRouter", global.ControlToyRouter);
})(window);
