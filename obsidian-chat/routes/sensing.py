"""
体感/体征/通知上报 API：接收 Android 端数据，落盘，广播给前端。
"""

import asyncio
import logging
import math
import time
from datetime import date
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sensing import (
    append_sensing_entries, append_sensing_entry, cleanup_old_sensing_logs,
    read_recent_sensing, format_sensing_for_prompt,
)
from app.daily_signals.aggregation import reconcile_daily_biometrics
from app.daily_signals.biometrics import (
    batch_audit_entries,
    batch_leaf_count,
    batch_to_observations,
    sensing_entry_to_observations,
)
from app.daily_signals.config import daily_timezone_name
from app.daily_signals.store import DailySignalStore, get_default_store
from app.legacy_adapters.evidence import record_sensing_entry_safely
from ws import manager

router = APIRouter()
log = logging.getLogger(__name__)


# ── 上报模型 ──────────────────────────────────────

class SensorTick(BaseModel):
    timestamp: Optional[float] = None
    motion: Optional[str] = None         # still | walking | running | in_vehicle | on_bicycle | tilting | unknown
    motion_confidence: Optional[int] = None
    light_lux: Optional[float] = None
    pressure_hpa: Optional[float] = None
    wifi_ssid: Optional[str] = None
    battery_pct: Optional[int] = None
    charging: Optional[bool] = None
    screen_on: Optional[bool] = None


class BiometricTick(BaseModel):
    timestamp: Optional[float] = None
    heart_rate: Optional[int] = None
    heart_rate_observed_at: Optional[float] = None
    spo2: Optional[int] = None
    spo2_observed_at: Optional[float] = None
    sleep_stage: Optional[str] = None    # awake | light | deep | rem
    steps_delta: Optional[int] = None
    steps_total_today: Optional[int] = None
    steps_total_date: Optional[str] = None
    steps_total_timezone: Optional[str] = None
    stress: Optional[int] = None


class _BatchModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HeartRateSample(_BatchModel):
    observed_at: float
    bpm: int = Field(gt=0)

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: float) -> float:
        return _finite_time(value)


class HeartRateBatchRecord(_BatchModel):
    source_id: str
    start_at: float
    end_at: float
    samples: list[HeartRateSample] = Field(min_length=1)

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        return _source_id(value)

    @field_validator("start_at", "end_at")
    @classmethod
    def validate_times(cls, value: float) -> float:
        return _finite_time(value)

    @model_validator(mode="after")
    def validate_interval(self):
        if self.start_at >= self.end_at:
            raise ValueError("start_at must be before end_at")
        return self


class Spo2BatchRecord(_BatchModel):
    source_id: str
    observed_at: float
    percentage: float = Field(ge=0, le=100)

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        return _source_id(value)

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: float) -> float:
        return _finite_time(value)


class SleepStageInterval(_BatchModel):
    start_at: float
    end_at: float
    stage: Literal["awake", "light", "deep", "rem", "sleeping"]

    @field_validator("start_at", "end_at")
    @classmethod
    def validate_times(cls, value: float) -> float:
        return _finite_time(value)

    @model_validator(mode="after")
    def validate_interval(self):
        if self.start_at >= self.end_at:
            raise ValueError("start_at must be before end_at")
        return self


class SleepSessionBatchRecord(_BatchModel):
    source_id: str
    start_at: float
    end_at: float
    stages: list[SleepStageInterval] = Field(default_factory=list)

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        return _source_id(value)

    @field_validator("start_at", "end_at")
    @classmethod
    def validate_times(cls, value: float) -> float:
        return _finite_time(value)

    @model_validator(mode="after")
    def validate_interval(self):
        if self.start_at >= self.end_at:
            raise ValueError("start_at must be before end_at")
        return self


class StepsDailyBatchRecord(_BatchModel):
    source_id: str
    daily_date: str
    aggregation_timezone: str
    total: int = Field(ge=0)
    observed_at: float

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        return _source_id(value)

    @field_validator("daily_date")
    @classmethod
    def validate_daily_date(cls, value: str) -> str:
        date.fromisoformat(value)
        return value

    @field_validator("aggregation_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("aggregation_timezone is required")
        return value

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: float) -> float:
        return _finite_time(value)


class BiometricBatch(_BatchModel):
    sent_at: float
    device_timezone: str
    heart_rate_records: list[HeartRateBatchRecord] = Field(default_factory=list)
    spo2_records: list[Spo2BatchRecord] = Field(default_factory=list)
    sleep_sessions: list[SleepSessionBatchRecord] = Field(default_factory=list)
    steps_daily: list[StepsDailyBatchRecord] = Field(default_factory=list)

    @field_validator("sent_at")
    @classmethod
    def validate_sent_at(cls, value: float) -> float:
        return _finite_time(value)

    @field_validator("device_timezone")
    @classmethod
    def validate_device_timezone(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("device_timezone is required")
        return value

    @model_validator(mode="after")
    def validate_leaf_count(self):
        if batch_leaf_count(self.model_dump()) > 1000:
            raise ValueError("batch exceeds 1000 leaf observations")
        return self


class BiometricTimezoneMismatch(ValueError):
    pass


class NotificationTick(BaseModel):
    timestamp: Optional[float] = None
    app: str                             # 已解析的中文名（微信/QQ 等）
    package: Optional[str] = None


class UnlockTick(BaseModel):
    timestamp: Optional[float] = None


def _store(type_: str, ts: Optional[float], data: dict):
    now = time.time()
    t = ts or now
    entry = {
        "timestamp": t,
        "time": time.strftime("%H:%M:%S", time.localtime(t)),
        "date": time.strftime("%Y-%m-%d", time.localtime(t)),
        "type": type_,
        "data": {k: v for k, v in data.items() if v is not None},
    }
    append_sensing_entry(entry)
    try:
        cleanup_old_sensing_logs()
    except Exception:
        pass
    return entry


# ── 路由 ──────────────────────────────────────────

@router.post("/api/sensing/tick")
async def report_sensor(tick: SensorTick):
    data = tick.model_dump()
    ts = data.pop("timestamp", None)
    entry = _store("sensor", ts, data)
    record_sensing_entry_safely(entry)
    await manager.broadcast({"type": "sensing_tick", "data": entry})
    return {"ok": True}


@router.post("/api/biometrics/tick")
async def report_biometric(tick: BiometricTick):
    data = tick.model_dump()
    ts = data.pop("timestamp", None)
    entry = _store("biometric", ts, data)
    try:
        await asyncio.to_thread(shadow_biometric_tick, entry)
    except Exception as exc:
        # The legacy live endpoint must retain its previous availability even
        # if the new durable shadow path is temporarily unavailable.
        log.warning("Biometric tick shadow upsert skipped: %s", exc)
    record_sensing_entry_safely(entry)
    await manager.broadcast({"type": "sensing_tick", "data": entry})
    return {"ok": True}


@router.post("/api/biometrics/batch")
async def report_biometric_batch(batch: BiometricBatch):
    try:
        return await asyncio.to_thread(ingest_biometric_batch, batch)
    except BiometricTimezoneMismatch as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/notification/tick")
async def report_notification(tick: NotificationTick):
    data = tick.model_dump()
    ts = data.pop("timestamp", None)
    entry = _store("notification", ts, data)
    record_sensing_entry_safely(entry)
    await manager.broadcast({"type": "sensing_tick", "data": entry})
    return {"ok": True}


@router.post("/api/unlock/tick")
async def report_unlock(tick: UnlockTick):
    ts = tick.timestamp
    entry = _store("unlock", ts, {})
    record_sensing_entry_safely(entry)
    await manager.broadcast({"type": "sensing_tick", "data": entry})
    return {"ok": True}


# ── 查询（用于调试 / 前端面板） ─────────────────────

@router.get("/api/sensing/recent")
async def get_recent(hours: int = 3):
    entries = read_recent_sensing(hours)
    return {"entries": entries, "hours": hours}


@router.get("/api/sensing/preview")
async def get_preview(hours: int = 3):
    """预览哨兵会看到的文字 timeline"""
    return {"text": format_sensing_for_prompt(hours), "hours": hours}


def ingest_biometric_batch(
    batch: BiometricBatch,
    *,
    store: DailySignalStore | None = None,
    timezone_name: str | None = None,
    audit_writer=None,
    sensing_logs_dir: Path | str | None = None,
) -> dict:
    """Durably ingest a historical batch and reconcile before acknowledging it."""

    zone_name = timezone_name or daily_timezone_name()
    payload = batch.model_dump()
    mismatched = sorted(
        {
            record["aggregation_timezone"]
            for record in payload["steps_daily"]
            if record["aggregation_timezone"] != zone_name
        }
    )
    if mismatched:
        raise BiometricTimezoneMismatch(
            "steps aggregation_timezone must equal server daily_timezone "
            f"{zone_name!r}; received {mismatched!r}"
        )

    received_count = batch_leaf_count(payload)
    empty_result = {
        "received_count": received_count,
        "upserted_count": 0,
        "affected_dates": [],
        "reconciled_dates": [],
        "deferred_dates": [],
        "daily_timezone": zone_name,
    }
    if received_count == 0:
        return empty_result

    target_store = store or get_default_store()
    observations = batch_to_observations(payload, zone_name)
    upsert = target_store.upsert_biometric_observations(
        observations,
        zone_name,
    )

    received_at = time.time()
    audit_entries = batch_audit_entries(payload, received_at)
    (audit_writer or append_sensing_entries)(audit_entries)
    if audit_writer is None:
        try:
            cleanup_old_sensing_logs()
        except Exception:
            pass

    reconciliation = reconcile_daily_biometrics(
        store=target_store,
        sensing_logs_dir=sensing_logs_dir,
        timezone_name=zone_name,
        dates=upsert["affected_dates"],
    )
    return {
        "received_count": received_count,
        "upserted_count": upsert["upserted_count"],
        "affected_dates": upsert["affected_dates"],
        "reconciled_dates": reconciliation["reconciled_dates"],
        "deferred_dates": reconciliation["deferred_dates"],
        "daily_timezone": zone_name,
    }


def shadow_biometric_tick(
    entry: dict,
    *,
    store: DailySignalStore | None = None,
    timezone_name: str | None = None,
) -> dict:
    zone_name = timezone_name or daily_timezone_name()
    observations = sensing_entry_to_observations(entry, zone_name)
    return (store or get_default_store()).upsert_biometric_observations(
        observations,
        zone_name,
    )


def _finite_time(value: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("timestamp must be finite")
    return value


def _source_id(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("source_id is required")
    return value
