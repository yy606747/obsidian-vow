import json
from pathlib import Path

from app.context_delivery import SourceStatus, build_context_delivery_projection
from app.events import EvidenceRecord


FIXTURE = Path(__file__).parent / "fixtures" / "context_delivery_replay.json"


def _record(**overrides):
    values = {
        "id": "ev",
        "kind": "sensing.sensor",
        "source": "android.sensing",
        "observed_at": 100.0,
        "received_at": 100.0,
        "confidence": 1.0,
        "payload": {},
        "metadata": {},
    }
    values.update(overrides)
    return EvidenceRecord(**values)


def _fixture_records():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return payload["reference_time"], [EvidenceRecord(**item) for item in payload["records"]]


def test_projection_uses_observed_time_deduplicates_and_scopes_motion_confidence():
    now, records = _fixture_records()

    projection = build_context_delivery_projection(records, reference_time=now)
    payload = projection.to_dict()
    current = {item["key"]: item for item in payload["observations"]}
    derived = {item["key"]: item for item in payload["device_derived"]}

    assert current["phone.screen"]["value"] == "on"
    assert current["phone.screen"]["observed_at"] == 920.0
    assert current["phone.screen"]["since_at"] == 900.0
    assert current["phone.screen"]["confidence"] == 1.0
    assert current["phone.battery"]["confidence"] == 1.0
    assert derived["phone.motion"]["value"] == "still"
    assert derived["phone.motion"]["confidence"] == 0.19
    assert current["mobile.owner-phone.foreground_app"]["value"] == "微信"
    assert payload["metrics"]["future_records_dropped"] == 1
    screen_events = [event for event in payload["recent_events"] if event["key"] == "phone.screen"]
    assert [(event["from_value"], event["to_value"]) for event in screen_events] == [
        ("off", "on"),
    ]


def test_same_periodic_state_has_unknown_since_and_no_transition():
    records = [
        _record(id="a", observed_at=800, received_at=800, payload={"screen_on": True}),
        _record(id="b", observed_at=900, received_at=900, payload={"screen_on": True}),
        _record(id="c", observed_at=950, received_at=950, payload={"screen_on": True}),
    ]

    projection = build_context_delivery_projection(records, reference_time=1000)
    screen = next(item for item in projection.observations if item.key == "phone.screen")

    assert screen.since_at is None
    assert not [event for event in projection.recent_events if event.key == "phone.screen"]


def test_screen_markers_never_become_foreground_apps_and_stale_current_disappears():
    records = [
        _record(
            id="screen-marker", kind="activity.app", source="android.activity",
            observed_at=990, received_at=990,
            payload={"device": "phone", "device_id": "p1", "app": "锁屏", "screen_state": "off"},
        ),
        _record(
            id="stale-app", kind="activity.app", source="android.activity",
            observed_at=1, received_at=1,
            payload={"device": "phone", "device_id": "p1", "app": "微信"},
        ),
        _record(
            id="hypothesis", kind="attention.hypothesis", source="sentinel.attention",
            observed_at=999, received_at=999, payload={"label": "low_confidence_sleep"},
        ),
    ]

    projection = build_context_delivery_projection(records, reference_time=1000)

    assert [(item.key, item.value) for item in projection.observations] == [
        ("phone.screen", "off"),
    ]
    assert "low_confidence_sleep" not in str(projection.to_dict())


def test_availability_requires_enabled_expected_source_and_never_invents_health_connect():
    statuses = [
        SourceStatus("pc.context", True, True, 300, observed_at=100),
        SourceStatus("location.v2", False, False, 1800),
        SourceStatus("android.sensing", True, True, 900, observed_at=None),
        SourceStatus("health_connect", False, False, 3600),
    ]

    projection = build_context_delivery_projection([], reference_time=1000, source_statuses=statuses)

    assert [(item.source, item.status) for item in projection.availability] == [
        ("android.sensing", "missing"),
        ("pc.context", "stale"),
    ]
    assert "health_connect" not in str(projection.to_dict())


def test_fresh_runtime_location_uses_named_region_without_coordinates():
    projection = build_context_delivery_projection(
        [],
        reference_time=1000,
        source_statuses=(SourceStatus(
            source="location.v2",
            enabled=True,
            expected_periodic=True,
            max_age_sec=1800,
            observed_at=980,
            received_at=985,
            since_at=900,
            values={"location.place": "家"},
        ),),
    )

    assert projection.observations[0].to_dict() == {
        "key": "location.place", "value": "家", "source": "location.v2",
        "observed_at": 980.0, "received_at": 985.0, "freshness_sec": 20.0,
        "since_at": 900.0, "confidence": 1.0,
    }


def test_location_address_and_geofence_share_one_observation_budget_slot():
    records = [
        _record(
            id="phone",
            observed_at=990,
            received_at=990,
            payload={
                "screen_on": True,
                "battery_pct": 80,
                "charging": False,
                "light_lux": 100,
                "wifi_ssid": "owner-wifi",
            },
        ),
        _record(
            id="mobile-app",
            kind="activity.app",
            source="android.activity",
            observed_at=990,
            received_at=990,
            payload={"device": "phone", "device_id": "p1", "app": "微信"},
        ),
        _record(
            id="pc",
            kind="activity.app",
            source="pc.activity",
            observed_at=990,
            received_at=990,
            payload={"device": "pc", "active_state": "active", "app": "VS Code"},
        ),
    ]
    statuses = (SourceStatus(
        source="location.v2",
        enabled=True,
        expected_periodic=True,
        max_age_sec=1800,
        observed_at=990,
        received_at=991,
        values={
            "location.place": "家",
            "location.address": "南京大学仙林校区",
        },
    ),)

    projection = build_context_delivery_projection(
        records,
        reference_time=1000,
        source_statuses=statuses,
    )
    keys = [item.key for item in projection.observations]
    semantic_slots = {
        "location.current" if key.startswith("location.") else key
        for key in keys
    }

    assert "location.place" in keys
    assert "location.address" in keys
    assert len(semantic_slots) == 8
    assert len(keys) == 9


def test_notification_flood_collapses_to_one_summary_without_evicting_transitions():
    records = [
        _record(
            id="home-1", kind="location.state", source="location.v2",
            observed_at=600, received_at=600,
            payload={"place_name": "home", "last_fix_at": 600, "state_updated_at": 600},
        ),
        _record(
            id="outside", kind="location.state", source="location.v2",
            observed_at=700, received_at=700,
            payload={"place_name": "outside", "last_fix_at": 700, "state_updated_at": 700},
        ),
        _record(
            id="home-2", kind="location.state", source="location.v2",
            observed_at=800, received_at=800,
            payload={"place_name": "home", "last_fix_at": 800, "state_updated_at": 800},
        ),
        _record(
            id="screen-off", observed_at=650, received_at=650,
            payload={"screen_on": False},
        ),
        _record(
            id="screen-on", observed_at=750, received_at=750,
            payload={"screen_on": True},
        ),
        *[
            _record(
                id=f"notification-{index}",
                kind="sensing.notification",
                observed_at=810 + index,
                received_at=810 + index,
                payload={"app": "群聊"},
            )
            for index in range(10)
        ],
    ]

    projection = build_context_delivery_projection(records, reference_time=1000)
    events = projection.recent_events
    notification = next(event for event in events if event.key == "phone.notification")

    assert [(event.key, event.from_value, event.to_value) for event in events] == [
        ("location.place", "home", "outside"),
        ("phone.screen", "off", "on"),
        ("location.place", "outside", "home"),
        ("phone.notification", None, "群聊 10"),
    ]
    assert notification.occurrence_count == 10
    assert notification.first_observed_at == 810
    assert notification.observed_at == 819


def test_projection_v2_keeps_geofence_current_and_transition_payloads_in_parallel():
    payload = {
        "payload_schema": "location_geofence.v1",
        "event_type": "transition",
        "geofence_direction": "inside_to_outside",
        "boundary_side": "outside",
        "distance_m": 610,
        "accuracy_m": 30,
        "configured_enter_m": 400,
        "configured_exit_m": 560,
        "last_fix_at": 990,
        "state_updated_at": 990,
    }
    projection = build_context_delivery_projection(
        [_record(
            id="left-home",
            kind="location.state",
            source="location.v2",
            observed_at=990,
            received_at=991,
            payload=payload,
        )],
        reference_time=1000,
    )

    serialized = projection.to_dict()
    current = next(item for item in serialized["observations"] if item["key"] == "location.place")
    transition = next(item for item in serialized["recent_events"] if item["key"] == "location.place")

    assert serialized["schema_version"] == "context_delivery_projection.v2"
    assert current["value"] == "unmatched"
    assert current["payload"]["event_type"] == "current"
    assert "geofence_direction" not in current["payload"]
    assert transition["from_value"] == "inside"
    assert transition["to_value"] == "outside"
    assert transition["payload"] == payload


def test_summon_record_uses_24_hour_policy_without_shortening_state_transitions():
    now = 20_000.0
    records = [
        _record(
            id="screen-off-old",
            observed_at=now - 8_000,
            received_at=now - 8_000,
            payload={"screen_on": False},
        ),
        _record(
            id="screen-on-old",
            observed_at=now - 7_000,
            received_at=now - 7_000,
            payload={"screen_on": True},
        ),
        _record(
            id="unlock-old",
            kind="sensing.unlock",
            observed_at=now - 4 * 3600,
            received_at=now - 4 * 3600,
        ),
        _record(
            id="summon-old",
            kind="presence.summon",
            source="presence.summon",
            observed_at=now - 4 * 3600,
            received_at=now - 4 * 3600,
        ),
    ]

    projection = build_context_delivery_projection(records, reference_time=now)
    keys = [event.key for event in projection.recent_events]

    assert "phone.screen" in keys
    assert "relationship.summon" in keys
    assert "phone.unlock" not in keys
    assert "phone.screen" not in {item.key for item in projection.observations}


def test_summon_group_preserves_all_click_times_and_latest_seven_other_events():
    now = 10_000.0
    records = [
        *[
            _record(
                id=f"summon-{index}",
                kind="presence.summon",
                source="presence.summon",
                observed_at=9_000 + index,
                received_at=9_000 + index,
            )
            for index in range(5)
        ],
        *[
            _record(
                id=f"unlock-{index}",
                kind="sensing.unlock",
                observed_at=9_100 + index,
                received_at=9_100 + index,
            )
            for index in range(8)
        ],
    ]

    projection = build_context_delivery_projection(records, reference_time=now)
    summons = [
        event for event in projection.recent_events
        if event.key == "relationship.summon"
    ]
    unlocks = [
        event for event in projection.recent_events
        if event.key == "phone.unlock"
    ]

    assert [event.observed_at for event in summons] == [9000, 9001, 9002, 9003, 9004]
    assert [event.observed_at for event in unlocks] == [9101, 9102, 9103, 9104, 9105, 9106, 9107]
