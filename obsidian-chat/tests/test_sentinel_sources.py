import pytest

from app.sentinel import build_attention_snapshot
from app.sentinel.sources import EvidenceRecord, adapt_replay_input


def _left_geofence_payload():
    return {
        "payload_schema": "location_geofence.v1",
        "event_type": "transition",
        "geofence_direction": "inside_to_outside",
        "boundary_side": "outside",
        "distance_m": 610,
        "accuracy_m": 30,
        "configured_enter_m": 400,
        "configured_exit_m": 560,
    }


def test_replay_adapter_normalizes_raw_signals_to_evidence_records():
    bundle = adapt_replay_input({
        "reference_time": "2026-05-14T10:25:00+08:00",
        "raw_signals": [
            {"kind": "chat.recent", "source": "chat", "text": "用户20分钟前说在开会"},
            {"kind": "sensing.screen", "source": "android.sensing", "text": "手机频繁点亮"},
        ],
        "recent_chat": ["我在开会，等下说。"],
    })

    assert bundle.reference_time == "2026-05-14T10:25:00+08:00"
    assert bundle.recent_chat == ("我在开会，等下说。",)
    assert all(isinstance(record, EvidenceRecord) for record in bundle.evidence)
    assert bundle.evidence[0].kind == "chat.recent"
    assert "busy_declared" in bundle.evidence[0].tags
    assert "phone_active" in bundle.evidence[1].tags
    assert "source_family:chat" in bundle.evidence[0].tags
    assert "source_family:sensing" in bundle.evidence[1].tags


def test_replay_adapter_keeps_disabled_sources_visible():
    bundle = adapt_replay_input({
        "raw_signals": [
            {
                "kind": "camera.evidence",
                "source": "camera.adapter",
                "text": "摄像头来源关闭，未提供画面",
            },
        ],
        "recent_chat": [],
    })

    record = bundle.evidence[0]
    assert record.kind == "camera.evidence"
    assert record.source == "camera.adapter"
    assert "camera_disabled" in record.tags
    assert "source_family:camera" in record.tags
    assert "source_status:disabled" in record.tags


@pytest.mark.parametrize(
    "text",
    [
        "状态从 at_home 变为 outside，距离家约1550米，GPS精度约500米。",
        (
            "手机的定位从你们标注的「家」范围里走到了范围外"
            "（距围栏中心约450米，配置半径约400米，定位精度约30米）；"
            "这只是手机越过了那条边界，说明不了她去了哪。"
        ),
    ],
)
def test_replay_adapter_tags_location_transition_text(text):
    bundle = adapt_replay_input({
        "raw_signals": [
            {
                "kind": "location.fix",
                "source": "legacy.location",
                "text": text,
            },
        ],
        "recent_chat": [],
    })

    record = bundle.evidence[0]
    assert "left_home_transition" in record.tags
    assert "source_family:location" in record.tags


def test_structured_location_tags_ignore_rewritten_or_contradictory_prompt_text():
    payloads = []
    snapshots = []
    for text in (
        "手机越过边界。",
        "完全换一种写法，甚至故意写成回到范围里。",
    ):
        input_payload = {
            "reference_time": "2026-08-25T10:00:00-07:00",
            "raw_signals": [{
                "kind": "location.geofence",
                "source": "context_delivery.projection",
                "text": text,
                "payload": _left_geofence_payload(),
            }],
            "recent_chat": [],
        }
        bundle = adapt_replay_input(input_payload)
        payloads.append(bundle.evidence[0])
        snapshots.append(build_attention_snapshot(input_payload))

    assert all("left_home_transition" in record.tags for record in payloads)
    assert all("return_home_transition" not in record.tags for record in payloads)
    assert snapshots[0]["world_state"] == snapshots[1]["world_state"]
    assert snapshots[0]["attention_targets"] == snapshots[1]["attention_targets"]
    assert snapshots[0]["hypotheses"] == snapshots[1]["hypotheses"]
    assert "610米" in snapshots[0]["compact_text"]
    assert "退出阈值560米" in snapshots[0]["compact_text"]


def test_declared_geofence_schema_never_falls_back_to_text_when_fields_are_missing():
    payload = _left_geofence_payload()
    payload.pop("distance_m")

    with pytest.raises(ValueError, match="distance_m"):
        adapt_replay_input({
            "raw_signals": [{
                "kind": "location.geofence",
                "source": "context_delivery.projection",
                "text": "围栏内变为围栏外，距离家约610米。",
                "payload": payload,
            }],
            "recent_chat": [],
        })


def test_structured_current_geofence_uses_boundary_side_not_sentence():
    payload = _left_geofence_payload()
    payload.update({"event_type": "current", "boundary_side": "outside"})
    payload.pop("geofence_direction")

    bundle = adapt_replay_input({
        "raw_signals": [{
            "kind": "location.geofence",
            "source": "context_delivery.projection",
            "text": "location_state=at_home，没有别的变化。",
            "payload": payload,
        }],
        "recent_chat": [],
    })
    tags = bundle.evidence[0].tags

    assert {"outside_continuous", "no_location_change"} <= tags
    assert "at_home" not in tags


def test_replay_adapter_fails_loud_on_malformed_signal():
    with pytest.raises(ValueError, match="raw_signals\\[0\\] requires text"):
        adapt_replay_input({
            "raw_signals": [
                {"kind": "sensing.screen", "source": "android.sensing"},
            ],
            "recent_chat": [],
        })


def test_replay_adapter_rejects_layer2_decision_fields():
    with pytest.raises(ValueError, match="forbidden decision fields: \\['call_core'\\]"):
        adapt_replay_input({
            "raw_signals": [
                {
                    "kind": "chat.recent",
                    "source": "chat",
                    "text": "最近刚正常聊天",
                    "call_core": True,
                },
            ],
            "recent_chat": [],
        })


def test_replay_adapter_rejects_top_level_decision_fields():
    with pytest.raises(ValueError, match="input contains forbidden decision fields"):
        adapt_replay_input({
            "raw_signals": [],
            "recent_chat": [],
            "wake_intent": True,
        })
