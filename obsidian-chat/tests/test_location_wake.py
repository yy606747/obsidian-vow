import location


def test_legacy_location_module_no_longer_owns_heartbeat_or_wake_side_effects():
    assert not hasattr(location, "process_heartbeat")
    assert not hasattr(location, "_notify_sentinel")
    assert not hasattr(location, "_call_core_location")


def test_legacy_location_prompt_reads_v2_state(monkeypatch):
    monkeypatch.setattr(location, "load_location_config", lambda: {"enabled": True})
    monkeypatch.setattr(location, "load_location_status", lambda: {
        "v2_state": {
            "place_id": "dorm",
            "place_name": "宿舍",
            "place_kind": "dorm",
            "last_fix_at": 1000.0,
            "state_updated_at": 900.0,
            "accuracy_m": 35.0,
        }
    })
    monkeypatch.setattr(location.time, "time", lambda: 1100.0)

    assert location.format_location_for_prompt() == (
        "设备定位落在你们标注过的「宿舍」范围里，定位精度约 35m；"
        "这个名字只是一片范围的标签，说明不了她具体在哪、在做什么，"
        "也说明不了手机是不是在她身上。"
    )


def test_location_prompt_hides_unknown_outside_but_sentinel_reports_transition(monkeypatch):
    monkeypatch.setattr(
        location,
        "load_location_config",
        lambda: {"enabled": True, "home_threshold": 400},
    )
    monkeypatch.setattr(location, "load_location_status", lambda: {
        "state": "outside",
        "accuracy": 500.0,
        "distance_from_home": 1550.0,
        "updated_at": 1000.0,
        "state_changed_at": 1000.0,
        "v2_state": {
            "place_id": None,
            "place_name": None,
            "place_kind": None,
            "last_fix_at": 1000.0,
            "state_updated_at": 1000.0,
            "accuracy_m": 500.0,
        },
    })
    monkeypatch.setattr(location.time, "time", lambda: 1100.0)

    assert location.format_location_for_prompt() == ""
    assert location.format_location_for_sentinel() == (
        "手机的定位从你们标注的「家」范围里走到了范围外"
        "（距围栏中心约1550米，配置半径约400米，定位精度约500米）；"
        "这只是手机越过了那条边界，说明不了她去了哪、"
        "回了哪、在做什么，也说明不了手机是不是在她身上。"
    )


def test_location_sentinel_keeps_recent_transition_visible_when_fix_is_aging(monkeypatch):
    monkeypatch.setattr(
        location,
        "load_location_config",
        lambda: {"enabled": True, "home_threshold": 400},
    )
    monkeypatch.setattr(location, "load_location_status", lambda: {
        "state": "outside",
        "accuracy": 500.0,
        "distance_from_home": 1550.0,
        "updated_at": 1000.0,
        "state_changed_at": 1000.0,
        "v2_state": {
            "place_id": None,
            "place_name": None,
            "place_kind": None,
            "last_fix_at": 1000.0,
            "state_updated_at": 1000.0,
            "accuracy_m": 500.0,
        },
    })
    monkeypatch.setattr(location.time, "time", lambda: 1000.0 + 35 * 60)

    assert location.format_location_for_sentinel() == (
        "手机的定位从你们标注的「家」范围里走到了范围外"
        "（距围栏中心约1550米，配置半径约400米，定位精度约500米，定位更新时间约35分钟前）；"
        "这只是手机越过了那条边界，旧定位不能作为当前位置，"
        "说明不了她去了哪、回了哪、在做什么，"
        "也说明不了手机是不是在她身上。"
    )


def test_location_sentinel_marks_stable_outside_without_transition(monkeypatch):
    monkeypatch.setattr(
        location,
        "load_location_config",
        lambda: {"enabled": True, "home_threshold": 400},
    )
    monkeypatch.setattr(location, "load_location_status", lambda: {
        "state": "outside",
        "accuracy": 300.0,
        "distance_from_home": 1800.0,
        "updated_at": 1000.0,
        "state_changed_at": 0.0,
        "v2_state": {
            "place_id": None,
            "place_name": None,
            "place_kind": None,
            "last_fix_at": 1000.0,
            "state_updated_at": 1000.0,
            "accuracy_m": 300.0,
        },
    })
    monkeypatch.setattr(location.time, "time", lambda: 1600.0)

    assert location.format_location_for_sentinel() == (
        "最近一次定位在你们标注的「家」范围外"
        "（距围栏中心约1800米，配置半径约400米，定位精度约300米，定位更新时间约10分钟前），"
        "之后没有新的进出记录；说明不了她去了哪、"
        "回了哪、在做什么，也说明不了手机是不是在她身上。"
    )


def test_location_sentinel_reports_device_geofence_entry_without_return_home_claim(
    monkeypatch,
):
    monkeypatch.setattr(
        location,
        "load_location_config",
        lambda: {"enabled": True, "home_threshold": 400},
    )
    monkeypatch.setattr(location, "load_location_status", lambda: {
        "state": "at_home",
        "accuracy": 30.0,
        "distance_from_home": 50.0,
        "updated_at": 1090.0,
        "state_changed_at": 1090.0,
        "v2_state": {
            "place_id": "home",
            "place_name": "家",
            "place_kind": "home",
            "last_fix_at": 1090.0,
            "state_updated_at": 1090.0,
            "accuracy_m": 30.0,
        },
    })
    monkeypatch.setattr(location.time, "time", lambda: 1100.0)

    assert location.format_location_for_sentinel() == (
        "手机的定位从你们标注的「家」范围外回到了范围里"
        "（距围栏中心约50米，配置半径约400米，定位精度约30米）；"
        "这只是手机越过了那条边界，说明不了她去了哪、"
        "回了哪、在做什么，也说明不了手机是不是在她身上。"
    )


def test_legacy_poi_prompt_requires_fresh_enrichment(monkeypatch):
    monkeypatch.setattr(location.time, "time", lambda: 3000.0)
    monkeypatch.setattr(location, "load_location_status", lambda: {
        "enriched_at": 1000.0,
        "nearby_pois": {
            "餐饮美食": [{"name": "面馆", "distance": "80"}],
        },
    })

    assert location.format_nearby_pois_for_prompt() == ""
