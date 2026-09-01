"""Working Model V2 request orchestration.

This is deliberately a small, bounded background workflow rather than a job
queue.  The assistant message already exists when it starts.  A stable request
id and SQLite transactions make successful replays idempotent; an interrupted
``processing`` row is intentionally not auto-replayed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, replace
from difflib import SequenceMatcher
from typing import Any

from app.desire import repository as desire_repository
from app.memory_v2 import memory_service
from config import get_slot, load_ai_behavior
from database import get_db

from . import repository
from .gate import (
    REFLECTION_TYPE_GATE_PROMPT_VERSION,
    WORKING_MODEL_GATE_PROMPT_VERSION,
    WORKING_MODEL_GATE_SLOT,
    WorkingModelGateProvider,
    run_reflection_type_gate,
    run_working_model_gate,
)
from .writer import (
    WorkingModelWriterProvider,
    run_working_model_writer,
    writer_prompt_version,
)


logger = logging.getLogger(__name__)

WORKING_MODEL_DIFF_ALGORITHM = "sequence_matcher_ratio.v1"
WORKING_MODEL_MAX_HEAD_ATTEMPTS = 2
WORKING_MODEL_V2_WRITE_FLAG = "working_model_v2_write_enabled"
WORKING_MODEL_V2_INJECTION_FLAG = "working_model_v2_injection_enabled"
REQUEST_STATUS_PROCESSING = "processing"
REQUEST_TERMINAL_STATUSES = frozenset({"routed", "applied", "writer_noop", "failed"})


@dataclass(frozen=True)
class WorkingModelPipelineInput:
    conv_id: str
    origin_user_message_id: str | None
    origin_assistant_message_id: str | None
    statement: str
    source: str
    model_key: str
    identity_snapshot: Mapping[str, str]
    gate_model: str
    gate_prompt_version: str
    writer_prompt_version: str
    source_kind: str = "chat"
    reflection_log_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def working_model_v2_write_enabled() -> bool:
    return bool(load_ai_behavior().get(WORKING_MODEL_V2_WRITE_FLAG, False))


def working_model_v2_injection_enabled() -> bool:
    return bool(load_ai_behavior().get(WORKING_MODEL_V2_INJECTION_FLAG, False))


def capture_working_model_pipeline_input(
    *,
    conv_id: str,
    origin_user_message_id: str,
    origin_assistant_message_id: str,
    statement: str,
    source: str,
    model_key: str,
    identity_snapshot: Mapping[str, str],
) -> WorkingModelPipelineInput:
    gate_slot = get_slot(WORKING_MODEL_GATE_SLOT) or {}
    identity = dict(identity_snapshot)
    return WorkingModelPipelineInput(
        conv_id=str(conv_id or ""),
        origin_user_message_id=str(origin_user_message_id or ""),
        origin_assistant_message_id=str(origin_assistant_message_id or ""),
        statement=str(statement or "").strip(),
        source=str(source or "").strip(),
        model_key=str(model_key or "").strip(),
        identity_snapshot=identity,
        gate_model=str(gate_slot.get("model") or ""),
        gate_prompt_version=WORKING_MODEL_GATE_PROMPT_VERSION,
        writer_prompt_version=writer_prompt_version(identity),
        source_kind="chat",
        reflection_log_id=None,
    )


def capture_reflection_working_model_pipeline_input(
    *,
    conv_id: str,
    reflection_log_id: str,
    statement: str,
    source: str,
    model_key: str,
    identity_snapshot: Mapping[str, str],
) -> WorkingModelPipelineInput:
    """Freeze the dedicated reflection handoff without inventing chat IDs."""

    gate_slot = get_slot(WORKING_MODEL_GATE_SLOT) or {}
    identity = dict(identity_snapshot)
    return WorkingModelPipelineInput(
        conv_id=str(conv_id or ""),
        origin_user_message_id=None,
        origin_assistant_message_id=None,
        statement=str(statement or "").strip(),
        source=str(source or "").strip(),
        model_key=str(model_key or "").strip(),
        identity_snapshot=identity,
        gate_model=str(gate_slot.get("model") or ""),
        gate_prompt_version=REFLECTION_TYPE_GATE_PROMPT_VERSION,
        writer_prompt_version=writer_prompt_version(identity),
        source_kind="reflection",
        reflection_log_id=str(reflection_log_id or "").strip() or None,
    )


def stable_working_model_request_id(value: WorkingModelPipelineInput) -> str:
    if value.source_kind == "reflection":
        payload = {
            "conv_id": value.conv_id,
            "source_kind": "reflection",
            "reflection_log_id": value.reflection_log_id,
            "statement": value.statement,
        }
    else:
        payload = {
            "conv_id": value.conv_id,
            "origin_user_message_id": value.origin_user_message_id,
            "origin_assistant_message_id": value.origin_assistant_message_id,
            "statement": value.statement,
            "source": value.source,
        }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "wmreq_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _stable_child_id(prefix: str, request_id: str) -> str:
    digest = hashlib.sha256(f"{prefix}:{request_id}".encode("utf-8")).hexdigest()
    return f"{prefix}_{digest}"


def stable_working_model_memory_id(request_id: str) -> str:
    """Return the durable memory id used by both runtime and repair tooling."""

    return _stable_child_id("memwm", str(request_id))


def working_model_diff_ratio(before: str, after: str) -> float:
    similarity = SequenceMatcher(None, before, after, autojunk=False).ratio()
    return round(1.0 - similarity, 6)


def validate_terminal_request_outcome(
    *,
    status: str,
    route: str | None,
    disposition: str | None,
    resulting_memory_id: str | None,
    failure_code: str | None,
    parse_error_code: str | None = None,
) -> None:
    """Executable truth table for every terminal request state."""

    if status not in REQUEST_TERMINAL_STATUSES:
        raise ValueError("request status is not terminal")
    if status == "failed":
        if not failure_code or resulting_memory_id is not None or disposition is not None:
            raise ValueError("failed requests need failure_code and no semantic result")
        if parse_error_code is not None and failure_code != "parse_failed":
            raise ValueError("parse error detail requires parse_failed")
        return
    if failure_code is not None or parse_error_code is not None:
        raise ValueError("semantic outcomes cannot carry technical failure detail")
    expected = {
        ("routed", "reject", None, False),
        ("routed", "memory", None, True),
        ("routed", "working_model", "memory", True),
        ("applied", "working_model", "integrated", False),
        ("writer_noop", "working_model", "noop", False),
    }
    key = (status, route, disposition, resulting_memory_id is not None)
    if key not in expected:
        raise ValueError(f"invalid terminal request outcome: {key}")


async def _load_exact_origin_messages(
    db_factory,
    value: WorkingModelPipelineInput,
) -> tuple[str | None, bool]:
    async with db_factory() as db:
        cursor = await db.execute(
            "SELECT content FROM messages "
            "WHERE id=? AND conv_id=? AND role='user' LIMIT 1",
            (value.origin_user_message_id, value.conv_id),
        )
        user_row = await cursor.fetchone()
        cursor = await db.execute(
            "SELECT 1 FROM messages "
            "WHERE id=? AND conv_id=? AND role='assistant' LIMIT 1",
            (value.origin_assistant_message_id, value.conv_id),
        )
        assistant_row = await cursor.fetchone()
    return (str(user_row[0]) if user_row else None, assistant_row is not None)


async def _claim_request(
    db_factory,
    *,
    value: WorkingModelPipelineInput,
    request_id: str,
    now: float,
) -> tuple[dict, bool]:
    async with db_factory() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            existing = await repository.get_request(db, request_id)
            if existing is not None:
                await db.rollback()
                return existing, False
            created = await repository.insert_request(
                db,
                request_id=request_id,
                conv_id=value.conv_id,
                origin_user_message_id=value.origin_user_message_id,
                origin_assistant_message_id=value.origin_assistant_message_id,
                statement=value.statement,
                source=value.source,
                status=REQUEST_STATUS_PROCESSING,
                created_at=now,
                gate_model=value.gate_model or None,
                gate_prompt_version=value.gate_prompt_version,
            )
            await db.commit()
            return created, True
        except BaseException:
            await db.rollback()
            raise


async def _link_and_reload_reflection_source(
    db_factory,
    *,
    value: WorkingModelPipelineInput,
    request_id: str,
) -> str:
    """Link the request immediately, then reload the exact labeled material."""

    if not value.reflection_log_id:
        raise RuntimeError("reflection_log_id is missing")
    from app.reflection import repository as reflection_repository
    from app.reflection.prompt import render_reflection_source

    async with db_factory() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            log_row = await reflection_repository.get_log(
                db,
                value.reflection_log_id,
            )
            if log_row is None:
                raise RuntimeError("reflection log is missing")
            if str(log_row.get("target_conv_id") or "") != value.conv_id:
                raise RuntimeError("reflection conversation mismatch")
            if str(log_row.get("verdict") or "") != "conflicts":
                raise RuntimeError("reflection verdict is not conflicts")
            if str(log_row.get("proposed_statement") or "").strip() != value.statement:
                raise RuntimeError("reflection proposal mismatch")
            source = render_reflection_source(log_row)
            linked = await reflection_repository.link_request(
                db,
                log_id=value.reflection_log_id,
                request_id=request_id,
            )
            if not linked:
                raise RuntimeError("reflection request link failed")
            # The request's durable source is replaced with the same reloaded
            # material.  A caller-supplied approximation can therefore never
            # diverge from what the writer actually saw.
            cursor = await db.execute(
                "UPDATE working_model_requests SET source=? WHERE id=?",
                (source, request_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("reflection request source update failed")
            await db.commit()
            return source
        except BaseException:
            await db.rollback()
            raise


async def _mark_reflection_handoff_failed(
    db_factory,
    *,
    reflection_log_id: str | None,
) -> None:
    if not reflection_log_id:
        return
    from app.reflection import repository as reflection_repository

    try:
        async with db_factory() as db:
            await db.execute("BEGIN IMMEDIATE")
            updated = await reflection_repository.update_log(
                db,
                reflection_log_id,
                outcome="handoff_failed",
            )
            if updated is None:
                raise RuntimeError("reflection log disappeared")
            await db.commit()
    except Exception:
        logger.exception(
            "failed to mark reflection handoff failure log=%s",
            reflection_log_id,
        )


async def _set_request_outcome(
    db_factory,
    *,
    request_id: str,
    status: str,
    route: str | None,
    gate_reason: str | None,
    gate_model: str | None,
    gate_prompt_version: str | None,
    disposition: str | None = None,
    writer_model: str | None = None,
    writer_prompt_version: str | None = None,
    writer_change_note: str | None = None,
    resulting_memory_id: str | None = None,
    failure_code: str | None = None,
    parse_error_code: str | None = None,
    now: float,
    terminal: bool = True,
) -> dict:
    if terminal:
        validate_terminal_request_outcome(
            status=status,
            route=route,
            disposition=disposition,
            resulting_memory_id=resulting_memory_id,
            failure_code=failure_code,
            parse_error_code=parse_error_code,
        )
    async with db_factory() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            updated = await repository.update_request(
                db,
                request_id=request_id,
                status=status,
                updated_at=now,
                route=route,
                gate_reason=gate_reason,
                gate_model=gate_model,
                gate_prompt_version=gate_prompt_version,
                disposition=disposition,
                writer_model=writer_model,
                writer_prompt_version=writer_prompt_version,
                writer_change_note=writer_change_note,
                resulting_memory_id=resulting_memory_id,
                failure_code=failure_code,
                parse_error_code=parse_error_code,
            )
            if not updated:
                raise RuntimeError("working-model request disappeared")
            row = await repository.get_request(db, request_id)
            await db.commit()
            return row or {}
        except BaseException:
            await db.rollback()
            raise


async def _default_memory_prepare(content: str) -> bytes | None:
    return await memory_service.prepare_ai_note_embedding(content)


async def _default_memory_insert(db, **kwargs) -> dict:
    return await memory_service.create_working_model_ai_note_in_tx(db, **kwargs)


async def _persist_memory_outcome(
    db_factory,
    *,
    value: WorkingModelPipelineInput,
    request_id: str,
    route: str,
    gate_reason: str,
    gate_model: str,
    gate_prompt_version: str,
    disposition: str | None,
    writer_model: str | None,
    writer_prompt_version: str | None,
    writer_change_note: str | None,
    now: float,
    memory_prepare: Callable[[str], Awaitable[bytes | None]],
    memory_insert_in_tx: Callable[..., Awaitable[dict]],
) -> tuple[dict, dict]:
    embedding_blob = await memory_prepare(value.statement)
    memory_id = stable_working_model_memory_id(request_id)
    async with db_factory() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            current = await repository.get_request(db, request_id)
            if current is None:
                raise RuntimeError("working-model request disappeared")
            if current["status"] != REQUEST_STATUS_PROCESSING:
                await db.rollback()
                return current, {}
            memory = await memory_insert_in_tx(
                db,
                memory_id=memory_id,
                content=value.statement,
                source_conv=value.conv_id,
                origin_request_id=request_id,
                embedding_blob=embedding_blob,
                created_at=now,
                importance=0.6,
            )
            validate_terminal_request_outcome(
                status="routed",
                route=route,
                disposition=disposition,
                resulting_memory_id=memory_id,
                failure_code=None,
            )
            await repository.update_request(
                db,
                request_id=request_id,
                status="routed",
                updated_at=now,
                route=route,
                gate_reason=gate_reason,
                gate_model=gate_model,
                gate_prompt_version=gate_prompt_version,
                disposition=disposition,
                writer_model=writer_model,
                writer_prompt_version=writer_prompt_version,
                writer_change_note=writer_change_note,
                resulting_memory_id=memory_id,
                failure_code=None,
            )
            row = await repository.get_request(db, request_id)
            await db.commit()
            if memory:
                from app.memory_v2.hybrid_recall import invalidate_full_corpus_cache

                invalidate_full_corpus_cache(notes=True)
            return row or {}, memory
        except BaseException:
            await db.rollback()
            raise


async def _broadcast_memory(memory: dict) -> None:
    if not memory:
        return
    try:
        from ws import manager

        await manager.broadcast({"type": "memory_added", "data": memory})
    except Exception:
        logger.exception("working-model memory broadcast failed")


async def _persist_integrated_outcome(
    db_factory,
    *,
    value: WorkingModelPipelineInput,
    request_id: str,
    expected_working_model_head: dict,
    expected_desire_head: dict,
    writer_result,
    gate_reason: str,
    gate_model: str,
    gate_prompt_version: str,
    now: float,
) -> tuple[str, dict]:
    """Return (applied|conflict|already_terminal, request row)."""

    async with db_factory() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            request = await repository.get_request(db, request_id)
            if request is None:
                raise RuntimeError("working-model request disappeared")
            if request["status"] != REQUEST_STATUS_PROCESSING:
                await db.rollback()
                return "already_terminal", request
            current_working_model = await repository.get_head(db)
            current_desire = await desire_repository.get_head(db)
            if (
                current_working_model is None
                or current_desire is None
                or current_working_model["id"] != expected_working_model_head["id"]
                or current_desire["id"] != expected_desire_head["id"]
            ):
                await db.rollback()
                return "conflict", request

            working_model_id = _stable_child_id("wmv", request_id)
            version_reason = (
                {
                    "statement": value.statement,
                    "reflection_log_id": value.reflection_log_id,
                }
                if value.source_kind == "reflection"
                else {"statement": value.statement, "source": value.source}
            )
            await repository.insert_version(
                db,
                version_id=working_model_id,
                previous_version_id=current_working_model["id"],
                content=writer_result.working_model,
                created_at=now,
                origin_conv_id=value.conv_id,
                origin_message_id=value.origin_assistant_message_id,
                origin_request_id=request_id,
                reason=json.dumps(
                    version_reason,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                writer_model=value.model_key,
                prompt_version=writer_result.prompt_version,
                diff_ratio=working_model_diff_ratio(
                    current_working_model["content"],
                    writer_result.working_model,
                ),
                flagged=0,
            )
            if writer_result.desire != current_desire["content"]:
                await desire_repository.insert_version(
                    db,
                    version_id=_stable_child_id("desv", request_id),
                    previous_version_id=current_desire["id"],
                    content=writer_result.desire,
                    change_note=writer_result.change_note,
                    origin_request_id=request_id,
                    working_model_id=working_model_id,
                    writer_model=value.model_key,
                    prompt_version=writer_result.prompt_version,
                    created_at=now,
                )
            validate_terminal_request_outcome(
                status="applied",
                route="working_model",
                disposition="integrated",
                resulting_memory_id=None,
                failure_code=None,
            )
            await repository.update_request(
                db,
                request_id=request_id,
                status="applied",
                updated_at=now,
                route="working_model",
                gate_reason=gate_reason,
                gate_model=gate_model,
                gate_prompt_version=gate_prompt_version,
                disposition="integrated",
                writer_model=value.model_key,
                writer_prompt_version=writer_result.prompt_version,
                writer_change_note=writer_result.change_note,
                resulting_memory_id=None,
                failure_code=None,
            )
            row = await repository.get_request(db, request_id)
            await db.commit()
            return "applied", row or {}
        except BaseException:
            await db.rollback()
            raise


async def run_working_model_pipeline(
    value: WorkingModelPipelineInput,
    *,
    db_factory=get_db,
    gate_provider: WorkingModelGateProvider | None = None,
    writer_provider: WorkingModelWriterProvider | None = None,
    memory_prepare: Callable[[str], Awaitable[bytes | None]] = _default_memory_prepare,
    memory_insert_in_tx: Callable[..., Awaitable[dict]] = _default_memory_insert,
    memory_broadcast: Callable[[dict], Awaitable[None]] = _broadcast_memory,
    clock: Callable[[], float] = time.time,
) -> dict:
    """Execute one captured request with bounded calls and terminal auditing."""

    request_id = stable_working_model_request_id(value)
    created, claimed = await _claim_request(
        db_factory,
        value=value,
        request_id=request_id,
        now=clock(),
    )

    if value.source_kind not in {"chat", "reflection"}:
        if claimed:
            row = await _set_request_outcome(
                db_factory,
                request_id=request_id,
                status="failed",
                route=None,
                gate_reason=None,
                gate_model=value.gate_model or None,
                gate_prompt_version=value.gate_prompt_version,
                failure_code="source_kind_invalid",
                now=clock(),
            )
            return {"request": row, "deduplicated": False}
        return {"request": created, "deduplicated": True}

    if value.source_kind == "reflection":
        try:
            reloaded_source = await _link_and_reload_reflection_source(
                db_factory,
                value=value,
                request_id=request_id,
            )
            value = replace(value, source=reloaded_source)
        except Exception:
            logger.exception(
                "reflection request handoff failed request=%s log=%s",
                request_id,
                value.reflection_log_id,
            )
            await _mark_reflection_handoff_failed(
                db_factory,
                reflection_log_id=value.reflection_log_id,
            )
            if claimed:
                row = await _set_request_outcome(
                    db_factory,
                    request_id=request_id,
                    status="failed",
                    route=None,
                    gate_reason=None,
                    gate_model=value.gate_model or None,
                    gate_prompt_version=value.gate_prompt_version,
                    failure_code="reflection_link_failed",
                    now=clock(),
                )
            else:
                row = created
            return {
                "request": row,
                "deduplicated": not claimed,
                "handoff_failed": True,
                "reflection_log_id": value.reflection_log_id,
            }

    if not claimed:
        return {
            "request": created,
            "deduplicated": True,
            "reflection_log_id": value.reflection_log_id,
        }

    expected_gate_prompt_version = (
        REFLECTION_TYPE_GATE_PROMPT_VERSION
        if value.source_kind == "reflection"
        else WORKING_MODEL_GATE_PROMPT_VERSION
    )
    if (
        value.gate_prompt_version != expected_gate_prompt_version
        or value.writer_prompt_version != writer_prompt_version(value.identity_snapshot)
    ):
        row = await _set_request_outcome(
            db_factory,
            request_id=request_id,
            status="failed",
            route=None,
            gate_reason=None,
            gate_model=value.gate_model or None,
            gate_prompt_version=value.gate_prompt_version,
            failure_code="prompt_snapshot_mismatch",
            now=clock(),
        )
        return {"request": row, "deduplicated": False}

    if value.source_kind == "reflection":
        gate_result = await run_reflection_type_gate(
            statement=value.statement,
            provider=gate_provider,
            model=value.gate_model or None,
        )
    else:
        latest_user_message, assistant_message_exists = await _load_exact_origin_messages(
            db_factory,
            value,
        )
        if latest_user_message is None:
            row = await _set_request_outcome(
                db_factory,
                request_id=request_id,
                status="failed",
                route=None,
                gate_reason=None,
                gate_model=value.gate_model or None,
                gate_prompt_version=value.gate_prompt_version,
                failure_code="origin_user_message_missing",
                now=clock(),
            )
            return {"request": row, "deduplicated": False}
        if not assistant_message_exists:
            row = await _set_request_outcome(
                db_factory,
                request_id=request_id,
                status="failed",
                route=None,
                gate_reason=None,
                gate_model=value.gate_model or None,
                gate_prompt_version=value.gate_prompt_version,
                failure_code="origin_assistant_message_missing",
                now=clock(),
            )
            return {"request": row, "deduplicated": False}

        gate_result = await run_working_model_gate(
            statement=value.statement,
            source=value.source,
            latest_user_message=latest_user_message,
            provider=gate_provider,
            model=value.gate_model or None,
        )
    if not gate_result.ok:
        row = await _set_request_outcome(
            db_factory,
            request_id=request_id,
            status="failed",
            route=None,
            gate_reason=None,
            gate_model=gate_result.model or value.gate_model or None,
            gate_prompt_version=gate_result.prompt_version,
            failure_code=gate_result.failure_code,
            now=clock(),
        )
        return {"request": row, "deduplicated": False, "gate": gate_result.to_dict()}

    if gate_result.route == "reject":
        row = await _set_request_outcome(
            db_factory,
            request_id=request_id,
            status="routed",
            route="reject",
            gate_reason=gate_result.reason,
            gate_model=gate_result.model,
            gate_prompt_version=gate_result.prompt_version,
            now=clock(),
        )
        return {"request": row, "deduplicated": False, "gate": gate_result.to_dict()}

    if gate_result.route == "memory":
        try:
            row, memory = await _persist_memory_outcome(
                db_factory,
                value=value,
                request_id=request_id,
                route="memory",
                gate_reason=gate_result.reason,
                gate_model=gate_result.model,
                gate_prompt_version=gate_result.prompt_version,
                disposition=None,
                writer_model=None,
                writer_prompt_version=None,
                writer_change_note=None,
                now=clock(),
                memory_prepare=memory_prepare,
                memory_insert_in_tx=memory_insert_in_tx,
            )
        except Exception:
            logger.exception("working-model memory route failed request=%s", request_id)
            row = await _set_request_outcome(
                db_factory,
                request_id=request_id,
                status="failed",
                route="memory",
                gate_reason=gate_result.reason,
                gate_model=gate_result.model,
                gate_prompt_version=gate_result.prompt_version,
                failure_code="memory_write_failed",
                now=clock(),
            )
            return {"request": row, "deduplicated": False, "gate": gate_result.to_dict()}
        try:
            await memory_broadcast(memory)
        except Exception:
            logger.exception("working-model memory broadcast adapter failed")
        return {
            "request": row,
            "memory": memory,
            "deduplicated": False,
            "gate": gate_result.to_dict(),
        }

    # Preserve the gate audit before the core call. A process exit can lose one
    # update, but it cannot make the routed request look like it never ran.
    await _set_request_outcome(
        db_factory,
        request_id=request_id,
        status=REQUEST_STATUS_PROCESSING,
        route="working_model",
        gate_reason=gate_result.reason,
        gate_model=gate_result.model,
        gate_prompt_version=gate_result.prompt_version,
        writer_model=value.model_key,
        writer_prompt_version=value.writer_prompt_version,
        now=clock(),
        terminal=False,
    )

    writer_results: list[dict] = []
    for head_attempt in range(1, WORKING_MODEL_MAX_HEAD_ATTEMPTS + 1):
        async with db_factory() as db:
            working_model_head = await repository.get_head(db)
            desire_head = await desire_repository.get_head(db)
        if working_model_head is None or desire_head is None:
            row = await _set_request_outcome(
                db_factory,
                request_id=request_id,
                status="failed",
                route="working_model",
                gate_reason=gate_result.reason,
                gate_model=gate_result.model,
                gate_prompt_version=gate_result.prompt_version,
                writer_model=value.model_key,
                writer_prompt_version=value.writer_prompt_version,
                failure_code="head_missing",
                now=clock(),
            )
            return {"request": row, "gate": gate_result.to_dict()}

        writer_result = await run_working_model_writer(
            model_key=value.model_key,
            identity_snapshot=value.identity_snapshot,
            current_working_model=working_model_head["content"],
            current_desire=desire_head["content"],
            statement=value.statement,
            source=value.source,
            provider=writer_provider,
        )
        writer_results.append(writer_result.to_dict())
        if not writer_result.ok:
            row = await _set_request_outcome(
                db_factory,
                request_id=request_id,
                status="failed",
                route="working_model",
                gate_reason=gate_result.reason,
                gate_model=gate_result.model,
                gate_prompt_version=gate_result.prompt_version,
                writer_model=value.model_key,
                writer_prompt_version=writer_result.prompt_version,
                failure_code=writer_result.failure_code,
                parse_error_code=writer_result.parse_error_code,
                now=clock(),
            )
            return {
                "request": row,
                "gate": gate_result.to_dict(),
                "writers": writer_results,
            }

        if writer_result.disposition == "noop":
            row = await _set_request_outcome(
                db_factory,
                request_id=request_id,
                status="writer_noop",
                route="working_model",
                gate_reason=gate_result.reason,
                gate_model=gate_result.model,
                gate_prompt_version=gate_result.prompt_version,
                disposition="noop",
                writer_model=value.model_key,
                writer_prompt_version=writer_result.prompt_version,
                writer_change_note=writer_result.change_note,
                now=clock(),
            )
            return {"request": row, "gate": gate_result.to_dict(), "writers": writer_results}

        if writer_result.disposition == "memory":
            try:
                row, memory = await _persist_memory_outcome(
                    db_factory,
                    value=value,
                    request_id=request_id,
                    route="working_model",
                    gate_reason=gate_result.reason,
                    gate_model=gate_result.model,
                    gate_prompt_version=gate_result.prompt_version,
                    disposition="memory",
                    writer_model=value.model_key,
                    writer_prompt_version=writer_result.prompt_version,
                    writer_change_note=writer_result.change_note,
                    now=clock(),
                    memory_prepare=memory_prepare,
                    memory_insert_in_tx=memory_insert_in_tx,
                )
            except Exception:
                logger.exception("working-model writer memory failed request=%s", request_id)
                row = await _set_request_outcome(
                    db_factory,
                    request_id=request_id,
                    status="failed",
                    route="working_model",
                    gate_reason=gate_result.reason,
                    gate_model=gate_result.model,
                    gate_prompt_version=gate_result.prompt_version,
                    writer_model=value.model_key,
                    writer_prompt_version=writer_result.prompt_version,
                    writer_change_note=writer_result.change_note,
                    failure_code="memory_write_failed",
                    now=clock(),
                )
                return {"request": row, "gate": gate_result.to_dict(), "writers": writer_results}
            try:
                await memory_broadcast(memory)
            except Exception:
                logger.exception("working-model memory broadcast adapter failed")
            return {
                "request": row,
                "memory": memory,
                "gate": gate_result.to_dict(),
                "writers": writer_results,
            }

        try:
            outcome, row = await _persist_integrated_outcome(
                db_factory,
                value=value,
                request_id=request_id,
                expected_working_model_head=working_model_head,
                expected_desire_head=desire_head,
                writer_result=writer_result,
                gate_reason=gate_result.reason,
                gate_model=gate_result.model,
                gate_prompt_version=gate_result.prompt_version,
                now=clock(),
            )
        except Exception:
            logger.exception("working-model version write failed request=%s", request_id)
            row = await _set_request_outcome(
                db_factory,
                request_id=request_id,
                status="failed",
                route="working_model",
                gate_reason=gate_result.reason,
                gate_model=gate_result.model,
                gate_prompt_version=gate_result.prompt_version,
                writer_model=value.model_key,
                writer_prompt_version=writer_result.prompt_version,
                writer_change_note=writer_result.change_note,
                failure_code="version_write_failed",
                now=clock(),
            )
            return {"request": row, "gate": gate_result.to_dict(), "writers": writer_results}
        if outcome in {"applied", "already_terminal"}:
            return {
                "request": row,
                "gate": gate_result.to_dict(),
                "writers": writer_results,
                "head_attempts": head_attempt,
            }

    row = await _set_request_outcome(
        db_factory,
        request_id=request_id,
        status="failed",
        route="working_model",
        gate_reason=gate_result.reason,
        gate_model=gate_result.model,
        gate_prompt_version=gate_result.prompt_version,
        writer_model=value.model_key,
        writer_prompt_version=value.writer_prompt_version,
        writer_change_note=(writer_results[-1].get("change_note") if writer_results else None),
        failure_code="successor_conflict",
        now=clock(),
    )
    return {
        "request": row,
        "gate": gate_result.to_dict(),
        "writers": writer_results,
        "head_attempts": WORKING_MODEL_MAX_HEAD_ATTEMPTS,
    }


__all__ = [
    "REQUEST_STATUS_PROCESSING",
    "REQUEST_TERMINAL_STATUSES",
    "WORKING_MODEL_DIFF_ALGORITHM",
    "WORKING_MODEL_MAX_HEAD_ATTEMPTS",
    "WORKING_MODEL_V2_INJECTION_FLAG",
    "WORKING_MODEL_V2_WRITE_FLAG",
    "WorkingModelPipelineInput",
    "capture_reflection_working_model_pipeline_input",
    "capture_working_model_pipeline_input",
    "run_working_model_pipeline",
    "stable_working_model_request_id",
    "validate_terminal_request_outcome",
    "working_model_diff_ratio",
    "working_model_v2_injection_enabled",
    "working_model_v2_write_enabled",
]
