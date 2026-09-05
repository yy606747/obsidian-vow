from app.events import EvidenceLedger
from app.legacy_adapters.evidence import record_location_state
from context_delivery_runtime_readers import (
    read_context_delivery_projection,
    read_context_delivery_projection_async,
)


def _disabled_location():
    return {"enabled": False}


def test_runtime_reader_combines_filtered_evidence_pc_and_location_status():
    ledger = EvidenceLedger(now=lambda: 1000)
    ledger.record(
        kind="sensing.sensor", source="android.sensing", observed_at=990,
        payload={"screen_on": True, "heart_rate": 88},
    )
    ledger.record(
        kind="sensing.biometric", source="android.sensing", observed_at=995,
        payload={"heart_rate": 90},
    )

    projection = read_context_delivery_projection(
        reference_time=1000,
        ledger=ledger,
        activity_enabled_loader=lambda: True,
        pc_status_loader=lambda _now: {
            "last_seen_at": 999, "observed_at": 998, "active_state": "idle",
            "foreground_app": "VS Code",
        },
        location_config_loader=lambda: {"enabled": True},
        location_status_loader=lambda: {
            "updated_at": 997, "heartbeat_received_at": 999,
            "v2_state": {
                "place_id": "home", "place_name": "家", "place_kind": "home",
                "last_fix_at": 997, "state_updated_at": 900,
            },
        },
    )
    current = {item.key: item.value for item in projection.observations}

    assert current == {
        "location.place": "家",
        "pc.foreground_app": "VS Code",
        "pc.state": "idle",
        "phone.screen": "on",
    }
    assert "heart_rate" not in str(projection.to_dict())
    assert projection.metrics["input_records"] == 1


def test_runtime_reader_post_restart_does_not_call_unknown_phone_or_health_data_missing():
    ledger = EvidenceLedger(now=lambda: 1000)

    projection = read_context_delivery_projection(
        reference_time=1000,
        ledger=ledger,
        activity_enabled_loader=lambda: False,
        pc_status_loader=lambda _now: {},
        location_config_loader=lambda: {"enabled": False},
        location_status_loader=lambda: {},
    )

    assert projection.is_empty
    assert "android.sensing" not in str(projection.to_dict())
    assert "health_connect" not in str(projection.to_dict())


def test_runtime_reader_reports_only_explicitly_enabled_periodic_source_as_missing():
    projection = read_context_delivery_projection(
        reference_time=1000,
        ledger=EvidenceLedger(now=lambda: 1000),
        activity_enabled_loader=lambda: True,
        pc_status_loader=lambda _now: {
            "last_seen_at": None, "active_state": "offline",
        },
        location_config_loader=lambda: {"enabled": True},
        location_status_loader=lambda: {"updated_at": 0, "v2_state": {}},
    )

    assert [(item.source, item.status) for item in projection.availability] == [
        ("location.v2", "missing"),
        ("pc.context", "missing"),
    ]


def test_runtime_reader_keeps_fresh_address_beside_home_geofence():
    projection = read_context_delivery_projection(
        reference_time=2000,
        ledger=EvidenceLedger(now=lambda: 2000),
        activity_enabled_loader=lambda: False,
        pc_status_loader=lambda _now: {},
        location_config_loader=lambda: {"enabled": True},
        location_status_loader=lambda: {
            "address": "南京大学仙林校区",
            "address_updated_at": 1900,
            "heartbeat_received_at": 1995,
            "v2_state": {
                "place_id": "home",
                "place_name": "家",
                "place_kind": "home",
                "last_fix_at": 1990,
                "state_updated_at": 1500,
            },
        },
    )

    current = {item.key: item for item in projection.observations}
    assert current["location.place"].value == "家"
    assert current["location.address"].value == "南京大学仙林校区"
    assert current["location.address"].observed_at == 1900


def test_runtime_reader_drops_stale_address_but_keeps_geofence_worded_as_range():
    projection = read_context_delivery_projection(
        reference_time=3000,
        ledger=EvidenceLedger(now=lambda: 3000),
        activity_enabled_loader=lambda: False,
        pc_status_loader=lambda _now: {},
        location_config_loader=lambda: {"enabled": True},
        location_status_loader=lambda: {
            "address": "旧的校园地址",
            "address_updated_at": 1700,
            "heartbeat_received_at": 2995,
            "v2_state": {
                "place_id": "home",
                "place_name": "家",
                "place_kind": "home",
                "last_fix_at": 2990,
                "state_updated_at": 1500,
            },
        },
    )

    current = {item.key: item.value for item in projection.observations}
    assert current == {"location.place": "家"}
    assert "旧的校园地址" not in str(projection.to_dict())


def test_runtime_reader_calls_fresh_unknown_fix_unmatched_instead_of_outside_place():
    projection = read_context_delivery_projection(
        reference_time=1000,
        ledger=EvidenceLedger(now=lambda: 1000),
        activity_enabled_loader=lambda: False,
        pc_status_loader=lambda _now: {},
        location_config_loader=lambda: {"enabled": True},
        location_status_loader=lambda: {
            "heartbeat_received_at": 999,
            "v2_state": {
                "place_id": None,
                "place_name": None,
                "place_kind": None,
                "last_fix_at": 995,
                "state_updated_at": 900,
            },
        },
    )

    assert [(item.key, item.value) for item in projection.observations] == [
        ("location.place", "unmatched"),
    ]


def test_runtime_reader_keeps_structured_geofence_evidence_ahead_of_status_fallback():
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
            "last_fix_at": 995,
            "state_updated_at": 995,
            "accuracy_m": 30,
        },
    }, ledger=ledger)

    projection = read_context_delivery_projection(
        reference_time=1000,
        ledger=ledger,
        activity_enabled_loader=lambda: False,
        pc_status_loader=lambda _now: {},
        location_config_loader=lambda: {"enabled": True},
        location_status_loader=lambda: {
            "heartbeat_received_at": 999,
            "v2_state": {
                "place_id": None,
                "place_name": None,
                "place_kind": None,
                "last_fix_at": 995,
                "state_updated_at": 995,
            },
        },
    )

    current = next(item for item in projection.observations if item.key == "location.place")
    event = next(item for item in projection.recent_events if item.key == "location.place")
    assert current.payload["boundary_side"] == "outside"
    assert current.payload["configured_exit_m"] == 560
    assert event.payload["geofence_direction"] == "inside_to_outside"


def test_async_runtime_reader_adds_durable_summons_and_excludes_current_uuid():
    class Summons:
        async def recent_facts(self, **kwargs):
            assert kwargs["conv_id"] == "conv"
            assert kwargs["exclude_summon_id"] == "current"
            return [
                {"summon_id": "older", "conv_id": "conv", "occurred_at": 1000 - 4 * 3600},
            ]

    projection = __import__("asyncio").run(read_context_delivery_projection_async(
        conv_id="conv",
        reference_time=1000,
        exclude_summon_id="current",
        summon_repository=Summons(),
        ledger=EvidenceLedger(now=lambda: 1000),
        activity_enabled_loader=lambda: False,
        pc_status_loader=lambda _now: {},
        location_config_loader=lambda: {"enabled": False},
        location_status_loader=lambda: {},
    ))

    assert [(event.key, event.observed_at) for event in projection.recent_events] == [
        ("relationship.summon", 1000 - 4 * 3600),
    ]
