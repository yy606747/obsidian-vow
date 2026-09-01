from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read_static(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_control_runtime_supports_refresh_recovery_and_owner_arbitration():
    source = _read_static("static/js/chat/control_runtime.js")

    assert "/api/control/sessions/current?conv_id=" in source
    assert "/api/control/sessions/tide/current" in source
    assert "/api/control/sessions/tide/${encodeURIComponent(current.session_id)}/claim" in source
    assert "recover(convId)" in source
    assert "BroadcastChannel" in source
    assert "OWNER_LOCK_PREFIX" in source
    assert "TIDE_OWNER_KEY" in source
    assert "aion_tide_owner_client_id" in source
    assert "localStorage.setItem(TIDE_OWNER_KEY" in source
    assert "sessionStorage.setItem(OWNER_KEY" in source
    assert "owner_conflict" in source
    assert "pagehide" in source
    assert "end(\"client_disconnect\"" not in source
    assert "close_reason: reason || \"normal\"" in source


def test_control_scripts_are_cache_busted_for_app_webview():
    html = _read_static("static/chat.html")
    facade = _read_static("static/js/chat/ai_dom_facade.js")

    assert "/static/js/chat/control_runtime.js?v=20260531-control-trace4" in html
    assert "/static/js/chat/toy_bridge.js?v=20260531-control-trace4" in html
    assert "/static/js/chat/control_toy_router.js?v=20260531-control-trace4" in html
    assert "/static/js/chat/realtime.js?v=20260627-error-persist" in html
    assert "/static/js/chat/send.js?v=20260825-voice-input" in html
    assert "/static/js/chat/messages.js?v=20260825-voice-input" in html
    assert "/static/js/chat/whisper_toy_facade.js?v=20260531-control-trace4" in html
    assert "/static/js/chat/ai_dom_facade.js?v=20260531-control-trace4" in html
    assert "/static/js/chat/ai_dom.js?v=20260531-control-trace4" in facade


def test_control_runtime_normalizes_snapshot_schema_before_upload():
    source = _read_static("static/js/chat/control_runtime.js")

    assert "schema_version: \"control_snapshot_v1\"" in source
    assert "normalizeSnapshot(kind, value)" in source
    assert "frontend_snapshot_json: safeSnapshot" in source
    dom_body = source[
        source.index('if ((kind || raw.kind) === "dom")'):
        source.index('if ((kind || raw.kind) === "whisper")')
    ]
    assert "toy_connected: !!raw.toy_connected" in dom_body
    assert "content:" not in source[source.index("function normalizeSnapshot"):source.index("function _snapshot")]
    assert "attachments" not in source[source.index("function normalizeSnapshot"):source.index("function _snapshot")]


def test_control_runtime_heartbeats_before_reusing_owner_session_snapshot():
    source = _read_static("static/js/chat/control_runtime.js")
    branch = source[
        source.index('reason: "reuse_owner_session"'):
        source.index('return session;', source.index('reason: "reuse_owner_session"'))
    ]

    assert "await heartbeat();" in branch
    assert branch.index("await heartbeat();") < branch.index("await updateSnapshot")


def test_control_runtime_replaces_owner_session_when_device_id_changes():
    source = _read_static("static/js/chat/control_runtime.js")
    start_body = source[
        source.index("async function start"):
        source.index("async function heartbeat")
    ]

    assert "const requestedDeviceId = options.deviceId ||" in start_body
    assert '"browser_toy_bridge"' in start_body
    assert "current.device_id !== requestedDeviceId" in start_body
    assert 'close_reason: "device_changed"' in start_body
    assert 'reason: "reuse_owner_session"' in start_body
    assert start_body.index("current.device_id !== requestedDeviceId") < start_body.index('reason: "reuse_owner_session"')


def test_control_runtime_releases_previous_local_mode_when_session_switches():
    source = _read_static("static/js/chat/control_runtime.js")
    set_session_body = source[
        source.index("function _setSession"):
        source.index("function _bindPageLifecycle")
    ]

    assert "previous.session_id !== session.session_id" in set_session_body
    assert '_releaseLocalMode(previous.kind, opts.reason || "session_switch")' in set_session_body
    assert set_session_body.index("previous.session_id !== session.session_id") < set_session_body.index("_applyRecoveredMode")


def test_send_path_uploads_control_snapshot_not_message_body():
    source = _read_static("static/js/chat/send.js")

    assert "ControlRuntime.updateSnapshot(sendBody)" not in source
    assert "aiDomBuildControlSnapshot()" in source
    assert "whisperBuildControlSnapshot()" in source


def test_lazy_control_modules_restore_runtime_session_after_load():
    ai_dom = _read_static("static/js/chat/ai_dom.js")
    whisper = _read_static("static/js/chat/whisper_toy.js")

    assert "aiDomRestoreControlRuntimeSession()" in ai_dom
    assert "ControlRuntime?.currentSession" in ai_dom
    assert "whisperRestoreControlRuntimeSession()" in whisper
    assert "ControlRuntime?.currentSession" in whisper


def test_ai_dom_starts_control_session_with_backend_bridge_device_id():
    source = _read_static("static/js/chat/ai_dom.js")
    enter_body = source[
        source.index("async function aiDomEnter"):
        source.index("function aiDomExit")
    ]

    assert "const AI_DOM_BACKEND_DEVICE_ID = 'browser_toy_bridge'" in source
    assert "deviceId: AI_DOM_BACKEND_DEVICE_ID" in enter_body
    assert "deviceId: toyDriver.active" not in enter_body


def test_ai_dom_control_snapshot_reports_toy_connection():
    source = _read_static("static/js/chat/ai_dom.js")
    snapshot_body = source[
        source.index("function aiDomBuildControlSnapshot"):
        source.index("function aiDomSceneClearTimers")
    ]

    assert "toy_connected:" in snapshot_body
    assert "toyBridgeIsConnected()" in snapshot_body
    assert "toyDriver.isConnected()" in snapshot_body


def test_toy_bridge_heartbeat_tracks_active_driver_connections():
    source = _read_static("static/js/chat/toy_bridge.js")
    heartbeat_body = source[
        source.index("function toyBridgeIsConnected"):
        source.index("function toyHexToBytes")
    ]

    assert "function toyBridgeIsConnected()" in heartbeat_body
    assert "toyDriver.isConnected()" in heartbeat_body
    assert "cxConnected" in heartbeat_body
    assert "window.AionBle.isConnected()" in heartbeat_body
    assert "if (toyBridgeIsConnected()) toyReportBridgeState(true" in heartbeat_body
    assert "if (toyConnected) toyReportBridgeState(true" not in heartbeat_body


def test_ai_dom_cx_web_bluetooth_reports_bridge_state():
    source = _read_static("static/js/chat/ai_dom.js")
    cx_body = source[
        source.index("async function cxConnect"):
        source.index("// CX492B 双通道")
    ]

    assert "toyReportBridgeState(true, { source_event: 'dom_cx492b_connected' })" in cx_body
    assert "toyReportBridgeState(false, { source_event: 'dom_cx492b_disconnected' })" in cx_body
    assert "toyReportBridgeState(false, { source_event: 'dom_cx492b_manual_disconnect' })" in cx_body


def test_control_toy_router_requires_runtime_decision():
    source = _read_static("static/js/chat/control_toy_router.js")

    assert "ControlRuntime.toyCommandDecision" in source
    assert "ControlRuntime.shouldExecuteToyCommand" in source
    assert "return true;" not in source[source.index("function shouldExecute"):source.index("function execute")]


def test_control_toy_router_always_allows_stop_failsafe():
    source = _read_static("static/js/chat/control_toy_router.js")

    assert "function _isStopCommand" in source
    assert "toyBridgeIsConnected()" in source
    assert "cxConnected" in source
    assert "AionBle" in source
    assert "const hasStopCommand = commands.some(_isStopCommand)" in source
    assert "if (!runtimeAllowed && !hasStopCommand)" in source
    assert "frontend_runtime_rejected" in source
    assert "if (!hasStopCommand && !isConnected)" in source
    assert "frontend_device_disconnected" in source
    assert "ControlEmergencyStop.stopDevice" in source


def test_control_toy_rejection_is_visible_in_frontend_handlers():
    router = _read_static("static/js/chat/control_toy_router.js")
    runtime = _read_static("static/js/chat/control_runtime.js")
    send = _read_static("static/js/chat/send.js")
    messages = _read_static("static/js/chat/messages.js")
    realtime = _read_static("static/js/chat/realtime.js")
    ai_dom = _read_static("static/js/chat/ai_dom.js")
    whisper = _read_static("static/js/chat/whisper_toy.js")

    assert "function reportRejected" in router
    assert "addErrorToSystemLog(message" in router
    assert ".then(ok =>" in router
    assert "toyCommandDecision" in runtime
    assert "reason: \"missing_control_metadata\"" in runtime
    assert "data.type === \"toy_command_rejected\"" in send
    assert "d.type === \"toy_command_rejected\"" in messages
    assert "type === \"toy_command_rejected\"" in realtime
    assert "data.type === 'toy_command_rejected'" in ai_dom
    assert "data.type === 'toy_command_rejected'" in whisper


def test_frontend_toy_execution_reports_local_send_failure():
    router = _read_static("static/js/chat/control_toy_router.js")
    ai_dom = _read_static("static/js/chat/ai_dom.js")
    whisper = _read_static("static/js/chat/whisper_toy.js")
    bridge = _read_static("static/js/chat/toy_bridge.js")

    assert "return false;" in bridge[bridge.index("async function toySendData2"):bridge.index("ChatApp.registerModule")]
    assert "return true;" in bridge[bridge.index("async function toySendData2"):bridge.index("ChatApp.registerModule")]
    assert "async function aiDomSceneDispatch" in ai_dom
    assert "const ok = await toyDriver.play" in ai_dom
    assert "return ok !== false" in ai_dom
    assert "return aiDomSceneDispatch(cmd)" in whisper
    assert "return await toyApplyPreset(p)" in whisper
    assert "frontend_command_failed" in router


def test_control_emergency_stop_can_call_android_adv_bridge_directly():
    source = _read_static("static/js/chat/control_emergency_stop.js")

    assert "function stopDevice" in source
    assert "toyDriver.stop" in source
    assert "global.AionAdv.emergencyStop" in source
    assert "global.AionAdv.stop" in source
    assert "stopDevice(closeReason)" in source


def test_tide_toy_router_rejects_stale_session_stop_but_allows_emergency():
    source = _read_static("static/js/chat/tide_toy_router.js")
    body = source[
        source.index("function shouldExecuteStopFrame"):
        source.index("function shouldExecute(frame)")
    ]

    assert "if (frame.emergency_stop === true) return true" in body
    assert "global.ControlRuntime?.currentSession?.()" in body
    assert "return frame.control_session_id === current.session_id" in body


def test_tide_facade_refreshes_from_backend_on_open():
    source = _read_static("static/js/chat/tide_facade.js")

    assert "async function refreshFromBackend()" in source
    assert "ControlRuntime?.recover" in source
    assert "refreshFromBackend();" in source[source.index("function open"):source.index("function close")]


def test_muse_ttl_stop_uses_broadcast_receiver_not_service_self_resurrection():
    service = (ROOT.parent / "AionApp/app/src/main/java/app/obsidianvow/core/MuseForegroundService.java").read_text(encoding="utf-8")
    receiver = (ROOT.parent / "AionApp/app/src/main/java/app/obsidianvow/core/MuseStopReceiver.java").read_text(encoding="utf-8")
    manifest = (ROOT.parent / "AionApp/app/src/main/AndroidManifest.xml").read_text(encoding="utf-8")

    ttl_pending = service[
        service.index("private PendingIntent ttlPendingIntent()"):
        service.index("private void ensureForeground()")
    ]
    assert "new Intent(this, MuseStopReceiver.class)" in ttl_pending
    assert "PendingIntent.getBroadcast" in ttl_pending
    assert "PendingIntent.getService" not in ttl_pending
    assert "goAsync()" in receiver
    assert "MuseBleCommands.sendStopBurst" in receiver
    assert "PowerManager.PARTIAL_WAKE_LOCK" in receiver
    assert "wakeLock.acquire(WAKELOCK_TIMEOUT_MS)" in receiver
    assert "wakeLock.release()" in receiver
    assert 'android:name=".MuseStopReceiver"' in manifest
