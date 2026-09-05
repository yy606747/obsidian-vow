import asyncio
import json

from app.chat.models import CamCheckTrigger
from routes import cam, chat


def test_legacy_camera_status_is_disabled():
    payload = asyncio.run(cam.cam_status())

    assert payload["ok"] is False
    assert payload["enabled"] is False
    assert payload["camera_open"] is False
    assert payload["monitoring"] is False
    assert payload["error"] == "legacy_local_camera_disabled"
    assert payload["sentinel_status_url"] == "/api/sentinel/status"


def test_legacy_camera_mutations_return_gone():
    response = asyncio.run(cam.cam_monitor_start())
    payload = json.loads(response.body)

    assert response.status_code == 410
    assert payload["ok"] is False
    assert payload["error"] == "legacy_local_camera_disabled"


def test_legacy_cam_check_trigger_is_disabled():
    payload = asyncio.run(chat.cam_check_trigger(
        CamCheckTrigger(conv_id="conv_legacy_cam", model_key="mock-model")
    ))

    assert payload["ok"] is False
    assert payload["error"] == "legacy_local_camera_disabled"
