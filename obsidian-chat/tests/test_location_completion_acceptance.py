import ast
import asyncio
from pathlib import Path

import pytest

import location
from app.location import LocationFix, LocationService, LocationTracker, Place, state_from_payload
from app.legacy_adapters import location_runtime as location_runtime_module
from routes import location as location_routes


ROOT = Path(__file__).resolve().parents[1]
LOCATION_DIR = ROOT / "app" / "location"


def test_location_v2_architecture_stays_small_and_free_of_legacy_side_effects():
    files = {
        "__init__.py": 25,
        "geo.py": 60,
        "places.py": 100,
        "tracker.py": 200,
        "service.py": 200,
        "use_case.py": 700,
    }
    forbidden_import_roots = {
        "activity",
        "ai_providers",
        "camera",
        "database",
        "location",
        "memory",
        "music",
        "schedule",
        "sensing",
        "voice",
        "ws",
    }
    forbidden_calls = {
        "append_monitor_log",
        "broadcast",
        "get_db",
        "save_chat_status",
        "stream_ai",
        "write_text",
    }

    assert sorted(path.name for path in LOCATION_DIR.glob("*.py")) == sorted(files)
    for filename, max_lines in files.items():
        path = LOCATION_DIR / filename
        assert len(path.read_text(encoding="utf-8").splitlines()) <= max_lines
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = set()
        calls = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imports.add(node.module.split(".", 1)[0])
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    calls.add(func.id)
                elif isinstance(func, ast.Attribute):
                    calls.add(func.attr)

        assert imports.isdisjoint(forbidden_import_roots)
        assert calls.isdisjoint(forbidden_calls)


def test_location_route_stays_thin_and_legacy_free():
    route_path = ROOT / "routes" / "location.py"
    tree = ast.parse(route_path.read_text(encoding="utf-8"), filename=str(route_path))
    imports = set()
    calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imports.add(node.module.split(".", 1)[0])
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                calls.add(func.id)
            elif isinstance(func, ast.Attribute):
                calls.add(func.attr)

    assert imports.isdisjoint({"config", "location", "ws", "sentinel_runtime"})
    assert calls.isdisjoint({"broadcast", "write_text", "append_and_broadcast_monitor_log"})


def test_dorm_single_far_drift_does_not_switch_or_fake_home_geofence_evidence(monkeypatch):
    state_calls = []
    fix_calls = []
    saved_status = {
        "state": "unknown",
        "lng": 0.0,
        "lat": 0.0,
        "accuracy": 0.0,
        "address": "麦当劳",
        "weather": {"weather": "晴", "temperature": "20"},
        "nearby_pois": {"餐饮美食": [{"name": "麦当劳", "distance": "30"}]},
        "updated_at": 0,
        "state_changed_at": 0,
    }

    async def fake_broadcast(_payload):
        return None

    def save_status(status):
        saved_status.clear()
        saved_status.update(status)

    runtime = location_routes.location_runtime
    monkeypatch.setattr(runtime, "service", LocationService())
    monkeypatch.setattr(runtime, "load_config", lambda: {"enabled": True})
    monkeypatch.setattr(runtime, "load_status", lambda: dict(saved_status))
    monkeypatch.setattr(runtime, "save_status", save_status)
    monkeypatch.setattr(runtime, "load_places", lambda _path: [
        Place("dorm", "宿舍", "dorm", 30.0, 120.0, 100.0, 220.0)
    ])
    monkeypatch.setattr(runtime, "publish_location_update", lambda _payload: fake_broadcast({"type": "location_update", "data": _payload}))
    monkeypatch.setattr(runtime, "publish_chat_status", lambda _status, _updated_at: fake_broadcast({
        "type": "chat_status",
        "data": {"status": _status, "updated_at": _updated_at},
    }))
    monkeypatch.setattr(location_runtime_module, "record_location_heartbeat_safely", lambda body, result: fix_calls.append((body, result)))
    monkeypatch.setattr(location_runtime_module, "record_location_state_safely", lambda state: state_calls.append(state))
    # 两次心跳分别落在 1000 / 1600；每次心跳内部 time.time() 调用次数不固定，
    # 耗尽后钳到末值，避免迭代器被多调几次就 StopIteration。
    times = [1000.0, 1600.0]
    clock = {"i": 0}

    def fake_now():
        value = times[min(clock["i"], len(times) - 1)]
        clock["i"] += 1
        return value

    monkeypatch.setattr(runtime, "now", fake_now)

    first = asyncio.run(location_routes.location_heartbeat(
        location_routes.HeartbeatBody(lng=120.0, lat=30.0, accuracy=25.0, is_gcj02=True)
    ))
    second = asyncio.run(location_routes.location_heartbeat(
        location_routes.HeartbeatBody(lng=120.02, lat=30.02, accuracy=35.0, is_gcj02=True)
    ))

    assert first["v2_new_place_id"] == "dorm"
    assert second["v2_new_place_id"] == "dorm"
    assert second["v2_state_changed"] is False
    assert state_calls == []
    assert len(fix_calls) == 2

    monkeypatch.setattr(location, "load_location_config", lambda: {"enabled": True})
    monkeypatch.setattr(location, "load_location_status", lambda: dict(saved_status))
    monkeypatch.setattr(location.time, "time", lambda: 1700.0)
    prompt = location.format_location_for_prompt()
    assert "宿舍" in prompt
    assert "麦当劳" not in prompt


def test_boundary_jitter_inside_exit_radius_keeps_stable_place():
    places = [
        Place("dorm", "宿舍", "dorm", 30.0, 120.0, 100.0, 220.0),
        Place("campus", "校园", "campus", 30.0, 120.0, 900.0, 1200.0),
    ]
    tracker = LocationTracker()
    changes = []

    for index, lat in enumerate([30.0, 30.00135, 30.0009, 30.0015, 30.0007]):
        changed = tracker.process_fix(
            LocationFix(lat=lat, lng=120.0, accuracy_m=40.0, received_at=1000.0 + index * 600),
            places,
        )
        if changed:
            changes.append(changed.place_id)

    assert changes == ["dorm"]
    assert tracker.state.place_id == "dorm"


def test_stale_location_address_weather_and_poi_do_not_enter_prompts(monkeypatch):
    monkeypatch.setattr(location, "load_location_config", lambda: {"enabled": True})
    monkeypatch.setattr(location, "load_location_status", lambda: {
        "state": "outside",
        "address": "半天前的地址",
        "weather": {"weather": "半天前的天气", "temperature": "1"},
        "nearby_pois": {"餐饮美食": [{"name": "半天前的 POI", "distance": "80"}]},
        "enriched_at": 1000.0,
        "v2_state": {
            "place_id": "classroom",
            "place_name": "教学楼",
            "place_kind": "classroom",
            "last_fix_at": 1000.0,
            "state_updated_at": 900.0,
            "accuracy_m": 35.0,
        },
    })
    monkeypatch.setattr(location.time, "time", lambda: 1000.0 + 6 * 3600)

    assert location.format_location_for_prompt() == ""
    assert location.format_nearby_pois_for_prompt() == ""


def test_bad_v2_state_fails_loud_instead_of_silent_fallback():
    with pytest.raises(KeyError):
        state_from_payload({"place_id": "dorm"})


def test_location_without_configured_home_does_not_emit_geofence_evidence(monkeypatch):
    state_calls = []

    async def fake_process_heartbeat(_lng, _lat, _accuracy, _is_gcj02, **_kwargs):
        return {
            "state": "at_home",
            "state_changed": False,
            "v2_state_changed": True,
            "v2_fix_accepted": True,
            "v2_old_place_id": "dorm",
            "v2_new_place_id": "dorm",
            "v2_state": {
                "place_id": "dorm",
                "place_name": "宿舍",
                "place_kind": "dorm",
                "last_fix_at": 1000.0,
                "state_updated_at": 900.0,
                "accuracy_m": 30.0,
            },
        }

    runtime = location_routes.location_runtime
    monkeypatch.setattr(runtime, "load_config", lambda: {"enabled": True})
    monkeypatch.setattr(runtime, "process_heartbeat", fake_process_heartbeat)
    monkeypatch.setattr(location_runtime_module, "record_location_heartbeat_safely", lambda _body, _result: None)
    monkeypatch.setattr(location_runtime_module, "record_location_state_safely", lambda state: state_calls.append(state))

    asyncio.run(location_routes.location_heartbeat(
        location_routes.HeartbeatBody(lng=120.0, lat=30.0, accuracy=20.0, is_gcj02=True)
    ))

    assert state_calls == []
