from app.context_delivery.shadow_rules import (
    ContextTriggerShadowRuleEngine,
    EVALUATION_MATCHED,
    EVALUATION_NOT_MATCHED,
    EVALUATION_UNAVAILABLE,
    RULE_FIRST_INTERACTION_AFTER_LONG_SILENCE,
    RULE_LOCATION_REGION_TRANSITION,
)
from app.events import EvidenceRecord
from app.sentinel import (
    GATE_REASON_CHAT_COOLDOWN,
    GATE_REASON_CLEAR_SLEEP,
    GATE_REASON_DEVICE_GATE,
    GATE_REASON_LOW_CONFIDENCE,
    GATE_REASON_QUIET_HOURS,
    GATE_REASON_WAKE_COOLDOWN,
    WAKE_BOUNDARY_RESULT_SCHEMA_VERSION,
    evaluate_wake_boundaries,
)
import pytest


def _record(
    event_id: str,
    *,
    kind: str,
    observed_at: float,
    payload=None,
    source: str = "android.sensing",
    confidence: float = 1.0,
):
    return EvidenceRecord(
        id=event_id,
        kind=kind,
        source=source,
        observed_at=observed_at,
        received_at=observed_at + 0.2,
        confidence=confidence,
        payload=payload or {},
    )


def _location_transition(event_id="location-1", observed_at=1000.0):
    return _record(
        event_id,
        kind="location.state",
        source="location.v2",
        observed_at=observed_at,
        payload={
            "payload_schema": "location_geofence.v1",
            "event_type": "transition",
            "geofence_direction": "inside_to_outside",
            "boundary_side": "outside",
            "distance_m": 570.0,
            "accuracy_m": 30.0,
            "configured_enter_m": 400.0,
            "configured_exit_m": 560.0,
            "place_id": "home",
            "place_name": "家",
        },
    )


def test_location_transition_is_the_only_location_shadow_candidate():
    engine = ContextTriggerShadowRuleEngine()

    result = engine.evaluate(_location_transition())

    assert result.rule == RULE_LOCATION_REGION_TRANSITION
    assert result.evaluation_status == EVALUATION_MATCHED
    assert result.evaluation_reason == "geofence_transition_inside_to_outside"
    assert result.features["configured_enter_m"] == 400.0
    assert result.features["configured_exit_m"] == 560.0

    current = _location_transition("location-current")
    current = EvidenceRecord(
        **{
            **current.__dict__,
            "payload": {
                **current.payload,
                "event_type": "current",
            },
        }
    )
    current.payload.pop("geofence_direction", None)
    assert engine.evaluate(current) is None


def test_interaction_rule_records_missing_below_and_at_least_threshold():
    engine = ContextTriggerShadowRuleEngine(long_silence_sec=3600)

    first = engine.evaluate(_record("unlock-1", kind="sensing.unlock", observed_at=1000))
    soon = engine.evaluate(_record("unlock-2", kind="sensing.unlock", observed_at=1200))
    late = engine.evaluate(_record("unlock-3", kind="sensing.unlock", observed_at=4800))

    assert first.rule == RULE_FIRST_INTERACTION_AFTER_LONG_SILENCE
    assert first.evaluation_status == EVALUATION_UNAVAILABLE
    assert first.evaluation_reason == "missing_previous_interaction"
    assert first.features["interaction_gap_sec"] is None
    assert soon.evaluation_status == EVALUATION_NOT_MATCHED
    assert soon.evaluation_reason == "gap_below_3600"
    assert soon.features["interaction_gap_sec"] == 200
    assert late.evaluation_status == EVALUATION_MATCHED
    assert late.evaluation_reason == "gap_at_least_3600"
    assert late.features["interaction_gap_sec"] == 3600


def test_restart_does_not_turn_missing_interaction_history_into_long_silence():
    before_restart = ContextTriggerShadowRuleEngine()
    before_restart.evaluate(_record("unlock-old", kind="sensing.unlock", observed_at=1000))

    after_restart = ContextTriggerShadowRuleEngine()
    result = after_restart.evaluate(
        _record("unlock-new", kind="sensing.unlock", observed_at=20_000)
    )

    assert result.evaluation_status == EVALUATION_UNAVAILABLE
    assert result.evaluation_reason == "missing_previous_interaction"


def test_screen_requires_confirmed_off_to_on_and_notification_is_ignored():
    engine = ContextTriggerShadowRuleEngine()
    assert engine.evaluate(
        _record("screen-on-unknown", kind="sensing.sensor", observed_at=1000, payload={"screen_on": True})
    ) is None
    assert engine.evaluate(
        _record("screen-off", kind="sensing.sensor", observed_at=1100, payload={"screen_on": False})
    ) is None

    transition = engine.evaluate(
        _record("screen-on", kind="sensing.sensor", observed_at=1200, payload={"screen_on": True})
    )

    assert transition.evaluation_status == EVALUATION_UNAVAILABLE
    assert transition.features["interaction_event"] == "screen_off_to_on"
    assert engine.evaluate(
        _record("screen-on-repeat", kind="sensing.sensor", observed_at=1300, payload={"screen_on": True})
    ) is None
    assert engine.evaluate(
        _record("notification", kind="sensing.notification", observed_at=1400, payload={"app": "群聊"})
    ) is None


def test_motion_confidence_does_not_lower_screen_interaction_confidence():
    engine = ContextTriggerShadowRuleEngine()
    engine.evaluate(
        _record("screen-off", kind="sensing.sensor", observed_at=1000, payload={"screen_on": False})
    )

    result = engine.evaluate(
        _record(
            "screen-on",
            kind="sensing.sensor",
            observed_at=1100,
            payload={"screen_on": True, "motion_confidence": 0},
            confidence=0.0,
        )
    )

    assert result.event_confidence == 1.0


def test_shared_wake_boundaries_are_judgment_free_and_use_normal_cooldown():
    result = evaluate_wake_boundaries(
        context={
            "quiet_hours_active": True,
            "clear_sleep": True,
            "last_user_message_age_sec": 30,
            "last_wake_age_sec": 700,
            "device_effect_requested": True,
            "device_effect_allowed": False,
        },
        event_confidence=0.2,
    )

    assert result == {
        "schema_version": WAKE_BOUNDARY_RESULT_SCHEMA_VERSION,
        "runtime_mode": "dry_run",
        "status": "blocked",
        "wake_allowed": False,
        "blocked_reasons": [
            GATE_REASON_QUIET_HOURS,
            GATE_REASON_CLEAR_SLEEP,
            GATE_REASON_CHAT_COOLDOWN,
            GATE_REASON_WAKE_COOLDOWN,
            GATE_REASON_LOW_CONFIDENCE,
            GATE_REASON_DEVICE_GATE,
        ],
        "side_effects": [],
    }
    assert "score" not in result
    assert "judgment" not in result


def _mobile_screen(event_id, at, state, *, device_id="phone-1", device_type="phone"):
    return _record(event_id, kind="activity.app", source="android.activity", observed_at=at,
                   payload={"device_id": device_id, "device_type": device_type, "device": device_type, "screen_state": state})


@pytest.mark.parametrize("unlock_first", [False, True])
def test_identified_screen_and_legacy_unlock_share_one_phone_history(unlock_first):
    engine = ContextTriggerShadowRuleEngine()
    engine.evaluate(_mobile_screen("on-initial", 1000, "on"))
    engine.evaluate(_record("unlock-initial", kind="sensing.unlock", observed_at=1001))
    engine.evaluate(_mobile_screen("off", 1100, "off"))
    screen = _mobile_screen("on", 5000 if not unlock_first else 5002, "on")
    unlock = _record("unlock", kind="sensing.unlock", observed_at=5000 if unlock_first else 5002)
    results = [engine.evaluate(row) for row in ([unlock, screen] if unlock_first else [screen, unlock])]

    matched = [row for row in results if row is not None and row.evaluation_status == EVALUATION_MATCHED]
    assert len(matched) == 1
    assert matched[0].features["device_key"] == "phone-1"
    assert matched[0].features["interaction_gap_sec"] == 3999


def test_phone_identity_arriving_later_migrates_previous_interaction_not_reset_it():
    engine = ContextTriggerShadowRuleEngine()
    engine.evaluate(_record("old", kind="sensing.unlock", observed_at=1000))
    engine.evaluate(_mobile_screen("off", 1100, "off"))
    result = engine.evaluate(_mobile_screen("on", 5000, "on"))
    assert result.evaluation_status == EVALUATION_MATCHED
    assert result.features["interaction_gap_sec"] == 4000


def test_tablet_never_inherits_anonymous_phone_interactions():
    engine = ContextTriggerShadowRuleEngine()
    engine.evaluate(_record("phone-old", kind="sensing.unlock", observed_at=1000))
    engine.evaluate(_mobile_screen("tablet-off", 1100, "off", device_id="tablet", device_type="tablet"))
    result = engine.evaluate(_mobile_screen("tablet-on", 5000, "on", device_id="tablet", device_type="tablet"))
    assert result.evaluation_status == EVALUATION_UNAVAILABLE
    assert result.features["device_key"] == "tablet"
    phone = engine.evaluate(_record("phone-new", kind="sensing.unlock", observed_at=5002))
    assert phone.evaluation_status == EVALUATION_MATCHED
    assert phone.features["device_key"] == "android-phone"


def test_two_known_phones_make_anonymous_sensing_unavailable_not_a_shared_history():
    engine = ContextTriggerShadowRuleEngine()
    engine.evaluate(_mobile_screen("a", 1000, "off", device_id="phone-a"))
    engine.evaluate(_mobile_screen("b", 1001, "off", device_id="phone-b"))
    result = engine.evaluate(_record("anonymous", kind="sensing.unlock", observed_at=5000))
    assert result.evaluation_status == EVALUATION_UNAVAILABLE
    assert result.evaluation_reason == "ambiguous_device_identity"
    assert engine.evaluate(_mobile_screen("a-on", 5001, "on", device_id="phone-a")).evaluation_status == EVALUATION_UNAVAILABLE
    assert engine.evaluate(_mobile_screen("b-on", 5002, "on", device_id="phone-b")).evaluation_status == EVALUATION_UNAVAILABLE


def test_delayed_screen_off_does_not_fabricate_a_new_transition():
    engine = ContextTriggerShadowRuleEngine()
    engine.evaluate(_mobile_screen("on", 1000, "on"))
    assert engine.evaluate(_mobile_screen("late-off", 900, "off")) is None
    assert engine.evaluate(_mobile_screen("on-again", 5000, "on")) is None
