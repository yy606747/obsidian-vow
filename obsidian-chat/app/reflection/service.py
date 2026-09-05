"""Orchestrate one invisible, audited working-model reflection."""

from __future__ import annotations

import inspect
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ai_providers import call_core_chat_once, call_slot_chat
from config import get_slot, load_ai_behavior
from database import get_db

from . import repository
from .harness import (
    REFLECTION_RECENT_CLUE_EXCLUSION,
    adapt_retrieved_items,
    split_working_model_sentences,
)
from .prompt import (
    INVERSE_QUERY_PROMPT_VERSION,
    ReflectionParseError,
    build_inverse_query_messages,
    build_reflection_messages,
    parse_inverse_query,
    parse_reflection_output,
    reflection_prompt_version,
    render_reflection_source,
)


logger = logging.getLogger(__name__)

REFLECTION_FEATURE_FLAG = "working_model_reflection_enabled"
REFLECTION_QUERY_SLOT = "harness_tool"
REFLECTION_QUERY_TIMEOUT_SEC = 60.0
REFLECTION_CORE_TIMEOUT_SEC = 120.0

QueryGenerator = Callable[[str], Awaitable[str] | str]
ReflectionRetriever = Callable[[str], Awaitable[Sequence[dict] | dict] | Sequence[dict] | dict]
ReflectionProvider = Callable[[list[dict[str, str]]], Awaitable[str] | str]
PipelineRunner = Callable[..., Awaitable[dict] | dict]


@dataclass(frozen=True)
class CapturedReflectionContext:
    target_conv_id: str
    model_key: str
    identity_snapshot: Mapping[str, str]
    working_model_head: Mapping[str, Any]
    candidate_clues: tuple[str, ...]


def reflection_feature_enabled() -> bool:
    return bool(load_ai_behavior().get(REFLECTION_FEATURE_FLAG, False))


async def capture_reflection_context(
    *,
    target_conv_id: str,
    model_key: str,
    identity_snapshot: Mapping[str, str],
    db_factory=get_db,
    require_feature_flag: bool = True,
    working_model_head: Mapping[str, Any] | None = None,
) -> CapturedReflectionContext | None:
    """Freeze the head/model/identity used by both reflection and writer."""

    if require_feature_flag and not reflection_feature_enabled():
        return None
    from app.working_model import repository as working_model_repository

    async with db_factory() as db:
        if working_model_head is None:
            working_model_head = await working_model_repository.get_head(db)
        recent_clues = await repository.list_recent_clues(
            db,
            limit=REFLECTION_RECENT_CLUE_EXCLUSION,
        )
    if working_model_head is None or not str(working_model_head.get("id") or "").strip():
        return None
    excluded = set(recent_clues)
    candidates = tuple(
        item
        for item in split_working_model_sentences(
            str(working_model_head.get("content") or "")
        )
        if item not in excluded
    )
    if not candidates:
        return None
    identity = dict(identity_snapshot)
    if not str(identity.get("text") or "").strip():
        return None
    return CapturedReflectionContext(
        target_conv_id=str(target_conv_id),
        model_key=str(model_key),
        identity_snapshot=identity,
        working_model_head=dict(working_model_head),
        candidate_clues=candidates,
    )


async def _update_log(db_factory, log_id: str, **changes: Any) -> dict:
    async with db_factory() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            row = await repository.update_log(db, log_id, **changes)
            if row is None:
                raise RuntimeError("reflection log disappeared")
            await db.commit()
            return row
        except BaseException:
            await db.rollback()
            raise


async def _default_query_generator(clue: str) -> str:
    raw = await call_slot_chat(
        REFLECTION_QUERY_SLOT,
        build_inverse_query_messages(clue),
        expect_json=True,
        timeout=REFLECTION_QUERY_TIMEOUT_SEC,
        temperature=0.2,
        scope="working_model:reflection_inverse_query",
    )
    return parse_inverse_query(raw)


async def _default_retriever(query: str) -> list[dict]:
    # Lazy import avoids pulling the memory package into database startup.
    from app.memory_v2 import memory_service

    plan = await memory_service.plan_v2_recall_for_reflection(query)
    selected = plan.get("selected") if isinstance(plan, dict) else None
    if not isinstance(selected, list):
        raise RuntimeError("production recall returned no selected contract")
    if any(not isinstance(item, dict) for item in selected[:5]):
        raise RuntimeError("production recall returned an invalid selected item")
    return [dict(item) for item in selected[:5]]


async def _await_result(value):
    return await value if inspect.isawaitable(value) else value


async def run_reflection(
    captured: CapturedReflectionContext,
    *,
    db_factory=get_db,
    query_generator: QueryGenerator | None = None,
    retriever: ReflectionRetriever | None = None,
    reflection_provider: ReflectionProvider | None = None,
    pipeline_runner: PipelineRunner | None = None,
    chooser=None,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Run one chain. All failures after INSERT update that same log row."""

    pick = chooser or random.choice
    clue = str(pick(captured.candidate_clues)) if captured.candidate_clues else None
    if not clue:
        return {"entered": False, "status": "no_sampleable_clue"}

    log_id = repository.new_reflection_log_id()
    try:
        async with db_factory() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                log_row = await repository.insert_log(
                    db,
                    log_id=log_id,
                    target_conv_id=captured.target_conv_id,
                    clue=clue,
                    working_model_id=str(captured.working_model_head.get("id") or ""),
                    created_at=clock(),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
    except Exception:
        logger.exception("reflection log insert failed")
        return {"entered": False, "status": "log_insert_failed"}

    query_slot = get_slot(REFLECTION_QUERY_SLOT) or {}
    query_model = str(query_slot.get("model") or "")
    generator = query_generator or _default_query_generator
    try:
        inverse_query = " ".join(
            str(await _await_result(generator(clue)) or "").split()
        )
        if not inverse_query:
            raise ReflectionParseError("inverse query is empty")
    except Exception:
        row = await _update_log(
            db_factory,
            log_id,
            query_model=query_model,
            query_prompt_version=INVERSE_QUERY_PROMPT_VERSION,
            outcome="query_failed",
        )
        return {"entered": True, "status": "query_failed", "log": row}

    await _update_log(
        db_factory,
        log_id,
        inverse_query=inverse_query,
        query_model=query_model,
        query_prompt_version=INVERSE_QUERY_PROMPT_VERSION,
    )

    retrieve = retriever or _default_retriever
    try:
        retrieved = await _await_result(retrieve(inverse_query))
        if isinstance(retrieved, dict):
            raw_items = retrieved.get("selected")
        else:
            raw_items = retrieved
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
            raise RuntimeError("retriever returned an invalid contract")
        if any(not isinstance(item, dict) for item in raw_items[:5]):
            raise RuntimeError("retriever returned an invalid item")
        raw_items = [dict(item) for item in raw_items[:5]]
        labeled_items = adapt_retrieved_items(raw_items)
        if any(
            not str(item.get(field) or "").strip()
            for item in labeled_items
            for field in ("id", "label", "text")
        ):
            raise RuntimeError("retriever returned incomplete evidence")
    except Exception:
        row = await _update_log(
            db_factory,
            log_id,
            outcome="retrieval_failed",
        )
        return {"entered": True, "status": "retrieval_failed", "log": row}

    items_json = json.dumps(
        labeled_items,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if not labeled_items:
        row = await _update_log(
            db_factory,
            log_id,
            retrieved_items_json=items_json,
            outcome="no_evidence",
        )
        return {"entered": True, "status": "no_evidence", "log": row}

    prompt_version = reflection_prompt_version(captured.identity_snapshot)
    messages = build_reflection_messages(
        identity_snapshot=captured.identity_snapshot,
        clue=clue,
        retrieved_items=labeled_items,
        working_model=str(captured.working_model_head.get("content") or ""),
    )
    await _update_log(
        db_factory,
        log_id,
        retrieved_items_json=items_json,
        reflection_model=captured.model_key,
        reflection_prompt_version=prompt_version,
    )
    try:
        if reflection_provider is None:
            raw_reflection = await call_core_chat_once(
                captured.model_key,
                messages,
                expect_json=True,
                timeout=REFLECTION_CORE_TIMEOUT_SEC,
                temperature=0.2,
                scope="working_model:reflection",
                max_tokens=700,
            )
        else:
            raw_reflection = await _await_result(reflection_provider(messages))
    except Exception:
        row = await _update_log(
            db_factory,
            log_id,
            outcome="reflection_provider_failed",
        )
        return {"entered": True, "status": "reflection_provider_failed", "log": row}

    if not isinstance(raw_reflection, str) or not raw_reflection.strip():
        row = await _update_log(
            db_factory,
            log_id,
            outcome="reflection_provider_failed",
        )
        return {"entered": True, "status": "reflection_provider_failed", "log": row}
    try:
        parsed = parse_reflection_output(raw_reflection)
    except ReflectionParseError:
        row = await _update_log(
            db_factory,
            log_id,
            outcome="reflection_parse_failed",
        )
        return {"entered": True, "status": "reflection_parse_failed", "log": row}

    row = await _update_log(
        db_factory,
        log_id,
        verdict=parsed["verdict"],
        reason=parsed["reason"],
        proposed_statement=parsed["proposed_statement"],
        outcome="ok",
    )
    if parsed["verdict"] != "conflicts":
        return {"entered": True, "status": "ok", "log": row}

    # The reflection outcome is already `ok`. From here on, gate/writer state
    # belongs to working_model_requests. Only creation/link failure may revise
    # the reflection outcome to handoff_failed.
    try:
        from app.working_model.runtime import (
            capture_reflection_working_model_pipeline_input,
            run_working_model_pipeline,
        )

        source = render_reflection_source(row)
        pipeline_input = capture_reflection_working_model_pipeline_input(
            conv_id=captured.target_conv_id,
            reflection_log_id=log_id,
            statement=parsed["proposed_statement"],
            source=source,
            model_key=captured.model_key,
            identity_snapshot=captured.identity_snapshot,
        )
        if pipeline_runner is None:
            pipeline = await run_working_model_pipeline(
                pipeline_input,
                db_factory=db_factory,
            )
        else:
            pipeline = await _await_result(pipeline_runner(pipeline_input))
    except Exception:
        logger.exception("reflection working-model pipeline failed log=%s", log_id)
        # `handoff_failed` is deliberately narrow: only request creation or
        # request-link failure belongs to the reflection state machine.  Once
        # the request id is linked, gate/writer failures belong exclusively to
        # working_model_requests and must not rewrite an already valid verdict.
        linked_log = row
        try:
            async with db_factory() as db:
                linked_log = await repository.get_log(db, log_id) or row
        except Exception:
            logger.exception("reflection link audit reload failed log=%s", log_id)
        if linked_log and linked_log.get("resulting_request_id"):
            return {
                "entered": True,
                "status": "ok",
                "log": linked_log,
                "pipeline": {"downstream_failed": True},
            }
        try:
            row = await _update_log(db_factory, log_id, outcome="handoff_failed")
        except Exception:
            logger.exception("reflection handoff outcome update failed log=%s", log_id)
        return {"entered": True, "status": "handoff_failed", "log": row}

    async with db_factory() as db:
        final_log = await repository.get_log(db, log_id)
    return {
        "entered": True,
        "status": "handoff_failed" if pipeline.get("handoff_failed") else "ok",
        "log": final_log or row,
        "pipeline": pipeline,
    }


__all__ = [
    "CapturedReflectionContext",
    "REFLECTION_FEATURE_FLAG",
    "REFLECTION_QUERY_SLOT",
    "capture_reflection_context",
    "reflection_feature_enabled",
    "run_reflection",
]
