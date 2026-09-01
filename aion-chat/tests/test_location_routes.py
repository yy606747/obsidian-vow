import asyncio

import location_diagnostics
from app.location import LocationService, Place
from app.legacy_adapters import location_runtime as location_runtime_module
from routes import location as location_routes


def _runtime():
    return location_routes.location_runtime


def _patch_broadcasts(monkeypatch, broadcasts):
    async def fake_location_update(data):
        broadcasts.append({"type": "location_update", "data": data})

    async def fake_chat_status(status, updated_at):
        broadcasts.append({
            "type": "chat_status",
            "data": {"status": status, "updated_at": updated_at},
        })

    monkeypatch.setattr(_runtime(), "publish_location_update", fake_location_update)
    monkeypatch.setattr(_runtime(), "publish_chat_status", fake_chat_status)


def test_location_diagnostic_records_android_failure(monkeypatch):
    events = []

    def fake_record(event):
        events.append(event)
        return {"request_id": "loc-test", **event}

    monkeypatch.setattr(location_runtime_module, "record_location_event", fake_record)

    result = asyncio.run(location_routes.location_diagnostic(
        location_routes.LocationDiagnosticBody(
            event="fresh_timeout",
            provider="network",
            message="timeout",
            elapsed_ms=30000,
            retryable=True,
            meta={"network_provider_enabled": True},
        )
    ))

    assert result["ok"] is True
    assert events[0]["scope"] == "android_location:fresh_timeout"
    assert events[0]["error_type"] == "fresh_timeout"
    assert events[0]["retryable"] is True
    assert events[0]["meta"]["provider"] == "network"
    assert events[0]["meta"]["network_provider_enabled"] is True


def test_location_route_keeps_legacy_payload_and_broadcasts_v2(monkeypatch):
    saved = []
    broadcasts = []
    events = []

    async def fake_broadcast(payload):
        broadcasts.append(payload)

    monkeypatch.setattr(_runtime(), "service", LocationService())
    monkeypatch.setattr(_runtime(), "load_config", lambda: {"enabled": True})
    monkeypatch.setattr(_runtime(), "load_status", lambda: {
        "state": "unknown",
        "lng": 0.0,
        "lat": 0.0,
        "accuracy": 0.0,
        "address": "",
        "weather": {},
        "nearby_pois": {},
        "updated_at": 0,
        "state_changed_at": 0,
    })
    monkeypatch.setattr(_runtime(), "save_status", lambda status: saved.append(status))
    monkeypatch.setattr(_runtime(), "load_places", lambda _path: [
        Place("dorm", "宿舍", "dorm", 30.0, 120.0, 120.0, 220.0)
    ])
    monkeypatch.setattr(_runtime(), "record_use_case_event", lambda event: events.append(event) or event)
    _patch_broadcasts(monkeypatch, broadcasts)
    monkeypatch.setattr(_runtime(), "now", lambda: 1000.0)

    result = asyncio.run(location_routes.process_heartbeat(120.0, 30.0, 20.0, True))

    assert result["state"] == "at_home"
    assert result["v2_state"] == {
        "place_id": "dorm",
        "place_name": "宿舍",
        "place_kind": "dorm",
        "last_fix_at": 1000.0,
        "state_updated_at": 1000.0,
        "accuracy_m": 20.0,
    }
    assert saved[0]["v2_state"]["place_id"] == "dorm"
    assert broadcasts[0]["type"] == "location_update"
    assert broadcasts[0]["data"]["v2_state"]["place_name"] == "宿舍"
    scopes = [event["scope"] for event in events]
    assert scopes.count("location:heartbeat_summary") == 1
    assert "location:heartbeat_received" not in scopes
    assert "location:state_processed" not in scopes
    assert "location:status_saved" not in scopes
    assert "location:broadcast_sent" not in scopes
    assert "location:enrichment_skipped" not in scopes
    summary = next(event for event in events if event["scope"] == "location:heartbeat_summary")
    assert summary["meta"]["enrichment_result"] == "skipped"
    assert summary["meta"]["enrichment_reason"] == "missing_amap_key"
    assert summary["meta"]["new_place_id"] == "dorm"
    assert summary["meta"]["place_kind"] == "dorm"
    assert summary["meta"]["moved_distance_m"] == -1.0
    assert summary["meta"]["distance_from_home_m"] == -1.0
    for event in events:
        meta = event.get("meta") or {}
        for forbidden in {"lat", "lng", "address"}:
            assert forbidden not in meta


def test_location_event_sink_failure_is_visible_and_nonfatal(monkeypatch, capsys):
    saved = []

    monkeypatch.setattr(_runtime(), "service", LocationService())
    monkeypatch.setattr(_runtime(), "load_config", lambda: {"enabled": True, "movement_threshold": 500})
    monkeypatch.setattr(_runtime(), "load_status", lambda: {
        "state": "outside",
        "lng": 120.0,
        "lat": 30.0,
        "accuracy": 30.0,
        "updated_at": 1000.0,
        "v2_state": {
            "place_id": None,
            "last_fix_at": 1000.0,
            "state_updated_at": 1000.0,
            "accuracy_m": 30.0,
        },
    })
    monkeypatch.setattr(_runtime(), "save_status", lambda status: saved.append(status))
    monkeypatch.setattr(_runtime(), "load_places", lambda _path: [])
    _patch_broadcasts(monkeypatch, [])
    monkeypatch.setattr(_runtime(), "record_use_case_event", lambda _event: (_ for _ in ()).throw(RuntimeError("disk full")))
    monkeypatch.setattr(_runtime(), "now", lambda: 2000.0)

    result = asyncio.run(location_routes.process_heartbeat(120.0, 30.0, 30.0, True))

    assert result["state"] == "outside"
    assert saved
    assert "record_event_failed scope=location:heartbeat_summary" in capsys.readouterr().err


def test_location_diagnostics_sanitizes_track_meta():
    clean = location_diagnostics._sanitize_meta({
        "lat": 30.0,
        "lng": 120.0,
        "distance_from_home_m": 500,
        "moved_distance_m": 1200,
        "place_id": "dorm",
        "new_place_id": "dorm",
        "place_kind": "dorm",
        "place_name": "宿舍",
        "address": "somewhere",
        "state": "outside",
        "has_address": True,
    })

    assert clean == {
        "distance_from_home_m": 500,
        "moved_distance_m": 1200,
        "place_id": "dorm",
        "new_place_id": "dorm",
        "place_kind": "dorm",
        "state": "outside",
        "has_address": True,
    }


def test_location_diagnostics_compacts_large_jsonl(monkeypatch, tmp_path):
    events_path = tmp_path / "location_events.jsonl"
    events_path.write_text(
        "\n".join([
            '{"scope":"old:1"}',
            '{"scope":"old:2"}',
            '{"scope":"old:3"}',
        ]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(location_diagnostics, "EVENTS_PATH", events_path)
    monkeypatch.setattr(location_diagnostics, "MAX_EVENTS_FILE_BYTES", 10)
    monkeypatch.setattr(location_diagnostics, "MAX_EVENTS_FILE_LINES", 2)

    location_diagnostics.record_location_event({"scope": "new:event", "ok": True})

    lines = events_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert '"old:1"' not in lines[0]
    assert '"new:event"' in lines[-1]


def test_heartbeat_refreshes_address_weather_after_significant_move(monkeypatch):
    saved = []
    broadcasts = []

    async def fake_broadcast(payload):
        broadcasts.append(payload)

    async def fake_regeo(_lng, _lat, _key):
        return {
            "address": "新的位置",
            "adcode": "330100",
            "province": "浙江省",
            "city": "杭州市",
            "district": "西湖区",
        }

    async def fake_weather(_adcode, _key):
        # amap_weather 的真实返回结构：{"live": {...扁平实况...}, "forecast": [...]}
        return {"live": {"weather": "晴", "temperature": "26"}, "forecast": []}

    monkeypatch.setattr(_runtime(), "service", LocationService())
    monkeypatch.setattr(_runtime(), "load_config", lambda: {
        "enabled": True,
        "amap_key": "amap-key",
        "movement_threshold": 500,
        "poi_types": {},
    })
    prev_status = {
        "state": "outside",
        "lng": 120.0,
        "lat": 30.0,
        "accuracy": 30.0,
        "address": "旧的位置",
        "address_updated_at": 1000.0,
        "weather": {},
        "nearby_pois": {},
        "updated_at": 1000.0,
        "state_changed_at": 1000.0,
        "v2_state": {
            "place_id": None,
            "place_name": None,
            "place_kind": None,
            "last_fix_at": 1000.0,
            "state_updated_at": 1000.0,
            "accuracy_m": 30.0,
        },
    }
    # 反映写入：增强阶段的 reload 守卫需要读到上一次落盘的那一帧
    monkeypatch.setattr(_runtime(), "load_status", lambda: saved[-1] if saved else prev_status)
    monkeypatch.setattr(_runtime(), "save_status", lambda status: saved.append(status))
    monkeypatch.setattr(_runtime(), "load_places", lambda _path: [])
    _patch_broadcasts(monkeypatch, broadcasts)
    monkeypatch.setattr(_runtime(), "is_quiet_hours", lambda: False)
    monkeypatch.setattr(_runtime(), "regeo", fake_regeo)
    monkeypatch.setattr(_runtime(), "weather", fake_weather)
    monkeypatch.setattr(_runtime(), "now", lambda: 2000.0)

    result = asyncio.run(location_routes.process_heartbeat(
        120.02,
        30.02,
        35.0,
        True,
        provider="network",
        location_age_ms=5000,
    ))

    # 重排后：第一次落盘/广播是不带地址的关键路径快照，地址/天气在增强完成后二次补齐。
    assert result["address"] == "新的位置"
    assert result["weather"] == {"weather": "晴", "temperature": "26"}
    assert result["provider"] == "network"
    assert result["location_age_sec"] == 5.0
    assert saved[-1]["address"] == "新的位置"
    assert saved[-1]["address_updated_at"] == 2000.0
    assert saved[-1]["forecast"] == []
    # 广播里既有 location_update 也有 chat_status；地址断言只看最后一条 location_update。
    loc_updates = [b for b in broadcasts if b["type"] == "location_update"]
    assert loc_updates[-1]["data"]["address"] == "新的位置"
    assert loc_updates[-1]["data"]["weather"] == {"weather": "晴", "temperature": "26"}
    assert loc_updates[-1]["data"]["location_age_sec"] == 5.0


def test_far_low_accuracy_fix_exits_legacy_home_state():
    service = LocationService()
    home = Place("home", "家", "home", 30.0, 120.0, 500.0, 700.0)
    previous_status = {
        "state": "at_home",
        "lng": 120.0,
        "lat": 30.0,
        "accuracy": 20.0,
        "updated_at": 1000.0,
        "state_changed_at": 1000.0,
        "v2_state": {
            "place_id": "home",
            "place_name": "家",
            "place_kind": "home",
            "last_fix_at": 1000.0,
            "state_updated_at": 1000.0,
            "accuracy_m": 20.0,
        },
    }

    result = service.process_fix(
        lng=120.0,
        lat=30.014,
        accuracy_m=500.0,
        is_gcj02=True,
        places=[home],
        previous_status=previous_status,
        now=1600.0,
        home_place=home,
    )

    assert result["state"] == "outside"
    assert result["old_state"] == "at_home"
    assert result["state_changed"] is True
    assert 1400 < result["distance_from_home"] < 1700
    assert result["v2_old_place_id"] == "home"
    assert result["v2_new_place_id"] is None
    assert result["v2_state"]["place_id"] is None


def test_state_transition_announces_monitor_log_and_user_status(monkeypatch):
    saved = []
    monitor_logs = []
    chat_status = {"status": ""}
    events = []

    async def fake_broadcast(payload):
        pass

    async def fake_monitor_log(entry):
        monitor_logs.append(entry)
        return True

    monkeypatch.setattr(_runtime(), "service", LocationService())
    monkeypatch.setattr(_runtime(), "load_config", lambda: {
        "enabled": True,
        "home_lng": 120.0,
        "home_lat": 30.0,
        "home_threshold": 500,
    })
    monkeypatch.setattr(_runtime(), "load_status", lambda: {
        "state": "at_home",
        "lng": 120.0,
        "lat": 30.0,
        "accuracy": 20.0,
        "updated_at": 1000.0,
        "state_changed_at": 1000.0,
        "v2_state": {
            "place_id": "home",
            "place_name": "家",
            "place_kind": "home",
            "last_fix_at": 1000.0,
            "state_updated_at": 1000.0,
            "accuracy_m": 20.0,
        },
    })
    monkeypatch.setattr(_runtime(), "save_status", lambda status: saved.append(status))
    monkeypatch.setattr(_runtime(), "load_places", lambda _path: [])
    _patch_broadcasts(monkeypatch, [])
    monkeypatch.setattr(_runtime(), "is_quiet_hours", lambda: False)
    monkeypatch.setattr(_runtime(), "load_worldbook", lambda: {"user_name": "阿玖"})
    monkeypatch.setattr(_runtime(), "record_use_case_event", lambda event: events.append(event) or event)

    def fake_set_line(prefix, line):
        chat_status["status"] = line
        return line

    monkeypatch.setattr(_runtime(), "set_status_line", fake_set_line)
    monkeypatch.setattr(_runtime(), "request_sentinel_evaluation", lambda: None)
    monkeypatch.setattr(_runtime(), "record_transition_log", fake_monitor_log)

    result = asyncio.run(location_routes.process_heartbeat(120.0, 30.014, 20.0, True))

    assert result["state"] == "outside"
    assert result["state_changed"] is True
    assert len(monitor_logs) == 1
    assert monitor_logs[0]["source"] == "location"
    assert "阿玖离开家外出了" in monitor_logs[0]["monitoringlog"]
    assert monitor_logs[0]["call_core"] is False
    assert "[位置] 外出中" in chat_status["status"]
    transition_event = next(event for event in events if event["scope"] == "location:transition_announced")
    assert "event_desc" not in transition_event["meta"]
    assert "monitoringlog" not in transition_event["meta"]
    assert transition_event["meta"]["transition_state"] == "outside"


def test_enrichment_discarded_when_a_newer_heartbeat_won_the_race(monkeypatch):
    saved = []
    broadcasts = []
    events = []

    async def fake_broadcast(payload):
        broadcasts.append(payload)

    async def fake_regeo(_lng, _lat, _key):
        return {"address": "增强地址", "adcode": "330100"}

    async def fake_weather(_adcode, _key):
        return {"live": {"weather": "晴"}, "forecast": []}

    # 增强阶段 reload 时，磁盘上已是“另一帧”（heartbeat_received_at 不同），守卫应丢弃本次增强。
    newer = {
        "state": "outside", "lng": 121.0, "lat": 31.0,
        "heartbeat_received_at": 9999.0, "updated_at": 9999.0,
        "v2_state": {"place_id": None, "last_fix_at": 9999.0, "state_updated_at": 9999.0, "accuracy_m": 20.0},
    }
    calls = {"n": 0}

    def fake_load():
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "state": "outside", "lng": 120.0, "lat": 30.0, "address": "旧地址",
                "updated_at": 1000.0,
                "v2_state": {"place_id": None, "last_fix_at": 1000.0, "state_updated_at": 1000.0, "accuracy_m": 30.0},
            }
        return newer

    monkeypatch.setattr(_runtime(), "service", LocationService())
    monkeypatch.setattr(_runtime(), "load_config", lambda: {
        "enabled": True, "amap_key": "k", "movement_threshold": 500, "poi_types": {},
    })
    monkeypatch.setattr(_runtime(), "load_status", fake_load)
    monkeypatch.setattr(_runtime(), "save_status", lambda s: saved.append(s))
    monkeypatch.setattr(_runtime(), "load_places", lambda _p: [])
    monkeypatch.setattr(_runtime(), "record_use_case_event", lambda event: events.append(event) or event)
    _patch_broadcasts(monkeypatch, broadcasts)
    monkeypatch.setattr(_runtime(), "is_quiet_hours", lambda: False)
    monkeypatch.setattr(_runtime(), "regeo", fake_regeo)
    monkeypatch.setattr(_runtime(), "weather", fake_weather)
    monkeypatch.setattr(_runtime(), "now", lambda: 2000.0)

    asyncio.run(location_routes.process_heartbeat(120.02, 30.02, 35.0, True))

    # 守卫生效：增强结果没有回写（否则会把更新心跳的坐标/状态回滚掉）
    assert not any(s.get("address") == "增强地址" for s in saved)
    assert len([b for b in broadcasts if b["type"] == "location_update"]) == 1
    scopes = [event["scope"] for event in events]
    assert "location:enrichment_finished" in scopes
    assert "location:enrichment_discarded" in scopes
    discarded = next(event for event in events if event["scope"] == "location:enrichment_discarded")
    assert discarded["meta"]["reason"] == "newer_heartbeat_won"


def test_transition_does_not_announce_stale_address(monkeypatch):
    saved = []
    chat_status = {"status": ""}
    monitor_logs = []

    async def fake_broadcast(payload):
        pass

    async def fake_monitor_log(entry):
        monitor_logs.append(entry)
        return True

    monkeypatch.setattr(_runtime(), "service", LocationService())
    monkeypatch.setattr(_runtime(), "load_config", lambda: {
        "enabled": True, "home_lng": 120.0, "home_lat": 30.0, "home_threshold": 500,
    })
    # 上一帧在家，带着家的地址；出门时绝不能把“家所在地”当成当前位置通报出去
    monkeypatch.setattr(_runtime(), "load_status", lambda: {
        "state": "at_home", "lng": 120.0, "lat": 30.0, "accuracy": 20.0,
        "address": "家所在地", "weather": {"weather": "阴"}, "nearby_pois": {"x": [1]},
        "updated_at": 1000.0, "state_changed_at": 1000.0,
        "v2_state": {"place_id": "home", "place_name": "家", "place_kind": "home",
                     "last_fix_at": 1000.0, "state_updated_at": 1000.0, "accuracy_m": 20.0},
    })
    monkeypatch.setattr(_runtime(), "save_status", lambda s: saved.append(s))
    monkeypatch.setattr(_runtime(), "load_places", lambda _p: [])
    _patch_broadcasts(monkeypatch, [])
    monkeypatch.setattr(_runtime(), "is_quiet_hours", lambda: False)
    monkeypatch.setattr(_runtime(), "load_worldbook", lambda: {"user_name": "阿玖"})
    monkeypatch.setattr(_runtime(), "set_status_line",
                        lambda prefix, line: chat_status.__setitem__("status", line) or line)
    monkeypatch.setattr(_runtime(), "request_sentinel_evaluation", lambda: None)
    monkeypatch.setattr(_runtime(), "record_transition_log", fake_monitor_log)

    result = asyncio.run(location_routes.process_heartbeat(120.0, 30.014, 20.0, True))

    assert result["state"] == "outside"
    assert result["configured_enter_m"] == 500.0
    assert result["configured_exit_m"] == 700.0
    assert "家所在地" not in monitor_logs[0]["monitoringlog"]
    assert "当前位置" not in monitor_logs[0]["monitoringlog"]
    assert "家所在地" not in chat_status["status"]
    assert "[位置] 外出中" in chat_status["status"]
    # 第一帧快照已清掉旧地址/天气/POI
    assert saved[0]["address"] == ""
    assert saved[0]["weather"] == {}
    assert saved[0]["nearby_pois"] == {}


def test_zero_distance_is_not_a_significant_move():
    cfg = {"movement_threshold": 500}
    assert location_routes._significant_location_move(cfg, {"moved_distance": 0.0}) is False
    assert location_routes._significant_location_move(cfg, {"moved_distance": 200.0}) is False
    assert location_routes._significant_location_move(cfg, {"moved_distance": 800.0}) is True
    # 无上一坐标（-1 / None / 缺失）才算“需要刷新”
    assert location_routes._significant_location_move(cfg, {"moved_distance": -1}) is True
    assert location_routes._significant_location_move(cfg, {"moved_distance": None}) is True
    assert location_routes._significant_location_move(cfg, {}) is True


def test_stationary_heartbeat_keeps_enrichment_and_does_not_broadcast_chat_status(monkeypatch):
    saved = []
    broadcasts = []

    async def fake_broadcast(payload):
        broadcasts.append(payload)

    chat_status_calls = []

    monkeypatch.setattr(_runtime(), "service", LocationService())
    monkeypatch.setattr(_runtime(), "load_config", lambda: {"enabled": True, "movement_threshold": 500})
    # 上一帧已在某外出地点，带着地址/天气/POI；同坐标再来一帧（位移 0）不该清掉它们。
    monkeypatch.setattr(_runtime(), "load_status", lambda: {
        "state": "outside", "lng": 120.0, "lat": 30.0, "accuracy": 30.0,
        "address": "某街某号", "weather": {"weather": "晴"}, "nearby_pois": {"餐饮": [{"name": "店"}]},
        "enriched_at": 1000.0, "address_updated_at": 1000.0,
        "updated_at": 1000.0, "state_changed_at": 500.0,
        "v2_state": {"place_id": None, "place_name": None, "place_kind": None,
                     "last_fix_at": 1000.0, "state_updated_at": 1000.0, "accuracy_m": 30.0},
    })
    monkeypatch.setattr(_runtime(), "save_status", lambda s: saved.append(s))
    monkeypatch.setattr(_runtime(), "load_places", lambda _p: [])
    _patch_broadcasts(monkeypatch, broadcasts)
    monkeypatch.setattr(_runtime(), "is_quiet_hours", lambda: False)
    monkeypatch.setattr(_runtime(), "set_status_line",
                        lambda prefix, line: chat_status_calls.append(line) or line)
    monkeypatch.setattr(_runtime(), "now", lambda: 2000.0)

    result = asyncio.run(location_routes.process_heartbeat(120.0, 30.0, 30.0, True))

    assert result["state"] == "outside"
    assert result["state_changed"] is False
    assert result["moved_distance"] == 0.0
    # 原地不动：地址/天气/POI 原样保留
    assert saved[0]["address"] == "某街某号"
    assert saved[0]["weather"] == {"weather": "晴"}
    assert saved[0]["nearby_pois"] == {"餐饮": [{"name": "店"}]}
    # 不刷新 [位置] 行、不广播 chat_status
    assert chat_status_calls == []
    assert [b["type"] for b in broadcasts] == ["location_update"]


def test_heartbeat_passes_full_result_to_structured_state_evidence(monkeypatch):
    fix_calls = []
    state_calls = []

    async def fake_process_heartbeat(_lng, _lat, _accuracy, _is_gcj02, **_kwargs):
        return {
            "state": "at_home",
            "state_changed": False,
            "v2_state_changed": True,
            "v2_fix_accepted": True,
            "distance_from_home": 30.0,
            "configured_enter_m": 400.0,
            "configured_exit_m": 560.0,
            "v2_old_place_id": None,
            "v2_new_place_id": "dorm",
            "v2_state": {
                "place_id": "dorm",
                "place_name": "宿舍",
                "place_kind": "dorm",
                "last_fix_at": 1000.0,
                "state_updated_at": 1000.0,
                "accuracy_m": 20.0,
            },
        }

    monkeypatch.setattr(_runtime(), "load_config", lambda: {"enabled": True})
    monkeypatch.setattr(_runtime(), "process_heartbeat", fake_process_heartbeat)
    monkeypatch.setattr(location_runtime_module, "record_location_heartbeat_safely", lambda body, result: fix_calls.append((body, result)))
    monkeypatch.setattr(location_runtime_module, "record_location_state_safely", lambda state_result: state_calls.append(state_result))

    result = asyncio.run(location_routes.location_heartbeat(
        location_routes.HeartbeatBody(lng=120.0, lat=30.0, accuracy=20.0)
    ))

    assert result["ok"] is True
    assert len(fix_calls) == 1
    assert state_calls[0]["v2_state"]["place_id"] == "dorm"
    assert state_calls[0]["v2_fix_accepted"] is True
    assert state_calls[0]["configured_exit_m"] == 560.0


def test_config_get_masks_amap_key_and_mask_put_does_not_overwrite(monkeypatch):
    stored = {"amap_key": "1234567890abcdef", "enabled": True}
    saved = []

    monkeypatch.setattr(_runtime(), "load_config", lambda: dict(stored))
    monkeypatch.setattr(_runtime(), "save_config", lambda cfg: saved.append(cfg))
    monkeypatch.setattr(_runtime(), "is_quiet_hours", lambda: False)
    monkeypatch.setattr(_runtime(), "sync_home_place", lambda _cfg: None)

    config = asyncio.run(location_routes.get_location_config())
    assert config["amap_key"] == "1234********cdef"

    asyncio.run(location_routes.update_location_config(
        location_routes.LocationConfigUpdate(amap_key="1234********cdef")
    ))
    assert saved[0]["amap_key"] == "1234567890abcdef"


def test_status_marks_stale_location_as_not_prompt_usable(monkeypatch):
    monkeypatch.setattr(_runtime(), "load_config", lambda: {"enabled": True})
    monkeypatch.setattr(_runtime(), "load_status", lambda: {
        "state": "at_home",
        "updated_at": 1000.0,
        "v2_state": {
            "place_id": "home",
            "place_name": "家",
            "place_kind": "home",
            "last_fix_at": 1000.0,
            "state_updated_at": 1000.0,
            "accuracy_m": 20.0,
        },
    })
    monkeypatch.setattr(_runtime(), "now", lambda: 2801.0)

    status = asyncio.run(location_routes.get_location_status())

    assert status["location_stale"] is True
    assert status["prompt_usable"] is False
    assert status["location_age_sec"] == 1801.0
