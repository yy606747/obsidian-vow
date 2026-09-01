from __future__ import annotations

import logging
import re
import time
from datetime import datetime

from database import get_db
from ws import manager

from . import alarm_context, store

log = logging.getLogger("schedule")

ALARM_CMD = re.compile(r"\[ALARM:(.+?)\|(.+?)\]")
REMINDER_CMD = re.compile(r"\[REMINDER:(.+?)\|(.+?)\]")
MONITOR_CMD = re.compile(r"\[Monitor:(.+?)\|(.+?)\]")
SCHEDULE_DEL_CMD = re.compile(r"\[SCHEDULE_DEL:(.+?)\]")
SCHEDULE_LIST_CMD = re.compile(r"\[SCHEDULE_LIST\]")


async def process_schedule_commands_with_results(
    full_text: str,
    conv_id: str | None = None,
    *,
    ai_name: str,
    source_message_id: str | None = None,
) -> tuple[str, list[dict]]:
    text = full_text
    results: list[dict] = []

    for match in ALARM_CMD.finditer(full_text):
        raw_text = match.group(0)
        try:
            raw_dt, content = match.group(1), match.group(2)
            dt = _parse_dt(raw_dt)
            log.info("ALARM detected: raw_dt=%s parsed=%s content=%s", raw_dt, dt, content)
            if dt and content.strip():
                created = await store.add_schedule("alarm", dt, content.strip())
                if created and conv_id:
                    try:
                        await alarm_context.capture_creation_context(
                            created,
                            conv_id,
                            source_message_id=source_message_id,
                        )
                    except Exception:
                        # The alarm itself is must-create; a context sidecar
                        # failure only removes optional background at fire time.
                        log.warning(
                            "alarm creation context capture failed: %s",
                            created,
                            exc_info=True,
                        )
                if created:
                    await manager.broadcast({"type": "schedule_changed"})
                if created and conv_id:
                    await _sys_msg(conv_id, f"{ai_name} 设置了 {dt.replace('T', ' ')} 的闹铃：{content.strip()}")
                results.append({
                    "type": "schedule_command",
                    "tool_name": "schedule.alarm",
                    "raw_text": raw_text,
                    "ok": bool(created),
                    "status": "succeeded" if created else "rejected",
                    "reason": "" if created else "duplicate",
                    "schedule_id": created,
                    "trigger_at": dt,
                    "content": content.strip(),
                })
            else:
                log.warning("ALARM skipped: dt=%s content=%s", dt, content)
                results.append({
                    "type": "schedule_command",
                    "tool_name": "schedule.alarm",
                    "raw_text": raw_text,
                    "ok": False,
                    "status": "rejected",
                    "reason": "invalid_datetime_or_content",
                    "trigger_at": dt,
                    "content": content.strip(),
                })
        except Exception as exc:
            log.error("ALARM processing error: %s", exc)
            results.append({
                "type": "schedule_command",
                "tool_name": "schedule.alarm",
                "raw_text": raw_text,
                "ok": False,
                "status": "failed",
                "reason": str(exc),
            })
    text = ALARM_CMD.sub("", text)

    for match in REMINDER_CMD.finditer(full_text):
        raw_text = match.group(0)
        try:
            raw_dt, content = match.group(1), match.group(2)
            dt = _parse_dt(raw_dt)
            log.info("REMINDER detected: raw_dt=%s parsed=%s content=%s", raw_dt, dt, content)
            if dt and content.strip():
                created = await store.add_schedule("reminder", dt, content.strip())
                if created:
                    await manager.broadcast({"type": "schedule_changed"})
                if created and conv_id:
                    await _sys_msg(conv_id, f"{ai_name} 设置了 {dt.replace('T', ' ')} 的日程：{content.strip()}")
                results.append({
                    "type": "schedule_command",
                    "tool_name": "schedule.reminder",
                    "raw_text": raw_text,
                    "ok": bool(created),
                    "status": "succeeded" if created else "rejected",
                    "reason": "" if created else "duplicate",
                    "schedule_id": created,
                    "trigger_at": dt,
                    "content": content.strip(),
                })
            else:
                log.warning("REMINDER skipped: dt=%s content=%s", dt, content)
                results.append({
                    "type": "schedule_command",
                    "tool_name": "schedule.reminder",
                    "raw_text": raw_text,
                    "ok": False,
                    "status": "rejected",
                    "reason": "invalid_datetime_or_content",
                    "trigger_at": dt,
                    "content": content.strip(),
                })
        except Exception as exc:
            log.error("REMINDER processing error: %s", exc)
            results.append({
                "type": "schedule_command",
                "tool_name": "schedule.reminder",
                "raw_text": raw_text,
                "ok": False,
                "status": "failed",
                "reason": str(exc),
            })
    text = REMINDER_CMD.sub("", text)

    for match in MONITOR_CMD.finditer(full_text):
        raw_text = match.group(0)
        try:
            raw_dt, content = match.group(1), match.group(2)
            dt = _parse_dt(raw_dt)
            log.info("MONITOR detected: raw_dt=%s parsed=%s content=%s", raw_dt, dt, content)
            if dt and content.strip():
                created = await store.add_schedule("monitor", dt, content.strip())
                if created:
                    await manager.broadcast({"type": "schedule_changed"})
                if created and conv_id:
                    await _sys_msg(conv_id, f"{ai_name} 设置了 {dt.replace('T', ' ')} 的查岗：{content.strip()}")
                results.append({
                    "type": "schedule_command",
                    "tool_name": "schedule.monitor",
                    "raw_text": raw_text,
                    "ok": bool(created),
                    "status": "succeeded" if created else "rejected",
                    "reason": "" if created else "duplicate",
                    "schedule_id": created,
                    "trigger_at": dt,
                    "content": content.strip(),
                })
            else:
                log.warning("MONITOR skipped: dt=%s content=%s", dt, content)
                results.append({
                    "type": "schedule_command",
                    "tool_name": "schedule.monitor",
                    "raw_text": raw_text,
                    "ok": False,
                    "status": "rejected",
                    "reason": "invalid_datetime_or_content",
                    "trigger_at": dt,
                    "content": content.strip(),
                })
        except Exception as exc:
            log.error("MONITOR processing error: %s", exc)
            results.append({
                "type": "schedule_command",
                "tool_name": "schedule.monitor",
                "raw_text": raw_text,
                "ok": False,
                "status": "failed",
                "reason": str(exc),
            })
    text = MONITOR_CMD.sub("", text)

    for match in SCHEDULE_DEL_CMD.finditer(full_text):
        raw_text = match.group(0)
        try:
            sid = match.group(1).strip()
            if sid:
                info = await store.get_schedule(sid)
                await store.delete_schedule(sid)
                await manager.broadcast({"type": "schedule_changed"})
                if conv_id and info:
                    type_labels = {"alarm": "闹铃", "reminder": "日程", "monitor": "定时查岗"}
                    label = type_labels.get(info["type"], "日程")
                    trigger_at = info["trigger_at"].replace("T", " ")
                    await _sys_msg(conv_id, f"{ai_name} 取消了 {trigger_at} 的{label}：{info['content']}")
                active = bool(info and info.get("status") == "active")
                results.append({
                    "type": "schedule_command",
                    "tool_name": "schedule.delete",
                    "raw_text": raw_text,
                    "ok": active,
                    "status": "succeeded" if active else "rejected",
                    "reason": "" if active else ("not_found" if not info else "not_active"),
                    "schedule_id": sid,
                    "schedule": info,
                })
        except Exception as exc:
            log.error("SCHEDULE_DEL processing error: %s", exc)
            results.append({
                "type": "schedule_command",
                "tool_name": "schedule.delete",
                "raw_text": raw_text,
                "ok": False,
                "status": "failed",
                "reason": str(exc),
            })
    text = SCHEDULE_DEL_CMD.sub("", text)

    for match in SCHEDULE_LIST_CMD.finditer(full_text):
        raw_text = match.group(0)
        try:
            schedules = await store.list_active()
            results.append({
                "type": "schedule_list",
                "tool_name": "schedule.list",
                "raw_text": raw_text,
                "ok": True,
                "status": "succeeded",
                "count": len(schedules),
                "schedules": schedules,
                "schedule_text": store.build_schedule_prompt(schedules),
            })
        except Exception as exc:
            log.error("SCHEDULE_LIST processing error: %s", exc)
            results.append({
                "type": "schedule_list",
                "tool_name": "schedule.list",
                "raw_text": raw_text,
                "ok": False,
                "status": "failed",
                "reason": str(exc),
                "count": 0,
                "schedules": [],
                "schedule_text": "",
            })
    return SCHEDULE_LIST_CMD.sub("", text).strip(), results


async def process_schedule_commands(
    full_text: str,
    conv_id: str | None = None,
    *,
    ai_name: str,
) -> str:
    cleaned, _results = await process_schedule_commands_with_results(
        full_text,
        conv_id,
        ai_name=ai_name,
    )
    return cleaned


def _parse_dt(raw: str) -> str | None:
    raw = raw.strip().replace("T", " ")
    now = datetime.now().replace(second=0, microsecond=0)
    for fmt in (
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%m-%d %H:%M",
        "%m/%d %H:%M",
    ):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.year == 1900:
                dt = dt.replace(year=now.year)
            dt = _normalize_future_dt(dt, now=now, explicit_year="%Y" in fmt)
            if dt is None:
                return None
            return dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m-%d", "%m/%d"):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.year == 1900:
                dt = dt.replace(year=now.year)
            dt = dt.replace(hour=9, minute=0)
            dt = _normalize_future_dt(dt, now=now, explicit_year="%Y" in fmt)
            if dt is None:
                return None
            return dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    return None


def _normalize_future_dt(dt: datetime, *, now: datetime, explicit_year: bool) -> datetime | None:
    if dt >= now:
        return dt
    if explicit_year:
        if dt.year < now.year:
            corrected = _replace_year(dt, now.year)
            if corrected and corrected >= now:
                return corrected
        return None
    while dt < now:
        dt = _replace_year(dt, dt.year + 1)
        if dt is None:
            return None
    return dt


def _replace_year(dt: datetime, year: int) -> datetime | None:
    try:
        return dt.replace(year=year)
    except ValueError:
        if dt.month == 2 and dt.day == 29:
            return dt.replace(year=year, day=28)
        return None


async def _sys_msg(conv_id: str, content: str) -> None:
    now = time.time()
    msg_id = f"msg_{int(now*1000)}_ss"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (msg_id, conv_id, "system", content, now, "[]"),
        )
        await db.commit()
    msg = {"id": msg_id, "conv_id": conv_id, "role": "system", "content": content, "created_at": now, "attachments": []}
    await manager.broadcast({"type": "msg_created", "data": msg})
