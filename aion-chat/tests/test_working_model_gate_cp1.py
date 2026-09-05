import asyncio
import hashlib
import json
from pathlib import Path

import pytest

import ai_providers
import config
from app.working_model import gate


def test_settings_migration_adds_an_independent_working_model_gate_slot():
    data = {
        "endpoints": [{"id": "sf", "name": "sf", "type": "openai"}],
        "slots": {
            "sentinel": {"endpoint": "sf", "model": "custom-sentinel"},
            "memory_digest": {"endpoint": "sf", "model": "custom-digest"},
            "asr": {
                "endpoint": "sf",
                "model": "custom-asr",
                "path": "/audio/transcriptions",
            },
        },
        "user_models": {},
    }

    changed = config._ensure_endpoints_and_slots(data)

    assert changed is True
    assert data["slots"]["sentinel"]["model"] == "custom-sentinel"
    assert data["slots"]["memory_digest"]["model"] == "custom-digest"
    assert data["slots"]["harness_tool"] == {
        "endpoint": "sf",
        "model": "custom-sentinel",
    }
    assert data["slots"]["ring_touch_translator"] == {
        "endpoint": "sf",
        "model": "custom-sentinel",
    }
    assert data["slots"]["working_model_gate"] == {
        "endpoint": "sf",
        "model": config.DEFAULT_WORKING_MODEL_GATE_MODEL,
    }
    assert data["slots"]["relational_card_generation"] == {
        "endpoint": "sf",
        "model": config.DEFAULT_RELATIONAL_CARD_GENERATION_MODEL,
    }


def test_settings_migration_preserves_a_custom_gate_slot(monkeypatch):
    monkeypatch.delenv("AION_SILICONFLOW_KEY", raising=False)
    data = {
        "endpoints": [{"id": "custom", "name": "custom", "type": "openai"}],
        "slots": {
            "sentinel": {"endpoint": "custom", "model": "sentinel-a"},
            "harness_tool": {"endpoint": "custom", "model": "harness-a"},
            "ring_touch_translator": {
                "endpoint": "custom",
                "model": "ring-a",
                "enable_thinking": False,
            },
            "memory_digest": {"endpoint": "custom", "model": "digest-b"},
            "working_model_gate": {"endpoint": "custom", "model": "gate-c"},
            "relational_card_generation": {
                "endpoint": "custom",
                "model": "custom-card-model",
            },
            "presence_renderer": {"endpoint": "custom", "model": "renderer-e"},
            "presence_image": {"endpoint": "custom", "model": "image-f"},
            "vision_summary": {"endpoint": "", "model": "glm-4.6v-flash", "enabled": False},
            "asr": {"endpoint": "custom", "model": "asr-d"},
        },
        "presence_image_slot_migration_v1": True,
        "user_models": {},
        "screen_capture_enabled": False,
        "mobile_screen_capture_enabled": False,
        "smart_ring_touch_enabled": False,
        "smart_ring_name_prefix": "AIZO",
        "smart_ring_keep_connected": False,
        "smart_ring_quiet_hours_enabled": False,
        "smart_ring_quiet_hours_start": "00:00",
        "smart_ring_quiet_hours_end": "08:00",
        "mock_devices_enabled": False,
    }

    changed = config._ensure_endpoints_and_slots(data)

    assert changed is False
    assert data["slots"]["working_model_gate"] == {
        "endpoint": "custom",
        "model": "gate-c",
    }
    assert data["slots"]["harness_tool"] == {
        "endpoint": "custom",
        "model": "harness-a",
    }
    assert data["slots"]["ring_touch_translator"] == {
        "endpoint": "custom",
        "model": "ring-a",
        "enable_thinking": False,
    }
    assert data["slots"]["relational_card_generation"] == {
        "endpoint": "custom",
        "model": "custom-card-model",
    }


def test_settings_migration_repairs_only_an_auto_generated_incompatible_gate_slot(
    monkeypatch,
):
    monkeypatch.delenv("AION_SILICONFLOW_KEY", raising=False)
    data = {
        "siliconflow_key": "legacy-secret",
        "endpoints": [
            {
                "id": "vertex",
                "name": "Vertex",
                "type": "vertex",
                "base_url": "https://aiplatform.googleapis.com/v1/projects/example",
            }
        ],
        "slots": {
            "sentinel": {"endpoint": "vertex", "model": "sentinel-a"},
            "memory_digest": {"endpoint": "vertex", "model": "digest-b"},
            "working_model_gate": {
                "endpoint": "vertex",
                "model": config.DEFAULT_WORKING_MODEL_GATE_MODEL,
            },
            "asr": {"endpoint": "vertex", "model": "asr-d"},
        },
        "user_models": {},
    }

    changed = config._ensure_endpoints_and_slots(data)

    assert changed is True
    assert data["slots"]["working_model_gate"] == {
        "endpoint": "sf",
        "model": config.DEFAULT_WORKING_MODEL_GATE_MODEL,
    }
    assert data["slots"]["sentinel"] == {
        "endpoint": "vertex",
        "model": "sentinel-a",
    }
    sf = next(endpoint for endpoint in data["endpoints"] if endpoint["id"] == "sf")
    assert sf["type"] == "openai"
    assert sf["base_url"] == "https://api.siliconflow.cn/v1"


def test_siliconflow_endpoint_can_use_legacy_credential_without_copying_it(monkeypatch):
    stored_endpoint = {
        "id": "sf",
        "name": "SiliconFlow",
        "type": "openai",
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key": "",
    }
    monkeypatch.delenv("AION_ENDPOINT_SF_KEY", raising=False)
    monkeypatch.delenv("AION_SILICONFLOW_KEY", raising=False)
    monkeypatch.setattr(
        config,
        "SETTINGS",
        {"endpoints": [stored_endpoint], "siliconflow_key": "legacy-secret"},
    )

    resolved = config.get_endpoint("sf")

    assert resolved["api_key"] == "legacy-secret"
    assert stored_endpoint["api_key"] == ""


def test_gate_messages_keep_all_untrusted_inputs_in_one_json_payload():
    messages = gate.build_working_model_gate_messages(
        statement="她会为了自主权牺牲便利。",
        source="用户说：我想把数据留在自己机器上。",
        latest_user_message="我想把数据留在自己机器上。",
    )

    assert messages[0] == {
        "role": "system",
        "content": gate.WORKING_MODEL_GATE_SYSTEM_PROMPT,
    }
    assert messages[1]["role"] == "user"
    assert json.loads(messages[1]["content"]) == {
        "statement": "她会为了自主权牺牲便利。",
        "source": "用户说：我想把数据留在自己机器上。",
        "latest_user_message": "我想把数据留在自己机器上。",
    }


@pytest.mark.parametrize(
    "raw_output",
    [
        "",
        "memory",
        "```json\n{\"route\":\"memory\",\"reason\":\"事件\"}\n```",
        "[]",
        '{"route":"other","reason":"x"}',
        '{"route":"memory","reason":""}',
        '{"route":"memory"}',
        '{"route":"memory","reason":"事件","extra":true}',
        '{"route":"memory","route":"reject","reason":"重复键"}',
    ],
)
def test_gate_parser_rejects_every_output_outside_the_exact_wire_contract(raw_output):
    with pytest.raises(gate.WorkingModelGateParseError):
        gate.parse_working_model_gate_output(raw_output)


def test_gate_parser_accepts_only_a_valid_route_and_nonempty_reason():
    assert gate.parse_working_model_gate_output(
        ' {"route":"working_model","reason":"这是跨情境理解。"} '
    ) == {
        "route": "working_model",
        "reason": "这是跨情境理解。",
    }


def test_gate_runner_calls_injected_provider_once_and_records_contract_fields(monkeypatch):
    calls = []

    async def fake_provider(messages):
        calls.append(messages)
        return '{"route":"memory","reason":"这是一次具体事件。"}'

    monkeypatch.setattr(
        gate,
        "get_slot",
        lambda _name: {"model": "configured-gate", "endpoint": {}, "extras": {}},
    )
    ticks = iter((10.0, 10.125))
    result = asyncio.run(
        gate.run_working_model_gate(
            statement="她今天删掉了 X 库。",
            source="用户说：我今天终于把那个库删了。",
            latest_user_message="我今天终于把那个库删了。",
            provider=fake_provider,
            clock=lambda: next(ticks),
        )
    )

    assert len(calls) == 1
    assert result.to_dict() == {
        "route": "memory",
        "reason": "这是一次具体事件。",
        "failure_code": None,
        "model": "configured-gate",
        "prompt_version": gate.WORKING_MODEL_GATE_PROMPT_VERSION,
        "latency_ms": 125,
    }
    assert "raw_output" not in result.to_dict()


@pytest.mark.parametrize(
    ("provider_result", "raises", "failure_code"),
    [
        ("", False, "provider_failed"),
        ("not-json", False, "parse_failed"),
        (None, True, "provider_failed"),
    ],
)
def test_gate_runner_technical_failure_is_noop_and_never_retries(
    monkeypatch,
    provider_result,
    raises,
    failure_code,
):
    calls = 0

    async def fake_provider(_messages):
        nonlocal calls
        calls += 1
        if raises:
            raise RuntimeError("injected provider failure")
        return provider_result

    monkeypatch.setattr(
        gate,
        "get_slot",
        lambda _name: {"model": "configured-gate", "endpoint": {}, "extras": {}},
    )
    result = asyncio.run(
        gate.run_working_model_gate(
            statement="她讨厌 X 库。",
            source="用户说：这个库我真是受够了。",
            latest_user_message="这个库我真是受够了。",
            provider=fake_provider,
        )
    )

    assert calls == 1
    assert result.route == "noop"
    assert result.failure_code == failure_code
    assert result.reason == ""


def test_gate_runner_invalid_input_does_not_call_provider(monkeypatch):
    calls = 0

    async def fake_provider(_messages):
        nonlocal calls
        calls += 1
        return '{"route":"reject","reason":"x"}'

    monkeypatch.setattr(
        gate,
        "get_slot",
        lambda _name: {"model": "configured-gate", "endpoint": {}, "extras": {}},
    )
    result = asyncio.run(
        gate.run_working_model_gate(
            statement="",
            source="用户说：x",
            latest_user_message="x",
            provider=fake_provider,
        )
    )

    assert calls == 0
    assert result.route == "noop"
    assert result.failure_code == "invalid_input"


def test_gate_runner_default_adapter_uses_only_the_gate_slot(monkeypatch):
    calls = []

    monkeypatch.setattr(
        gate,
        "get_slot",
        lambda name: {
            "model": "deepseek-ai/DeepSeek-V4-Flash",
            "endpoint": {"id": "sf"},
            "extras": {},
        }
        if name == "working_model_gate"
        else None,
    )

    async def fake_call_slot_chat(slot_name, *, messages, **kwargs):
        calls.append((slot_name, messages, kwargs))
        return '{"route":"reject","reason":"出处与陈述无关。"}'

    monkeypatch.setattr(gate, "call_slot_chat", fake_call_slot_chat)
    result = asyncio.run(
        gate.run_working_model_gate(
            statement="她害怕承诺。",
            source="用户说：今天下雨。",
            latest_user_message="今天下雨。",
        )
    )

    assert result.route == "reject"
    assert result.model == "deepseek-ai/DeepSeek-V4-Flash"
    assert len(calls) == 1
    slot_name, _messages, kwargs = calls[0]
    assert slot_name == "working_model_gate"
    assert kwargs == {
        "expect_json": True,
        "timeout": gate.WORKING_MODEL_GATE_TIMEOUT_SEC,
        "temperature": gate.WORKING_MODEL_GATE_TEMPERATURE,
        "scope": "working_model:gate",
        "max_tokens": gate.WORKING_MODEL_GATE_MAX_TOKENS,
        "model_override": "deepseek-ai/DeepSeek-V4-Flash",
    }


def test_gate_runner_missing_slot_is_noop_without_provider_call(monkeypatch):
    monkeypatch.setattr(gate, "get_slot", lambda _name: None)

    result = asyncio.run(
        gate.run_working_model_gate(
            statement="她害怕承诺。",
            source="用户说：今天下雨。",
            latest_user_message="今天下雨。",
        )
    )

    assert result.route == "noop"
    assert result.failure_code == "slot_unconfigured"
    assert result.model == ""


def test_slot_chat_optionally_exposes_provider_usage_for_smoke_costing(monkeypatch):
    client_kwargs = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "choices": [{
                    "message": {"content": '{"route":"memory","reason":"x"}'},
                    "finish_reason": "length",
                }],
                "usage": {
                    "prompt_tokens": 123,
                    "completion_tokens": 17,
                    "total_tokens": 140,
                },
            }

    class FakeClient:
        def __init__(self, **kwargs):
            client_kwargs.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(
        ai_providers,
        "get_slot",
        lambda _name: {
            "endpoint": {
                "id": "fake",
                "name": "fake",
                "type": "openai",
                "base_url": "https://example.invalid/v1",
                "api_key": "secret",
            },
            "model": "fake-model",
            "extras": {},
        },
    )
    monkeypatch.setattr(ai_providers.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(
        ai_providers,
        "_finish_provider_call",
        lambda meta, _ctx, **_kwargs: {
            "elapsed_ms": 1,
            "request_id": "fake-request",
            "meta": dict(meta or {}),
        },
    )
    usage_meta = {}

    output = asyncio.run(
        ai_providers.call_slot_chat(
            "working_model_gate",
            messages=[{"role": "user", "content": "x"}],
            usage_meta=usage_meta,
        )
    )

    assert output == '{"route":"memory","reason":"x"}'
    assert usage_meta["prompt_tokens"] == 123
    assert usage_meta["completion_tokens"] == 17
    assert usage_meta["total_tokens"] == 140
    assert usage_meta["finish_reason"] == "length"
    assert client_kwargs["proxy"] is None
    assert client_kwargs["trust_env"] is False


def test_slot_chat_uses_only_the_explicit_aion_proxy(monkeypatch):
    client_kwargs = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {},
            }

    class FakeClient:
        def __init__(self, **kwargs):
            client_kwargs.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setenv("http_proxy", "http://ambient.invalid:8888")
    monkeypatch.setenv("https_proxy", "http://ambient.invalid:8888")
    monkeypatch.setenv("AION_OPENAI_PROXY", "http://explicit.invalid:7890")
    monkeypatch.setattr(
        ai_providers,
        "get_slot",
        lambda _name: {
            "endpoint": {
                "id": "fake",
                "name": "fake",
                "type": "openai",
                "base_url": "https://example.invalid/v1",
                "api_key": "secret",
            },
            "model": "fake-model",
            "extras": {},
        },
    )
    monkeypatch.setattr(ai_providers.httpx, "AsyncClient", FakeClient)

    output = asyncio.run(
        ai_providers.call_slot_chat(
            "working_model_gate",
            messages=[{"role": "user", "content": "x"}],
        )
    )

    assert output == "ok"
    assert client_kwargs["proxy"] == "http://explicit.invalid:7890"
    assert client_kwargs["trust_env"] is False


def test_frozen_cp1_slice_is_an_exact_subset_of_the_spike_1_cases():
    repo_root = Path(__file__).resolve().parents[2]
    source_path = repo_root / "experiments/working_model_gate_spike/cases_v1.json"
    frozen_path = (
        repo_root
        / "docs/planning/checkpoints/working_model_v2/artifacts/CP1_FROZEN_CASES.json"
    )
    if not source_path.exists() or not frozen_path.exists():
        pytest.skip("原始研究及冻结原话不随公开仓库发布；完整私有版本保留此来源断言")
    source_bytes = source_path.read_bytes()
    source = json.loads(source_bytes)
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    source_by_id = {case["id"]: case for case in source["cases"]}

    assert hashlib.sha256(source_bytes).hexdigest() == frozen["source_file_sha256"]
    assert len(frozen["cases"]) == frozen["case_count"] == 20
    assert [case["order"] for case in frozen["cases"]] == list(range(1, 21))
    assert len({case["id"] for case in frozen["cases"]}) == 20
    for case in frozen["cases"]:
        original = source_by_id[case["id"]]
        assert case["original_expected"] == original["expected"]
        for field in ("source", "latest_user_message", "statement", "stratum"):
            assert case[field] == original[field]

    actual_route_counts = {
        route: sum(case["expected_route"] == route for case in frozen["cases"])
        for route in ("reject", "memory", "working_model")
    }
    assert actual_route_counts == frozen["route_counts"]
