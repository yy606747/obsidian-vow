from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiosqlite
from fastapi.responses import StreamingResponse

from app.background_tasks import create_tracked_task
from app.memory_v3.timeline import timeline_service
from app.tools.schemas import ToolContext, ToolIntent
from app.tools.ledger import tool_invocation_ledger
from app.tools.service import tool_service
from app.vows.service import VowReadError, vow_service

from .action_executor import execute_postprocessed_actions
from .commands import _SYSTEM_MSG_CONTEXT_KEYWORDS
from .error_text import looks_like_model_error_text
from .postprocess import PostProcessor, looks_like_structured_reply, strip_retry_marker
from .streaming import (
    _RecallIntentStreamFilter,
    _TideIntentStreamFilter,
    _VowStreamFilter,
    _log_toy_delivery,
    _toy_command_intents,
    _toy_delivery_debug,
    _toy_payload_from_results,
    _toy_rejection_payload,
)
from .turn_profiles import (
    classify_opportunity_control_output,
    initiative_turn_profile,
)
from .worldbook import build_worldbook_prefix

_DISABLED_COMMAND_MARKER_RE = re.compile(r"\[(?:TOY|REMEMBER):[^\]]+\]", re.IGNORECASE)


@dataclass(frozen=True)
class InitiativeSpec:
    kind: str
    context_limit: int
    msg_suffix: str
    ability_block: str
    event_block: str
    include_sse_msg_id: bool = False
    assistant_attachments: str = "[]"
    toy_enabled: bool = True
    control_context_source: str = "none"
    control_session_id: str | None = None
    control_epoch: int | None = None
    owner_client_id: str | None = None
    advertised_tools: tuple[str, ...] = ()

def _message_from_row(row: Mapping[str, Any]) -> dict | None:
    item = dict(row)
    role = item.get("role")
    if role == "trigger":
        item["role"] = "user"
        item["attachments"] = []
        return item
    if role == "system":
        content = str(item.get("content") or "")
        if not any(kw in content for kw in _SYSTEM_MSG_CONTEXT_KEYWORDS):
            return None
        item["role"] = "user"
        item["content"] = f"[系统事件] {content}"
        item["attachments"] = []
        return item
    try:
        raw_attachments = item.get("attachments") or "[]"
        item["attachments"] = json.loads(raw_attachments) if raw_attachments else []
    except Exception:
        item["attachments"] = []
    if item.get("created_at"):
        dt = datetime.fromtimestamp(item["created_at"])
        item["content"] = f"{item['content']}\n<meta>发送时间：{dt.month}月{dt.day}日 {dt.strftime('%H:%M')}</meta>"
    return item

async def _load_model_and_history(*, conv_id: str, context_limit: int, get_db: Callable) -> tuple[str | None, list[dict]]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT model FROM conversations WHERE id=?", (conv_id,))
        conv = await cur.fetchone()
        if not conv:
            return None, []
        model_key = conv["model"]
        cur = await db.execute(
            "SELECT role, content, attachments, created_at FROM messages WHERE conv_id=? AND role IN ('user','assistant','system','trigger') ORDER BY created_at DESC LIMIT ?",
            (conv_id, context_limit),
        )
        rows = await cur.fetchall()

    history = []
    for row in reversed(rows):
        item = _message_from_row(row)
        if item is not None:
            item["attachments"] = []
            history.append(item)
    return model_key, history

def _with_worldbook_prefix(history: list[dict], worldbook: Mapping[str, Any]) -> tuple[list[dict], int]:
    prefix = build_worldbook_prefix(worldbook)
    return prefix + history, len(prefix)

def _insert_system_blocks(history: list[dict], *, cap_idx: int, spec: InitiativeSpec, vow_block: str = "") -> None:
    offset = 0
    if vow_block:
        # 誓约 block 恒在 ability block 之前（誓约设计 §5.1）
        history.insert(cap_idx, {"role": "user", "content": vow_block})
        history.insert(cap_idx + 1, {"role": "assistant", "content": "（嗯，这些一直都算数。）"})
        offset = 2
    history.insert(cap_idx + offset, {"role": "user", "content": spec.ability_block})
    history.insert(cap_idx + offset + 1, {"role": "assistant", "content": "（我知道自己现在能做什么。）"})
    now_str = datetime.now().strftime("%Y年%m月%d日  %H:%M:%S")
    history.insert(cap_idx + offset + 2, {"role": "user", "content": f"系统当前的准确时间是 {now_str}"})
    history.insert(cap_idx + offset + 3, {"role": "assistant", "content": "（嗯，知道了。）"})
    history.append({"role": "user", "content": spec.event_block})

async def stream_initiative_response(
    *, conv_id: str, spec: InitiativeSpec, worldbook: Mapping[str, Any],
    get_db: Callable, stream_ai: Callable, post_processor: PostProcessor,
    broadcast: Callable[[dict], Awaitable[None]], export_conversation: Callable[[str], Awaitable[Any]],
    store_remember_notes: Callable[[list[str], str], Awaitable[Any]], toy_sys_msg: Callable[[str, list[str]], Awaitable[Any]],
) -> StreamingResponse | dict:
    model_key, history = await _load_model_and_history(conv_id=conv_id, context_limit=spec.context_limit, get_db=get_db)
    if not model_key:
        return {"error": "conversation not found"}

    # 誓约读取失败 → 系统主动路径，跳过本次生成并记录（誓约设计 §5.2），
    # 不向用户报错。本路径不允许立约，prompt 不提供 [VOW] 能力说明（§4.6）。
    try:
        vow_block, _ = await vow_service.load_vow_prompt_context()
    except VowReadError as exc:
        print(f"[Initiative] 誓约读取失败，跳过本次生成: {exc}")
        return {"error": "vow_read_failed"}

    history, cap_idx = _with_worldbook_prefix(history, worldbook)
    _insert_system_blocks(history, cap_idx=cap_idx, spec=spec, vow_block=vow_block)

    ai_msg_id = f"msg_{int(time.time() * 1000)}_{spec.msg_suffix}"
    invocation_id = tool_invocation_ledger.new_invocation_id(
        "initiative_core"
    )
    turn_profile = initiative_turn_profile(toy_enabled=spec.toy_enabled)
    mode = ("device_control" if spec.kind == "dom" else "intimate") if spec.toy_enabled else "normal"
    toy_context = ToolContext(
        conv_id=conv_id,
        msg_id=ai_msg_id,
        request_id=ai_msg_id,
        model_key=model_key,
        mode=mode,
        capabilities=tuple(turn_profile.allowed_tool_capabilities),
        metadata={
            "source": "initiative",
            "source_chain": "initiative",
            "invocation_id": invocation_id,
            "advertised_tools": spec.advertised_tools,
            "control_kind": spec.kind,
            "control_context_source": spec.control_context_source,
            "control_session_id": spec.control_session_id,
            "control_epoch": spec.control_epoch,
            "owner_client_id": spec.owner_client_id,
        },
    )
    usage_meta: dict = {}
    queue: asyncio.Queue = asyncio.Queue()

    async def _bg_generate() -> None:
        full_text = ""
        has_error = False
        assistant_persisted = False
        turn_outcome = "pipeline_failed"
        provider_error_text = ""
        buffering_structured_reply: bool | None = None
        buffering_disabled_command = False
        tide_filter = _TideIntentStreamFilter()
        recall_filter = _RecallIntentStreamFilter()
        vow_filter = _VowStreamFilter()
        try:
            await queue.put({"id": ai_msg_id, "type": "start"})
            try:
                async for chunk in stream_ai(history, model_key, usage_meta):
                    if isinstance(chunk, Mapping):
                        usage_meta.update(chunk)
                        continue
                    chunk = str(chunk)
                    full_text += chunk
                    if not spec.toy_enabled:
                        if buffering_disabled_command:
                            continue
                        if _DISABLED_COMMAND_MARKER_RE.search(full_text[-256:]):
                            buffering_disabled_command = True
                            continue
                    if buffering_structured_reply is None:
                        probe = full_text.lstrip()
                        if not probe:
                            continue
                        buffering_structured_reply = looks_like_structured_reply(probe)
                        if not buffering_structured_reply:
                            visible = vow_filter.feed(
                                tide_filter.feed(recall_filter.feed(full_text))
                            )
                            if visible:
                                await queue.put({"type": "chunk", "content": visible})
                        continue
                    if not buffering_structured_reply:
                        visible = vow_filter.feed(
                            tide_filter.feed(recall_filter.feed(chunk))
                        )
                        if visible:
                            await queue.put({"type": "chunk", "content": visible})
            except Exception as exc:
                has_error = True
                turn_outcome = "provider_failed"
                provider_error_text = f"\n[请求出错: {exc}]"
                full_text += provider_error_text
                if not buffering_structured_reply and not buffering_disabled_command:
                    await queue.put({"type": "chunk", "content": provider_error_text})

            full_text = strip_retry_marker(full_text)
            if looks_like_model_error_text(full_text.strip()):
                has_error = True
                turn_outcome = "provider_failed"
            elif not has_error:
                turn_outcome = "succeeded" if full_text.strip() else "invalid_output"
            if not buffering_structured_reply and not buffering_disabled_command:
                tail = vow_filter.feed(tide_filter.feed(recall_filter.flush()))
                tail += vow_filter.feed(tide_filter.flush()) + vow_filter.flush()
                if tail:
                    await queue.put({"type": "chunk", "content": tail})

            # Initiative has no control markers of its own. Any opportunity
            # control syntax here is reserved/misrouted and must fail closed
            # before PostProcessor or an adapter sees the output.
            if classify_opportunity_control_output(
                full_text,
                profile=turn_profile,
            ) != "ordinary":
                turn_outcome = "invalid_output"
                return
            try:
                postprocess_text = (
                    full_text[: -len(provider_error_text)]
                    if provider_error_text and full_text.endswith(provider_error_text)
                    else full_text
                )
                try:
                    postprocessed = await post_processor.process(
                        postprocess_text,
                        conv_id=conv_id,
                        enabled_commands=turn_profile.enabled_commands,
                        tool_context=toy_context,
                    )
                except TypeError as exc:
                    if "tool_context" not in str(exc):
                        raise
                    postprocessed = await post_processor.process(
                        postprocess_text,
                        conv_id=conv_id,
                        enabled_commands=turn_profile.enabled_commands,
                    )
            except Exception:
                if turn_outcome == "succeeded":
                    turn_outcome = "postprocess_failed"
                return
            full_text = postprocessed.content
            if not spec.toy_enabled:
                # Disabled private commands remain non-visible even though the
                # profile correctly prevents them from becoming executable
                # intents.
                full_text = _DISABLED_COMMAND_MARKER_RE.sub("", full_text).strip()
            if (buffering_structured_reply or buffering_disabled_command) and full_text:
                await queue.put({"type": "chunk", "content": full_text})
            if has_error:
                if provider_error_text and (
                    buffering_structured_reply or buffering_disabled_command
                ):
                    await queue.put({"type": "chunk", "content": provider_error_text})
                return
            toy_intents = _toy_command_intents(postprocessed)

            async def _store_remember(
                intent: ToolIntent,
                context: ToolContext,
            ) -> dict:
                content = str(intent.arguments.get("content") or "").strip()
                if content:
                    await store_remember_notes([content], context.conv_id)
                return {"content": content, "stored": bool(content)}

            toy_results = []
            if spec.toy_enabled:
                toy_execution = await execute_postprocessed_actions(
                    postprocessed,
                    profile=turn_profile,
                    context=toy_context,
                    only_capabilities=frozenset({"device.toy"}),
                    tool_service_override=tool_service,
                )
                toy_results = toy_execution.results_for("device.toy")
            await execute_postprocessed_actions(
                postprocessed,
                profile=turn_profile,
                context=toy_context,
                only_capabilities=frozenset({"memory.remember"}),
                adapter_overrides={"memory.remember": _store_remember},
                tool_service_override=tool_service,
            )
            toy_payload = _toy_payload_from_results(toy_results)
            toy_delivery = _toy_delivery_debug(toy_intents, toy_results, toy_payload)
            _log_toy_delivery(
                conv_id=conv_id,
                msg_id=ai_msg_id,
                model_key=model_key,
                context=toy_context,
                delivery=toy_delivery,
            )
            if not has_error and full_text:
                now = time.time()
                async with get_db() as db:
                    await db.execute("INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)", (ai_msg_id, conv_id, "assistant", full_text, now, spec.assistant_attachments))
                    await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
                    await db.commit()
                assistant_persisted = True
                ai_msg = {"id": ai_msg_id, "conv_id": conv_id, "role": "assistant", "content": full_text, "created_at": now, "attachments": []}
                await broadcast({"type": "msg_created", "data": ai_msg})
                timeline_service.start_background_refresh()
                await export_conversation(conv_id)
                if postprocessed.tide_intent:
                    from app.tide.intent import tide_intent_service
                    await tide_intent_service.record_intent(
                        conv_id=conv_id,
                        msg_id=ai_msg_id,
                        intent_text=postprocessed.tide_intent,
                        invocation_id=invocation_id,
                        advertised_tools=spec.advertised_tools,
                    )

            if not has_error and toy_payload:
                toy_matches = toy_payload["commands"]
                toy_event = dict(toy_payload)
                if spec.include_sse_msg_id:
                    toy_event["msg_id"] = ai_msg_id
                await queue.put(toy_event)
                toy_payload["msg_id"] = ai_msg_id
                await broadcast({"type": "toy_command", "data": toy_payload})
                await toy_sys_msg(conv_id, toy_matches)
            elif not has_error and spec.toy_enabled:
                toy_rejection = _toy_rejection_payload(toy_intents, toy_results, toy_delivery, msg_id=ai_msg_id)
                if toy_rejection:
                    await queue.put(toy_rejection)
                    await broadcast({"type": "toy_command_rejected", "data": toy_rejection})
        except Exception:
            if turn_outcome in {"succeeded", "invalid_output"}:
                turn_outcome = "pipeline_failed"
            import traceback
            traceback.print_exc()
        finally:
            await tool_invocation_ledger.record_turn(
                toy_context,
                prompt_source="initiative",
                advertised_tools=spec.advertised_tools,
                turn_outcome=turn_outcome,
                metadata={
                    "initiative_kind": spec.kind,
                    "assistant_persisted": assistant_persisted,
                    "has_error": has_error,
                },
            )
            await queue.put({"type": "done"})

    create_tracked_task(_bg_generate(), name=f"initiative:{conv_id}:{ai_msg_id}")

    async def _events():
        while True:
            data = await queue.get()
            if data.get("type") == "done":
                break
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    return StreamingResponse(_events(), media_type="text/event-stream")
