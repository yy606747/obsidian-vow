"""Side-effect handlers triggered by chat model commands."""

from __future__ import annotations

import json
import re
import time
import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.background_tasks import create_tracked_task
from app.pc_screen import ScreenCheckRequest
from app.pc_screen import service as screen_service
from app.vows.service import VowReadError, strip_vow_markers, vow_service
from app.memory_v3.recall_intent import strip_recall_intent_markers
from activity import get_activity_summary_for_prompt
from ai_providers import stream_ai
from app.memory_v2 import memory_service
from app.memory_v3.config import load_memory_v3_config, normalize_memory_v3_config
from app.memory_v3.timeline import timeline_service
from app.tools.ledger import tool_invocation_ledger
from app.tools.schemas import ToolContext
from config import SETTINGS, WORKING_MODEL_MAX_CHARS, load_worldbook, save_working_model
from database import get_db
from music import get_audio_url, search_songs
from routes.files import export_conversation
from ws import manager

from .commands import (
    ACTIVITY_CHECK_PATTERN,
    HEART_CMD_PATTERN,
    POI_SEARCH_PATTERN,
    REMEMBER_CMD_PATTERN,
    TOY_CMD_PATTERN,
    _strip_eval_side_effect_commands,
    _strip_retry,
    _toy_cmd_label,
)
from .worldbook import build_worldbook_prefix, resolve_worldbook_names


async def _update_memory_chunks(
    conv_id: str,
    *,
    reason: str = "chat",
    memory_v3_config: dict | None = None,
) -> None:
    config_snapshot = normalize_memory_v3_config(
        memory_v3_config if memory_v3_config is not None else load_memory_v3_config()
    )
    try:
        result = await memory_service.ensure_conversation_chunks(conv_id)
        print(
            f"[MemoryChunks] {reason} conv={conv_id} "
            f"inserted={result.get('inserted_chunks', 0)} "
            f"embedded={result.get('embedding_success', 0)} "
            f"failed={result.get('embedding_failed', 0)}"
        )
    except Exception as exc:
        print(f"[MemoryChunks] update failed conv={conv_id} reason={reason}: {exc}")
        return
    if _relational_card_v2_generation_active(config_snapshot):
        try:
            generated = await memory_service.generate_stable_relational_cards(
                config_snapshot=config_snapshot,
            )
            print(
                f"[MemoryCards] trigger_conv={conv_id} selected={generated.get('selected', 0)} "
                f"created={generated.get('created', 0)} "
                f"abstained={generated.get('abstained', 0)} "
                f"invalid={generated.get('invalid', 0)} "
                f"provider_failed={generated.get('provider_failed', 0)} "
                f"source_changed={generated.get('source_changed', 0)} "
                f"skipped_before_cutoff={generated.get('skipped_before_cutoff', 0)} "
                f"skipped={generated.get('skipped', '')}"
            )
        except Exception as exc:
            print(f"[MemoryCards] generation failed conv={conv_id}: {exc}")


def _schedule_chunk_index_update(
    conv_id: str,
    *,
    reason: str = "chat",
    memory_v3_config: dict | None = None,
) -> dict:
    # Freeze the whole post-message batch once. A flag change cannot make the
    # same batch partly digest and partly relational-card generation.
    config_snapshot = normalize_memory_v3_config(
        memory_v3_config if memory_v3_config is not None else load_memory_v3_config()
    )
    create_tracked_task(
        _update_memory_chunks(
            conv_id,
            reason=reason,
            memory_v3_config=config_snapshot,
        ),
        name=f"memory_chunks:{conv_id}:{reason}",
    )
    return config_snapshot


async def _store_remember_notes(notes: list[str], conv_id: str) -> None:
    """Persist parsed REMEMBER notes as ai_note memories."""
    for raw in notes:
        content = raw.strip()
        if not content: continue
        try:
            mem = await memory_service.create_memory(
                content, "ai_note", source_conv=conv_id, importance=0.6
            )
            await manager.broadcast({"type": "memory_added", "data": mem})
        except Exception as e:
            print(f"[REMEMBER] 落盘失败: {e}")


async def _handle_remember_cmd(full_text: str, conv_id: str) -> str:
    """检测 [REMEMBER:xxx] 指令 → 落盘为 type='ai_note' 记忆 → 广播 memory_added。返回剥除指令后的文本。"""
    matches = REMEMBER_CMD_PATTERN.findall(full_text)
    if not matches:
        return full_text
    cleaned = REMEMBER_CMD_PATTERN.sub("", full_text).strip()
    await _store_remember_notes(matches, conv_id)
    return cleaned


_PRIVATE_BLOCK_PATTERNS = (
    re.compile(r"<meta\b[^>]*>.*?</meta>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<(think|thinking|thought|analysis|reasoning)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL),
    re.compile(r"```(?:think|thinking|thought|analysis|reasoning)\b[\s\S]*?```", re.IGNORECASE),
)
_UNFINISHED_PRIVATE_BLOCK_PATTERN = re.compile(
    r"<(?:meta|think|thinking|thought|analysis|reasoning)\b[^>]*>[\s\S]*$",
    re.IGNORECASE,
)


def _sanitize_working_model_update(content: str) -> str:
    cleaned = str(content or "")
    for pattern in _PRIVATE_BLOCK_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    cleaned = _UNFINISHED_PRIVATE_BLOCK_PATTERN.sub("", cleaned)
    cleaned = _strip_eval_side_effect_commands(cleaned)
    lines = [" ".join(line.split()) for line in cleaned.splitlines()]
    return "\n".join(line for line in lines if line).strip()


async def store_working_model_update(
    content: str,
    *,
    conv_id: str = "",
    msg_id: str = "",
) -> dict:
    content = _sanitize_working_model_update(content)
    if not content:
        return {"ok": False, "reason": "empty"}
    if len(content) > WORKING_MODEL_MAX_CHARS:
        result = {
            "ok": False,
            "reason": "too_long",
            "length": len(content),
            "max_chars": WORKING_MODEL_MAX_CHARS,
        }
        await manager.broadcast({"type": "working_model_update_rejected", "data": result})
        return result
    data = save_working_model(content, source_conv=conv_id, source_msg_id=msg_id)
    result = {
        "ok": True,
        "updated_at": data.get("updated_at"),
        "version": data.get("version"),
        "length": len(content),
    }
    await manager.broadcast({"type": "working_model_updated", "data": result})
    return result


async def _store_heart_whisper(conv_id: str, msg_id: str, content: str) -> dict | None:
    """Persist a parsed HEART command and return the SSE/WS payload."""
    content = content.strip()
    if not content:
        return None
    hw_now = time.time()
    hw_id = f"hw_{int(hw_now*1000)}"
    async with get_db() as hw_db:
        await hw_db.execute(
            "INSERT INTO heart_whispers (id, conv_id, msg_id, content, created_at) VALUES (?,?,?,?,?)",
            (hw_id, conv_id, msg_id, content, hw_now),
        )
        await hw_db.commit()
    return {
        "type": "heart_whisper",
        "id": hw_id,
        "msg_id": msg_id,
        "content": content,
        "created_at": hw_now,
    }

async def _toy_sys_msg(conv_id: str, commands: list):
    """为玩具指令插入系统消息"""
    wb = load_worldbook()
    _user_name, ai_name = resolve_worldbook_names(wb)
    for cmd in commands:
        text = f"❤️ {ai_name} · {_toy_cmd_label(cmd)}"
        now = time.time()
        msg_id = f"msg_{int(now*1000)}_toy"
        async with get_db() as db:
            await db.execute(
                "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                (msg_id, conv_id, "system", text, now, "[]"),
            )
            await db.commit()
        msg = {"id": msg_id, "conv_id": conv_id, "role": "system",
               "content": text, "created_at": now, "attachments": []}
        await manager.broadcast({"type": "msg_created", "data": msg})

def _vow_inject_pair(vow_block: str) -> list[dict]:
    """誓约 block 的消息对注入形态（§5.1），各追加回复管道共用。"""
    if not vow_block:
        return []
    return [
        {"role": "user", "content": vow_block},
        {"role": "assistant", "content": "（嗯，这些一直都算数。）"},
    ]


VOW_BLOCKED_TEXT = "暂时无法生成回复"


async def persist_vow_blocked_message(conv_id: str) -> dict:
    """fail-closed 可见错误（§5.2）：持久化 role='system' 消息并经 WebSocket
    msg_created 广播。绝不创建 assistant 消息——错误提示若以她的角色落库，
    仍然是"她在缺失誓约时开口"。返回完整消息对象供 SSE 通道复用（按 id 去重）。"""
    now = time.time()
    sys_msg_id = f"msg_{int(now*1000)}_vow_blocked"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (sys_msg_id, conv_id, "system", VOW_BLOCKED_TEXT, now, "[]"),
        )
        await db.commit()
    sys_msg = {
        "id": sys_msg_id,
        "conv_id": conv_id,
        "role": "system",
        "content": VOW_BLOCKED_TEXT,
        "created_at": now,
        "attachments": [],
    }
    await manager.broadcast({"type": "msg_created", "data": sys_msg})
    return sys_msg


_auto_digest_running = False  # 避免并发重复触发


def _relational_card_v2_generation_active(config_snapshot: dict) -> bool:
    return bool(
        config_snapshot.get("relational_card_generation_enabled")
        and config_snapshot.get("relational_card_v2_generation_enabled")
    )


async def _maybe_auto_digest(memory_v3_config: dict | None = None):
    """每次 AI 回复后调用：若新消息累计达 10 条，后台触发一次 digest。"""
    global _auto_digest_running
    config_snapshot = normalize_memory_v3_config(
        memory_v3_config if memory_v3_config is not None else load_memory_v3_config()
    )
    if (
        _relational_card_v2_generation_active(config_snapshot)
        and config_snapshot["replace_auto_digest"]
    ):
        return
    if _auto_digest_running:
        return
    try:
        anchor_ts = memory_service.load_digest_anchor()
        async with get_db() as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM messages WHERE role IN ('user','assistant') AND created_at > ?",
                (anchor_ts,)
            )
            row = await cur.fetchone()
            new_count = row[0] if row else 0
        if new_count < 10:
            return
        _auto_digest_running = True
        print(f"[AutoDigest] 触发自动摘要，新消息数={new_count}")
        await memory_service.manual_digest()
    except Exception as e:
        print(f"[AutoDigest] 出错: {e}")
    finally:
        _auto_digest_running = False

# ── 服务端 POI 搜索 + 自动追加 Core 回复 ─────────
async def perform_poi_check(
    conv_id: str,
    model_key: str,
    categories: list[str],
    *,
    request_id: str = "",
):
    """Core 主动搜索周边 POI：拿最新坐标 → 搜索 → 携带结果自动追加一轮 Core 回复"""
    from location import (
        load_location_config, load_location_status, save_location_status,
        amap_poi_search, amap_regeo, format_location_for_prompt,
    )

    cfg = load_location_config()
    amap_key = cfg.get("amap_key", "")
    if not amap_key:
        await tool_invocation_ledger.record_terminal_outcome(
            correlation_id=request_id,
            outcome="rejected",
            event_type="poi_search.rejected",
            error="amap_key_missing",
            result={"categories": categories},
        )
        return

    # 1. 取最新坐标（直接用缓存的最新 GPS 上报坐标，而不是上次 API 坐标）
    status = load_location_status()
    lng = status.get("lng", 0)
    lat = status.get("lat", 0)
    if not lng or not lat:
        await tool_invocation_ledger.record_terminal_outcome(
            correlation_id=request_id,
            outcome="rejected",
            event_type="poi_search.rejected",
            error="location_unavailable",
            result={"categories": categories},
        )
        return

    # 2. 用最新坐标重新做逆地理编码，更新地址
    geo_info = await amap_regeo(lng, lat, amap_key)
    if geo_info:
        status["address"] = geo_info["address"]
        status["adcode"] = geo_info["adcode"]

    # 3. 搜索用户指定的 POI 类别
    poi_types = cfg.get("poi_types", {})
    search_results = {}
    for cat in categories:
        cat = cat.strip()
        type_code = poi_types.get(cat)
        if type_code:
            pois = await amap_poi_search(lng, lat, type_code, amap_key, cfg.get("poi_radius", 2000))
            search_results[cat] = pois
            # 更新缓存
            if "nearby_pois" not in status:
                status["nearby_pois"] = {}
            status["nearby_pois"][cat] = pois

    # 更新 last_api 坐标
    status["last_api_lng"] = lng
    status["last_api_lat"] = lat
    status["enriched_at"] = time.time()
    save_location_status(status)

    if not search_results:
        await tool_invocation_ledger.record_terminal_outcome(
            correlation_id=request_id,
            outcome="rejected",
            event_type="poi_search.rejected",
            error="unsupported_or_empty_category",
            result={"categories": categories},
        )
        return

    # 4. 格式化搜索结果
    result_lines = []
    for cat, pois in search_results.items():
        if not pois:
            result_lines.append(f"【{cat}】附近暂无相关结果")
            continue
        result_lines.append(f"【{cat}】")
        for p in pois[:10]:
            entry = f"  - {p['name']}"
            if p.get("distance"):
                entry += f"（{int(p['distance'])}m）"
            if p.get("rating") and p["rating"] != "[]":
                entry += f" ⭐{p['rating']}"
            if p.get("cost") and p["cost"] != "[]":
                entry += f" 人均¥{p['cost']}"
            if p.get("address") and p["address"] != "[]":
                entry += f" | {p['address']}"
            result_lines.append(entry)
    poi_text = "\n".join(result_lines)

    # 誓约常驻注入（§5.1）；读取失败 → 系统主动路径，跳过本次生成并记录（§5.2）
    try:
        vow_block, _ = await vow_service.load_vow_prompt_context()
    except VowReadError as exc:
        print(f"[POI_CHECK] 誓约读取失败，跳过本次生成: {exc}")
        await tool_invocation_ledger.record_terminal_outcome(
            correlation_id=request_id,
            outcome="failed",
            event_type="poi_search.followup_failed",
            error="vow_read_failed",
            result={"categories": categories},
        )
        return
    vow_inject = _vow_inject_pair(vow_block)

    # 5. 构建消息上下文，携带 POI 搜索结果，让 Core 追加一轮回复
    wb = load_worldbook()
    user_name, ai_name = resolve_worldbook_names(wb)

    prefix = build_worldbook_prefix(wb)

    # 获取最近对话上下文
    import aiosqlite
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT role, content FROM messages WHERE conv_id=? AND role IN ('user','assistant') ORDER BY created_at DESC LIMIT 6",
            (conv_id,)
        )
        rows = await cur.fetchall()
    recent = [{"role": r["role"], "content": r["content"], "attachments": []} for r in reversed(rows)]

    loc_prompt = format_location_for_prompt()
    poi_prompt = (
        f"你刚才想帮{user_name}搜索周边信息，以下是系统根据{user_name}最新实时坐标搜索到的结果：\n\n"
        f"{poi_text}\n\n"
        f"{loc_prompt}\n\n"
        f"请根据搜索结果，自然地向{user_name}推荐或回答。不需要再说\"让我帮你搜一下\"之类的话，直接根据结果回复即可。"
    )
    messages = prefix + vow_inject + recent + [
        {"role": "user", "content": poi_prompt}
    ]

    invocation_id = tool_invocation_ledger.new_invocation_id("main_poi_followup")
    observation_context = ToolContext(
        conv_id=conv_id,
        request_id=f"{request_id}:followup" if request_id else invocation_id,
        model_key=model_key,
        metadata={
            "source": "poi_followup",
            "source_chain": "main",
            "invocation_id": invocation_id,
            "correlation_id": request_id,
        },
    )
    await tool_invocation_ledger.record_model_request(
        observation_context,
        invocation_id=invocation_id,
        request_snapshot=messages,
        advertised_tools=(),
        metadata={"categories": categories},
    )
    raw_output = ""
    provider_error = ""
    try:
        _temp = SETTINGS.get("temperature")
        async for chunk in stream_ai(messages, model_key, temperature=_temp):
            raw_output += str(chunk)
    except Exception as e:
        provider_error = str(e)
    await tool_invocation_ledger.record_model_output(
        observation_context,
        invocation_id=invocation_id,
        raw_output=raw_output,
        outcome="failed" if provider_error else (
            "succeeded" if raw_output.strip() else "unknown"
        ),
        error=provider_error,
    )
    full_text = (
        f"[周边搜索完成但回复生成失败] {provider_error}"
        if provider_error
        else raw_output
    )

    # strip 先于一切后续处理（§4.4）：本路径不允许立约，剥除即丢弃；剥空不落库
    full_text = strip_recall_intent_markers(
        strip_vow_markers(_strip_retry(full_text))
    )
    if not full_text.strip():
        await tool_invocation_ledger.record_turn(
            observation_context,
            prompt_source="poi_followup",
            advertised_tools=(),
            turn_outcome="invalid_output",
        )
        await tool_invocation_ledger.record_terminal_outcome(
            correlation_id=request_id,
            outcome="failed",
            event_type="poi_search.followup_failed",
            error="empty_followup",
            result={"categories": categories},
        )
        return

    # 6. 插入系统提示 + AI 回复
    sys_now = time.time()
    sys_msg_id = f"msg_{int(sys_now*1000)}_poi_sys"
    searched_cats = "、".join(c.strip() for c in categories)
    sys_content = f"{ai_name}搜索了{user_name}周边的{searched_cats}信息"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (sys_msg_id, conv_id, "system", sys_content, sys_now, "[]")
        )
        await db.commit()
    sys_msg = {"id": sys_msg_id, "conv_id": conv_id, "role": "system",
               "content": sys_content, "created_at": sys_now, "attachments": []}
    await manager.broadcast({"type": "msg_created", "data": sys_msg})

    now = time.time()
    msg_id = f"msg_{int(now*1000)}_poi"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (msg_id, conv_id, "assistant", full_text, now, "[]")
        )
        await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
        await db.commit()

    ai_msg = {"id": msg_id, "conv_id": conv_id, "role": "assistant",
              "content": full_text, "created_at": now, "attachments": []}
    await manager.broadcast({"type": "msg_created", "data": ai_msg, "tts": True})
    await tool_invocation_ledger.record_visible_message(
        observation_context,
        invocation_id=invocation_id,
        cleaned_content=full_text,
        message_id=msg_id,
    )
    await tool_invocation_ledger.record_turn(
        observation_context,
        prompt_source="poi_followup",
        advertised_tools=(),
        turn_outcome="failed" if provider_error else "succeeded",
    )
    await tool_invocation_ledger.record_terminal_outcome(
        correlation_id=request_id,
        outcome="succeeded",
        event_type="poi_search.completed",
        result={"categories": categories, "result_count": sum(len(v) for v in search_results.values())},
    )
    timeline_service.start_background_refresh()
    await export_conversation(conv_id)
    print(f"[POI_CHECK] 搜索完成，已自动追加回复: {searched_cats}")


# ── [查看动态:n] 查看设备活动摘要 → 自动追加 Core 回复 ─────
async def perform_activity_check(
    conv_id: str,
    model_key: str,
    n: int = 6,
    *,
    request_id: str = "",
):
    """Core 在聊天中使用 [查看动态:n]：获取摘要 → 注入 prompt → Core 回应"""
    n = max(1, min(12, n)) if n > 0 else 6

    # 誓约常驻注入；读取失败 → 系统主动路径，跳过本次生成并记录（§5.2）
    try:
        vow_block, _ = await vow_service.load_vow_prompt_context()
    except VowReadError as exc:
        print(f"[ACTIVITY_CHECK] 誓约读取失败，跳过本次生成: {exc}")
        await tool_invocation_ledger.record_terminal_outcome(
            correlation_id=request_id,
            outcome="failed",
            event_type="activity_summary.followup_failed",
            error="vow_read_failed",
            result={"n": n},
        )
        return
    vow_inject = _vow_inject_pair(vow_block)

    summary_text = get_activity_summary_for_prompt(n)
    if not summary_text:
        summary_text = "（当前没有设备活动记录）"

    wb = load_worldbook()
    user_name, ai_name = resolve_worldbook_names(wb)
    minutes = n * 10

    prefix = build_worldbook_prefix(wb)

    import aiosqlite
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT role, content FROM messages WHERE conv_id=? AND role IN ('user','assistant') ORDER BY created_at DESC LIMIT 6",
            (conv_id,)
        )
        rows = await cur.fetchall()
    recent = [{"role": r["role"], "content": r["content"], "attachments": []} for r in reversed(rows)]

    activity_prompt = (
        f"你刚才想了解{user_name}最近在干什么，以下是系统采集到的{user_name}过去{minutes}分钟的设备使用动态（每10分钟一条摘要）：\n\n"
        f"【设备活动动态】\n{summary_text}\n\n"
        f"请根据这些动态信息，自然地和{user_name}聊聊。不需要再说\"让我看看\"之类的话，直接根据动态内容回应即可。"
    )
    messages = prefix + vow_inject + recent + [
        {"role": "user", "content": activity_prompt}
    ]

    invocation_id = tool_invocation_ledger.new_invocation_id(
        "main_activity_followup"
    )
    observation_context = ToolContext(
        conv_id=conv_id,
        request_id=f"{request_id}:followup" if request_id else invocation_id,
        model_key=model_key,
        metadata={
            "source": "activity_followup",
            "source_chain": "main",
            "invocation_id": invocation_id,
            "correlation_id": request_id,
        },
    )
    await tool_invocation_ledger.record_model_request(
        observation_context,
        invocation_id=invocation_id,
        request_snapshot=messages,
        advertised_tools=(),
        metadata={"n": n, "minutes": minutes},
    )
    raw_output = ""
    provider_error = ""
    try:
        _temp = SETTINGS.get("temperature")
        async for chunk in stream_ai(messages, model_key, temperature=_temp):
            raw_output += str(chunk)
    except Exception as e:
        provider_error = str(e)
    await tool_invocation_ledger.record_model_output(
        observation_context,
        invocation_id=invocation_id,
        raw_output=raw_output,
        outcome="failed" if provider_error else (
            "succeeded" if raw_output.strip() else "unknown"
        ),
        error=provider_error,
    )
    full_text = f"[查看动态失败] {provider_error}" if provider_error else raw_output

    full_text = strip_recall_intent_markers(
        strip_vow_markers(_strip_retry(full_text))
    )
    if not full_text.strip():
        await tool_invocation_ledger.record_turn(
            observation_context,
            prompt_source="activity_followup",
            advertised_tools=(),
            turn_outcome="invalid_output",
        )
        await tool_invocation_ledger.record_terminal_outcome(
            correlation_id=request_id,
            outcome="failed",
            event_type="activity_summary.followup_failed",
            error="empty_followup",
            result={"n": n},
        )
        return

    sys_now = time.time()
    sys_msg_id = f"msg_{int(sys_now*1000)}_ac_sys"
    sys_content = f"{ai_name}查看了{user_name}过去{minutes}分钟的动态"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (sys_msg_id, conv_id, "system", sys_content, sys_now, "[]")
        )
        await db.commit()
    sys_msg = {"id": sys_msg_id, "conv_id": conv_id, "role": "system",
               "content": sys_content, "created_at": sys_now, "attachments": []}
    await manager.broadcast({"type": "msg_created", "data": sys_msg})

    now = time.time()
    msg_id = f"msg_{int(now*1000)}_ac"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (msg_id, conv_id, "assistant", full_text, now, "[]")
        )
        await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
        await db.commit()

    ai_msg = {"id": msg_id, "conv_id": conv_id, "role": "assistant",
              "content": full_text, "created_at": now, "attachments": []}
    await manager.broadcast({"type": "msg_created", "data": ai_msg, "tts": True})
    await tool_invocation_ledger.record_visible_message(
        observation_context,
        invocation_id=invocation_id,
        cleaned_content=full_text,
        message_id=msg_id,
    )
    await tool_invocation_ledger.record_turn(
        observation_context,
        prompt_source="activity_followup",
        advertised_tools=(),
        turn_outcome="failed" if provider_error else "succeeded",
    )
    await tool_invocation_ledger.record_terminal_outcome(
        correlation_id=request_id,
        outcome="succeeded",
        event_type="activity_summary.completed",
        result={"n": n, "minutes": minutes, "has_activity": summary_text != "（当前没有设备活动记录）"},
    )
    timeline_service.start_background_refresh()
    await export_conversation(conv_id)
    print(f"[ACTIVITY_CHECK] 查看动态完成，n={n}，已自动追加回复")


def _schedule_list_followup_prompt(result: dict, user_name: str) -> str:
    if result.get("status") == "succeeded":
        schedule_text = str(result.get("schedule_text") or "暂无日程")
        return (
            f"你刚才请求查看{user_name}当前的日程。以下是系统刚刚从日程表读取的真实结果：\n\n"
            f"【当前日程列表】\n{schedule_text}\n\n"
            f"请直接根据这份真实列表回答{user_name}。不要再说“我看看”，也不要编造列表里没有的日程。"
        )
    reason = str(result.get("reason") or "adapter_failed")
    return (
        f"你刚才请求查看{user_name}当前的日程，但系统读取日程表失败了（{reason}）。\n\n"
        "当前日程列表是未知的，不是“暂无日程”。"
        f"请直接告诉{user_name}这次无法读取，不要编造日程，也不要把失败说成列表为空。"
    )


async def perform_schedule_list_followup(
    conv_id: str,
    model_key: str,
    result: dict,
    *,
    parent_request_id: str = "",
) -> dict:
    """Return a truthful schedule.list result to Core in the same logical turn."""

    try:
        vow_block, _ = await vow_service.load_vow_prompt_context()
    except VowReadError:
        return {"status": "vow_read_failed"}

    wb = load_worldbook()
    user_name, _ai_name = resolve_worldbook_names(wb)
    prefix = build_worldbook_prefix(wb)

    import aiosqlite

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT role, content FROM messages WHERE conv_id=? "
            "AND role IN ('user','assistant') ORDER BY created_at DESC LIMIT 6",
            (conv_id,),
        )
        rows = await cursor.fetchall()
    recent = [
        {"role": row["role"], "content": row["content"], "attachments": []}
        for row in reversed(rows)
    ]
    vow_inject = _vow_inject_pair(vow_block)
    prompt_text = _schedule_list_followup_prompt(result, user_name)
    messages = prefix + vow_inject + recent + [
        {"role": "user", "content": prompt_text}
    ]

    now = time.time()
    msg_id = f"msg_{int(now * 1000)}_schedule_list"
    request_id = (
        f"{parent_request_id}:schedule_list_followup"
        if parent_request_id
        else msg_id
    )
    invocation_id = tool_invocation_ledger.new_invocation_id(
        "main_schedule_list"
    )
    context = ToolContext(
        conv_id=conv_id,
        msg_id=msg_id,
        request_id=request_id,
        model_key=model_key,
        metadata={
            "source": "schedule_list_followup",
            "source_chain": "main",
            "invocation_id": invocation_id,
            "parent_request_id": parent_request_id,
        },
    )
    await tool_invocation_ledger.record_model_request(
        context,
        invocation_id=invocation_id,
        request_snapshot=messages,
        advertised_tools=(),
        metadata={"tool_name": "schedule.list"},
    )

    raw_output = ""
    error = ""
    try:
        temperature = SETTINGS.get("temperature")
        async for chunk in stream_ai(messages, model_key, temperature=temperature):
            raw_output += str(chunk)
    except Exception as exc:
        error = str(exc)
    await tool_invocation_ledger.record_model_output(
        context,
        invocation_id=invocation_id,
        raw_output=raw_output,
        outcome="failed" if error else ("succeeded" if raw_output.strip() else "unknown"),
        error=error,
        metadata={"tool_name": "schedule.list"},
    )

    if error:
        cleaned = f"[日程列表读取完成但回复生成失败] {error}"
    else:
        cleaned = strip_recall_intent_markers(
            strip_vow_markers(_strip_retry(raw_output))
        ).strip()
    if not cleaned:
        await tool_invocation_ledger.record_turn(
            context,
            prompt_source="schedule_list_followup",
            advertised_tools=(),
            turn_outcome="invalid_output",
        )
        return {"status": "empty", "invocation_id": invocation_id}

    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) "
            "VALUES (?,?,?,?,?,?)",
            (msg_id, conv_id, "assistant", cleaned, now, "[]"),
        )
        await db.execute(
            "UPDATE conversations SET updated_at=? WHERE id=?",
            (now, conv_id),
        )
        await db.commit()
    await manager.broadcast({
        "type": "msg_created",
        "data": {
            "id": msg_id,
            "conv_id": conv_id,
            "role": "assistant",
            "content": cleaned,
            "created_at": now,
            "attachments": [],
        },
        "tts": True,
    })
    await tool_invocation_ledger.record_visible_message(
        context,
        invocation_id=invocation_id,
        cleaned_content=cleaned,
        message_id=msg_id,
    )
    await tool_invocation_ledger.record_turn(
        context,
        prompt_source="schedule_list_followup",
        advertised_tools=(),
        turn_outcome="failed" if error else "succeeded",
    )
    timeline_service.start_background_refresh()
    await export_conversation(conv_id)
    return {
        "status": "failed" if error else "succeeded",
        "message_id": msg_id,
        "invocation_id": invocation_id,
    }


_PC_REJECT_TEXT = {
    "denied": "她没有允许这次截图",
    "confirm_timeout": "确认弹窗超时，她没有明确同意",
    "offline": "PC 截图 agent 当前不在线",
    "locked": "电脑处于锁屏状态",
    "rate_limited": "截图频率限制还没结束",
    "duplicate_pending": "上一条截图请求还没有结束",
    "model_no_vision": "当前主脑模型不支持直接看图",
    "hard_blocked": "本地隐私 gate 拦截了高风险窗口",
    "upload_failed": "截图上传失败",
    "request_expired": "等待截图结果超时",
}

# Mobile shares the base wording; offline/locked are filled in per device, and
# the Android-only capture failures are added.
_MOBILE_REJECT_TEXT = {
    **_PC_REJECT_TEXT,
    "hard_blocked": "这台设备没有截图能力",
    "permission_denied": "她在系统弹窗里没有授权录屏",
    "projection_failed": "系统录屏初始化失败",
    "capture_failed": "截图采集失败",
    "ambiguous_target": "有多台设备在线，没说清要看手机还是平板",
}


@dataclass(frozen=True)
class _ScreenFollowupOps:
    """Service operations the follow-up runner needs, so the same flow serves
    both the PC and mobile screen services (plan §9.3)."""
    timeout: float
    expire: Callable[[Any], Any]
    audit: Callable[[str, Any], Awaitable[None]]
    delete_files: Callable[[Any], None]
    release: Callable[[str], None]


async def perform_screen_check(request: ScreenCheckRequest):
    """Wait for a PC screenshot result, then append a Core follow-up."""
    ops = _ScreenFollowupOps(
        timeout=screen_service.REQUEST_TIMEOUT_SEC,
        expire=screen_service.expire_request,
        audit=screen_service.audit_screen_event,
        delete_files=screen_service.delete_request_files,
        release=screen_service.release_request,
    )
    await _run_screen_followup(request, ops=ops, screen_label="电脑", reject_text=_PC_REJECT_TEXT)


async def perform_mobile_screen_check(request):
    """Wait for a mobile screenshot result, then append a Core follow-up.

    Same runner as PC; only the service ops and the device label differ."""
    from app.mobile_screen import mobile_screen_service as mobile_service

    label = request.target_device_name or "手机"
    reject_text = dict(_MOBILE_REJECT_TEXT)
    reject_text["offline"] = f"{label}端当前不在线，打开 App 后才能截图"
    reject_text["locked"] = f"{label}处于锁屏状态"
    ops = _ScreenFollowupOps(
        timeout=mobile_service.request_timeout_sec(),
        expire=mobile_service.expire_request,
        audit=mobile_service.audit_screen_event,
        delete_files=mobile_service.delete_request_files,
        release=mobile_service.release_request,
    )
    await _run_screen_followup(request, ops=ops, screen_label=label, reject_text=reject_text)


async def _run_screen_followup(request, *, ops: _ScreenFollowupOps, screen_label: str, reject_text: dict):
    """Shared follow-up runner: wait for the screenshot result, then append a
    Core reply. Behaviour-preserving extraction of the original PC flow; the
    only variation points are the service ``ops`` and the human ``screen_label``."""
    try:
        try:
            await asyncio.wait_for(request._done_event.wait(), timeout=ops.timeout)
        except asyncio.TimeoutError:
            ops.expire(request)
            await ops.audit("rejected", request)

        event_type = "screen_check_complete" if request.status == "completed" else "screen_check_rejected"
        await manager.broadcast({"type": event_type, "data": _screen_event_payload(request)})
        await tool_invocation_ledger.record_terminal_outcome(
            correlation_id=request.request_id,
            outcome="succeeded" if request.status == "completed" else "rejected",
            event_type=event_type,
            error="" if request.status == "completed" else str(request.reject_reason or "rejected"),
            result=_screen_event_payload(request),
        )

        # 誓约读取失败 → fail-closed（§5.2）：截图是用户参与的主动请求，
        # 给可见错误；没有 SSE 通道，持久化 system 消息走 WebSocket msg_created。
        try:
            vow_block, _ = await vow_service.load_vow_prompt_context()
        except VowReadError:
            await persist_vow_blocked_message(request.conv_id)
            return

        wb = load_worldbook()
        user_name, ai_name = resolve_worldbook_names(wb)
        messages = await _screen_followup_messages(
            request, wb, user_name, screen_label=screen_label, reject_text=reject_text,
            vow_block=vow_block,
        )

        invocation_id = tool_invocation_ledger.new_invocation_id(
            "main_screen_followup"
        )
        observation_context = ToolContext(
            conv_id=request.conv_id,
            request_id=f"{request.request_id}:followup",
            model_key=request.model_key,
            metadata={
                "source": "screen_followup",
                "source_chain": "main",
                "invocation_id": invocation_id,
                "correlation_id": request.request_id,
            },
        )
        await tool_invocation_ledger.record_model_request(
            observation_context,
            invocation_id=invocation_id,
            request_snapshot=messages,
            advertised_tools=(),
            metadata={"screen_status": request.status},
        )
        raw_output = ""
        provider_error = ""
        try:
            _temp = SETTINGS.get("temperature")
            async for chunk in stream_ai(messages, request.model_key, temperature=_temp):
                raw_output += str(chunk)
        except Exception as e:
            provider_error = str(e)
        await tool_invocation_ledger.record_model_output(
            observation_context,
            invocation_id=invocation_id,
            raw_output=raw_output,
            outcome="failed" if provider_error else (
                "succeeded" if raw_output.strip() else "unknown"
            ),
            error=provider_error,
        )
        full_text = (
            f"[屏幕查看完成但回复生成失败] {provider_error}"
            if provider_error
            else raw_output
        )

        full_text = strip_recall_intent_markers(
            strip_vow_markers(_strip_retry(full_text))
        )
        if request.status == "completed":
            await ops.audit("completed", request)
        if not full_text.strip():
            await tool_invocation_ledger.record_turn(
                observation_context,
                prompt_source="screen_followup",
                advertised_tools=(),
                turn_outcome="invalid_output",
            )
            return

        if request.status == "completed":
            sys_now = time.time()
            sys_msg_id = f"msg_{int(sys_now*1000)}_screen_sys"
            sys_content = f"{ai_name}查看了{user_name}当前{screen_label}画面"
            async with get_db() as db:
                await db.execute(
                    "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                    (sys_msg_id, request.conv_id, "system", sys_content, sys_now, "[]"),
                )
                await db.commit()
            await manager.broadcast({"type": "msg_created", "data": {
                "id": sys_msg_id,
                "conv_id": request.conv_id,
                "role": "system",
                "content": sys_content,
                "created_at": sys_now,
                "attachments": [],
            }})

        now = time.time()
        msg_id = f"msg_{int(now*1000)}_screen"
        async with get_db() as db:
            await db.execute(
                "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                (msg_id, request.conv_id, "assistant", full_text, now, "[]"),
            )
            await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, request.conv_id))
            await db.commit()

        await manager.broadcast({"type": "msg_created", "data": {
            "id": msg_id,
            "conv_id": request.conv_id,
            "role": "assistant",
            "content": full_text,
            "created_at": now,
            "attachments": [],
        }, "tts": True})
        await tool_invocation_ledger.record_visible_message(
            observation_context,
            invocation_id=invocation_id,
            cleaned_content=full_text,
            message_id=msg_id,
        )
        await tool_invocation_ledger.record_turn(
            observation_context,
            prompt_source="screen_followup",
            advertised_tools=(),
            turn_outcome="failed" if provider_error else "succeeded",
        )
        timeline_service.start_background_refresh()
        await export_conversation(request.conv_id)
    finally:
        ops.delete_files(request)
        ops.release(request.request_id)


def _screen_event_payload(request) -> dict:
    payload = {
        "request_id": request.request_id,
        "conv_id": request.conv_id,
        "msg_id": request.msg_id,
        "reason": request.reason,
        "status": request.status,
        "reject_reason": request.reject_reason,
    }
    # 移动端请求带目标设备身份，让前端/日志能区分手机与平板（PC 请求无这些字段）。
    target_id = getattr(request, "target_device_id", None)
    if target_id:
        payload["target_device_id"] = target_id
        payload["target_device_name"] = getattr(request, "target_device_name", "")
        payload["target_device_type"] = getattr(request, "target_device_type", "")
    return payload


async def _screen_followup_messages(
    request, wb: dict, user_name: str, *, screen_label: str = "电脑",
    reject_text: dict | None = None, vow_block: str = ""
) -> list[dict]:
    prefix = build_worldbook_prefix(wb)

    import aiosqlite
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT role, content FROM messages WHERE conv_id=? AND role IN ('user','assistant') ORDER BY created_at DESC LIMIT 6",
            (request.conv_id,),
        )
        rows = await cur.fetchall()
    recent = [{"role": r["role"], "content": r["content"], "attachments": []} for r in reversed(rows)]
    vow_inject = _vow_inject_pair(vow_block)

    if request.status == "completed" and request.image_path:
        prompt = (
            f"你刚才请求查看{user_name}当前{screen_label}屏幕，原因是：{request.reason}\n\n"
            f"这张图片是{user_name}明确同意后采集的一次性截图。请根据画面自然接上对话，"
            "只概括她大概在做什么或你接下来要怎么管她。不要逐字转述屏幕文字、文件路径、聊天内容或账号信息。"
            "不要说“我看到图片里”这种工具口吻，也不要再说“让我看一眼”。"
        )
        return prefix + vow_inject + recent + [{"role": "user", "content": prompt, "attachments": [request.image_path]}]

    reason_text = (reject_text or _PC_REJECT_TEXT).get(request.reject_reason, "系统没有拿到截图")
    prompt = (
        f"你刚才想查看{user_name}当前{screen_label}画面，原因是：{request.reason}\n\n"
        f"但这次没有拿到截图，原因：{reason_text}。请自然接上对话，不要编造你看到了什么。"
        "如果是她拒绝或超时，不要施压，轻轻接住即可。"
    )
    return prefix + vow_inject + recent + [{"role": "user", "content": prompt}]
