(function (global) {
  function _payload(event) {
    return event && event.data && typeof event.data === "object" ? event.data : (event || {});
  }

  function _pattern(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return 0;
    return Math.max(0, Math.min(9, Math.round(n)));
  }

  function _ttl(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return 3000;
    return Math.max(500, Math.min(3000, Math.round(n)));
  }

  function _bridge() {
    return global.ObsidianMuse || null;
  }

  function isStopFrame(frame) {
    frame = _payload(frame);
    return frame.global_stop === true
      || frame.emergency_stop === true
      || (_pattern(frame.vib_pattern) === 0 && _pattern(frame.thrust_pattern) === 0);
  }

  function shouldExecuteStopFrame(frame) {
    frame = _payload(frame);
    if (frame.emergency_stop === true) return true;
    const current = global.ControlRuntime?.currentSession?.();
    if (!current || current.kind !== "tide") return true;
    if (!frame.control_session_id) return true;
    return frame.control_session_id === current.session_id;
  }

  function shouldExecute(frame) {
    frame = _payload(frame);
    if (isStopFrame(frame)) return shouldExecuteStopFrame(frame);
    if (global.ControlRuntime?.shouldExecuteTideFrame) {
      return ControlRuntime.shouldExecuteTideFrame(frame);
    }
    return false;
  }

  function execute(event) {
    const frame = _payload(event);
    const bridge = _bridge();
    if (!bridge) return false;

    if (isStopFrame(frame)) {
      if (!shouldExecuteStopFrame(frame)) return false;
      try {
        if (frame.emergency_stop && typeof bridge.emergencyStop === "function") bridge.emergencyStop();
        else if (typeof bridge.stop === "function") bridge.stop();
        return true;
      } catch (e) {
        console.warn("[TideToyRouter] stop failed", e);
        return false;
      }
    }

    if (!shouldExecute(frame)) return false;
    if (typeof bridge.isSupported === "function" && !bridge.isSupported()) return false;
    const payload = {
      vib_pattern: _pattern(frame.vib_pattern),
      thrust_pattern: _pattern(frame.thrust_pattern),
      ttl_ms: _ttl(frame.ttl_ms),
      control_session_id: frame.control_session_id || null,
      control_resource_id: frame.control_resource_id || "toy:muse",
      owner_client_id: frame.owner_client_id || null,
    };
    try {
      if (typeof bridge.playFrame === "function") bridge.playFrame(JSON.stringify(payload));
      else if (typeof bridge.play === "function") bridge.play(JSON.stringify(payload));
      else return false;
      return true;
    } catch (e) {
      console.warn("[TideToyRouter] frame failed", e);
      return false;
    }
  }

  global.tideNativeBle = global.tideNativeBle || {
    onLog(msg) { console.log("[TideMuse]", msg); },
    onError(msg) { console.warn("[TideMuse]", msg); },
    onConnected() {},
    onDisconnected() {},
  };

  global.TideToyRouter = { execute, shouldExecute, isStopFrame };
  global.ChatApp?.registerModule?.("tideToyRouter", global.TideToyRouter);
})(window);
