(function (global) {
  const HEARTBEAT_MS = 15000;
  const SAFETY_GUARD_MS = 20 * 60 * 1000;
  const OWNER_KEY = "obsidian_control_owner_client_id";
  const TIDE_OWNER_KEY = "obsidian_tide_owner_client_id";
  const OWNER_LOCK_PREFIX = "obsidian_control_owner_lock:";
  const SAFETY_KEY_PREFIX = "obsidian_control_safety_stop:";
  const CHANNEL_NAME = "obsidian_control_runtime";

  let ownerClientId = sessionStorage.getItem(OWNER_KEY);
  let tideOwnerClientId = localStorage.getItem(TIDE_OWNER_KEY);
  let session = null;
  let activeConvId = null;
  let heartbeatTimer = null;
  let listenersBound = false;
  let channel = null;

  if (!ownerClientId) {
    ownerClientId = `client_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
    sessionStorage.setItem(OWNER_KEY, ownerClientId);
  }
  if (!tideOwnerClientId) {
    tideOwnerClientId = `tide_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
    localStorage.setItem(TIDE_OWNER_KEY, tideOwnerClientId);
  }

  try {
    channel = "BroadcastChannel" in global ? new BroadcastChannel(CHANNEL_NAME) : null;
  } catch (e) {
    channel = null;
  }

  function _convId(explicit) {
    if (explicit) return explicit;
    try { if (typeof currentConvId !== "undefined") return currentConvId; } catch (e) {}
    return activeConvId;
  }

  function _lockKey(convId) {
    return `${OWNER_LOCK_PREFIX}${convId || ""}`;
  }

  function _safetyKey(convId) {
    return `${SAFETY_KEY_PREFIX}${convId || ""}`;
  }

  function _now() {
    return Date.now();
  }

  function _ownerForKind(kind) {
    return kind === "tide" ? tideOwnerClientId : ownerClientId;
  }

  function _ownerForSession(value) {
    return _ownerForKind((value || session)?.kind);
  }

  function _isActiveSession(value) {
    return !!(value && value.session_id && value.status !== "ended");
  }

  function isOwner(value) {
    const current = value || session;
    return !!(current && current.owner_client_id === _ownerForSession(current) && current.status === "active");
  }

  function isOwnerOrStaleOwner(value) {
    const current = value || session;
    return !!(current && current.owner_client_id === _ownerForSession(current) && current.status !== "ended");
  }

  function _status(reason) {
    return {
      reason: reason || "state",
      conv_id: activeConvId,
      kind: session ? session.kind : null,
      owner_client_id: _ownerForSession(),
      tide_owner_client_id: tideOwnerClientId,
      is_owner: isOwner(),
      session: session ? { ...session } : null,
    };
  }

  function _emit(reason) {
    const detail = _status(reason);
    try {
      global.dispatchEvent(new CustomEvent("obsidian:control-session", { detail }));
    } catch (e) {}
    try {
      channel?.postMessage({ type: "control_session", detail });
    } catch (e) {}
    return detail;
  }

  function _readJson(key) {
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : null;
    } catch (e) {
      return null;
    }
  }

  function _writeOwnerLock(reason) {
    if (!session || !activeConvId || !isOwnerOrStaleOwner(session)) return;
    const owner = _ownerForSession(session);
    try {
      localStorage.setItem(_lockKey(activeConvId), JSON.stringify({
        session_id: session.session_id,
        conv_id: activeConvId,
        kind: session.kind,
        status: session.status,
        owner_client_id: owner,
        control_epoch: session.control_epoch,
        control_resource_id: session.control_resource_id || null,
        updated_at: _now(),
        expires_at: _now() + HEARTBEAT_MS * 3,
        reason: reason || "update",
      }));
    } catch (e) {}
  }

  function _clearOwnerLock(convId, sessionId) {
    const key = _lockKey(convId || activeConvId);
    const lock = _readJson(key);
    if (!lock) return;
    if (sessionId && lock.session_id && lock.session_id !== sessionId) return;
    if (lock.owner_client_id && lock.owner_client_id !== _ownerForKind(lock.kind)) return;
    try { localStorage.removeItem(key); } catch (e) {}
  }

  function _rememberSafetyStop(convId) {
    const target = convId || activeConvId;
    if (!target) return;
    try {
      localStorage.setItem(_safetyKey(target), String(_now()));
    } catch (e) {}
  }

  function _safetyGuardActive(convId) {
    const target = convId || activeConvId;
    const localAt = Number(_readJson(_safetyKey(target)) || localStorage.getItem(_safetyKey(target)) || 0);
    return localAt > 0 && _now() - localAt <= SAFETY_GUARD_MS;
  }

  function _clearHeartbeat() {
    if (heartbeatTimer) clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }

  function _startHeartbeat() {
    _clearHeartbeat();
    if (!isOwnerOrStaleOwner()) return;
    heartbeatTimer = setInterval(heartbeat, HEARTBEAT_MS);
  }

  function _normalizeNumber(value, fallback, min, max) {
    let n = Number(value);
    if (!Number.isFinite(n)) n = fallback;
    if (min != null) n = Math.max(min, n);
    if (max != null) n = Math.min(max, n);
    return Math.round(n);
  }

  function _normalizeFloat(value, fallback, min, max) {
    let n = Number(value);
    if (!Number.isFinite(n)) n = fallback;
    if (min != null) n = Math.max(min, n);
    if (max != null) n = Math.min(max, n);
    return n;
  }

  function _normalizeString(value, maxLen) {
    const text = String(value == null ? "" : value).trim();
    return text.slice(0, maxLen || 80);
  }

  function _normalizeList(value, maxItems, maxLen) {
    const items = Array.isArray(value) ? value : (typeof value === "string" ? value.split(/[,\n|]/) : []);
    return items.map(item => _normalizeString(item, maxLen || 80)).filter(Boolean).slice(0, maxItems || 20);
  }

  function normalizeSnapshot(kind, value) {
    const raw = value && typeof value === "object" ? value : {};
    const base = {
      schema_version: "control_snapshot_v1",
      kind: kind || raw.kind || null,
      captured_at: _now(),
    };
    if ((kind || raw.kind) === "dom") {
      return {
        ...base,
        kind: "dom",
        safeword_set: !!raw.safeword_set,
        toy_connected: !!raw.toy_connected,
        dom_history: _normalizeList(raw.dom_history, 8, 80),
        cnc_enabled: !!raw.cnc_enabled,
        cnc_weakness: _normalizeList(raw.cnc_weakness, 20, 80),
        resist_hits: _normalizeNumber(raw.resist_hits, 0, 0, 20),
        short_streak: _normalizeNumber(raw.short_streak, 0, 0, 50),
        reply_delay_ms: _normalizeNumber(raw.reply_delay_ms, 0, 0, 24 * 60 * 60 * 1000),
        compliance_streak: _normalizeNumber(raw.compliance_streak, 0, 0, 50),
        session_elapsed: _normalizeNumber(raw.session_elapsed, 0, 0, 24 * 60 * 60),
        scene_name: _normalizeString(raw.scene_name || "", 80) || null,
        scene_elapsed: _normalizeNumber(raw.scene_elapsed, 0, 0, 24 * 60 * 60),
        since_last_punish: raw.since_last_punish == null || raw.since_last_punish === ""
          ? null
          : _normalizeNumber(raw.since_last_punish, 0, 0, 24 * 60 * 60),
        ratchet_valley: _normalizeNumber(raw.ratchet_valley, 0, 0, 10),
        debt: _normalizeFloat(raw.debt, 0, 0, 100),
        stubborn_streak: _normalizeNumber(raw.stubborn_streak, 0, 0, 50),
      };
    }
    if ((kind || raw.kind) === "whisper") {
      return {
        ...base,
        kind: "whisper",
        whisper_mode: !!raw.whisper_mode,
        toy_connected: !!raw.toy_connected,
        active_preset: _normalizeNumber(raw.active_preset, -1, -1, 8),
        initiative_enabled: !!raw.initiative_enabled,
      };
    }
    if ((kind || raw.kind) === "tide") {
      return {
        ...base,
        kind: "tide",
        tide_mode: !!raw.tide_mode,
        toy_connected: !!raw.toy_connected,
      };
    }
    return base;
  }

  function _snapshot(kind) {
    if (kind === "dom" && typeof global.aiDomBuildControlSnapshot === "function") {
      return normalizeSnapshot("dom", global.aiDomBuildControlSnapshot());
    }
    if (kind === "whisper" && typeof global.whisperBuildControlSnapshot === "function") {
      return normalizeSnapshot("whisper", global.whisperBuildControlSnapshot());
    }
    if (kind === "tide") {
      return normalizeSnapshot("tide", { tide_mode: !!global.tideMode, toy_connected: !!global.ObsidianMuse?.isSupported?.() });
    }
    return normalizeSnapshot(kind, {});
  }

  function _post(url, body, keepalive) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
      keepalive: !!keepalive,
    }).then(async r => {
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || `http_${r.status}`);
      return data;
    });
  }

  function _get(url) {
    return fetch(url, { method: "GET", headers: { "Content-Type": "application/json" } }).then(async r => {
      const data = await r.json().catch(() => null);
      if (!r.ok) throw new Error(data?.detail || `http_${r.status}`);
      return data;
    });
  }

  function _setGlobalFlag(name, value) {
    try {
      if (name === "aiDomMode" && typeof aiDomMode !== "undefined") aiDomMode = !!value;
      if (name === "whisperMode" && typeof whisperMode !== "undefined") whisperMode = !!value;
    } catch (e) {}
    try { global[name] = !!value; } catch (e) {}
  }

  function _releaseLocalMode(kind, reason) {
    if (kind === "dom") {
      try { if (typeof aiDomFidgetStop === "function") aiDomFidgetStop(); } catch (e) {}
      try { if (typeof aiDomInitiativeStop === "function") aiDomInitiativeStop(); } catch (e) {}
      try { if (typeof aiDomSceneStop === "function") aiDomSceneStop(); } catch (e) {}
      try {
        if (global.ControlEmergencyStop?.stopDevice) global.ControlEmergencyStop.stopDevice(reason || "control_release");
        else if (typeof toyDriver !== "undefined" && toyDriver?.stop) toyDriver.stop();
      } catch (e) {}
      _setGlobalFlag("aiDomMode", false);
      document.getElementById("aiDomPill")?.classList.remove("show");
    }
    if (kind === "whisper") {
      try { if (typeof whisperInitStop === "function") whisperInitStop(); } catch (e) {}
      _setGlobalFlag("whisperMode", false);
      const toggle = document.getElementById("whisperModeToggle");
      if (toggle) toggle.checked = false;
    }
    if (kind === "tide") {
      try { global.ObsidianMuse?.stop?.(); } catch (e) {}
      _setGlobalFlag("tideMode", false);
      document.getElementById("tidePill")?.classList.remove("show");
      const toggle = document.getElementById("tideModeToggle");
      if (toggle) toggle.checked = false;
    }
    if (reason === "owner_conflict") {
      global.showToast?.("另一个标签页正在控制当前对话", 2200);
    }
  }

  function _applyRecoveredMode(reason, previous) {
    if (!session || !isOwnerOrStaleOwner(session)) {
      if (previous && previous.owner_client_id === _ownerForSession(previous)) _releaseLocalMode(previous.kind, reason);
      return;
    }
    if (session.kind === "dom") {
      _setGlobalFlag("aiDomMode", true);
      try {
        if (typeof aiDomSessionStartAt !== "undefined" && session.started_at) {
          aiDomSessionStartAt = Number(session.started_at) * 1000;
        }
      } catch (e) {}
      const pill = document.getElementById("aiDomPill");
      if (pill) {
        const dev = document.getElementById("aiDomPillDev");
        const safe = document.getElementById("aiDomPillSafe");
        if (dev) dev.textContent = session.device_id || "设备";
        if (safe) safe.textContent = session.safeword_set ? "已设置" : "未设置";
        pill.classList.add("show");
      }
    }
    if (session.kind === "whisper") {
      _setGlobalFlag("whisperMode", true);
      const toggle = document.getElementById("whisperModeToggle");
      if (toggle) toggle.checked = true;
    }
    if (session.kind === "tide") {
      _setGlobalFlag("tideMode", true);
      const toggle = document.getElementById("tideModeToggle");
      if (toggle) toggle.checked = true;
      const pill = document.getElementById("tidePill");
      if (pill) {
        const dev = document.getElementById("tidePillDev");
        if (dev) dev.textContent = session.device_id || "muse";
        pill.classList.add("show");
      }
    }
  }

  function _setSession(next, options) {
    const opts = options || {};
    const previous = session;
    const previousConv = activeConvId;
    if (opts.convId) activeConvId = opts.convId;
    session = _isActiveSession(next) ? next : null;

    if (!session || session.status === "ended") {
      _clearHeartbeat();
      if (previousConv) _clearOwnerLock(previousConv, previous?.session_id);
    } else if (isOwnerOrStaleOwner(session)) {
      _writeOwnerLock(opts.reason);
      _startHeartbeat();
    } else {
      _clearHeartbeat();
    }

    if (
      session
      && isOwnerOrStaleOwner(session)
      && previous
      && previous.owner_client_id === _ownerForSession(previous)
      && previous.session_id !== session.session_id
    ) {
      _releaseLocalMode(previous.kind, opts.reason || "session_switch");
    }
    _applyRecoveredMode(opts.reason, previous);
    return _emit(opts.reason);
  }

  function _bindPageLifecycle() {
    if (listenersBound) return;
    listenersBound = true;
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState !== "visible") return;
      if (isOwnerOrStaleOwner()) heartbeat();
      else recover();
    });
    window.addEventListener("pagehide", () => {
      _clearHeartbeat();
      if (isOwnerOrStaleOwner()) _writeOwnerLock("pagehide");
    });
    window.addEventListener("storage", event => {
      if (!event.key || !event.key.startsWith(OWNER_LOCK_PREFIX)) return;
      const convId = event.key.slice(OWNER_LOCK_PREFIX.length);
      if (convId && convId === activeConvId) recover(convId);
    });
    if (channel) {
      channel.onmessage = event => {
        const msg = event.data || {};
        if (msg.type !== "control_session") return;
        const convId = msg.detail?.conv_id;
        if (convId && convId === activeConvId && msg.detail?.owner_client_id !== _ownerForKind(msg.detail?.session?.kind || msg.detail?.kind)) {
          recover(convId);
        }
      };
    }
  }

  async function fetchCurrent(convId) {
    const target = _convId(convId);
    if (!target) return null;
    return await _get(`/api/control/sessions/current?conv_id=${encodeURIComponent(target)}`);
  }

  async function fetchCurrentTide() {
    return await _get("/api/control/sessions/tide/current");
  }

  async function claimTide(current) {
    if (!current || current.kind !== "tide" || current.status === "ended") return current;
    if (current.owner_client_id === tideOwnerClientId) return current;
    return await _post(`/api/control/sessions/tide/${encodeURIComponent(current.session_id)}/claim`, {
      owner_client_id: tideOwnerClientId,
    });
  }

  async function recover(convId) {
    _bindPageLifecycle();
    const target = _convId(convId);
    try {
      const tide = await fetchCurrentTide();
      if (tide && tide.status !== "ended") {
        const claimed = await claimTide(tide);
        _setSession(claimed, { convId: claimed.conv_id || target, reason: tide.owner_client_id === tideOwnerClientId ? "recover_tide" : "claim_tide" });
        return session;
      }
      if (!target) {
        _setSession(null, { reason: "recover_no_conversation" });
        return null;
      }
      const current = await fetchCurrent(target);
      _setSession(current, { convId: target, reason: "recover" });
      return session;
    } catch (e) {
      console.warn("[ControlRuntime] recover failed", e);
      _setSession(null, { convId: target, reason: "recover_failed" });
      return null;
    }
  }

  async function start(kind, options) {
    options = options || {};
    const convId = _convId(options.convId);
    if (!convId) return null;
    const requestedDeviceId = options.deviceId || (kind === "tide" ? "muse" : "browser_toy_bridge");
    const requestOwnerClientId = _ownerForKind(kind);
    _bindPageLifecycle();
    try {
      let current = kind === "tide" ? await fetchCurrentTide() : await fetchCurrent(convId);
      if (kind === "tide" && current && current.owner_client_id !== requestOwnerClientId && current.status !== "ended") {
        current = await claimTide(current);
      }
      if (current && current.owner_client_id !== requestOwnerClientId && current.status === "active" && !options.force) {
        _setSession(current, { convId, reason: "owner_conflict" });
        return session;
      }
      if (current && current.owner_client_id === requestOwnerClientId && current.kind === kind && current.status !== "ended") {
        if (current.device_id && current.device_id !== requestedDeviceId) {
          await _post(`/api/control/sessions/${encodeURIComponent(current.session_id)}/end`, {
            owner_client_id: requestOwnerClientId,
            close_reason: "device_changed",
          });
          _setSession(null, { convId, reason: "device_changed" });
        } else {
          _setSession(current, { convId, reason: "reuse_owner_session" });
          await heartbeat();
          await updateSnapshot(options.snapshot || _snapshot(kind));
          return session;
        }
      }
    } catch (e) {}

    try {
      const started = await _post("/api/control/sessions/start", {
        conv_id: convId,
        kind,
        owner_client_id: requestOwnerClientId,
        device_id: requestedDeviceId,
        control_resource_id: options.controlResourceId || (kind === "tide" ? "toy:muse" : null),
        safeword_set: !!options.safewordSet,
      });
      _setSession(started, { convId, reason: "start" });
      await updateSnapshot(options.snapshot || _snapshot(kind));
      return session;
    } catch (e) {
      console.warn("[ControlRuntime] start failed", e);
      await recover(convId);
      return null;
    }
  }

  async function heartbeat() {
    if (!session || !isOwnerOrStaleOwner(session)) return session;
    const owner = _ownerForSession(session);
    try {
      const updated = await _post(`/api/control/sessions/${encodeURIComponent(session.session_id)}/heartbeat`, {
        owner_client_id: owner,
      });
      _setSession(updated, { convId: activeConvId, reason: "heartbeat" });
      return session;
    } catch (e) {
      console.warn("[ControlRuntime] heartbeat failed", e);
      await recover(activeConvId);
      return session;
    }
  }

  async function updateSnapshot(snapshot) {
    if (!session || !isOwnerOrStaleOwner(session)) return null;
    const owner = _ownerForSession(session);
    try {
      const safeSnapshot = normalizeSnapshot(session.kind, snapshot || _snapshot(session.kind));
      const updated = await _post(`/api/control/sessions/${encodeURIComponent(session.session_id)}/snapshot`, {
        owner_client_id: owner,
        frontend_snapshot_json: safeSnapshot,
      });
      _setSession(updated, { convId: activeConvId, reason: "snapshot" });
      return session;
    } catch (e) {
      console.warn("[ControlRuntime] snapshot failed", e);
      await recover(activeConvId);
      return session;
    }
  }

  async function end(reason, options) {
    if (!session || !isOwnerOrStaleOwner(session)) {
      _setSession(null, { reason: "end_without_owner" });
      return null;
    }
    const endedSession = session;
    const convId = activeConvId;
    const owner = _ownerForSession(endedSession);
    _setSession(null, { convId, reason: "local_end" });
    if (["panic", "safeword", "device_emergency_stop"].includes(reason || "")) {
      _rememberSafetyStop(convId);
    }
    try {
      return await _post(`/api/control/sessions/${encodeURIComponent(endedSession.session_id)}/end`, {
        owner_client_id: owner,
        close_reason: reason || "normal",
      }, options && options.keepalive);
    } catch (e) {
      console.warn("[ControlRuntime] end failed", e);
      return null;
    }
  }

  async function panicStop(reason, localStop) {
    const closeReason = reason || "panic";
    if (typeof localStop === "function") {
      try { localStop(closeReason); } catch (e) {}
    }
    _rememberSafetyStop(activeConvId);
    return await end(closeReason);
  }

  function toyCommandDecision(payload) {
    payload = payload || {};
    if (_safetyGuardActive(activeConvId)) return { ok: false, reason: "safety_guard_active" };
    if (payload.legacy_allowed === true && payload.control_legacy_fallback === true && !session) {
      return { ok: true, reason: "legacy_allowed" };
    }
    if (!session) return { ok: false, reason: "no_frontend_session" };
    if (!isOwner()) return { ok: false, reason: "not_owner" };
    if (!payload.control_session_id || payload.control_epoch == null || !payload.owner_client_id) {
      return { ok: false, reason: "missing_control_metadata" };
    }
    if (payload.control_session_id !== session.session_id) return { ok: false, reason: "session_mismatch" };
    if (payload.owner_client_id !== _ownerForSession(session)) return { ok: false, reason: "owner_mismatch" };
    if (Number(payload.control_epoch) !== Number(session.control_epoch || 0)) {
      return { ok: false, reason: "epoch_mismatch" };
    }
    if (session.status !== "active") return { ok: false, reason: `session_${session.status || "inactive"}` };
    return { ok: true, reason: "accepted" };
  }

  function shouldExecuteToyCommand(payload) {
    return !!toyCommandDecision(payload).ok;
  }

  function shouldExecuteTideFrame(payload) {
    payload = payload || {};
    if (payload.global_stop === true || payload.emergency_stop === true) return true;
    if (_safetyGuardActive(activeConvId)) return false;
    if (!session || !isOwner() || session.kind !== "tide") return false;
    if (!payload.control_session_id || !payload.owner_client_id) return false;
    if (payload.control_session_id !== session.session_id) return false;
    if (payload.owner_client_id !== tideOwnerClientId) return false;
    if (payload.control_resource_id && session.control_resource_id && payload.control_resource_id !== session.control_resource_id) return false;
    return session.status === "active";
  }

  function clear(reason) {
    _setSession(null, { reason: reason || "clear" });
  }

  _bindPageLifecycle();

  global.ControlRuntime = {
    start,
    recover,
    heartbeat,
    updateSnapshot,
    end,
    panicStop,
    clear,
    normalizeSnapshot,
    currentSession: () => session,
    ownedSession: () => isOwnerOrStaleOwner() ? session : null,
    currentEpoch: () => session ? session.control_epoch : null,
    currentResourceId: () => session ? session.control_resource_id : null,
    ownerClientId: () => ownerClientId,
    tideOwnerClientId: () => tideOwnerClientId,
    ownerClientIdForKind: kind => _ownerForKind(kind),
    fetchCurrentTide,
    claimTide,
    isOwner: () => isOwner(),
    status: () => _status(),
    toyCommandDecision,
    shouldExecuteToyCommand,
    shouldExecuteTideFrame,
  };

  global.ChatApp?.registerModule?.("controlRuntime", global.ControlRuntime);
})(window);
