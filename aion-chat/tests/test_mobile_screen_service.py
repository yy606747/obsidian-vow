import asyncio

import pytest

from app.mobile_screen.service import MobileScreenService, TargetDeviceInfo


def _online_tablet(name="华为平板", device_type="tablet"):
    return TargetDeviceInfo(
        exists=True, name=name, device_type=device_type,
        online=True, screen_online=True, has_capability=True,
    )


def _make_service(devices, *, enabled=True, vision=True, now_box=None):
    """devices: dict[device_id, TargetDeviceInfo]"""
    polls = []

    async def resolver(device_id):
        return devices.get(device_id, TargetDeviceInfo())

    def poll_marker(device_id, now):
        polls.append((device_id, now))
        return device_id in devices

    async def audit(event, request):
        return None

    clock = now_box or [1000.0]
    svc = MobileScreenService(
        now=lambda: clock[0],
        device_resolver=resolver,
        poll_marker=poll_marker,
        vision_checker=lambda mk: vision,
        enabled_reader=lambda: enabled,
        audit=audit,
    )
    svc._test_polls = polls
    svc._test_clock = clock
    return svc


def _create(svc, device_id, **kw):
    return asyncio.run(svc.create_request(
        conv_id=kw.get("conv_id", "conv_1"),
        msg_id=kw.get("msg_id", "msg_1"),
        model_key=kw.get("model_key", "gemini-pro"),
        target_device_id=device_id,
        reason=kw.get("reason", "看一下平板"),
    ))


def test_create_request_happy_path_is_pending_and_routed():
    svc = _make_service({"android_tab1": _online_tablet()})
    req = _create(svc, "android_tab1")
    assert req.status == "pending"
    assert req.target_device_id == "android_tab1"
    assert req.target_device_name == "华为平板"
    assert req.target_device_type == "tablet"
    assert req.conv_id == "conv_1" and req.msg_id == "msg_1"  # follow-up anchors kept


@pytest.mark.parametrize("setup,expected", [
    ("disabled", "disabled"),
    ("no_vision", "model_no_vision"),
    ("missing", "offline"),
    ("no_capability", "hard_blocked"),
])
def test_create_request_gating(setup, expected):
    info = _online_tablet()
    devices = {"android_tab1": info}
    kwargs = {}
    if setup == "disabled":
        kwargs["enabled"] = False
    elif setup == "no_vision":
        kwargs["vision"] = False
    elif setup == "missing":
        devices = {}  # device not registered
    elif setup == "no_capability":
        devices = {"android_tab1": TargetDeviceInfo(
            exists=True, name="平板", online=True, screen_online=True, has_capability=False)}
    svc = _make_service(devices, **kwargs)
    req = _create(svc, "android_tab1")
    assert req.status == "rejected"
    assert req.reject_reason == expected


def test_offline_when_screen_poll_not_alive():
    # ordinary online but screen agent not polling
    devices = {"android_tab1": TargetDeviceInfo(
        exists=True, name="平板", online=True, screen_online=False, has_capability=True)}
    svc = _make_service(devices)
    req = _create(svc, "android_tab1")
    assert req.reject_reason == "offline"


def test_duplicate_pending_per_device():
    svc = _make_service({"android_tab1": _online_tablet()})
    first = _create(svc, "android_tab1")
    second = _create(svc, "android_tab1")
    assert first.status == "pending"
    assert second.status == "rejected"
    assert second.reject_reason == "duplicate_pending"


def test_two_devices_can_each_have_a_pending():
    svc = _make_service({
        "android_tab1": _online_tablet(),
        "android_ph1": _online_tablet(name="我的手机", device_type="phone"),
    })
    tab = _create(svc, "android_tab1")
    phone = _create(svc, "android_ph1")
    assert tab.status == "pending"
    assert phone.status == "pending"
    # request meant for the tablet is not served to the phone poll
    served = asyncio.run(svc.wait_pending("android_ph1", timeout=1.0))
    assert served.request_id == phone.request_id


def test_pending_not_cross_delivered_to_wrong_device():
    svc = _make_service({
        "android_tab1": _online_tablet(),
        "android_ph1": _online_tablet(name="手机", device_type="phone"),
    })
    _create(svc, "android_tab1")  # only the tablet has a request
    served = asyncio.run(svc.wait_pending("android_ph1", timeout=1.0))
    assert served is None  # phone gets nothing


def test_wait_pending_marks_screen_poll():
    svc = _make_service({"android_tab1": _online_tablet()})
    _create(svc, "android_tab1")
    asyncio.run(svc.wait_pending("android_tab1", timeout=1.0))
    assert any(d == "android_tab1" for d, _ in svc._test_polls)


def test_decision_then_upload_completes_and_rate_limits(tmp_path, monkeypatch):
    import app.mobile_screen.service as mod
    monkeypatch.setattr(mod, "MOBILE_TMP_DIR", tmp_path / "mobile_tmp")
    monkeypatch.setattr(mod, "UPLOADS_DIR", tmp_path / "uploads")

    svc = _make_service({"android_tab1": _online_tablet()})
    req = _create(svc, "android_tab1")

    approved = asyncio.run(svc.mark_decision(req.request_id, "approved"))
    assert approved.status == "approved"

    completed = asyncio.run(svc.save_uploaded_screenshot(req.request_id, b"\xff\xd8jpegbytes"))
    assert completed.status == "completed"
    assert completed.image_path == f"{req.request_id}.jpg"
    assert (tmp_path / "uploads" / f"{req.request_id}.jpg").exists()

    # rate-limited immediately after completion
    svc.release_request(req.request_id)
    again = _create(svc, "android_tab1")
    assert again.reject_reason == "rate_limited"

    # allowed again after the window passes
    svc._test_clock[0] += mod.RATE_LIMIT_SEC + 1
    later = _create(svc, "android_tab1")
    assert later.status == "pending"


def test_rejected_decision_sets_reason():
    svc = _make_service({"android_tab1": _online_tablet()})
    req = _create(svc, "android_tab1")
    rejected = asyncio.run(svc.mark_decision(req.request_id, "rejected", "permission_denied"))
    assert rejected.status == "rejected"
    assert rejected.reject_reason == "permission_denied"


def test_invalid_reject_reason_raises():
    svc = _make_service({"android_tab1": _online_tablet()})
    req = _create(svc, "android_tab1")
    with pytest.raises(ValueError):
        svc._reject(req, "not_a_real_reason")
