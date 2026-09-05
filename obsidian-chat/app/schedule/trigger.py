from __future__ import annotations

import json
import logging
import time
from datetime import datetime

import aiosqlite

from ai_providers import stream_ai
from config import DEFAULT_MODEL, SETTINGS, load_worldbook
from database import get_db
from music import get_audio_url, search_songs
from routes.files import export_conversation
from routes.music import MUSIC_CMD_PATTERN
from sentinel_runtime import append_monitor_log
from ws import manager

from app.vows.prompt import build_alarm_fallback_text
from app.vows.service import VowReadError, strip_vow_markers, vow_service
from app.memory_v3.recall_intent import strip_recall_intent_markers
from app.memory_v3.timeline import timeline_service
from app.chat.worldbook import build_worldbook_prefix, resolve_worldbook_names
from app.tools.ledger import tool_invocation_ledger
from app.tools.schemas import ToolContext, ToolResult, ToolStatus
from app.web_push import service as web_push_service

from . import alarm_context, evidence, prompt, store
from .commands import ALARM_CMD, process_schedule_commands_with_results

log = logging.getLogger("schedule")


def _monitor_log_entry(
    *,
    schedule_id: str,
    trigger_at: str,
    content: str,
    status: str,
    monitoringlog: str,
    call_core: bool = False,
    core_reason: str = "",
    **extra,
) -> dict:
    now = time.time()
    entry = {
        "timestamp": now,
        "time": time.strftime("%H:%M:%S", time.localtime(now)),
        "date": time.strftime("%Y-%m-%d", time.localtime(now)),
        "monitoringlog": monitoringlog,
        "summary": "",
        "score": None,
        "call_core": call_core,
        "core_reason": core_reason,
        "screenshot": "",
        "source": "schedule_monitor",
        "status": status,
        "schedule_id": schedule_id,
        "trigger_at": trigger_at,
        "content": content,
    }
    entry.update(extra)
    return entry


async def _append_and_broadcast_monitor_log(entry: dict) -> None:
    try:
        append_monitor_log(entry)
    except Exception:
        log.warning("append monitor_log failed", exc_info=True)
    try:
        await manager.broadcast({"type": "monitor_log", "data": entry})
    except Exception:
        log.warning("broadcast monitor_log failed", exc_info=True)


async def fire_due_items(items: list[dict]) -> None:
    if not items:
        return
    for item in items:
        log.info("firing schedule %s: %s @%s", item["id"], item["content"], item["trigger_at"])
    await _broadcast_start(items)
    await _fire(items)


async def _broadcast_alarm_web_push(data: dict) -> None:
    try: await web_push_service.broadcast_alarm(data)
    except Exception: log.warning("alarm Web Push failed", exc_info=True)

async def _broadcast_start(items: list[dict]) -> None:
    alarms = [item for item in items if item["type"] == "alarm"]
    monitors = [item for item in items if item["type"] == "monitor"]
    if len(items) == 1 and alarms:
        item = alarms[0]
        alarm_data = {"id": item["id"], "ids": [item["id"]], "content": item["content"], "trigger_at": item["trigger_at"]}
        await manager.broadcast({"type": "schedule_alarm", "data": alarm_data})
        await _broadcast_alarm_web_push(alarm_data)
        await manager.broadcast({"type": "schedule_changed"})
        return
    if len(items) > 1 and alarms:
        item = alarms[0]
        alarm_data = {"id": item["id"], "ids": [alarm["id"] for alarm in alarms], "content": f"{len(items)} 条日程同时到期", "trigger_at": item["trigger_at"]}
        await manager.broadcast({"type": "schedule_alarm", "data": alarm_data})
        await _broadcast_alarm_web_push(alarm_data)
    await manager.broadcast({"type": "schedule_changed"})
    for item in monitors:
        await _append_and_broadcast_monitor_log(_monitor_log_entry(
            schedule_id=item["id"],
            trigger_at=item["trigger_at"],
            content=item["content"],
            status="started",
            monitoringlog=f"👁 定时查岗触发：{item['content']}",
            call_core=True,
            core_reason=f"定时查岗到点：{item['content']}",
        ))
    if monitors:
        await manager.broadcast({"type": "monitor_alert", "data": {"content": monitors[0]["content"]}})


async def _fire(items: list[dict]) -> None:
    wb = load_worldbook()
    user_name, ai_name = resolve_worldbook_names(wb)
    conv = await _latest_conversation()
    if not conv:
        await _log_monitor_failure(items, "failed", "no_conversation", "⚠️ 定时查岗无法执行：没有可用对话。目的：{content}")
        return
    conv_id = conv["id"]
    model_key = conv["model"] or DEFAULT_MODEL
    now_str = datetime.now().strftime("%Y年%m月%d日  %H:%M:%S")
    try:
        messages, trigger_prompt, evidence_errors, context_meta = await _build_messages(
            items,
            wb,
            conv_id,
            now_str,
            user_name,
        )
    except VowReadError as exc:
        # 誓约读取失败（誓约设计 §5.2）：闹铃 must-fire → 降级固定模板照常触发；
        # 查岗/monitor → 跳过本次生成并记录。两种情况都不以人格开口。
        await _fire_vow_fallback(items, conv_id, exc)
        return
    advertised_tools = next(
        (
            tuple(getattr(message.get("content"), "advertised_tools", ()))
            for message in messages
            if getattr(message.get("content"), "advertised_tools", None) is not None
        ),
        (),
    )
    invocation_id = tool_invocation_ledger.new_invocation_id("schedule_core")
    request_id = "schedule:" + ",".join(str(item["id"]) for item in items)
    tool_context = ToolContext(
        conv_id=conv_id,
        request_id=request_id,
        model_key=model_key,
        capabilities=advertised_tools,
        metadata={
            "source": "schedule",
            "source_chain": "schedule",
            "invocation_id": invocation_id,
            "advertised_tools": advertised_tools,
        },
    )
    await tool_invocation_ledger.record_model_request(
        tool_context,
        invocation_id=invocation_id,
        request_snapshot=messages,
        advertised_tools=advertised_tools,
        metadata={"schedule_ids": [item["id"] for item in items]},
    )
    full_text, stream_error, raw_output = await _stream_reply(messages, model_key, items)
    await tool_invocation_ledger.record_model_output(
        tool_context,
        invocation_id=invocation_id,
        raw_output=raw_output,
        outcome="failed" if stream_error else (
            "succeeded" if raw_output.strip() else "unknown"
        ),
        error=stream_error,
    )
    full_text, music_cards = await _postprocess_reply(
        full_text,
        conv_id,
        tool_context=tool_context,
        ai_name=ai_name,
    )
    if not full_text.strip():
        await tool_invocation_ledger.record_turn(
            tool_context,
            prompt_source="schedule",
            advertised_tools=advertised_tools,
            turn_outcome="invalid_output",
        )
        await _log_monitor_failure(items, "core_empty", "core_empty", "⚠️ 定时查岗 Core 返回空内容。目的：{content}")
        return
    ai_msg_id = await _write_and_broadcast_messages(items, conv_id, ai_name, trigger_prompt, full_text, music_cards)
    visible_context = ToolContext(
        conv_id=conv_id,
        msg_id=ai_msg_id,
        request_id=request_id,
        model_key=model_key,
        capabilities=advertised_tools,
        metadata={
            "source": "schedule",
            "source_chain": "schedule",
            "invocation_id": invocation_id,
            "advertised_tools": advertised_tools,
        },
    )
    await tool_invocation_ledger.record_visible_message(
        visible_context,
        invocation_id=invocation_id,
        cleaned_content=full_text,
        message_id=ai_msg_id,
    )
    await tool_invocation_ledger.record_turn(
        visible_context,
        prompt_source="schedule",
        advertised_tools=advertised_tools,
        turn_outcome="failed" if stream_error else "succeeded",
        metadata={"schedule_ids": [item["id"] for item in items]},
    )
    try:
        await timeline_service.record_injection_usage(
            context_meta.get("timeline"),
            conv_id=conv_id,
            assistant_message_id=ai_msg_id,
            response_text=full_text,
        )
    except Exception:
        log.warning("schedule timeline usage record failed", exc_info=True)
    await _log_monitor_success(items, conv_id, ai_msg_id, full_text, stream_error, evidence_errors)
    if music_cards:
        await manager.broadcast({"type": "music", "data": {"type": "music", "msg_id": ai_msg_id, "cards": music_cards, "autoplay": True}})
    try:
        await export_conversation(conv_id)
    except Exception:
        if any(item["type"] == "monitor" for item in items):
            log.warning("monitor export_conversation failed", exc_info=True)
        else:
            raise


async def _latest_conversation() -> dict | None:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 1")
        row = await cur.fetchone()
        return dict(row) if row else None


async def _build_messages(
    items: list[dict],
    wb: dict,
    conv_id: str,
    now_str: str,
    user_name: str,
) -> tuple[list[dict], str, list[str], dict]:
    # 誓约常驻注入（誓约设计 §5.1），恒在 ability block 之前；
    # 读取失败抛 VowReadError，由 _fire 按 must-fire / 跳过分类处置。
    vow_block, _ = await vow_service.load_vow_prompt_context()
    prefix = _worldbook_prefix(wb, now_str)
    has_alarm = any(item["type"] == "alarm" for item in items)
    has_monitor = any(item["type"] == "monitor" for item in items)
    # Alarm-only turns intentionally do not inherit whichever conversation is
    # newest at fire time.  Monitors keep their existing recent-history input.
    history = (
        await _history(conv_id, clear_attachments=True)
        if has_monitor
        else []
    )
    schedule_text = store.build_schedule_prompt(await store.list_active())
    ability_block = prompt.build_abilities_block(user_name, schedule_text)
    cap_idx = len(prefix) if prefix else 0
    if vow_block:
        history.insert(cap_idx, {"role": "user", "content": vow_block})
        history.insert(cap_idx + 1, {"role": "assistant", "content": "（嗯，这些一直都算数。）"})
        cap_idx += 2
    history.insert(cap_idx, {"role": "user", "content": ability_block})
    history.insert(cap_idx + 1, {"role": "assistant", "content": "（我知道自己现在能做什么。）"})
    evidence_text, evidence_errors = ("", [])
    if has_monitor:
        evidence_text, evidence_errors = evidence.load_evidence(user_name)
        if evidence_errors:
            for item in _monitors(items):
                await _append_and_broadcast_monitor_log(_monitor_log_entry(
                    schedule_id=item["id"],
                    trigger_at=item["trigger_at"],
                    content=item["content"],
                    status="evidence_partial",
                    monitoringlog=f"⚠️ 定时查岗部分证据读取失败：{'; '.join(evidence_errors)}",
                    evidence_errors=evidence_errors,
                ))
    context_inject: list[dict] = []
    alarm_context_meta = {
        "status": "not_applicable",
        "block": "",
        "visible_messages": [],
    }
    timeline_meta = {"status": "not_applicable", "block": "", "entries": []}
    if has_alarm:
        try:
            alarm_context_meta = await alarm_context.load_prompt_context(
                items,
                user_name=user_name,
                ai_name=resolve_worldbook_names(wb)[1],
            )
        except Exception:
            alarm_context_meta = {
                "status": "error",
                "block": "",
                "visible_messages": [],
            }
            log.warning("alarm creation context load failed", exc_info=True)
        creation_block = str(alarm_context_meta.get("block") or "")
        if creation_block:
            context_inject.extend(_prompt_pair(
                creation_block,
                "（嗯，我知道当时为什么设下这个闹钟；仍以眼前的新事实为准。）",
            ))
        try:
            timeline_meta = await timeline_service.prompt_context(
                visible_messages=alarm_context_meta.get("visible_messages") or [],
            )
        except Exception:
            timeline_meta = {"status": "error", "block": "", "entries": []}
            log.warning("schedule timeline context load failed", exc_info=True)
        timeline_block = str(timeline_meta.get("block") or "")
        if timeline_block:
            context_inject.extend(_prompt_pair(
                timeline_block,
                "（嗯，近几天发生的事和新的纠正，我会优先按它们理解。）",
            ))
    trigger_prompt = _trigger_prompt(items, now_str, user_name, evidence_text, evidence_errors)
    messages = prefix + context_inject + history + [{"role": "user", "content": trigger_prompt}]
    return messages, trigger_prompt, evidence_errors, {
        "alarm_creation": alarm_context_meta,
        "timeline": timeline_meta,
    }


def _worldbook_prefix(wb: dict, now_str: str) -> list[dict]:
    prefix = build_worldbook_prefix(wb)
    if prefix:
        prefix[-1]["content"] += f"\n系统当前的准确时间是 {now_str}"
    return prefix


def _prompt_pair(content: str, ack: str) -> list[dict]:
    return [
        {"role": "user", "content": content},
        {"role": "assistant", "content": ack},
    ]


async def _history(conv_id: str, *, clear_attachments: bool) -> list[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT role, content, attachments FROM messages WHERE conv_id=? "
            "AND role IN ('user','assistant') ORDER BY created_at DESC LIMIT 20",
            (conv_id,),
        )
        rows = await cur.fetchall()
    history = []
    for row in reversed(rows):
        item = dict(row)
        try:
            item["attachments"] = json.loads(item.get("attachments") or "[]") if item.get("attachments") else []
        except Exception:
            item["attachments"] = []
        if clear_attachments:
            item["attachments"] = []
        history.append(item)
    return history


def _trigger_prompt(items: list[dict], now_str: str, user_name: str, evidence_text: str, evidence_errors: list[str]) -> str:
    if len(items) > 1:
        return prompt.build_merged_trigger_prompt(items, now_str, user_name, evidence_text, evidence_errors)
    item = items[0]
    if item["type"] == "monitor":
        return prompt.build_monitor_trigger_prompt(item, now_str, user_name, evidence_text, evidence_errors)
    return prompt.build_alarm_trigger_prompt(item, now_str, user_name)


async def _stream_reply(
    messages: list[dict],
    model_key: str,
    items: list[dict],
) -> tuple[str, str, str]:
    full_text = ""
    try:
        async for chunk in stream_ai(messages, model_key, temperature=SETTINGS.get("temperature")):
            full_text += chunk
        return full_text, "", full_text
    except Exception as exc:
        if any(item["type"] == "monitor" for item in items):
            await _log_monitor_failure(items, "core_failed", "core_stream_failed", f"⚠️ 定时查岗 Core 回复失败：{exc}", error=str(exc))
            return f"[定时查岗回复失败] {exc}", str(exc), full_text
        return f"[闹铃提醒回复失败] {exc}", str(exc), full_text


async def _fire_vow_fallback(items: list[dict], conv_id: str, exc: Exception) -> None:
    """誓约读取失败时的分类处置（誓约设计 §5.2）。

    闹铃是 must-fire 封闭集合：降级为固定模板的 system 消息（非模型生成），
    提醒功能不哑火；monitor 等其余类型静默跳过并记录。"""
    alarms = [item for item in items if item["type"] == "alarm"]
    await _log_monitor_failure(
        items, "vow_read_failed", "vow_read_failed",
        "⚠️ 定时查岗前誓约读取失败，本次跳过。目的：{content}", error=str(exc),
    )
    if not alarms:
        return
    for item in alarms:
        now = time.time()
        msg_id = f"msg_{int(now*1000)}_alarm_fb"
        text = build_alarm_fallback_text(item["content"])
        async with get_db() as db:
            await db.execute(
                "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                (msg_id, conv_id, "system", text, now, "[]"),
            )
            await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
            await db.commit()
        await manager.broadcast({"type": "msg_created", "data": {
            "id": msg_id, "conv_id": conv_id, "role": "system",
            "content": text, "created_at": now, "attachments": [],
        }})
    try:
        await export_conversation(conv_id)
    except Exception:
        log.warning("alarm fallback export_conversation failed", exc_info=True)


async def _postprocess_reply(
    full_text: str,
    conv_id: str,
    *,
    tool_context: ToolContext,
    ai_name: str,
) -> tuple[str, list[dict]]:
    from app.tools.parser import parse_tool_intents

    # strip 先于 MUSIC / schedule 指令解析（誓约设计 §4.4 硬约束）：
    # 本路径不允许立约，[VOW:] 内部的指令是惰性文本，绝不进入解析。
    full_text = strip_recall_intent_markers(strip_vow_markers(full_text))
    full_text = _strip_trigger_alarm_commands(full_text)
    parsed_intents = parse_tool_intents(
        full_text,
        enabled_commands={"music", "schedule"},
    )
    await tool_invocation_ledger.record_postprocess(
        tool_context,
        raw_output=full_text,
        intents=parsed_intents,
        plan_results=(),
        enabled_commands={"music", "schedule"},
    )
    music_cards = []
    normalized_results: list[dict] = []
    for keyword in MUSIC_CMD_PATTERN.findall(full_text):
        try:
            results = search_songs(keyword.strip(), limit=5)
            if results:
                song = results[0]
                song["audio_url"] = get_audio_url(song["id"])
                song["candidates"] = results[1:4]
                music_cards.append(song)
            normalized_results.append({
                "type": "music_search",
                "tool_name": "music.search",
                "ok": True,
                "status": "succeeded",
                "query": keyword.strip(),
                "cards": results[:1] if results else [],
            })
        except Exception as exc:
            normalized_results.append({
                "type": "music_search",
                "tool_name": "music.search",
                "ok": False,
                "status": "failed",
                "query": keyword.strip(),
                "reason": str(exc),
            })
    full_text = MUSIC_CMD_PATTERN.sub("", full_text).strip()
    cleaned, schedule_results = await process_schedule_commands_with_results(
        full_text,
        conv_id,
        ai_name=ai_name,
    )
    normalized_results.extend(schedule_results)
    if normalized_results:
        unused = list(normalized_results)
        intents_by_id = {intent.id: intent for intent in parsed_intents}
        tool_results: list[ToolResult] = []
        for intent in parsed_intents:
            match_index = next(
                (
                    index
                    for index, payload in enumerate(unused)
                    if payload.get("tool_name") == intent.tool_name
                ),
                None,
            )
            if match_index is None:
                continue
            payload = unused.pop(match_index)
            tool_results.append(ToolResult.from_intent(
                intent,
                status=ToolStatus.EXECUTED,
                result=payload,
                error=(
                    str(payload.get("reason") or "")
                    if payload.get("status") == "failed"
                    else None
                ),
            ))
        await tool_invocation_ledger.record_execution(
            tool_context,
            results=tool_results,
            intents_by_id=intents_by_id,
        )
    return cleaned, music_cards


def _strip_trigger_alarm_commands(text: str) -> str:
    if ALARM_CMD.search(text):
        log.warning("ignored alarm command emitted by non-user schedule trigger")
    return ALARM_CMD.sub("", text).strip()


async def _write_and_broadcast_messages(items: list[dict], conv_id: str, ai_name: str, trigger_prompt: str, full_text: str, music_cards: list[dict]) -> str:
    now = time.time()
    kind = _message_kind(items)
    sys_msg_id = f"msg_{int(now*1000)}_{kind['system']}"
    trigger_msg_id = f"msg_{int(now*1000)}_{kind['trigger']}"
    sys_content = prompt.build_system_message(items, ai_name)
    async with get_db() as db:
        await db.execute("INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)", (sys_msg_id, conv_id, "system", sys_content, now, "[]"))
        await db.execute("INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)", (trigger_msg_id, conv_id, "trigger", trigger_prompt, now + 0.001, "[]"))
        await db.commit()
    await manager.broadcast({"type": "msg_created", "data": {"id": sys_msg_id, "conv_id": conv_id, "role": "system", "content": sys_content, "created_at": now, "attachments": []}})
    now2 = time.time()
    ai_msg_id = f"msg_{int(now2*1000)}_{kind['assistant']}"
    music_atts = [{"type": "music", "name": s["name"], "artist": s["artist"], "id": s["id"]} for s in music_cards] if music_cards else []
    async with get_db() as db:
        await db.execute("INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)", (ai_msg_id, conv_id, "assistant", full_text, now2, json.dumps(music_atts, ensure_ascii=False) if music_atts else "[]"))
        await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now2, conv_id))
        await db.commit()
    ai_msg = {"id": ai_msg_id, "conv_id": conv_id, "role": "assistant", "content": full_text, "created_at": now2, "attachments": music_atts}
    await manager.broadcast({"type": "msg_created", "data": ai_msg, "tts": True})
    timeline_service.start_background_refresh()
    return ai_msg_id


def _message_kind(items: list[dict]) -> dict:
    if len(items) > 1:
        return {"system": "sb", "trigger": "tb", "assistant": "ba"}
    if items[0]["type"] == "monitor":
        return {"system": "sm", "trigger": "tr", "assistant": "ma"}
    return {"system": "st", "trigger": "ta", "assistant": "sa"}


async def _log_monitor_failure(items: list[dict], status: str, error_type: str, template: str, **extra) -> None:
    for item in _monitors(items):
        await _append_and_broadcast_monitor_log(_monitor_log_entry(
            schedule_id=item["id"],
            trigger_at=item["trigger_at"],
            content=item["content"],
            status=status,
            monitoringlog=template.format(content=item["content"]),
            error_type=error_type,
            **extra,
        ))


async def _log_monitor_success(items: list[dict], conv_id: str, ai_msg_id: str, full_text: str, stream_error: str, evidence_errors: list[str]) -> None:
    for item in _monitors(items):
        await _append_and_broadcast_monitor_log(_monitor_log_entry(
            schedule_id=item["id"],
            trigger_at=item["trigger_at"],
            content=item["content"],
            status="failed_reply_inserted" if stream_error else "succeeded",
            monitoringlog=f"✅ 定时查岗已生成回复：{full_text[:80]}",
            ai_msg_id=ai_msg_id,
            conv_id=conv_id,
            evidence_errors=evidence_errors,
        ))


def _monitors(items: list[dict]) -> list[dict]:
    return [item for item in items if item["type"] == "monitor"]
