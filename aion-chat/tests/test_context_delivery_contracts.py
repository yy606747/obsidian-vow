import json
from pathlib import Path

import pytest

from app.context_delivery.contracts import (
    ContextDeliveryProjection,
    CurrentContextItem,
    LEGACY_SCHEMA_VERSION,
    RecentContextEvent,
    SCHEMA_VERSION,
)
from app.context_delivery.policy import (
    CONTEXT_KEY_POLICIES,
    LOCATION_ADDRESS_MAX_AGE_SEC,
    LOCATION_CURRENT_MAX_AGE_SEC,
    PC_CURRENT_MAX_AGE_SEC,
    PHONE_CURRENT_MAX_AGE_SEC,
)


FIXTURE = Path(__file__).parent / "fixtures" / "context_delivery_replay.json"


def test_projection_contract_round_trips_exact_v2_shape():
    projection = ContextDeliveryProjection(
        generated_at=1000.0,
        observations=(CurrentContextItem(
            key="phone.screen",
            value="on",
            source="android.sensing",
            observed_at=990.0,
            received_at=991.0,
            freshness_sec=10.0,
            since_at=None,
            confidence=1.0,
        ),),
        recent_events=(RecentContextEvent(
            key="phone.unlock",
            event="occurred",
            to_value="unlocked",
            observed_at=995.0,
            source="android.sensing",
        ),),
    )

    payload = projection.to_dict()

    assert payload["schema_version"] == SCHEMA_VERSION
    assert ContextDeliveryProjection.from_dict(payload).to_dict() == payload
    assert set(payload) == {
        "schema_version", "generated_at", "observations", "device_derived",
        "recent_events", "baseline_deviations", "availability", "metrics",
    }


def test_projection_reader_accepts_exact_v1_but_v1_rejects_v2_item_payload():
    legacy = ContextDeliveryProjection(
        schema_version=LEGACY_SCHEMA_VERSION,
        generated_at=1000,
        observations=(CurrentContextItem(
            key="location.place",
            value="家",
            source="location.v2",
            observed_at=990,
            received_at=991,
            freshness_sec=10,
        ),),
    ).to_dict()

    assert legacy["schema_version"] == LEGACY_SCHEMA_VERSION
    assert "payload" not in legacy["observations"][0]
    assert ContextDeliveryProjection.from_dict(legacy).to_dict() == legacy

    legacy["observations"][0]["payload"] = {
        "payload_schema": "location_geofence.v1",
    }
    with pytest.raises(ValueError, match="unknown current item fields"):
        ContextDeliveryProjection.from_dict(legacy)


def test_contracts_reject_unknown_fields_invalid_since_and_fake_transition():
    with pytest.raises(ValueError, match="unknown current item fields"):
        CurrentContextItem.from_dict({
            "key": "phone.screen", "value": "on", "source": "android.sensing",
            "observed_at": 10, "received_at": 10, "freshness_sec": 0,
            "confidence": 1, "since_at": None, "owner_activity": "working",
        })
    with pytest.raises(ValueError, match="since_at"):
        CurrentContextItem(
            "phone.screen", "on", "android.sensing", 10, 10, 0,
            since_at=11,
        )
    with pytest.raises(ValueError, match="must differ"):
        RecentContextEvent(
            "phone.screen", "transition", "on", 10, "android.sensing",
            from_value="on",
        )
    with pytest.raises(ValueError, match="observations must contain"):
        ContextDeliveryProjection(generated_at=10, observations=({"key": "phone.screen"},))
    with pytest.raises(ValueError, match="requires first_observed_at"):
        RecentContextEvent(
            "phone.notification",
            "occurred",
            "群聊 2",
            10,
            "android.sensing",
            occurrence_count=2,
        )


def test_aggregated_occurred_event_round_trips_optional_count_and_time_range():
    event = RecentContextEvent(
        key="phone.notification",
        event="occurred",
        to_value="群聊 10",
        observed_at=20,
        source="android.sensing",
        occurrence_count=10,
        first_observed_at=10,
    )

    payload = event.to_dict()

    assert payload["occurrence_count"] == 10
    assert payload["first_observed_at"] == 10.0
    assert RecentContextEvent.from_dict(payload) == event


def test_policy_freezes_authorities_and_per_source_freshness_without_activity_label():
    assert CONTEXT_KEY_POLICIES["phone.screen"].kinds == (
        "activity.app", "sensing.sensor",
    )
    assert CONTEXT_KEY_POLICIES["phone.motion"].confidence_field == "motion_confidence"
    assert CONTEXT_KEY_POLICIES["phone.screen"].max_age_sec == PHONE_CURRENT_MAX_AGE_SEC == 900
    assert CONTEXT_KEY_POLICIES["pc.state"].max_age_sec == PC_CURRENT_MAX_AGE_SEC == 300
    assert CONTEXT_KEY_POLICIES["location.place"].max_age_sec == LOCATION_CURRENT_MAX_AGE_SEC == 1800
    assert CONTEXT_KEY_POLICIES["location.address"].max_age_sec == LOCATION_ADDRESS_MAX_AGE_SEC == 1200
    assert not any("activity_state" in key or "primary_activity" in key for key in CONTEXT_KEY_POLICIES)


def test_replay_fixture_covers_delay_duplicate_future_and_low_motion_confidence():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    ids = {record["id"] for record in payload["records"]}

    assert {"screen-off-delayed", "screen-on-duplicate", "future"} <= ids
    assert any(record["confidence"] < 0.2 for record in payload["records"])
    assert payload["edge_cases"] == {
        "restart_empty": {"records": [], "expected_phone_missing": False},
        "disabled_location": {"enabled": False, "expected_availability": []},
        "low_coverage_baseline": {"coverage": 0.1, "expected_deviations": []},
    }
