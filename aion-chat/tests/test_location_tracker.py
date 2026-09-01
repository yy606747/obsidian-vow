from app.location import LocationFix, LocationTracker, Place, match_place


def _places():
    return [
        Place("dorm", "宿舍", "dorm", 30.0, 120.0, 100.0, 220.0),
        Place("classroom", "教学楼", "classroom", 30.002, 120.0, 100.0, 220.0),
        Place("campus", "校园", "campus", 30.0, 120.0, 900.0, 1200.0),
    ]


def _fix(lat, lng, t, accuracy=30.0):
    return LocationFix(lat=lat, lng=lng, accuracy_m=accuracy, received_at=t)


def test_nested_campus_does_not_swallow_more_specific_place():
    places = _places()

    assert match_place(30.0, 120.0, places, "campus") == "dorm"


def test_cold_start_one_fix_establishes_state():
    tracker = LocationTracker()

    changed = tracker.process_fix(_fix(30.0, 120.0, 1000.0), _places())

    assert changed is tracker.state
    assert tracker.state.place_id == "dorm"
    assert tracker.state.place_name == "宿舍"


def test_existing_state_needs_two_votes_to_change():
    tracker = LocationTracker()
    tracker.process_fix(_fix(30.0, 120.0, 1000.0), _places())

    assert tracker.process_fix(_fix(30.002, 120.0, 1600.0), _places()) is None
    assert tracker.state.place_id == "dorm"

    changed = tracker.process_fix(_fix(30.002, 120.0, 2200.0), _places())
    assert changed.place_id == "classroom"
    assert tracker.state.place_id == "classroom"


def test_small_window_vote_tolerates_one_middle_drift():
    tracker = LocationTracker()
    tracker.process_fix(_fix(30.0, 120.0, 1000.0), _places())
    tracker.process_fix(_fix(30.0036, 120.0, 1600.0), _places())
    assert tracker.state.place_id == "dorm"

    tracker.process_fix(_fix(30.0001, 120.0, 2200.0), _places())

    assert tracker.state.place_id == "dorm"


def test_low_accuracy_fix_does_not_switch_or_refresh_state():
    tracker = LocationTracker()
    tracker.process_fix(_fix(30.0, 120.0, 1000.0, accuracy=20.0), _places())

    changed = tracker.process_fix(_fix(30.002, 120.0, 1600.0, accuracy=300.0), _places())

    assert changed is None
    assert tracker.state.place_id == "dorm"
    assert tracker.state.last_fix_at == 1000.0


def test_low_accuracy_fix_far_outside_current_place_forces_exit():
    tracker = LocationTracker()
    tracker.process_fix(_fix(30.0, 120.0, 1000.0, accuracy=20.0), _places())

    changed = tracker.process_fix(_fix(30.09, 120.0, 1600.0, accuracy=500.0), _places())

    assert changed is tracker.state
    assert tracker.state.place_id is None
    assert tracker.state.last_fix_at == 1600.0
    assert tracker.state.accuracy_m == 500.0


def test_accurate_fix_far_outside_current_place_forces_exit_without_second_vote():
    tracker = LocationTracker()
    tracker.process_fix(_fix(30.0, 120.0, 1000.0, accuracy=20.0), _places())

    changed = tracker.process_fix(_fix(30.09, 120.0, 1600.0, accuracy=30.0), _places())

    assert changed is tracker.state
    assert tracker.state.place_id is None
    assert tracker.state.last_fix_at == 1600.0


def test_home_force_exit_uses_lower_distance_floor():
    places = [Place("home", "家", "home", 30.0, 120.0, 500.0, 700.0)]
    tracker = LocationTracker()
    tracker.process_fix(_fix(30.0, 120.0, 1000.0, accuracy=20.0), places)

    changed = tracker.process_fix(_fix(30.014, 120.0, 1600.0, accuracy=500.0), places)

    assert changed is tracker.state
    assert tracker.state.place_id is None


def test_window_samples_expire_after_three_heartbeats():
    tracker = LocationTracker()
    tracker.process_fix(_fix(30.0, 120.0, 1000.0), _places())
    tracker.process_fix(_fix(30.002, 120.0, 1600.0), _places())

    tracker.process_fix(_fix(30.002, 120.0, 4001.0), _places())

    assert tracker.state.place_id == "dorm"


def test_confirmed_none_state_still_needs_two_votes_to_change():
    tracker = LocationTracker()
    tracker.process_fix(_fix(30.02, 120.0, 1000.0), _places())
    assert tracker.state.place_id is None

    tracker.process_fix(_fix(30.0, 120.0, 1600.0), _places())
    assert tracker.state.place_id is None

    tracker.process_fix(_fix(30.0, 120.0, 2200.0), _places())
    assert tracker.state.place_id == "dorm"
