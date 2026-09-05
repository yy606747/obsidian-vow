import asyncio
from types import SimpleNamespace

import pytest

import app.chat.side_effects as side_effects
import routes.mobile_screen as routes
from app.mobile_screen.service import MobileScreenService, TargetDeviceInfo


def _tablet():
    return TargetDeviceInfo(exists=True, name="华为平板", device_type="tablet",
                            online=True, screen_online=True, has_capability=True)


def _service_with(device):
    async def resolver(device_id):
        return device if device_id == "android_tab1" else TargetDeviceInfo()

    async def audit(event, request):
        return None

    return MobileScreenService(
        device_resolver=resolver,
        poll_marker=lambda d, n: True,
        vision_checker=lambda mk: True,
        enabled_reader=lambda: True,
        audit=audit,
    )


# ── routes ──────────────────────────────────────────────

def test_pending_route_returns_204_without_request(monkeypatch):
    svc = _service_with(_tablet())
    monkeypatch.setattr(routes, "service", svc)
    resp = asyncio.run(routes.pending_mobile_screen_request(device_id="android_tab1", timeout=1.0))
    assert getattr(resp, "status_code", None) == 204


def test_pending_route_serves_routed_request(monkeypatch):
    svc = _service_with(_tablet())
    monkeypatch.setattr(routes, "service", svc)
    req = asyncio.run(svc.create_request(
        conv_id="c1", msg_id="m1", model_key="gemini", target_device_id="android_tab1", reason="看平板"))
    payload = asyncio.run(routes.pending_mobile_screen_request(device_id="android_tab1", timeout=1.0))
    assert payload["request_id"] == req.request_id
    assert payload["target_device_id"] == "android_tab1"
    assert payload["target_device_name"] == "华为平板"
    assert "ai_name" in payload


def test_decision_route_404_for_unknown(monkeypatch):
    svc = _service_with(_tablet())
    monkeypatch.setattr(routes, "service", svc)
    body = routes.MobileScreenDecision(decision="approved")
    resp = asyncio.run(routes.mobile_screen_decision("nope", body))
    assert getattr(resp, "status_code", None) == 404


def test_decision_route_approves(monkeypatch):
    svc = _service_with(_tablet())
    monkeypatch.setattr(routes, "service", svc)
    req = asyncio.run(svc.create_request(
        conv_id="c1", msg_id="m1", model_key="gemini", target_device_id="android_tab1", reason="x"))
    body = routes.MobileScreenDecision(decision="approved")
    result = asyncio.run(routes.mobile_screen_decision(req.request_id, body))
    assert result["ok"] is True
    assert result["request"]["status"] == "approved"


def test_get_config_reflects_service(monkeypatch):
    svc = _service_with(_tablet())
    monkeypatch.setattr(routes, "service", svc)
    cfg = asyncio.run(routes.get_mobile_screen_config())
    assert cfg == {"mobile_screen_capture_enabled": True}


# ── follow-up wrapper wiring (DB-free, runner stubbed) ──

def test_mobile_followup_uses_device_label_and_mobile_reject_text(monkeypatch):
    captured = {}

    async def fake_runner(request, *, ops, screen_label, reject_text):
        captured["ops"] = ops
        captured["screen_label"] = screen_label
        captured["reject_text"] = reject_text

    monkeypatch.setattr(side_effects, "_run_screen_followup", fake_runner)
    request = SimpleNamespace(target_device_name="华为平板")
    asyncio.run(side_effects.perform_mobile_screen_check(request))

    assert captured["screen_label"] == "华为平板"
    # offline wording is device-specific
    assert "华为平板" in captured["reject_text"]["offline"]
    # android-only reasons are present
    assert "permission_denied" in captured["reject_text"]
    assert "capture_failed" in captured["reject_text"]
    # ops bound to the mobile service timeout
    from app.mobile_screen import mobile_screen_service
    assert captured["ops"].timeout == mobile_screen_service.request_timeout_sec()


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


def test_event_payload_includes_target_device_for_mobile_only():
    from app.mobile_screen.schemas import MobileScreenCheckRequest

    mobile = MobileScreenCheckRequest(
        request_id="r1", conv_id="c1", msg_id="m1", model_key="gemini",
        reason="看平板", target_device_id="android_tab1",
        target_device_name="华为平板", target_device_type="tablet",
    )
    p = side_effects._screen_event_payload(mobile)
    assert p["target_device_id"] == "android_tab1"
    assert p["target_device_name"] == "华为平板"
    assert p["target_device_type"] == "tablet"

    pc = SimpleNamespace(request_id="r2", conv_id="c1", msg_id="m1",
                         reason="x", status="pending", reject_reason="")
    p2 = side_effects._screen_event_payload(pc)
    assert "target_device_id" not in p2  # PC request unaffected


def test_mobile_followup_falls_back_to_phone_label(monkeypatch):
    captured = {}

    async def fake_runner(request, *, ops, screen_label, reject_text):
        captured["screen_label"] = screen_label

    monkeypatch.setattr(side_effects, "_run_screen_followup", fake_runner)
    asyncio.run(side_effects.perform_mobile_screen_check(SimpleNamespace(target_device_name="")))
    assert captured["screen_label"] == "手机"
