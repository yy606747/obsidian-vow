import asyncio
import os
import time

import pytest

from app.pc_screen import service


@pytest.fixture(autouse=True)
def reset_pc_screen(monkeypatch):
    service._reset_state_for_tests()
    monkeypatch.setitem(service.SETTINGS, "screen_capture_enabled", True)
    monkeypatch.setattr(service, "model_supports_vision", lambda _model_key: True)
    async def noop_audit(_event, _request):
        return None
    monkeypatch.setattr(service, "audit_screen_event", noop_audit)
    yield
    service._reset_state_for_tests()


def test_disabled_gateway_does_not_create_request(monkeypatch):
    monkeypatch.setitem(service.SETTINGS, "screen_capture_enabled", False)

    request = asyncio.run(service.create_screen_request(
        conv_id="conv",
        msg_id="msg",
        model_key="gemini",
        reason="看一眼",
    ))

    assert request is None
    assert service.current_request is None


def test_offline_gateway_returns_rejected_request():
    request = asyncio.run(service.create_screen_request(
        conv_id="conv",
        msg_id="msg",
        model_key="gemini",
        reason="看一眼",
    ))

    assert request is not None
    assert request.status == "rejected"
    assert request.reject_reason == "offline"
    assert service.current_request is None


def test_pending_request_long_poll_and_decision():
    async def run():
        waiter = asyncio.create_task(service.wait_pending_request(1))
        await asyncio.sleep(0)
        request = await service.create_screen_request(
            conv_id="conv",
            msg_id="msg",
            model_key="gemini",
            reason="确认状态",
        )
        pending = await waiter
        assert pending is request
        assert service.current_request is request
        await service.mark_decision(request.request_id, "approved")
        assert request.status == "approved"
        await service.mark_decision(request.request_id, "rejected", "locked")
        assert request.status == "rejected"
        assert request.reject_reason == "locked"

    asyncio.run(run())


def test_rate_limit_after_completed_upload(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "SCREEN_TMP_DIR", tmp_path / "screens")
    monkeypatch.setattr(service, "UPLOADS_DIR", tmp_path / "uploads")

    async def run():
        waiter = asyncio.create_task(service.wait_pending_request(1))
        await asyncio.sleep(0)
        request = await service.create_screen_request(
            conv_id="conv",
            msg_id="msg",
            model_key="gemini",
            reason="确认状态",
        )
        await waiter
        await service.mark_decision(request.request_id, "approved")
        await service.save_uploaded_screenshot(request.request_id, b"jpg")
        assert request.status == "completed"
        service.release_request(request.request_id)
        blocked = await service.create_screen_request(
            conv_id="conv",
            msg_id="msg2",
            model_key="gemini",
            reason="再看一次",
        )
        assert blocked.status == "rejected"
        assert blocked.reject_reason == "rate_limited"

    asyncio.run(run())


def test_model_no_vision_rejection(monkeypatch):
    monkeypatch.setattr(service, "model_supports_vision", lambda _model_key: False)

    request = asyncio.run(service.create_screen_request(
        conv_id="conv",
        msg_id="msg",
        model_key="text-only",
        reason="看一眼",
    ))

    assert request.status == "rejected"
    assert request.reject_reason == "model_no_vision"


def test_duplicate_pending_rejection_preserves_current_request():
    async def run():
        waiter = asyncio.create_task(service.wait_pending_request(1))
        await asyncio.sleep(0)
        first = await service.create_screen_request(
            conv_id="conv",
            msg_id="msg",
            model_key="gemini",
            reason="第一次",
        )
        await waiter
        second = await service.create_screen_request(
            conv_id="conv",
            msg_id="msg2",
            model_key="gemini",
            reason="第二次",
        )
        assert first.status == "pending"
        assert service.current_request is first
        assert second.status == "rejected"
        assert second.reject_reason == "duplicate_pending"

    asyncio.run(run())


def test_expire_request_marks_timeout_and_sets_event():
    async def run():
        waiter = asyncio.create_task(service.wait_pending_request(1))
        await asyncio.sleep(0)
        request = await service.create_screen_request(
            conv_id="conv",
            msg_id="msg",
            model_key="gemini",
            reason="确认状态",
        )
        await waiter
        service.expire_request(request)
        assert request.status == "rejected"
        assert request.reject_reason == "request_expired"
        assert request._done_event.is_set()

    asyncio.run(run())


def test_invalid_reject_reason_raises():
    async def run():
        waiter = asyncio.create_task(service.wait_pending_request(1))
        await asyncio.sleep(0)
        request = await service.create_screen_request(
            conv_id="conv",
            msg_id="msg",
            model_key="gemini",
            reason="确认状态",
        )
        await waiter
        with pytest.raises(ValueError):
            await service.mark_decision(request.request_id, "rejected", "bad_reason")

    asyncio.run(run())


def test_cleanup_expired_files_removes_tmp_and_upload_copy(monkeypatch, tmp_path):
    tmp_dir = tmp_path / "screens"
    upload_dir = tmp_path / "uploads"
    tmp_dir.mkdir()
    upload_dir.mkdir()
    monkeypatch.setattr(service, "SCREEN_TMP_DIR", tmp_dir)
    monkeypatch.setattr(service, "UPLOADS_DIR", upload_dir)
    old_tmp = tmp_dir / "old.jpg"
    old_upload = upload_dir / "old.jpg"
    fresh_tmp = tmp_dir / "fresh.jpg"
    old_tmp.write_bytes(b"old")
    old_upload.write_bytes(b"old")
    fresh_tmp.write_bytes(b"fresh")
    old_mtime = time.time() - service.SCREENSHOT_TTL_SEC - 5
    os.utime(old_tmp, (old_mtime, old_mtime))

    deleted = service.cleanup_expired_files()

    assert deleted == 1
    assert not old_tmp.exists()
    assert not old_upload.exists()
    assert fresh_tmp.exists()


def test_screen_agent_online_uses_poll_heartbeat():
    service.last_screen_poll_at = 100.0

    assert service.screen_agent_online(190.0)
    assert not service.screen_agent_online(191.0)
