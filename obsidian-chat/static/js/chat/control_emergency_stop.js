(function (global) {
  function runLocalStop(localStop, reason) {
    if (typeof localStop !== "function") return;
    try { localStop(reason || "panic"); } catch (e) {}
  }

  function _watchStop(result, label) {
    if (result && typeof result.catch === "function") {
      result.catch(e => console.warn(`[ControlEmergencyStop] ${label} failed`, e));
    }
  }

  function _currentDriver() {
    try { if (typeof toyDriver !== "undefined") return toyDriver; } catch (e) {}
    return global.toyDriver || null;
  }

  function _isAdvDriver(driver) {
    try { if (driver?._isAdv) return !!driver._isAdv(); } catch (e) {}
    const active = String(driver?.active || "").toLowerCase();
    return active === "sk30" || active === "sk40";
  }

  function stopDevice(reason) {
    let attempted = false;
    const closeReason = reason || "stop";
    const reasonKey = String(closeReason).toLowerCase();
    const driver = _currentDriver();
    const advDriver = _isAdvDriver(driver);
    const nativeAlreadyStopping = reasonKey === "device_emergency_stop";
    const canUseAdvBridge = global.ObsidianAdv && (advDriver || !driver?.active);
    let usedAdvBridge = false;

    if (!nativeAlreadyStopping && global.ObsidianMuse) {
      try {
        if (_shouldUseEmergencyBridge(closeReason) && typeof global.ObsidianMuse.emergencyStop === "function") {
          global.ObsidianMuse.emergencyStop();
        } else if (typeof global.ObsidianMuse.stop === "function") {
          global.ObsidianMuse.stop();
        }
        attempted = true;
      } catch (e) {
        console.warn("[ControlEmergencyStop] ObsidianMuse.stop failed", e);
      }
    }

    if (!nativeAlreadyStopping && canUseAdvBridge) {
      try {
        if (_shouldUseEmergencyBridge(closeReason)
          && typeof global.ObsidianAdv.emergencyStop === "function") {
          global.ObsidianAdv.emergencyStop();
          attempted = true;
          usedAdvBridge = true;
        } else if (typeof global.ObsidianAdv.stop === "function") {
          global.ObsidianAdv.stop();
          attempted = true;
          usedAdvBridge = true;
        }
      } catch (e) {
        console.warn("[ControlEmergencyStop] ObsidianAdv.stop failed", e);
      }
    }

    if (!nativeAlreadyStopping && !(advDriver && usedAdvBridge)) {
      try {
        if (driver && typeof driver.stop === "function") {
          _watchStop(driver.stop(closeReason), "toyDriver.stop");
          attempted = true;
        }
      } catch (e) {
        console.warn("[ControlEmergencyStop] toyDriver.stop failed", e);
      }
    }
    return attempted;
  }

  function _shouldUseEmergencyBridge(reason) {
    const r = String(reason || "").toLowerCase();
    return r === "panic" || r === "safeword" || r === "device_emergency_stop"
      || r === "emergency_stop";
  }

  async function panic(reason, localStop) {
    const closeReason = reason || "panic";
    stopDevice(closeReason);
    runLocalStop(localStop, closeReason);
    if (global.ControlRuntime?.panicStop) return await ControlRuntime.panicStop(closeReason);
    return null;
  }

  function triggerSafeword(text, options) {
    options = options || {};
    const active = typeof options.active === "function" ? options.active() : !!options.active;
    if (!active || !text || typeof options.matches !== "function") return false;
    if (!options.matches(text)) return false;
    if (typeof options.panic === "function") options.panic(options.reason || "safeword");
    return true;
  }

  global.ControlEmergencyStop = { runLocalStop, stopDevice, panic, triggerSafeword };
  global.ChatApp?.registerModule?.("controlEmergencyStop", global.ControlEmergencyStop);
})(window);
