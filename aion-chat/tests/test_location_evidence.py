import pytest

from app.events import EvidenceLedger
from app.legacy_adapters.evidence import record_location_state


def test_location_state_evidence_has_structured_geofence_contract():
    ledger = EvidenceLedger(now=lambda: 2000.0)

    record = record_location_state(
        {
            "state": "outside",
            "old_state": "at_home",
            "state_changed": True,
            "distance_from_home": 610.0,
            "configured_enter_m": 400.0,
            "configured_exit_m": 560.0,
            "v2_state": {
                "place_id": None,
                "place_name": None,
                "place_kind": None,
                "last_fix_at": 1900.0,
                "state_updated_at": 1900.0,
                "accuracy_m": 35.0,
            },
        },
        ledger=ledger,
    )

    payload = record.to_dict(reference_time=2000.0)
    assert payload["kind"] == "location.state"
    assert payload["source"] == "location.v2"
    assert payload["payload"] == {
        "payload_schema": "location_geofence.v1",
        "event_type": "transition",
        "boundary_side": "outside",
        "geofence_direction": "inside_to_outside",
        "distance_m": 610.0,
        "accuracy_m": 35.0,
        "configured_enter_m": 400.0,
        "configured_exit_m": 560.0,
        "last_fix_at": 1900.0,
        "state_updated_at": 1900.0,
    }
    assert payload["metadata"] == {"contract": "location_geofence.v1"}


def test_location_geofence_evidence_refuses_to_guess_missing_actual_radii():
    with pytest.raises(ValueError, match="configured_enter_m"):
        record_location_state({
            "state": "at_home",
            "old_state": "unknown",
            "state_changed": False,
            "distance_from_home": 30,
            "v2_state": {
                "place_id": "home",
                "place_name": "家",
                "place_kind": "home",
                "last_fix_at": 1900,
                "state_updated_at": 1800,
                "accuracy_m": 20,
            },
        })
