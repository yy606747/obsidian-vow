import pytest

from app.events import SideEffectGateway, SideEffectKind, SideEffectStatus


def test_side_effect_gateway_plans_known_effect_without_executing_it():
    gateway = SideEffectGateway()

    plan = gateway.plan(
        request_id="se_1",
        kind=SideEffectKind.WS_BROADCAST,
        source="sentinel",
        payload={"type": "debug"},
        reason="show diagnostic event",
    )
    payload = plan.to_dict()

    assert payload == {
        "request": {
            "id": "se_1",
            "kind": "ws.broadcast",
            "source": "sentinel",
            "payload": {"type": "debug"},
            "reason": "show diagnostic event",
            "metadata": {},
        },
        "status": "planned",
        "executor": "gateway_required",
        "message": "",
    }


def test_side_effect_gateway_can_reject_disallowed_effects():
    gateway = SideEffectGateway(allowed_kinds={SideEffectKind.MONITOR_LOG})

    plan = gateway.plan(
        request_id="se_2",
        kind=SideEffectKind.DEVICE_COMMAND,
        source="control",
        payload={"device_id": "mock_ring"},
    )

    assert plan.status is SideEffectStatus.REJECTED
    assert plan.message == "side_effect_kind_not_allowed"


def test_side_effect_gateway_rejects_unknown_kind_at_contract_boundary():
    gateway = SideEffectGateway()

    with pytest.raises(ValueError):
        gateway.plan(
            request_id="se_3",
            kind="unknown.effect",
            source="sentinel",
        )


def test_side_effect_gateway_catalog_covers_critical_effects():
    gateway = SideEffectGateway()

    assert gateway.allowed_kinds_payload() == [
        "core.wake",
        "device.command",
        "memory.write",
        "message.write",
        "monitor.log",
        "ws.broadcast",
    ]
