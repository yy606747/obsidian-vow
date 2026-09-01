import asyncio
import json

from app.context_delivery import build_context_delivery_projection
from app.events import EvidenceLedger
from app.legacy_adapters.evidence import record_location_state
from app.sentinel import build_sentinel_runtime_context, run_sentinel_chain_dry_run


def _projection_and_geofence_payload():
    ledger = EvidenceLedger(now=lambda: 1000)
    record_location_state({
        "state": "outside",
        "old_state": "at_home",
        "state_changed": True,
        "distance_from_home": 610,
        "configured_enter_m": 400,
        "configured_exit_m": 560,
        "v2_state": {
            "place_id": None,
            "place_name": None,
            "place_kind": None,
            "last_fix_at": 990,
            "state_updated_at": 990,
            "accuracy_m": 30,
        },
    }, ledger=ledger)
    projection = build_context_delivery_projection(
        ledger.recent(reference_time=1000),
        reference_time=1000,
    )
    event_payload = next(
        item.payload
        for item in projection.recent_events
        if item.key == "location.place"
    )
    return projection, event_payload


def _wake_judgment():
    return {
        "monitoringlog": "手机越过了配置围栏边界，具体人身状态未知。",
        "summary": "设备边界变化值得主脑看一眼，但不能判断她去了哪里。",
        "score": 8,
        "confidence": 0.9,
        "wake_intent": True,
        "call_core": True,
        "core_reason": "设备定位发生边界变化，适合轻轻确认一下。",
        "restraint_reason": "",
        "uncertainty": "手机是否在她身上、她具体在哪都未知。",
        "suggested_next_check_sec": 600,
        "tone_hint": "自然一点，别审问",
    }


def test_schema_v2_delivers_one_structured_geofence_fact_to_all_three_consumers():
    projection, geofence_payload = _projection_and_geofence_payload()
    runtime_context = build_sentinel_runtime_context({
        "user_name": "阿玖",
        "ai_name": "Aion",
        "context_projection": projection.to_dict(),
    })
    captured = {}

    async def provider(messages):
        captured["messages"] = messages
        return json.dumps(_wake_judgment(), ensure_ascii=False)

    result = asyncio.run(run_sentinel_chain_dry_run(
        {
            "reference_time": "2026-08-25T10:00:00-07:00",
            "raw_signals": [{
                "kind": "location.geofence",
                "source": "context_delivery.projection",
                "text": "措辞可以随便改；结构字段才参与判断。",
                "payload": geofence_payload,
            }],
            "recent_chat": [],
        },
        judgment_provider=provider,
        runtime_context=runtime_context,
        core_execution_context={
            "conv_id": "conv_sentinel",
            "model_key": "mock-model",
            "user_name": "阿玖",
            "ai_name": "Aion",
        },
        request_id="cp3b-schema-v2",
    ))

    judgment_prompt = "\n".join(message["content"] for message in captured["messages"])
    core_prompt = result["core_wake_preflight"]["core_request"]["core_prompt"]
    attention_text = result["attention_snapshot"]["compact_text"]
    wake_projection = result["wake_package"]["context"]["context_projection"]

    assert runtime_context["schema_version"] == "sentinel_runtime_context.v2"
    assert runtime_context["judgment_context"]["context_projection"] == runtime_context["wake_context"]["context_projection"]
    assert result["wake_package"]["schema_version"] == "sentinel_core_wake_package.v2"
    assert wake_projection == projection.to_dict()
    for text in (judgment_prompt, core_prompt, attention_text):
        assert "610" in text
        assert "400" in text
        assert "560" in text
        assert "30" in text
