import asyncio
import pytest

from sentinel_v2_full_wake_acceptance import (
    SENTINEL_V2_FULL_WAKE_ACCEPTANCE_SCHEMA_VERSION,
    run_acceptance_scenarios,
)


@pytest.fixture(autouse=True)
def _empty_vow_context(monkeypatch):
    """誓约层（Phase 2）在本管道经局部 import 读取 vow_service 单例；
    既有用例用空桩隔离，不读真实库。"""

    class _Stub:
        async def load_vow_prompt_context(self):
            return "", ""

    monkeypatch.setattr("app.vows.service.vow_service", _Stub())

    async def empty_working_model_context():
        return "", ""

    monkeypatch.setattr(
        "sentinel_core_wake_adapters.load_sentinel_working_model_prompt_context",
        empty_working_model_context,
    )



def test_full_wake_acceptance_harness_passes_all_primary_scenarios():
    result = asyncio.run(run_acceptance_scenarios())

    assert result["schema_version"] == SENTINEL_V2_FULL_WAKE_ACCEPTANCE_SCHEMA_VERSION
    assert result["runtime_mode"] == "local_acceptance"
    assert result["production_side_effects"] == []
    assert result["fallback_used"] is False
    assert result["metrics"] == {"total": 5, "passed": 5, "failed": 0}
    assert [scenario["name"] for scenario in result["scenarios"]] == [
        "success",
        "toy_command",
        "core_empty",
        "gate_block",
        "provider_failure",
    ]
    assert all(scenario["passed"] for scenario in result["scenarios"])
    assert all(scenario["slot_call_kinds"] == ["v2"] for scenario in result["scenarios"])
    assert all("legacy" not in scenario["slot_call_kinds"] for scenario in result["scenarios"])


def test_full_wake_acceptance_harness_can_run_selected_scenario():
    result = asyncio.run(run_acceptance_scenarios(["toy_command"]))

    assert result["metrics"] == {"total": 1, "passed": 1, "failed": 0}
    scenario = result["scenarios"][0]
    assert scenario["name"] == "toy_command"
    assert scenario["toy_commands"] == []
    assert scenario["logged_toy_commands"] == ["2"]
    assert scenario["toy_command_delivery"]["status"] == "gateway_rejected"
    assert scenario["toy_command_delivery"]["reason"] == "capability_not_frozen"
    assert "[TOY:" not in scenario["assistant_text"]
