"""Phase 8.0 local HTTP smoke for evidence shadow-write.

This script starts a minimal FastAPI app with only the legacy report routes and
read-only evidence diagnostics. It patches file writes, broadcasts, and location
processing so the smoke does not touch the real database, model providers, or
background services.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import uvicorn
from fastapi import FastAPI

from app.events import evidence_ledger
from routes import activity, events, location, sensing, sentinel


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(method: str, url: str, payload: dict | None = None) -> dict:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wait_for_server(base_url: str):
    deadline = time.time() + 10
    last_error = None
    while time.time() < deadline:
        try:
            _request("GET", f"{base_url}/api/events/evidence-summary")
            return
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            time.sleep(0.1)
    raise RuntimeError(f"server did not start: {last_error}")


def _patch_legacy_side_effects():
    async def fake_broadcast(_payload):
        return None

    async def fake_process_heartbeat(_lng, _lat, _accuracy, _is_gcj02, **_kwargs):
        return {
            "state": "outside",
            "old_state": "at_home",
            "state_changed": True,
            "distance_from_home": 800.0,
            "full_api": False,
        }

    sensing.append_sensing_entry = lambda _entry: None
    sensing.cleanup_old_sensing_logs = lambda: None
    sensing.manager = SimpleNamespace(broadcast=fake_broadcast)
    activity.append_activity_log = lambda _entry: None
    activity.cleanup_old_activity_logs = lambda: None
    activity.manager = SimpleNamespace(broadcast=fake_broadcast)
    location.load_location_config = lambda: {"enabled": True}
    location.process_heartbeat = fake_process_heartbeat


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(sensing.router)
    app.include_router(activity.router)
    app.include_router(location.router)
    app.include_router(sentinel.router)
    app.include_router(events.router)
    return app


def main() -> int:
    evidence_ledger.clear()
    _patch_legacy_side_effects()

    port = int(os.environ.get("AION_PHASE8_SMOKE_PORT") or _free_port())
    base_url = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(
        _build_app(),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    try:
        _wait_for_server(base_url)
        assert _request("POST", f"{base_url}/api/sensing/tick", {
            "motion": "walking",
            "motion_confidence": 90,
            "battery_pct": 66,
        }) == {"ok": True}
        assert _request("POST", f"{base_url}/api/activity/report", {
            "device": "phone",
            "app": "微信",
            "title": "chat",
        }) == {"ok": True}
        location_payload = _request("POST", f"{base_url}/api/location/heartbeat", {
            "lng": 120.1,
            "lat": 30.2,
            "accuracy": 25.0,
        })
        assert location_payload["ok"] is True
        assert location_payload["state"] == "outside"

        snapshot = _request("GET", f"{base_url}/api/sentinel/evidence-snapshot?max_age_sec=120")
        summary = _request("GET", f"{base_url}/api/events/evidence-summary?max_age_sec=120")
        assert snapshot["count"] == 3
        assert snapshot["policy"] == {"read_only": True, "decision": None, "side_effects": []}
        assert snapshot["kind_counts"] == {
            "activity.app": 1,
            "location.fix": 1,
            "sensing.sensor": 1,
        }
        assert summary["count"] == 3
        assert summary["lifecycle"]["storage"] == "process_memory"
        print(json.dumps({
            "ok": True,
            "base_url": base_url,
            "snapshot_count": snapshot["count"],
            "summary_count": summary["count"],
            "kind_counts": snapshot["kind_counts"],
            "source_counts": snapshot["source_counts"],
        }, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        evidence_ledger.clear()


if __name__ == "__main__":
    raise SystemExit(main())
