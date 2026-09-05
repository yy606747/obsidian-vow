import asyncio
import io
import uuid
from contextlib import asynccontextmanager

import aiosqlite
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.presence.db import init_presence_tables
from app.presence.service import PresenceDeliveryService
from app.presence.sprites import SpriteLibrary
from routes import presence as presence_routes


class Ledger:
    async def record_terminal_outcome(self, **_kwargs):
        return 1


def _png():
    image = Image.new("RGBA", (20, 16), (0, 0, 0, 0))
    for x in range(4, 16):
        for y in range(3, 13):
            image.putpixel((x, y), (100, 60, 210, 255))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _trajectory():
    return {
        "sprite_id": "route_fog",
        "target_screen": "primary",
        "anchor": "center",
        "transform_origin": "center",
        "duration_ms": 500,
        "tracks": [{"prop": "opacity", "keys": [[0, 0], [500, 1]]}],
    }


def test_presence_http_delivery_ack_and_independent_sprite_channel(tmp_path, monkeypatch):
    db_path = tmp_path / "routes.db"

    @asynccontextmanager
    async def db_factory():
        async with aiosqlite.connect(db_path, timeout=2) as db:
            yield db

    sprites = SpriteLibrary(
        get_db_factory=db_factory,
        storage_dir=tmp_path / "sprites",
        now=lambda: 1_000.0,
        timezone_name="UTC",
    )
    service = PresenceDeliveryService(
        get_db_factory=db_factory,
        sprites=sprites,
        now=lambda: 1_000.0,
        monotonic=lambda: 1_000.0,
        terminal_recorder=Ledger(),
    )

    async def arrange():
        async with db_factory() as db:
            await init_presence_tables(db)
            await db.commit()
        return await sprites.add_sprite(
            sprite_id="route_fog",
            png=_png(),
            description="route test fog",
            base_height_dip=200,
        )

    added = asyncio.run(arrange())
    monkeypatch.setattr(presence_routes, "sprite_library", sprites)
    monkeypatch.setattr(presence_routes, "presence_service", service)
    app = FastAPI()
    app.include_router(presence_routes.router)

    with TestClient(app) as client:
        manifest = client.get("/api/presence/sprites/manifest")
        assert manifest.status_code == 200
        assert manifest.json()["sprites"][0]["sync_status"] == "pending"

        downloaded = client.get(
            f"/api/presence/sprites/{added['sprite_hash']}"
        )
        assert downloaded.status_code == 200
        assert downloaded.headers["content-type"] == "image/png"
        assert downloaded.content == _png()

        synced = client.post(
            f"/api/presence/sprites/{added['sprite_hash']}/synced",
            json={"device_id": "pc"},
        )
        assert synced.status_code == 200
        assert synced.json()["available_on_device"] is True

        event = asyncio.run(service.enqueue_trajectory(
            conv_id="conv-route", intent_text="route", trajectory=_trajectory()
        ))
        pending = client.get("/api/presence/pending?timeout=0&device_id=pc")
        assert pending.status_code == 200
        assert pending.json()["event_id"] == event["event_id"]
        assert pending.json()["server_now"] == 1_000.0

        accepted = client.post(
            f"/api/presence/{event['event_id']}/ack",
            json={"status": "accepted", "device_id": "pc"},
        )
        assert accepted.status_code == 200
        too_long = client.post(
            f"/api/presence/{event['event_id']}/ack",
            json={
                "status": "played",
                "actual_playback_ms": 2_501,
                "device_id": "pc",
            },
        )
        assert too_long.status_code == 400
        assert too_long.json()["detail"] == "presence_actual_playback_invalid"
        played = client.post(
            f"/api/presence/{event['event_id']}/ack",
            json={
                "status": "played",
                "actual_playback_ms": 500,
                "device_id": "pc",
            },
        )
        assert played.status_code == 200
        assert played.json()["status"] == "played"
        duplicate = client.post(
            f"/api/presence/{event['event_id']}/ack",
            json={"status": "accepted", "device_id": "pc"},
        )
        assert duplicate.json()["status"] == "played"

        missing = client.post(
            "/api/presence/missing-event/ack",
            json={
                "status": "played",
                "actual_playback_ms": -1,
                "device_id": "pc",
            },
        )
        assert missing.status_code == 404
        assert missing.json()["detail"] == "presence_event_not_found"

        empty = client.get("/api/presence/pending?timeout=0&device_id=pc")
        assert empty.status_code == 204


def test_summon_route_is_opaque_idempotent_and_backgrounded(monkeypatch):
    summon_id = str(uuid.uuid4())
    inserted = True
    scheduled = []

    async def target():
        return {"conv_id": "conv", "model_key": "core", "last_user_ts": 10}

    class Repository:
        async def insert(self, **kwargs):
            return {"inserted": inserted, "event": {**kwargs, "status": "processing"}}

    class Coordinator:
        async def process(self, **_kwargs):
            return {"status": "processed"}

    def track(coro, *, name):
        scheduled.append(name)
        coro.close()

    monkeypatch.setattr(
        presence_routes,
        "load_ai_behavior",
        lambda: {"presence_summon_enabled": True},
    )
    monkeypatch.setattr(presence_routes, "resolve_summon_target", target)
    monkeypatch.setattr(presence_routes, "summon_event_repository", Repository())
    monkeypatch.setattr(presence_routes, "summon_coordinator", Coordinator())
    monkeypatch.setattr(presence_routes, "create_tracked_task", track)
    app = FastAPI()
    app.include_router(presence_routes.router)

    with TestClient(app) as client:
        first = client.post(
            "/api/presence/summon",
            json={"summon_id": summon_id, "device_id": "pc"},
        )
        assert first.status_code == 202
        assert first.json() == {"accepted": True, "summon_id": summon_id}
        assert scheduled == [f"presence_summon:{summon_id}"]

        inserted = False
        duplicate = client.post(
            "/api/presence/summon",
            json={"summon_id": summon_id, "device_id": "pc"},
        )
        assert duplicate.status_code == 202
        assert duplicate.json() == first.json()
        assert len(scheduled) == 1


def test_summon_route_rejects_disabled_and_missing_target(monkeypatch):
    app = FastAPI()
    app.include_router(presence_routes.router)
    summon_id = str(uuid.uuid4())

    monkeypatch.setattr(
        presence_routes,
        "load_ai_behavior",
        lambda: {"presence_summon_enabled": False},
    )
    with TestClient(app) as client:
        assert client.post(
            "/api/presence/summon",
            json={"summon_id": summon_id},
        ).status_code == 404

    async def no_target():
        return None

    monkeypatch.setattr(
        presence_routes,
        "load_ai_behavior",
        lambda: {"presence_summon_enabled": True},
    )
    monkeypatch.setattr(presence_routes, "resolve_summon_target", no_target)
    with TestClient(app) as client:
        assert client.post(
            "/api/presence/summon",
            json={"summon_id": summon_id},
        ).status_code == 409
