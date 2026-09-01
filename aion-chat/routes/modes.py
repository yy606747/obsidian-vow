"""Read-only mode and capability diagnostics."""

from __future__ import annotations

from fastapi import APIRouter

from app.modes import ChatMode, mode_service


router = APIRouter()


def _snapshot_payload(mode: ChatMode | str, *, source: str = "catalog") -> dict:
    return mode_service.snapshot(mode, source=source).to_dict()


@router.get("/api/modes")
async def list_modes():
    return {
        "default_mode": ChatMode.NORMAL.value,
        "modes": [
            _snapshot_payload(mode)
            for mode in ChatMode
        ],
    }


@router.get("/api/modes/resolve")
async def resolve_mode(
    mode: str = "normal",
    whisper_mode: bool = False,
    ai_dom_mode: bool = False,
):
    if whisper_mode or ai_dom_mode:
        return mode_service.snapshot_from_flags(
            whisper_mode=whisper_mode,
            ai_dom_mode=ai_dom_mode,
            fallback=mode,
        ).to_dict()
    return mode_service.snapshot(mode, source="query").to_dict()
