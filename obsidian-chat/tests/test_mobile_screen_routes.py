"""Compatibility behavior after phone/tablet one-shot capture retirement."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

import app.chat.side_effects as side_effects
import routes.mobile_screen as routes


@pytest.mark.parametrize("timeout, expected", [(0, 5.0), (15, 15.0), (999, 30.0), (float("nan"), 30.0)])
def test_old_apk_poll_is_empty_and_does_not_busy_loop(monkeypatch, timeout, expected):
    waits = []

    async def sleep(delay):
        waits.append(delay)

    monkeypatch.setattr(routes.asyncio, "sleep", sleep)

    async def run():
        app = FastAPI()
        app.include_router(routes.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.get("/api/mobile-screen/pending", params={"device_id": "android_old", "timeout": timeout})

    response = asyncio.run(run())
    assert response.status_code == 204
    assert response.content == b""
    assert response.headers["X-Obsidian-Retired"] == "mobile-screen"
    assert waits == [expected]


@pytest.mark.parametrize("method, path", [
    ("GET", "config"), ("PUT", "config"),
    ("POST", "old-request/decision"), ("POST", "old-request/upload"),
])
def test_old_apk_actions_are_retired_without_creating_capture(method, path):
    async def run():
        app = FastAPI()
        app.include_router(routes.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.request(method, "/api/mobile-screen/" + path,
                                        content=b"obsolete request or screenshot")

    response = asyncio.run(run())
    assert response.status_code == 410
    assert response.json()["error"] == "mobile_screen_retired"
    assert response.json()["replacement"] == "together"


def test_pc_followup_keeps_label_and_pc_reject_text(monkeypatch):
    captured = {}

    async def fake_runner(request, *, ops, screen_label, reject_text):
        captured["screen_label"] = screen_label
        captured["reject_text"] = reject_text

    monkeypatch.setattr(side_effects, "_run_screen_followup", fake_runner)
    request = SimpleNamespace()
    asyncio.run(side_effects.perform_screen_check(request))

    assert captured["screen_label"] == "电脑"
    assert captured["reject_text"] is side_effects._PC_REJECT_TEXT
    assert "permission_denied" not in captured["reject_text"]  # PC has no projection reasons
