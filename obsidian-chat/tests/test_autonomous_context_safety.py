import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import location
import sensing
import app.chat.autonomous_capabilities as autonomous_capabilities
from app.chat.autonomous_capabilities import build_autonomous_runtime_context
from app.context_delivery import ContextDeliveryProjection, CurrentContextItem
from app.context_delivery.renderer import render_context_delivery_projection
from app.location import LocationState
from app.self_wake import trigger as self_wake_trigger
from app.sentinel.attention import build_attention_snapshot
from app.sentinel.handoff import build_layer2_handoff
from app.sentinel.judgment import build_sentinel_judgment_messages


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "context_delivery_production_replays_2026_08_25.json"
)

LIGHT_DERIVED_TEXTS = (
    "黑暗",
    "昏暗",
    "室内灯光",
    "明亮室内",
    "户外强光",
    "手机在口袋/包里（光传感器被遮）",
    "手机静置（光传感器可能被遮）",
    "手机静置暗处（可能正面朝下/在包中/环境暗）",
)


@pytest.fixture(autouse=True)
def _cp3a_legacy_context_fallback(monkeypatch):
    monkeypatch.setattr(
        autonomous_capabilities,
        "load_ai_behavior",
        lambda: {"context_delivery_autonomous_enabled": False},
    )


def _production_cases():
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "context_delivery.production_replays.v1"
    return payload["cases"]


def _runtime_prompt(monkeypatch, case):
    monkeypatch.setattr(
        sensing,
        "read_recent_sensing",
        lambda _hours: list(case["sensing_entries"]),
    )
    monkeypatch.setattr(
        location,
        "load_location_config",
        lambda: dict(case["location_config"]),
    )
    monkeypatch.setattr(
        location,
        "load_location_status",
        lambda: dict(case["location_status"]),
    )
    monkeypatch.setattr(location.time, "time", lambda: float(case["now"]))
    return build_autonomous_runtime_context(
        now=float(case["now"]),
        last_user_ts=float(case["last_user_ts"]),
        capabilities=frozenset(),
        user_name="owner",
        ai_name="companion",
        heading="Self-Wake 实时状态",
    )


def _prepare_self_wake_provider_text(monkeypatch, case):
    async def load_target(_conv_id):
        return {
            "model_key": "test-model",
            "last_user_ts": float(case["last_user_ts"]),
        }

    async def history(*_args, **_kwargs):
        return SimpleNamespace(
            history=[{
                "role": "user",
                "content": case["recent_owner_statement"],
                "attachments": [],
            }],
            cap_idx=0,
            wb={"user_name": "owner", "ai_name": "AI"},
            model_key="test-model",
        )

    async def no_mobile(**_kwargs):
        return None

    async def no_capabilities(**_kwargs):
        return frozenset()

    async def no_vow():
        return "", ""

    monkeypatch.setattr(self_wake_trigger, "_load_target", load_target)
    monkeypatch.setattr(self_wake_trigger, "prepare_chat_history", history)
    monkeypatch.setattr(
        self_wake_trigger,
        "_autonomous_mobile_screen_target",
        no_mobile,
    )
    monkeypatch.setattr(
        self_wake_trigger,
        "resolve_autonomous_capabilities",
        no_capabilities,
    )
    monkeypatch.setattr(
        self_wake_trigger.vow_service,
        "load_vow_prompt_context",
        no_vow,
    )
    monkeypatch.setattr(
        self_wake_trigger,
        "build_writer_identity_snapshot",
        lambda *_args, **_kwargs: {"text": "identity"},
    )
    monkeypatch.setattr(
        self_wake_trigger,
        "working_model_v2_injection_enabled",
        lambda: False,
    )
    monkeypatch.setattr(
        self_wake_trigger,
        "load_ai_behavior",
        lambda: {"heart_whisper_prompt": ""},
    )

    prepared = asyncio.run(self_wake_trigger.prepare_self_wake_turn({
        "id": case["id"],
        "wake_at": float(case["now"]),
        "intent": case["intent"],
        "requested_capabilities_json": "[]",
        "origin": "relationship",
        "origin_ref": "fixture",
        "source": "chat",
        "conv_id": "conv",
        "source_turn_id": "source-turn",
        "owner_timezone": "UTC",
        "state": "consumed",
    }, now=float(case["now"])))
    return "\n".join(str(message.get("content") or "") for message in prepared.messages)


@pytest.mark.parametrize("case", _production_cases(), ids=lambda case: case["id"])
def test_production_shapes_reach_autonomous_prompt_without_proxy_overclaims(
    monkeypatch,
    case,
):
    prompt = _runtime_prompt(monkeypatch, case)

    assert "静止" in prompt
    assert "亮屏" in prompt
    assert "电量" in prompt
    assert "NJU-WLAN" not in prompt
    assert "lux" not in prompt.lower()
    assert "用户当前大概在家" not in prompt
    assert all(text not in prompt for text in LIGHT_DERIVED_TEXTS)
    assert "设备定位落在你们标注过的「家」范围里（配置半径约 400m）" in prompt
    assert "分不出宿舍、健身房还是教室" in prompt
    assert "最近亲口说的情况，永远压过设备信号" in prompt
    assert "骗你、撒谎、编故事，或者被你抓到了" in prompt
    assert "都只是当时的念头" in prompt


@pytest.mark.parametrize("case", _production_cases(), ids=lambda case: case["id"])
def test_production_shapes_are_safe_in_final_self_wake_provider_messages(
    monkeypatch,
    case,
):
    _runtime_prompt(monkeypatch, case)
    provider_text = _prepare_self_wake_provider_text(monkeypatch, case)

    assert case["recent_owner_statement"] in provider_text
    assert case["intent"] in provider_text
    assert "当时想做的事只是你自己安排的念头，不是她现在的状态" in provider_text
    assert "NJU-WLAN" not in provider_text
    assert "lux" not in provider_text.lower()
    assert "用户当前大概在家" not in provider_text
    assert all(text not in provider_text for text in LIGHT_DERIVED_TEXTS)
    assert "最近亲口说的情况，永远压过设备信号" in provider_text
    assert "骗你、撒谎、编故事，或者被你抓到了" in provider_text


@pytest.mark.parametrize(
    ("sensor_data", "preserved"),
    [
        ({"motion": "still", "light_lux": 1, "screen_on": True, "battery_pct": 80}, ("静止", "亮屏", "电量80%")),
        ({"motion": "walking", "light_lux": 1, "screen_on": False, "battery_pct": 79}, ("走动", "锁屏", "电量79%")),
        ({"motion": "still", "light_lux": 1, "screen_on": False, "charging": True, "battery_pct": 78}, ("静止", "锁屏", "电量78%充电中")),
        ({"motion": "unknown", "light_lux": 1, "screen_on": False, "battery_pct": 77}, ("未知", "锁屏", "电量77%")),
        ({"motion": "tilting", "light_lux": 3, "screen_on": False, "battery_pct": 76}, ("轻微晃动（状态不明）", "锁屏", "电量76%")),
    ],
)
def test_low_lux_branches_emit_no_light_text_without_dropping_device_facts(
    monkeypatch,
    sensor_data,
    preserved,
):
    monkeypatch.setattr(
        sensing,
        "read_recent_sensing",
        lambda _hours: [{"timestamp": 1000.0, "type": "sensor", "data": sensor_data}],
    )
    monkeypatch.setattr(location, "format_location_for_prompt", lambda: "")

    prompt = build_autonomous_runtime_context(
        now=1100.0,
        last_user_ts=900.0,
        capabilities=frozenset(),
        user_name="owner",
    )

    assert all(text not in prompt for text in LIGHT_DERIVED_TEXTS)
    assert "lux" not in prompt.lower()
    for text in preserved:
        assert text in prompt


def test_prompt_and_sentinel_location_paths_share_cautious_geofence_semantics(
    monkeypatch,
):
    state = LocationState("home", "家", "home", 1000.0, 900.0, 30.0)
    expected = (
        "设备定位落在你们标注过的「家」范围里（配置半径约 400m），"
        "定位精度约 30m；这个范围太大，分不出宿舍、健身房还是教室，"
        "也说明不了她在做什么，或者手机是不是在她身上。"
    )

    assert state.for_prompt(1100.0, geofence_radius_m=400.0) == expected
    assert state.for_sentinel(1100.0, geofence_radius_m=400.0) == expected

    payload = {
        "state": "at_home",
        "accuracy": 30.0,
        "updated_at": 1000.0,
        "v2_state": {
            "place_id": "home",
            "place_name": "家",
            "place_kind": "home",
            "last_fix_at": 1000.0,
            "state_updated_at": 900.0,
            "accuracy_m": 30.0,
        },
    }
    monkeypatch.setattr(
        location,
        "load_location_config",
        lambda: {"enabled": True, "home_threshold": 400},
    )
    monkeypatch.setattr(location, "load_location_status", lambda: dict(payload))
    monkeypatch.setattr(location.time, "time", lambda: 1100.0)

    assert location.format_location_for_prompt() == expected
    assert location.format_location_for_sentinel() == expected
    assert "用户当前大概在家" not in expected


@pytest.mark.parametrize(
    (
        "legacy_state",
        "state_changed_at",
        "v2_place_id",
        "v2_place_name",
        "v2_place_kind",
        "distance_m",
        "expected_label",
        "expected_state",
        "expected_transition",
    ),
    [
        (
            "outside",
            1090.0,
            None,
            None,
            None,
            450.0,
            "设备定位越出家围栏",
            "outside",
            "inside_to_outside",
        ),
        (
            "at_home",
            1090.0,
            "home",
            "家",
            "home",
            50.0,
            "设备定位进入家围栏",
            "inside",
            "outside_to_inside",
        ),
        (
            "outside",
            0.0,
            None,
            None,
            None,
            450.0,
            "设备定位在家围栏外（无新变化）",
            "outside",
            "none",
        ),
    ],
)
def test_sentinel_geofence_branches_stay_device_scoped_in_final_judgment_prompt(
    monkeypatch,
    legacy_state,
    state_changed_at,
    v2_place_id,
    v2_place_name,
    v2_place_kind,
    distance_m,
    expected_label,
    expected_state,
    expected_transition,
):
    now = 1100.0
    status = {
        "state": legacy_state,
        "accuracy": 30.0,
        "distance_from_home": distance_m,
        "updated_at": 1040.0,
        "state_changed_at": state_changed_at,
        "v2_state": {
            "place_id": v2_place_id,
            "place_name": v2_place_name,
            "place_kind": v2_place_kind,
            "last_fix_at": 1040.0,
            "state_updated_at": state_changed_at,
            "accuracy_m": 30.0,
        },
    }
    monkeypatch.setattr(
        location,
        "load_location_config",
        lambda: {"enabled": True, "home_threshold": 400},
    )
    monkeypatch.setattr(location, "load_location_status", lambda: dict(status))
    monkeypatch.setattr(location.time, "time", lambda: now)

    location_text = location.format_location_for_sentinel()
    snapshot = build_attention_snapshot({
        "reference_time": "2026-08-25T15:00:00+08:00",
        "raw_signals": [{
            "kind": "location.fix",
            "source": "legacy.location",
            "text": location_text,
        }],
        "recent_chat": ["Owner: 我在健身房练背。"],
    })
    handoff = build_layer2_handoff(snapshot)
    provider_text = "\n".join(
        message["content"]
        for message in build_sentinel_judgment_messages(
            handoff,
            context={"recent_chat": ["Owner: 我在健身房练背。"]},
        )
    )

    assert snapshot["world_state"] == {
        "device_geofence_state": expected_state,
        "device_geofence_transition": expected_transition,
    }
    assert snapshot["hypotheses"][0]["label"] == expected_label
    assert expected_label in provider_text
    assert "Owner: 我在健身房练背。" in provider_text
    assert "手机是不是在她身上" in provider_text
    assert "最近亲口说的情况，永远压过设备信号" in provider_text
    assert "刚离开家" not in provider_text
    assert "刚回到家" not in provider_text
    assert '"location_state": "outside"' not in provider_text
    assert '"location_state": "at_home"' not in provider_text


def _context_item(key, value, *, confidence=1.0):
    return CurrentContextItem(
        key=key,
        value=value,
        source="android.sensing",
        observed_at=1000.0,
        received_at=1001.0,
        freshness_sec=1.0,
        since_at=None,
        confidence=confidence,
    )


def test_shared_renderer_keeps_diagnostic_signals_internal():
    projection = ContextDeliveryProjection(
        generated_at=1010.0,
        observations=(
            _context_item("phone.screen", "on"),
            _context_item("phone.light_lux", 1200.0),
            _context_item("phone.wifi", "NJU-WLAN"),
        ),
        device_derived=(
            _context_item("phone.motion", "still", confidence=0.42),
        ),
    )

    rendered = render_context_delivery_projection(
        projection,
        user_name="owner",
        ai_name="companion",
        time_formatter=lambda _value: "15:00",
    )

    assert "手机报告屏幕亮起" in rendered
    assert "手机运动分类为 静止（设备端归纳）" in rendered
    assert "NJU-WLAN" not in rendered
    assert "lux" not in rendered.lower()
    assert "0.42" not in rendered
    assert "置信度" not in rendered


def test_cp3b_autonomous_context_uses_shared_renderer_once_without_legacy_reads(
    monkeypatch,
):
    shared = (
        "[设备与环境上下文]\n"
        "直接观测：\n"
        "- 15:00 手机报告屏幕亮起。"
    )
    monkeypatch.setattr(
        autonomous_capabilities,
        "load_ai_behavior",
        lambda: {"context_delivery_autonomous_enabled": True},
    )
    monkeypatch.setattr(
        autonomous_capabilities,
        "render_autonomous_context_delivery",
        lambda **kwargs: shared if kwargs["user_name"] == "阿玖" else "",
    )
    monkeypatch.setattr(
        sensing,
        "format_sensing_for_prompt",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy sensing must stay off")
        ),
    )
    monkeypatch.setattr(
        location,
        "format_location_for_prompt",
        lambda: (_ for _ in ()).throw(
            AssertionError("legacy location must stay off")
        ),
    )

    prompt = build_autonomous_runtime_context(
        now=1000.0,
        last_user_ts=900.0,
        capabilities=frozenset(),
        user_name="阿玖",
    )

    assert prompt.count("[设备与环境上下文]") == 1
    assert "体感信号：" not in prompt
    assert "位置：" not in prompt
    assert "距阿玖上次说话" in prompt
    assert "阿玖最近亲口说的情况" in prompt


def test_cp3b_autonomous_context_failure_does_not_revive_legacy_formatters(
    monkeypatch,
):
    monkeypatch.setattr(
        autonomous_capabilities,
        "load_ai_behavior",
        lambda: {"context_delivery_autonomous_enabled": True},
    )
    monkeypatch.setattr(
        autonomous_capabilities,
        "render_autonomous_context_delivery",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("projection failed")),
    )
    monkeypatch.setattr(
        sensing,
        "format_sensing_for_prompt",
        lambda **_kwargs: "LEGACY_SENSING_MUST_NOT_RETURN",
    )
    monkeypatch.setattr(
        location,
        "format_location_for_prompt",
        lambda: "LEGACY_LOCATION_MUST_NOT_RETURN",
    )

    prompt = build_autonomous_runtime_context(
        now=1000.0,
        capabilities=frozenset(),
        user_name="阿玖",
    )

    assert "LEGACY_SENSING_MUST_NOT_RETURN" not in prompt
    assert "LEGACY_LOCATION_MUST_NOT_RETURN" not in prompt
    assert "阿玖最近亲口说的情况" in prompt
