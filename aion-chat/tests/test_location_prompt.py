import time

import location as location_module
from app.location import LocationState


def test_nearby_pois_prompt_requires_outside_state(monkeypatch):
    pois = {"餐饮美食": [{"name": "某店", "distance": "100"}]}
    base = {"enriched_at": time.time(), "nearby_pois": pois}

    monkeypatch.setattr(location_module, "load_location_status", lambda: {**base, "state": "at_home"})
    assert location_module.format_nearby_pois_for_prompt() == ""

    monkeypatch.setattr(location_module, "load_location_status", lambda: {**base, "state": "outside"})
    assert "某店" in location_module.format_nearby_pois_for_prompt()


def test_prompt_uses_fresh_place_state_only():
    state = LocationState("dorm", "宿舍", "dorm", 1000.0, 900.0, 35.0)

    assert state.for_prompt(1100.0) == (
        "设备定位落在你们标注过的「宿舍」范围里，定位精度约 35m；"
        "这个名字只是一片范围的标签，说明不了她具体在哪、在做什么，"
        "也说明不了手机是不是在她身上。"
    )
    assert state.for_prompt(2801.0) == ""


def test_sentinel_sees_expired_runtime_status():
    state = LocationState("dorm", "宿舍", "dorm", 1000.0, 900.0, 35.0)

    assert state.for_sentinel(2801.0) == "定位数据已超过 30 分钟，不作为位置判断依据。"


def test_unknown_place_does_not_enter_core_prompt():
    state = LocationState(None, None, None, 1000.0, 1000.0, 60.0)

    assert state.for_prompt(1100.0) == ""
    assert state.for_sentinel(1100.0) == "当前未命中可信地点，定位精度约 60m。"


def test_campus_is_not_described_as_dorm():
    state = LocationState("campus", "校园", "campus", 1000.0, 900.0, 80.0)

    text = state.for_prompt(1100.0)
    assert "校园" in text
    assert "宿舍" not in text
    assert "用户当前大概在" not in text
