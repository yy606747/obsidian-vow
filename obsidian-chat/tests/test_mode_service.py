from app.modes import ChatMode, service as mode_module, mode_service


def test_mode_service_normal_mode_excludes_device_toy():
    snapshot = mode_service.snapshot(ChatMode.NORMAL)

    assert snapshot.mode is ChatMode.NORMAL
    assert "music.search" in snapshot.capabilities
    assert "memory.remember" in snapshot.capabilities
    assert "monitor.camera" not in snapshot.capabilities
    assert "device.toy" not in snapshot.capabilities
    assert mode_service.has_capability(ChatMode.NORMAL, "device.toy") is False


def test_mode_service_device_modes_include_device_toy():
    for mode in (ChatMode.DEVICE_CONTROL, ChatMode.INTIMATE, ChatMode.CONTROL_SESSION):
        snapshot = mode_service.snapshot(mode)
        assert "device.toy" in snapshot.capabilities
        assert mode_service.has_capability(mode, "device.toy") is True


def test_mode_service_resolves_existing_chat_flags_without_new_api_fields():
    normal = mode_service.snapshot_from_flags()
    whisper = mode_service.snapshot_from_flags(whisper_mode=True)
    dom = mode_service.snapshot_from_flags(whisper_mode=True, ai_dom_mode=True)

    assert normal.mode is ChatMode.NORMAL
    assert normal.source == "server_default"
    assert whisper.mode is ChatMode.INTIMATE
    assert whisper.source == "whisper_mode"
    assert dom.mode is ChatMode.DEVICE_CONTROL
    assert dom.source == "ai_dom_mode"


def test_mode_service_unknown_mode_falls_back_to_normal():
    snapshot = mode_service.snapshot("unknown")

    assert snapshot.mode is ChatMode.NORMAL
    assert "device.toy" not in snapshot.capabilities


def test_mode_service_adds_ring_touch_from_single_settings_source(monkeypatch):
    monkeypatch.setitem(mode_module.SETTINGS, "smart_ring_touch_enabled", True)
    monkeypatch.setitem(mode_module.SETTINGS, "smart_ring_quiet_hours_enabled", False)

    snapshot = mode_service.snapshot(ChatMode.NORMAL)

    assert "device.ring_touch" in snapshot.capabilities
    assert mode_service.has_capability(ChatMode.NORMAL, "device.ring_touch") is True


def test_mode_service_excludes_ring_touch_during_quiet_hours(monkeypatch):
    monkeypatch.setitem(mode_module.SETTINGS, "smart_ring_touch_enabled", True)
    monkeypatch.setitem(mode_module.SETTINGS, "smart_ring_quiet_hours_enabled", True)
    monkeypatch.setitem(mode_module.SETTINGS, "smart_ring_quiet_hours_start", "00:00")
    monkeypatch.setitem(mode_module.SETTINGS, "smart_ring_quiet_hours_end", "23:59")

    snapshot = mode_service.snapshot(ChatMode.NORMAL)

    assert "device.ring_touch" not in snapshot.capabilities
