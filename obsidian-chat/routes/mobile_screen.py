"""Retirement responses for old Android clients; no capture queue or model calls.

Old APKs retry immediately after 204, and every ten seconds after an error.
Keep their empty poll bounded until those devices upgrade. New APKs never poll.
"""

import asyncio
import math

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

router = APIRouter()


@router.get("/api/mobile-screen/pending", include_in_schema=False)
async def retired_mobile_screen_poll(timeout: float = 30):
    await asyncio.sleep(min(30.0, max(5.0, timeout)) if math.isfinite(timeout) else 30.0)
    return Response(status_code=204, headers={"X-Obsidian-Retired": "mobile-screen"})


@router.api_route("/api/mobile-screen/{path:path}", methods=["GET", "POST", "PUT"], include_in_schema=False)
async def retired_mobile_screen_endpoint(path: str):
    return JSONResponse(status_code=410, content={
        "error": "mobile_screen_retired", "replacement": "together",
        "message": "手机和平板看屏已并入相伴，请更新 Android 安装包后从相伴开启。",
    })
