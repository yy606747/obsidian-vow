import asyncio
from copy import deepcopy
import json
import logging

import pytest

from app.scenes.compiler import SequenceValidationError, compile_intent, validate_sequence
from app.scenes.config import validate_config
from app.scenes.hardware import compiler_capabilities, devices
from app.scenes.prompts import compiler_messages
from test_scenes import Models, harness, settle, start


def sequence(device, **values):
    caps = device["capabilities"]
    return {"action": "replace", "steps": [{
        "duration_ms": caps.get("min_step_ms", 1000),
        "values": {**{k: b["min"] for k, b in caps["channels"].items()}, **values},
    }]}


@pytest.mark.parametrize("bench,single", [(False, False), (True, False), (True, True)])
def test_effective_descriptions_follow_exact_ranges_without_touching_core_introductions(bench, single):
    profiles = devices(bench=bench, single_channel=single)
    for device in profiles:
        caps = device["capabilities"]
        assert caps["description"] == compiler_capabilities(device["driver"], caps)["description"]
        for name, bounds in caps["channels"].items():
            assert f"{name}：整数 {bounds['min']}～{bounds['max']}" in caps["description"]
        if bench and device["driver"] in ("muse_ble", "sosexy_ble"):
            assert "～100" not in caps["description"] and "～9" not in caps["description"]
        # Old persisted protocol prose cannot contradict effective restrictions.
        expected_description = caps["description"]
        intro = device["description"] = "OWNER_DEVICE_FACTS"
        device["capabilities"]["description"] = "STALE_RANGE_0_TO_999"
        saved = deepcopy(device)
        result = validate_config({"enabled": True, "devices": [device]})["devices"][0]
        assert result["description"] == intro
        assert result["capabilities"]["description"] == expected_description
        assert result["capabilities"]["channels"] == caps["channels"]
        assert device == saved


def test_default_catalog_opens_the_full_existing_protocol_ranges_and_rejects_unknown_values():
    muse, sosexy, sk30, sk40, cx = devices()
    assert [b["max"] for b in muse["capabilities"]["channels"].values()] == [9, 9]
    assert [b["max"] for b in sosexy["capabilities"]["channels"].values()] == [4, 100, 4, 100, 4, 100]
    assert [b["max"] for b in cx["capabilities"]["channels"].values()] == [11, 10, 5, 10]
    for sk in (sk30, sk40):
        assert [b["max"] for b in sk["capabilities"]["channels"].values()] == [1, 100, 100]
    for d in (muse, sosexy, sk30, sk40, cx):
        upper = {k: b["max"] for k, b in d["capabilities"]["channels"].items()}
        raw = sequence(d, **upper)
        assert validate_sequence(raw, d["capabilities"]) == raw
        for key in upper:
            bad = deepcopy(raw)
            bad["steps"][0]["values"][key] += 1
            with pytest.raises(SequenceValidationError, match="range_invalid"):
                validate_sequence(bad, d["capabilities"])


@pytest.mark.parametrize("value,code", [
    (101, "range_invalid"), (-1, "range_invalid"), (10**2000, "range_invalid"),
    (True, "number_required"), (None, "number_required"), ("8", "number_required"),
    ("PRIVATE_OUTPUT_PROSE", "number_required"), ({"PRIVATE_KEY": "PRIVATE_VALUE"}, "number_required"),
    (float("nan"), "nonfinite"), (float("inf"), "nonfinite"),
    (float("-inf"), "nonfinite"), (8.5, "integer_required"),
])
def test_numeric_diagnostics_distinguish_type_range_finite_and_integer_without_leaking_prose(value, code):
    d = devices()[1]
    raw = sequence(d)
    raw["steps"].append(deepcopy(raw["steps"][0]))
    raw["steps"][1]["values"]["suction_speed"] = value
    with pytest.raises(SequenceValidationError, match=code) as caught:
        validate_sequence(raw, d["capabilities"])
    detail = caught.value.detail
    assert detail["field"] == "suction_speed" and detail["step"] == 1
    assert detail["expected"] == {"min": 0, "max": 100, "integer": True}
    assert detail["actual_type"] == type(value).__name__
    text = json.dumps(detail, allow_nan=False)
    assert "PRIVATE" not in text and len(text) < 600
    if type(value) in (str, dict):
        assert "actual" not in detail and detail["actual_length"] == len(value)
    if value == 101:
        assert detail["actual"] == 101


@pytest.mark.parametrize("mutate,code,field", [
    (lambda r: r.update(action=["PRIVATE_ACTION"]), "action_invalid", "action"),
    (lambda r: r.update(steps="PRIVATE_STEPS"), "steps_invalid", "steps"),
    (lambda r: r["steps"].__setitem__(0, "PRIVATE_STEP"), "step_invalid", "step"),
    (lambda r: r["steps"][0].update(duration_ms="PRIVATE_DURATION"), "duration_invalid", "duration_ms"),
    (lambda r: r["steps"][0]["values"].update(PRIVATE_KEY="PRIVATE_VALUE"), "channels_invalid", "values"),
])
def test_structural_failures_have_bounded_diagnostics(mutate, code, field):
    d = devices()[0]
    raw = sequence(d)
    mutate(raw)
    with pytest.raises(SequenceValidationError, match=code) as caught:
        validate_sequence(raw, d["capabilities"])
    assert caught.value.detail["field"] == field
    assert "PRIVATE" not in json.dumps(caught.value.detail)


def test_json_failure_does_not_capture_the_model_body():
    with pytest.raises(SequenceValidationError, match="json_invalid") as caught:
        validate_sequence('{"PRIVATE_INTENT"', devices()[0]["capabilities"])
    assert caught.value.detail["actual_type"] == "str"
    assert "PRIVATE" not in json.dumps(caught.value.detail)


def test_disabled_outputs_and_stop_modes_stay_distinct():
    d = devices(bench=True, single_channel=True)[1]
    raw = sequence(d)
    assert validate_sequence(raw, d["capabilities"]) == raw
    for field in ("electro_speed", "suction_speed"):
        bad = sequence(d, **{field: 1})
        with pytest.raises(SequenceValidationError, match="range_invalid"):
            validate_sequence(bad, d["capabilities"])
    with pytest.raises(SequenceValidationError, match="range_invalid") as caught:
        validate_sequence(sequence(d, electro_mode=0), d["capabilities"])
    assert caught.value.detail["expected"]["min"] == 1


def test_partial_step_transition_does_not_override_transport_minimum():
    d = devices()[0]
    old = sequence(d, vib_pattern=9, thrust_pattern=8)
    messages = compiler_messages(intent="只停伸缩，震动沿用", capabilities=d["capabilities"],
                                 previous=old, position={"playing": True, "elapsed_ms": 2300})
    assert "旧步骤剩余时间不足最小步长" in messages[0]["content"]
    assert "可以调整衔接相位" in messages[0]["content"]
    bad = sequence(d, vib_pattern=9)
    bad["steps"][0]["duration_ms"] = 2700
    with pytest.raises(SequenceValidationError, match="duration_invalid") as caught:
        validate_sequence(bad, d["capabilities"])
    assert caught.value.detail["actual"] == 2700
    assert caught.value.detail["expected"]["min"] == 5000


def test_compile_normalizes_old_capability_prose_preserves_previous_and_logs_one_safe_failure(caplog):
    d = devices()[1]
    d["capabilities"]["description"] = "STALE_RANGE_0_TO_999"
    original = deepcopy(d)
    previous = sequence(d, vibration_speed=70, suction_speed=60)
    position = {"playing": True, "elapsed_ms": 2500}
    models = Models()
    async def failed(messages):
        payload = json.loads(messages[1]["content"])
        assert "STALE_RANGE" not in payload["capabilities"]["description"]
        assert payload["previous_sequence"] == previous
        assert payload["playback_position"] == position
        return sequence(d, suction_speed=101)
    models.compile_hook = failed
    with caplog.at_level(logging.WARNING, logger="app.scenes.compiler"):
        with pytest.raises(SequenceValidationError):
            asyncio.run(compile_intent(models, intent="PRIVATE_INTENT", device=d,
                                       previous=previous, position=position))
    assert d == original and len(models.compilations) == 1  # No implicit retry.
    line = next(r.getMessage() for r in caplog.records if r.name == "app.scenes.compiler")
    detail = json.loads(line.split("scene compile rejected: ", 1)[1])
    assert detail["device_id"] == "sosexy" and detail["driver"] == "sosexy_ble"
    assert detail["actual"] == 101 and detail["field"] == "suction_speed"
    assert "PRIVATE" not in caplog.text


def test_invalid_compilation_still_pauses_all_outputs_without_exposing_details_on_wire(tmp_path, caplog):
    async def scenario():
        models = Models()
        async with harness(tmp_path, models=models) as (service, _, events):
            initial = await start(service)
            await settle(service)
            assert any(service.devices.outputs.values())
            before = len(models.compilations)
            async def failed(_):
                return {"action": "replace", "steps": [{"duration_ms": 1000, "values": {"level": "PRIVATE_BAD_VALUE"}}]}
            models.compile_hook = failed
            models.outputs.append("<toy1>PRIVATE_INTENT</toy1>")
            await service.message(initial["id"], owner_id="owner1", text="本场普通反馈")
            await settle(service)
            assert service.active.row["status"] == "paused"
            assert service.active.row["reason"] == "compile_failed:scene_sequence_number_required"
            assert len(models.compilations) == before + 1
            assert not any(service.devices.outputs.values())
            assert service.active.due_at is None
            assert "PRIVATE" not in json.dumps(events)
    with caplog.at_level(logging.WARNING, logger="app.scenes.compiler"):
        asyncio.run(scenario())
    assert '"device_id": "mock_a"' in caplog.text
    assert '"actual_type": "str"' in caplog.text
    assert "PRIVATE" not in caplog.text
