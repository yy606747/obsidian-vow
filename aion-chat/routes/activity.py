"""
设备活动日志 API：上报、查询日期列表、查询指定日期日志、查询最近 N 小时日志
"""

import json, time

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

from activity import (
    append_activity_log, read_activity_logs, read_recent_activity,
    get_available_dates, cleanup_old_activity_logs, KEEP_HOURS,
    resolve_app_name, generate_activity_summary,
    is_activity_tracking_enabled, set_activity_tracking_enabled,
)
from app.legacy_adapters.evidence import record_activity_entry_safely
from app.pc_context.service import get_pc_status_payload, ingest_report as ingest_pc_report
from ws import manager

router = APIRouter()


# ── 上报 ──────────────────────────────────────────

class ActivityReport(BaseModel):
    device: str             # legacy coarse class: "phone" | "pc" | "tablet"
    app: Optional[str] = ""  # 应用名 / 进程名
    title: Optional[str] = ""   # 窗口标题 / 额外描述
    timestamp: Optional[float] = None  # 客户端时间戳，不传则用服务端时间
    active_state: Optional[str] = None
    last_input_age_sec: Optional[int] = None
    # 移动端多设备身份（迁移期与 legacy device 双写；PC 上报留空）
    device_id: Optional[str] = None
    device_name: Optional[str] = None
    device_type: Optional[str] = None  # "phone" | "tablet" | "unknown"
    platform: Optional[str] = None     # "android"


@router.post("/api/activity/report")
async def report_activity(report: ActivityReport):
    """接收设备活动上报"""
    if report.device == "pc":
        return await _report_pc_activity(report)

    # 解析 App 名称（包名 → 中文名）
    resolved = resolve_app_name(report.app or "", report.title or "")
    if resolved is None:
        # 需要过滤的系统应用（桌面、SystemUI 等）
        return {"ok": True, "filtered": True}

    now = time.time()
    ts = report.timestamp or now

    entry = {
        "timestamp": ts,
        "time": time.strftime("%H:%M:%S", time.localtime(ts)),
        "date": time.strftime("%Y-%m-%d", time.localtime(ts)),
        "device": report.device,
        "app": resolved,
        "title": report.title or "",
    }
    # 迁移期：携带稳定设备身份，供摘要按 device_id 分组、按 device_name 展示。
    for field in ("device_id", "device_name", "device_type", "platform"):
        value = getattr(report, field, None)
        if value:
            entry[field] = value

    append_activity_log(entry)
    record_activity_entry_safely(entry)

    # 每次上报后顺带清理过期数据
    try:
        cleanup_old_activity_logs()
    except Exception:
        pass

    # 广播给前端
    await manager.broadcast({
        "type": "activity_log",
        "data": entry
    })

    return {"ok": True}


async def _report_pc_activity(report: ActivityReport):
    now = time.time()
    entry = ingest_pc_report({
        "timestamp": report.timestamp or now,
        "app": report.app,
        "title": report.title,
        "active_state": report.active_state,
        "last_input_age_sec": report.last_input_age_sec,
    }, now=now)

    append_activity_log(entry)
    record_activity_entry_safely(entry)

    try:
        cleanup_old_activity_logs()
    except Exception:
        pass

    await manager.broadcast({
        "type": "activity_log",
        "data": entry
    })

    return {"ok": True}


# ── 查询 ──────────────────────────────────────────

@router.get("/api/activity/status")
async def activity_tracker_status():
    """Remote PC agent 状态诊断"""
    return get_pc_status_payload(time.time())

def _resolve_entries(entries: list) -> list:
    """对历史条目做名称解析 + 过滤"""
    result = []
    for e in entries:
        resolved = resolve_app_name(e.get("app", ""), e.get("title", ""))
        if resolved is None:
            continue  # 过滤系统应用
        e["app"] = resolved
        result.append(e)
    return result


@router.get("/api/activity/dates")
async def list_activity_dates():
    """返回所有有日志的日期"""
    return {"dates": get_available_dates()}


@router.get("/api/activity/logs/{date_str}")
async def get_activity_logs(date_str: str):
    """返回指定日期的活动日志"""
    entries = read_activity_logs(date_str)
    return {"entries": _resolve_entries(entries), "date": date_str}


@router.post("/api/activity/clear")
async def clear_all_activity_logs():
    """清除所有活动日志"""
    from activity import ACTIVITY_LOGS_DIR
    count = 0
    for f in ACTIVITY_LOGS_DIR.glob("*.jsonl"):
        f.unlink(missing_ok=True)
        count += 1
    return {"ok": True, "deleted": count}


@router.get("/api/activity/recent")
async def get_recent_activity(hours: int = KEEP_HOURS):
    """返回最近 N 小时的活动日志"""
    entries = read_recent_activity(hours)
    return {"entries": _resolve_entries(entries), "hours": hours}


@router.get("/api/activity/summary")
async def get_activity_summary(hours: int = KEEP_HOURS):
    """返回最近 N 小时的 10 分钟窗口活动摘要"""
    summaries = generate_activity_summary(hours)
    return {"summaries": summaries, "hours": hours}


# ── 活动追踪总开关 ──────────────────────────────────

@router.get("/api/activity/config")
async def get_activity_config():
    """获取活动追踪配置"""
    return {"activity_tracking_enabled": is_activity_tracking_enabled()}


class ActivityConfigUpdate(BaseModel):
    activity_tracking_enabled: bool


@router.put("/api/activity/config")
async def update_activity_config(body: ActivityConfigUpdate):
    """更新活动追踪配置"""
    set_activity_tracking_enabled(body.activity_tracking_enabled)
    await manager.broadcast({
        "type": "activity_config_changed",
        "data": {"activity_tracking_enabled": body.activity_tracking_enabled}
    })
    return {"ok": True, "activity_tracking_enabled": body.activity_tracking_enabled}
