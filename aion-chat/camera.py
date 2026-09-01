"""Disabled legacy local camera compatibility surface.

Cloud deployments should not depend on local webcam APIs. Future camera input
must be reintroduced as a Sentinel evidence source adapter, not through this
module.
"""

from __future__ import annotations

from config import load_cam_config


CAM_CHECK_CMD = "[CAM_CHECK]"
CAMERA_DISABLED_REASON = "legacy_local_camera_disabled"


class CameraFeatureDisabled(RuntimeError):
    """Raised if old camera analysis is called after the compatibility cleanup."""


class DisabledCamera:
    enabled = False
    running = False
    monitoring = False

    def __init__(self):
        self.cfg = load_cam_config()

    def open_camera(self, index: int | None = None) -> bool:
        return False

    def close_camera(self) -> None:
        return None

    def save_screenshot(self):
        return None

    def get_frame_jpeg(self):
        return None

    def set_crop(self, zoom, cx, cy) -> None:
        return None

    def get_crop(self) -> dict:
        return {"zoom": 1.0, "cx": 0.5, "cy": 0.5}


cam = DisabledCamera()


def detect_cameras(max_test: int = 5, skip_index: int = -1) -> list:
    return []


async def perform_cam_check(conv_id: str, model_key: str):
    raise CameraFeatureDisabled(
        "[CAM_CHECK] is disabled; add camera input through Sentinel evidence adapters."
    )
