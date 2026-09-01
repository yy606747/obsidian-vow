"""Phase 5: the mobile.screen_check production path — parse the marker, resolve
the target device, and create a routed request."""

import asyncio
from types import SimpleNamespace

import pytest

import app.chat.streaming as streaming
from app.devices.schemas import DeviceState, DeviceStatus
from app.tools.parser import _mobile_screen_args, parse_tool_intents


# ── parsing ─────────────────────────────────────────────

def test_parse_legacy_marker_yields_intent_with_target_and_reason():
    intents = parse_tool_intents(
        "好的[MOBILE_SCREEN_CHECK:华为平板|看看你在干嘛]",
        enabled_commands={"mobile_screen"},
    )
    mobile = [i for i in intents if i.tool_name == "mobile.screen_check"]
    assert len(mobile) == 1
    assert mobile[0].arguments["target"] == "华为平板"
    assert mobile[0].arguments["reason"] == "看看你在干嘛"


def test_mobile_screen_args_without_pipe_is_reason_only():
    assert _mobile_screen_args("看一下屏幕") == {"target": "", "reason": "看一下屏幕"}
    assert _mobile_screen_args("手机|在干嘛") == {"target": "手机", "reason": "在干嘛"}


def test_parse_disabled_group_yields_nothing():
    intents = parse_tool_intents(
        "[MOBILE_SCREEN_CHECK:手机|x]", enabled_commands={"screen"})
    assert not [i for i in intents if i.tool_name == "mobile.screen_check"]


# ── target resolution ───────────────────────────────────

def _dev(device_id, name, dtype, *, online=True, screen=True, cap=True, seen=1000.0):
    return DeviceState(
        device_id=device_id, name=name, kind=f"android_{dtype}",
        status=DeviceStatus.ONLINE if online else DeviceStatus.OFFLINE,
        driver_id="android_mobile",
        capabilities=("screen.capture",) if cap else ("activity.report",),
        last_seen_at=seen,
        metadata={"device_type": dtype, "screen_agent_online": screen},
    )


class _FakeDriver:
    def __init__(self, devices):
        self._devices = devices
    async def list_devices(self):
        return self._devices


def _resolve(monkeypatch, devices, target):
    monkeypatch.setattr(streaming.device_service, "get_driver",
                        lambda did: _FakeDriver(devices) if did == "android_mobile" else None)
    return asyncio.run(streaming._resolve_mobile_target(target))


def test_resolve_by_type_keyword(monkeypatch):
    devices = [_dev("android_ph", "我的手机", "phone"), _dev("android_tab", "华为平板", "tablet")]
    assert _resolve(monkeypatch, devices, "平板") == ("android_tab", None)
    assert _resolve(monkeypatch, devices, "手机") == ("android_ph", None)


def test_resolve_by_name(monkeypatch):
    devices = [_dev("android_ph", "我的手机", "phone"), _dev("android_tab", "华为平板", "tablet")]
    assert _resolve(monkeypatch, devices, "华为平板") == ("android_tab", None)


def test_resolve_empty_target_single_device(monkeypatch):
    devices = [_dev("android_tab", "华为平板", "tablet")]
    assert _resolve(monkeypatch, devices, "") == ("android_tab", None)


def test_resolve_empty_target_multiple_is_ambiguous_not_silent_pick(monkeypatch):
    devices = [_dev("android_ph", "手机", "phone", seen=1000.0),
               _dev("android_tab", "平板", "tablet", seen=2000.0)]
    # must NOT silently pick the most-recent device — ambiguous, require target
    assert _resolve(monkeypatch, devices, "") == (None, "ambiguous_target")


def test_resolve_same_target_matches_multiple_is_ambiguous(monkeypatch):
    devices = [_dev("android_t1", "平板A", "tablet"), _dev("android_t2", "平板B", "tablet")]
    assert _resolve(monkeypatch, devices, "平板") == (None, "ambiguous_target")


def test_resolve_excludes_offline_and_non_capture(monkeypatch):
    devices = [_dev("android_ph", "手机", "phone", online=False),
               _dev("android_tab", "平板", "tablet", screen=False)]
    assert _resolve(monkeypatch, devices, "") == (None, "offline")


def test_resolve_named_but_absent_is_offline_not_random(monkeypatch):
    devices = [_dev("android_ph", "我的手机", "phone")]
    # asked for a tablet, only a phone online → don't misroute to the phone
    assert _resolve(monkeypatch, devices, "平板") == (None, "offline")


def test_autonomous_target_requires_exactly_one_active_mobile_device(monkeypatch):
    import app.mobile_screen.autonomous as auto

    devices = [_dev("android_ph", "手机", "phone"), _dev("android_tab", "华为平板", "tablet")]
    monkeypatch.setattr(auto.device_service, "get_driver", lambda did: _FakeDriver(devices))
    monkeypatch.setattr(auto.mobile_screen_service, "is_enabled", lambda: True)
    monkeypatch.setattr(auto, "model_supports_vision", lambda model_key: True)
    monkeypatch.setattr(auto, "read_recent_activity", lambda hours=1: [
        {"device_id": "android_tab", "timestamp": 1000.0, "app": "微信"},
        {"device_id": "android_ph", "timestamp": 999.0, "app": "锁屏"},
    ])
    auto._reset_autonomous_state_for_tests()

    target = asyncio.run(auto.autonomous_mobile_screen_target(model_key="gemini", now=1001.0))
    assert target["device_id"] == "android_tab"
    assert target["label"] == "华为平板"


def test_autonomous_target_rejects_multiple_active_devices(monkeypatch):
    import app.mobile_screen.autonomous as auto

    devices = [_dev("android_ph", "手机", "phone"), _dev("android_tab", "华为平板", "tablet")]
    monkeypatch.setattr(auto.device_service, "get_driver", lambda did: _FakeDriver(devices))
    monkeypatch.setattr(auto.mobile_screen_service, "is_enabled", lambda: True)
    monkeypatch.setattr(auto, "model_supports_vision", lambda model_key: True)
    monkeypatch.setattr(auto, "read_recent_activity", lambda hours=1: [
        {"device_id": "android_tab", "timestamp": 1000.0, "app": "微信"},
        {"device_id": "android_ph", "timestamp": 1000.0, "app": "抖音"},
    ])
    auto._reset_autonomous_state_for_tests()

    assert asyncio.run(auto.autonomous_mobile_screen_target(model_key="gemini", now=1001.0)) is None


def test_autonomous_target_respects_long_cooldown(monkeypatch):
    import app.mobile_screen.autonomous as auto

    devices = [_dev("android_tab", "华为平板", "tablet")]
    monkeypatch.setattr(auto.device_service, "get_driver", lambda did: _FakeDriver(devices))
    monkeypatch.setattr(auto.mobile_screen_service, "is_enabled", lambda: True)
    monkeypatch.setattr(auto, "model_supports_vision", lambda model_key: True)
    monkeypatch.setattr(auto, "read_recent_activity", lambda hours=1: [
        {"device_id": "android_tab", "timestamp": 1000.0, "app": "微信"},
    ])
    auto._reset_autonomous_state_for_tests()
    auto.record_autonomous_mobile_screen_request(1000.0)

    assert asyncio.run(auto.autonomous_mobile_screen_target(model_key="gemini", now=1100.0)) is None
    target = asyncio.run(auto.autonomous_mobile_screen_target(
        model_key="gemini",
        now=1100.0,
        ignore_cooldown=True,
    ))
    assert target["device_id"] == "android_tab"


# ── executor ────────────────────────────────────────────

def test_execute_failure_spawns_followup_not_silent(monkeypatch):
    # resolution fails (ambiguous) → must build a rejected request AND spawn the
    # follow-up so the user gets a natural-language explanation, not a void reply.
    monkeypatch.setattr(streaming, "_resolve_mobile_target",
                        lambda t: _async_return((None, "ambiguous_target")))
    fake_rej = SimpleNamespace(request_id="r0", reason="x")
    built = {}

    def fake_build(**kwargs):
        built.update(kwargs)
        return fake_rej

    spawned = []
    monkeypatch.setattr(streaming.mobile_screen_service, "build_rejected_request", fake_build)
    monkeypatch.setattr(streaming, "create_tracked_task",
                        lambda coro, name=None: spawned.append(name) or coro.close())

    intent = SimpleNamespace(id="i1", arguments={"target": "", "reason": "x"})
    ctx = SimpleNamespace(conv_id="c1", msg_id="m1", model_key="gemini")
    out = asyncio.run(streaming._execute_mobile_screen_check(intent, ctx))
    assert out["type"] == "screen_check_rejected"
    assert out["reject_reason"] == "ambiguous_target"
    assert built["reject_reason"] == "ambiguous_target"
    assert spawned  # follow-up scheduled even though no real request was created


def test_execute_creates_pending_and_spawns_followup(monkeypatch):
    monkeypatch.setattr(streaming, "_resolve_mobile_target",
                        lambda t: _async_return(("android_tab", None)))
    fake_req = SimpleNamespace(
        request_id="r1", status="pending", target_device_id="android_tab",
        target_device_name="华为平板", reason="看看")

    async def fake_create(**kwargs):
        assert kwargs["target_device_id"] == "android_tab"
        return fake_req

    spawned = []
    monkeypatch.setattr(streaming.mobile_screen_service, "create_request", fake_create)
    monkeypatch.setattr(streaming, "create_tracked_task", lambda coro, name=None: spawned.append(name) or coro.close())

    intent = SimpleNamespace(id="i1", arguments={"target": "平板", "reason": "看看"})
    ctx = SimpleNamespace(conv_id="c1", msg_id="m1", model_key="gemini")
    out = asyncio.run(streaming._execute_mobile_screen_check(intent, ctx))
    assert out["type"] == "screen_check_pending"
    assert out["target_device_id"] == "android_tab"
    assert out["target_device_name"] == "华为平板"
    assert spawned  # follow-up task scheduled


def test_execute_prefers_server_locked_target_over_model_marker(monkeypatch):
    resolved = []

    async def fake_resolve(target):
        resolved.append(target)
        return "android_tab", None

    monkeypatch.setattr(streaming, "_resolve_mobile_target", fake_resolve)
    fake_req = SimpleNamespace(
        request_id="r-locked",
        status="pending",
        target_device_id="android_tab",
        target_device_name="华为平板",
        reason="看看",
    )
    monkeypatch.setattr(
        streaming.mobile_screen_service,
        "create_request",
        lambda **_kwargs: _async_return(fake_req),
    )
    monkeypatch.setattr(
        streaming,
        "create_tracked_task",
        lambda coro, name=None: coro.close(),
    )

    intent = SimpleNamespace(
        id="i-locked",
        arguments={"target": "另一台设备", "reason": "看看"},
    )
    ctx = SimpleNamespace(
        conv_id="c1",
        msg_id="m1",
        model_key="gemini",
        metadata={"mobile_target_device_id": "android_tab"},
    )
    out = asyncio.run(streaming._execute_mobile_screen_check(intent, ctx))

    assert resolved == ["android_tab"]
    assert out["target_device_id"] == "android_tab"


def _async_return(value):
    async def _coro():
        return value
    return _coro()


# ── chat-side parse chain (postprocess) ─────────────────

def test_postprocess_yields_mobile_intent_and_strips_marker():
    from app.chat.postprocess import PostProcessor

    processor = PostProcessor()
    result = asyncio.run(processor.process(
        "好的，我看看[MOBILE_SCREEN_CHECK:华为平板|看看你在干嘛]",
        conv_id="conv_ms",
    ))
    mobile = [i for i in result.tool_intents if i.tool_name == "mobile.screen_check"]
    assert len(mobile) == 1
    assert mobile[0].arguments["target"] == "华为平板"
    assert "MOBILE_SCREEN_CHECK" not in result.content  # marker stripped from visible reply


# ── sentinel auto-line strip helper ─────────────────────

def test_sentinel_strip_mobile_screen_check_commands():
    from app.sentinel.core_wake_orchestrator import _strip_mobile_screen_check_commands

    content, checks = _strip_mobile_screen_check_commands(
        "嗯[MOBILE_SCREEN_CHECK:华为平板|看看]在干嘛[MOBILE_SCREEN_CHECK:|随便看看]")
    assert checks == [
        {"target": "华为平板", "reason": "看看"},
        {"target": "", "reason": "随便看看"},
    ]
    assert "MOBILE_SCREEN_CHECK" not in content


def test_sentinel_strip_returns_empty_when_no_marker():
    from app.sentinel.core_wake_orchestrator import _strip_mobile_screen_check_commands

    content, checks = _strip_mobile_screen_check_commands("普通回复，没有标记")
    assert checks == []
    assert content == "普通回复，没有标记"
