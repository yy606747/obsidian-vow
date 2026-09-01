"""Sentinel diagnostics and control."""

from __future__ import annotations

import time
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.sentinel import sentinel_evidence_service
from app.sentinel.evidence import (
    DEFAULT_SENTINEL_EVIDENCE_LIMIT,
    DEFAULT_SENTINEL_EVIDENCE_MAX_AGE_SEC,
)
from config import MONITOR_LOGS_DIR, save_cam_config
from context_delivery_shadow_runtime import context_trigger_shadow_runtime
from sentinel_runtime import read_monitor_logs, sentinel_runtime


router = APIRouter()


class SentinelConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    quiet_hours_enabled: Optional[bool] = None
    quiet_hours_start: Optional[str] = None
    quiet_hours_end: Optional[str] = None
    auto_interval_min: Optional[int] = None
    auto_interval_max: Optional[int] = None


class ContextTriggerShadowLabelUpdate(BaseModel):
    label: Literal["right", "wrong", "indifferent"]


@router.get("/api/sentinel/evidence-snapshot")
async def get_evidence_snapshot(
    max_age_sec: float = DEFAULT_SENTINEL_EVIDENCE_MAX_AGE_SEC,
    limit: int = DEFAULT_SENTINEL_EVIDENCE_LIMIT,
    kind: list[str] | None = Query(default=None),
    source: list[str] | None = Query(default=None),
):
    return sentinel_evidence_service.snapshot_payload(
        max_age_sec=max_age_sec,
        limit=limit,
        kinds=kind,
        sources=source,
    )


@router.get("/api/sentinel/status")
async def get_sentinel_status():
    return sentinel_runtime.status_payload()


@router.get("/api/sentinel/context-trigger-shadow")
async def get_context_trigger_shadow(
    include_non_matched: bool = False,
    rule: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    before_occurred_at: float | None = None,
):
    context_trigger_shadow_runtime.refresh_due_outcomes()
    return context_trigger_shadow_runtime.list_payload(
        include_non_matched=include_non_matched,
        rule=rule,
        limit=limit,
        before_occurred_at=before_occurred_at,
    )


@router.put("/api/sentinel/context-trigger-shadow/{evaluation_id}/label")
async def put_context_trigger_shadow_label(
    evaluation_id: str,
    body: ContextTriggerShadowLabelUpdate,
):
    try:
        entry = context_trigger_shadow_runtime.set_owner_label(
            evaluation_id,
            body.label,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "entry": entry}


@router.put("/api/sentinel/config")
async def update_sentinel_config(body: SentinelConfigUpdate):
    cfg = sentinel_runtime.cfg
    if body.quiet_hours_enabled is not None:
        cfg["quiet_hours_enabled"] = body.quiet_hours_enabled
    if body.quiet_hours_start is not None:
        cfg["quiet_hours_start"] = body.quiet_hours_start
    if body.quiet_hours_end is not None:
        cfg["quiet_hours_end"] = body.quiet_hours_end
    if body.auto_interval_min is not None:
        cfg["auto_interval_min"] = max(1, body.auto_interval_min)
    if body.auto_interval_max is not None:
        cfg["auto_interval_max"] = max(1, body.auto_interval_max)
    if cfg["auto_interval_max"] < cfg.get("auto_interval_min", 1):
        cfg["auto_interval_max"] = cfg["auto_interval_min"]
    save_cam_config(cfg)
    if body.enabled is not None:
        if body.enabled and not sentinel_runtime.monitoring:
            sentinel_runtime.start_monitoring()
        elif not body.enabled and sentinel_runtime.monitoring:
            sentinel_runtime.stop_monitoring()
    return sentinel_runtime.status_payload()


@router.get("/api/sentinel/logs")
async def list_sentinel_log_dates():
    dates = [f.stem for f in sorted(MONITOR_LOGS_DIR.glob("*.jsonl"), reverse=True)]
    return {"dates": dates}


@router.get("/api/sentinel/logs/today/entries")
async def get_today_sentinel_logs():
    entries = read_monitor_logs()
    return {"date": time.strftime("%Y-%m-%d"), "entries": entries}


@router.get("/api/sentinel/logs/{date_str}")
async def get_sentinel_log_entries(date_str: str):
    entries = read_monitor_logs(date_str)
    return {"date": date_str, "entries": entries}
