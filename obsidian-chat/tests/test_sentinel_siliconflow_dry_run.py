import asyncio
import json
from pathlib import Path

from sentinel_siliconflow_dry_run import (
    RUNTIME_MODE_PROVIDER_DRY_RUN,
    SENTINEL_SILICONFLOW_DRY_RUN_SCHEMA_VERSION,
    build_markdown,
    resolve_siliconflow_endpoint,
    run_live_judgment_cases,
)


ROOT = Path(__file__).resolve().parents[1]
ATTENTION_CASES_PATH = ROOT / "app" / "sentinel" / "eval_cases.json"
JUDGMENT_CASES_PATH = ROOT / "app" / "sentinel" / "judgment_eval_cases.json"


def _attention_cases():
    return json.loads(ATTENTION_CASES_PATH.read_text(encoding="utf-8"))


def _judgment_cases():
    return json.loads(JUDGMENT_CASES_PATH.read_text(encoding="utf-8"))


def test_resolve_siliconflow_endpoint_uses_env_key_without_exposing_it(monkeypatch):
    monkeypatch.setenv("OBSIDIAN_SILICONFLOW_KEY", "sk-test-secret-value")

    resolved = resolve_siliconflow_endpoint(model="Pro/test-model")

    assert resolved["endpoint"]["api_key"] == "sk-test-secret-value"
    assert resolved["public"]["has_api_key"] is True
    assert resolved["public"]["model"] == "Pro/test-model"
    assert resolved["public"]["base_url"] == "https://api.siliconflow.cn/v1"
    assert "api_key" not in resolved["public"]
    assert "env:OBSIDIAN_SILICONFLOW_KEY" in resolved["public"]["source"]


def test_live_judgment_cases_with_fake_provider_are_provider_dry_run_only():
    case = _judgment_cases()[0]

    async def provider(messages):
        prompt = "\n".join(message["content"] for message in messages)
        assert "debug_trace" not in prompt
        assert "raw_signal_count" not in prompt
        return json.dumps(case["fixture_model_output"], ensure_ascii=False)

    result = asyncio.run(run_live_judgment_cases(
        [case],
        attention_cases=_attention_cases(),
        provider=provider,
        provider_info={"endpoint_name": "fake-sf", "model": "fake-model"},
    ))

    assert result["schema_version"] == SENTINEL_SILICONFLOW_DRY_RUN_SCHEMA_VERSION
    assert result["runtime_mode"] == RUNTIME_MODE_PROVIDER_DRY_RUN
    assert result["side_effects"] == ["siliconflow_chat_completion"]
    assert result["production_side_effects"] == []
    assert result["metrics"]["total"] == 1
    assert result["metrics"]["unique_cases"] == 1
    assert result["metrics"]["repeat"] == 1
    assert result["metrics"]["failed"] == 0
    assert result["metrics"]["case_pass_rates"][case["id"]]["pass_rate"] == 1.0
    assert result["records"][0]["ok"] is True
    assert result["records"][0]["trace"]["production_side_effects"] == []
    assert result["records"][0]["trace"]["chain"]["side_effects"] == []


def test_live_judgment_cases_use_provider_expect_without_weakening_fixture_expect():
    case = dict(_judgment_cases()[0])
    case["expect"] = {"wake_intent": True}
    case["provider_expect"] = {
        "wake_intent": False,
        "call_core": False,
        "core_reason_empty": True,
    }

    async def provider(_messages):
        return json.dumps(case["fixture_model_output"], ensure_ascii=False)

    result = asyncio.run(run_live_judgment_cases(
        [case],
        attention_cases=_attention_cases(),
        provider=provider,
        provider_info={"endpoint_name": "fake-sf", "model": "fake-model"},
    ))

    assert result["metrics"]["failed"] == 0
    assert result["records"][0]["expectation_mode"] == "provider_expect"
    assert result["records"][0]["trace"]["expectation_mode"] == "provider_expect"


def test_live_judgment_cases_can_repeat_selected_cases():
    case = _judgment_cases()[0]
    calls = 0

    async def provider(_messages):
        nonlocal calls
        calls += 1
        return json.dumps(case["fixture_model_output"], ensure_ascii=False)

    result = asyncio.run(run_live_judgment_cases(
        [case],
        attention_cases=_attention_cases(),
        provider=provider,
        provider_info={"endpoint_name": "fake-sf", "model": "fake-model"},
        repeat=2,
    ))

    assert calls == 2
    assert result["metrics"]["total"] == 2
    assert result["metrics"]["unique_cases"] == 1
    assert result["metrics"]["repeat"] == 2
    assert result["metrics"]["case_pass_rates"][case["id"]] == {
        "total": 2,
        "passed": 2,
        "failed": 0,
        "pass_rate": 1.0,
    }
    assert [record["iteration"] for record in result["records"]] == [1, 2]
    assert result["records"][0]["trace"]["trace_id"].endswith(":r1")
    assert result["records"][1]["trace"]["trace_id"].endswith(":r2")


def test_live_judgment_cases_reject_non_positive_repeat():
    case = _judgment_cases()[0]

    async def provider(_messages):
        return json.dumps(case["fixture_model_output"], ensure_ascii=False)

    try:
        asyncio.run(run_live_judgment_cases(
            [case],
            attention_cases=_attention_cases(),
            provider=provider,
            provider_info={"endpoint_name": "fake-sf", "model": "fake-model"},
            repeat=0,
        ))
    except ValueError as exc:
        assert "repeat must be positive" in str(exc)
    else:
        raise AssertionError("expected repeat=0 to fail loud")


def test_live_judgment_cases_fail_loud_on_bad_provider_output():
    case = _judgment_cases()[0]

    async def provider(_messages):
        return "not json"

    result = asyncio.run(run_live_judgment_cases(
        [case],
        attention_cases=_attention_cases(),
        provider=provider,
        provider_info={"endpoint_name": "fake-sf", "model": "fake-model"},
    ))

    assert result["metrics"]["failed"] == 1
    assert result["records"][0]["ok"] is False
    assert result["records"][0]["actual"]["wake_intent"] is None
    assert result["records"][0]["trace"]["chain"] == {}
    assert result["records"][0]["trace"]["raw_output"] == "not json"
    assert "provider_dry_run_failed: ValueError" in result["records"][0]["failures"][0]


def test_live_dry_run_markdown_is_compact():
    case = _judgment_cases()[0]

    async def provider(_messages):
        return json.dumps(case["fixture_model_output"], ensure_ascii=False)

    result = asyncio.run(run_live_judgment_cases(
        [case],
        attention_cases=_attention_cases(),
        provider=provider,
        provider_info={"endpoint_name": "fake-sf", "model": "fake-model"},
    ))
    markdown = build_markdown(result)

    assert "# Sentinel SiliconFlow Dry Run" in markdown
    assert "fake-model" in markdown
    assert case["id"] in markdown
    assert "unique_cases" in markdown
    assert "expectation_mode" in markdown
    assert "production_side_effects" in markdown
