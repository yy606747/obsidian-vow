"""Memory recall helpers used by chat generation routes."""

from __future__ import annotations

import time
from datetime import datetime

from app.memory_v2 import memory_service

def _chat_memory_mode(*, whisper_mode: bool = False, ai_dom_mode: bool = False) -> str:
    return "intimate" if whisper_mode or ai_dom_mode else "normal"


async def _plan_v2_recall_debug(
    recall_query: str,
    recall_keywords: list,
    *,
    whisper_mode: bool = False,
    ai_dom_mode: bool = False,
    prompt_seed: str = "",
    pending_items: list[dict] | None = None,
    visible_message_ids: list[str] | None = None,
    user_name: str = "她",
) -> dict:
    """V2 recall sidecar. Never block legacy chat if it fails."""
    try:
        return await memory_service.plan_v2_recall_for_chat(
            recall_query,
            keywords=recall_keywords,
            mode=_chat_memory_mode(whisper_mode=whisper_mode, ai_dom_mode=ai_dom_mode),
            prompt_seed=prompt_seed,
            pending_items=pending_items,
            visible_message_ids=visible_message_ids,
            user_name=user_name,
        )
    except Exception as exc:
        try:
            runtime = memory_service.get_v2_recall_config().get("runtime", {})
        except Exception:
            runtime = {}
        return {
            "runtime": runtime,
            "summary": None,
            "trace": None,
            "prompt_block": None,
            "prompt_decision": {"inject": False, "reason": "error"},
            "error": f"{exc.__class__.__name__}: {exc}",
        }


def _inject_v2_prompt_block_if_enabled(
    history: list[dict],
    *,
    cap_idx: int,
    inject_offset: int,
    memory_v2_recall_debug: dict | None,
) -> int:
    if not memory_v2_recall_debug:
        return inject_offset
    block = memory_v2_recall_debug.get("prompt_block") or {}
    decision = memory_v2_recall_debug.get("prompt_decision") or {}
    if not decision.get("inject") or not block.get("enabled") or not block.get("content"):
        return inject_offset
    history.insert(cap_idx + inject_offset, {"role": "user", "content": block["content"]})
    history.insert(
        cap_idx + inject_offset + 1,
        {"role": "assistant", "content": "好的，我心里有数了。"},
    )
    return inject_offset + 2


def _v2_owns_memory_prompt(memory_v2_recall_debug: dict | None) -> bool:
    """Return whether V2 should be treated as the primary memory prompt path."""
    if not memory_v2_recall_debug or memory_v2_recall_debug.get("error"):
        return False
    runtime = memory_v2_recall_debug.get("runtime") or {}
    if not runtime.get("v2_enabled"):
        return False
    rollout = memory_v2_recall_debug.get("rollout_decision") or memory_v2_recall_debug.get("prompt_decision") or {}
    mode = rollout.get("mode") or runtime.get("mode")
    if mode == "full":
        return True
    return mode == "canary" and bool(rollout.get("inject"))


async def _record_v2_memory_usage_for_chat(
    memory_v2_recall_debug: dict | None,
    *,
    conv_id: str,
    msg_id: str,
    chat_succeeded: bool = True,
    response_text: str = "",
) -> dict | None:
    if not memory_v2_recall_debug:
        return None
    if not chat_succeeded:
        usage = {"status": "skipped", "reason": "model_error", "count": 0}
        memory_v2_recall_debug["usage"] = usage
        return usage
    try:
        usage = await memory_service.record_v2_prompt_usage(
            memory_v2_recall_debug,
            conv_id=conv_id,
            request_id=msg_id,
            response_text=response_text,
        )
    except Exception as exc:
        usage = {"status": "error", "reason": f"{exc.__class__.__name__}: {exc}", "count": 0}
    memory_v2_recall_debug["usage"] = usage
    return usage


def _humanize_ago(ts) -> str:
    """把记忆时间戳翻译成人话：刚才 / 今天 HH:MM / 昨天 / N 天前 / N 周前 / 上个月 / N 个月前 / N 年前"""
    try:
        ts = float(ts) if ts else 0
    except Exception:
        return ""
    if not ts: return ""
    now = time.time()
    delta = now - ts
    if delta < 600: return "刚才"
    if delta < 3600: return f"{int(delta // 60)} 分钟前"
    try:
        dt = datetime.fromtimestamp(ts)
        days = (datetime.fromtimestamp(now).date() - dt.date()).days
    except Exception:
        return ""
    if days == 0: return f"今天 {dt.strftime('%H:%M')}"
    if days == 1: return "昨天"
    if days == 2: return "前天"
    if days < 7: return f"{days} 天前"
    if days < 14: return "上周"
    if days < 30: return f"{days // 7} 周前"
    if days < 60: return "上个月"
    if days < 365: return f"{days // 30} 个月前"
    return f"{days // 365} 年前"
