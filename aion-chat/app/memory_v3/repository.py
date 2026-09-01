"""Thin repositories for Memory V3 persistent records."""

from __future__ import annotations

import json
import time
import uuid

import aiosqlite

from database import get_db
from .provenance import source_hash_for_messages


def new_id(prefix: str) -> str:
    return f"{prefix}_{time.time_ns()}_{uuid.uuid4().hex[:8]}"


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_list(value: object) -> list:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _json_dict(value: object) -> dict:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class RelationalCardRepository:
    async def active_for_chunk(self, source_chunk_id: str) -> dict | None:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM memory_relational_cards "
                "WHERE source_chunk_id=? AND status='active'",
                (source_chunk_id,),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def active_for_chunks(self, source_chunk_ids: list[str]) -> dict[str, dict]:
        if not source_chunk_ids:
            return {}
        result: dict[str, dict] = {}
        for start in range(0, len(source_chunk_ids), 500):
            batch = source_chunk_ids[start: start + 500]
            placeholders = ",".join("?" for _ in batch)
            async with get_db() as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT * FROM memory_relational_cards "
                    f"WHERE status='active' AND source_chunk_id IN ({placeholders})",
                    batch,
                )
                rows = await cur.fetchall()
            result.update({row["source_chunk_id"]: dict(row) for row in rows})
        return result

    async def append_active(
        self,
        *,
        source_chunk_id: str,
        content: str,
        source_message_ids: list[str],
        evidence: list[dict],
        source_hash: str,
        prompt_version: str,
        generator_model: str | None = None,
        metadata: dict | None = None,
        expected_chunk_hash: str | None = None,
    ) -> dict:
        now = time.time()
        card_id = new_id("relcard")
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(
                    "SELECT message_ids_json, source_hash, status "
                    "FROM memory_chunks WHERE id=?",
                    (source_chunk_id,),
                )
                chunk = await cur.fetchone()
                if chunk is None or chunk["status"] not in {"active", "cold"}:
                    raise ValueError("source chunk is not recallable")
                chunk_hash = str(chunk["source_hash"] or "")
                if expected_chunk_hash is not None and chunk_hash != expected_chunk_hash:
                    raise ValueError("source chunk changed before card commit")
                chunk_message_ids = {
                    str(value) for value in _json_list(chunk["message_ids_json"]) if str(value)
                }
                declared_ids = list(dict.fromkeys(str(value) for value in source_message_ids))
                if not declared_ids or any(value not in chunk_message_ids for value in declared_ids):
                    raise ValueError("card sources are not contained by the source chunk")
                placeholders = ",".join("?" for _ in declared_ids)
                cur = await db.execute(
                    "SELECT id, role, content, created_at FROM messages "
                    f"WHERE id IN ({placeholders})",
                    declared_ids,
                )
                source_rows = [dict(row) for row in await cur.fetchall()]
                if len(source_rows) != len(declared_ids):
                    raise ValueError("one or more card source messages no longer exist")
                current_source_hash = source_hash_for_messages(source_rows)
                if current_source_hash != str(source_hash or ""):
                    raise ValueError("card source messages changed before commit")
                cur = await db.execute(
                    "SELECT id, version FROM memory_relational_cards "
                    "WHERE source_chunk_id=? AND status='active'",
                    (source_chunk_id,),
                )
                previous = await cur.fetchone()
                cur = await db.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM memory_relational_cards "
                    "WHERE source_chunk_id=?",
                    (source_chunk_id,),
                )
                version = int((await cur.fetchone())[0] or 0) + 1
                previous_id = str(previous["id"]) if previous else None
                if previous_id:
                    await db.execute(
                        "UPDATE memory_relational_cards "
                        "SET status='superseded', updated_at=? WHERE id=? AND status='active'",
                        (now, previous_id),
                    )
                await db.execute(
                    "INSERT INTO memory_relational_cards "
                    "(id, source_chunk_id, version, content, source_message_ids_json, "
                    "evidence_json, source_hash, status, supersedes_card_id, generator_model, "
                    "prompt_version, metadata_json, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,'active',?,?,?,?,?,?)",
                    (
                        card_id,
                        source_chunk_id,
                        version,
                        content,
                        _json(source_message_ids),
                        _json(evidence),
                        source_hash,
                        previous_id,
                        generator_model,
                        prompt_version,
                        _json(metadata or {}),
                        now,
                        now,
                    ),
                )
                await db.execute(
                    "UPDATE memory_chunks SET card_generation_hash=?, "
                    "card_generation_status='created', card_generation_reason='', "
                    "card_generation_prompt_version=?, card_generation_attempted_at=? "
                    "WHERE id=?",
                    (chunk_hash, prompt_version, now, source_chunk_id),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return {
            "id": card_id,
            "source_chunk_id": source_chunk_id,
            "version": version,
            "content": content,
            "source_hash": source_hash,
            "status": "active",
            "supersedes_card_id": previous_id,
            "created_at": now,
        }

    async def mark_generation_outcome(
        self,
        *,
        source_chunk_id: str,
        expected_chunk_hash: str,
        outcome: str,
        prompt_version: str,
        reason: str = "",
    ) -> bool:
        if outcome not in {"abstained", "invalid", "provider_failed"}:
            raise ValueError(
                "generation outcome must be abstained, invalid, or provider_failed"
            )
        now = time.time()
        async with get_db() as db:
            cur = await db.execute(
                "UPDATE memory_chunks SET card_generation_hash=source_hash, "
                "card_generation_status=?, card_generation_reason=?, "
                "card_generation_prompt_version=?, card_generation_attempted_at=? "
                "WHERE id=? AND status IN ('active','cold') AND source_hash=?",
                (
                    outcome,
                    str(reason or "")[:200],
                    prompt_version,
                    now,
                    source_chunk_id,
                    expected_chunk_hash,
                ),
            )
            await db.commit()
        return int(cur.rowcount or 0) > 0

    async def invalidate_active(
        self,
        card_id: str,
        *,
        reason: str,
        actor: str = "owner_api",
    ) -> dict:
        """Invalidate one active card without deleting its audit/version history.

        The source chunk is also marked as explicitly resolved-invalid so the
        normal background writer cannot immediately recreate the same rejected
        interpretation. Historical regeneration remains an explicit backfill
        operation.
        """

        normalized_id = str(card_id or "").strip()
        normalized_reason = str(reason or "").strip()
        normalized_actor = str(actor or "owner_api").strip() or "owner_api"
        if not normalized_id:
            raise ValueError("card_id is required")
        if not normalized_reason:
            raise ValueError("invalidation reason is required")

        now = time.time()
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(
                    "SELECT id, source_chunk_id, status, prompt_version, metadata_json, "
                    "updated_at FROM memory_relational_cards WHERE id=?",
                    (normalized_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise KeyError(normalized_id)
                if row["status"] == "invalid":
                    await db.rollback()
                    return {
                        "id": normalized_id,
                        "source_chunk_id": str(row["source_chunk_id"]),
                        "status": "invalid",
                        "invalidated": False,
                        "already_invalid": True,
                        "updated_at": float(row["updated_at"]),
                    }
                if row["status"] != "active":
                    raise ValueError(
                        f"only an active card can be invalidated; status={row['status']}"
                    )

                metadata = _json_dict(row["metadata_json"])
                metadata["invalidation"] = {
                    "actor": normalized_actor[:80],
                    "reason": normalized_reason[:500],
                    "invalidated_at": now,
                }
                cur = await db.execute(
                    "UPDATE memory_relational_cards SET status='invalid', "
                    "metadata_json=?, updated_at=? WHERE id=? AND status='active'",
                    (_json(metadata), now, normalized_id),
                )
                if int(cur.rowcount or 0) != 1:
                    raise RuntimeError("active card changed during invalidation")

                marker_reason = f"owner_invalidated:{normalized_id}:{normalized_reason}"[:200]
                await db.execute(
                    "UPDATE memory_chunks SET card_generation_hash=source_hash, "
                    "card_generation_status='invalid', card_generation_reason=?, "
                    "card_generation_prompt_version=?, card_generation_attempted_at=? "
                    "WHERE id=? AND status IN ('active','cold')",
                    (
                        marker_reason,
                        str(row["prompt_version"] or ""),
                        now,
                        str(row["source_chunk_id"]),
                    ),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

        return {
            "id": normalized_id,
            "source_chunk_id": str(row["source_chunk_id"]),
            "status": "invalid",
            "invalidated": True,
            "already_invalid": False,
            "reason": normalized_reason[:500],
            "updated_at": now,
        }


class PendingRecallRepository:
    ACTIVE_STATUSES = ("queued", "ready", "selected")

    async def get(self, pending_id: str) -> dict | None:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM memory_pending_recalls WHERE id=?",
                (pending_id,),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def active_for_conversation(self, conv_id: str) -> dict | None:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM memory_pending_recalls "
                "WHERE conv_id=? AND status IN ('queued','ready','selected')",
                (conv_id,),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def mark_retrieval_started(self, pending_id: str, *, now: float) -> bool:
        async with get_db() as db:
            cur = await db.execute(
                "UPDATE memory_pending_recalls "
                "SET retrieval_started_at=COALESCE(retrieval_started_at, ?), "
                "attempt_count=attempt_count+1, updated_at=? "
                "WHERE id=? AND status='queued'",
                (now, now, pending_id),
            )
            await db.commit()
        return int(cur.rowcount or 0) > 0

    async def mark_ready(
        self,
        pending_id: str,
        *,
        candidates: list[dict],
        now: float,
    ) -> bool:
        async with get_db() as db:
            cur = await db.execute(
                "UPDATE memory_pending_recalls "
                "SET status='ready', candidate_json=?, retrieval_completed_at=?, "
                "failure_reason=NULL, updated_at=? "
                "WHERE id=? AND status='queued'",
                (_json(candidates), now, now, pending_id),
            )
            await db.commit()
        return int(cur.rowcount or 0) > 0

    async def mark_failed(self, pending_id: str, *, reason: str, now: float) -> bool:
        async with get_db() as db:
            cur = await db.execute(
                "UPDATE memory_pending_recalls "
                "SET status='failed', candidate_json='[]', selected_json='[]', "
                "failure_reason=?, updated_at=? "
                "WHERE id=? AND status IN ('queued','ready','selected')",
                (str(reason or "failed")[:200], now, pending_id),
            )
            await db.commit()
        return int(cur.rowcount or 0) > 0

    async def record_deferred(
        self,
        pending_id: str,
        *,
        user_message_id: str,
        now: float,
    ) -> bool:
        async with get_db() as db:
            cur = await db.execute(
                "UPDATE memory_pending_recalls "
                "SET deferred_count=deferred_count+1, last_deferred_user_message_id=?, "
                "updated_at=? WHERE id=? AND status='queued'",
                (user_message_id, now, pending_id),
            )
            await db.commit()
        return int(cur.rowcount or 0) > 0

    async def bind_target(
        self,
        pending_id: str,
        *,
        user_message_id: str,
        now: float,
    ) -> dict | None:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                "UPDATE memory_pending_recalls SET target_user_message_id=?, updated_at=? "
                "WHERE id=? AND status='ready' AND target_user_message_id IS NULL",
                (user_message_id, now, pending_id),
            )
            await db.commit()
            cur = await db.execute(
                "SELECT * FROM memory_pending_recalls WHERE id=?",
                (pending_id,),
            )
            row = await cur.fetchone()
        if row is None or row["status"] not in {"ready", "selected"}:
            return None
        if row["target_user_message_id"] != user_message_id:
            return None
        return dict(row)

    async def save_selection(
        self,
        pending_id: str,
        *,
        user_message_id: str,
        selection: dict,
        now: float,
    ) -> dict | None:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                "UPDATE memory_pending_recalls "
                "SET status='selected', candidate_json='[]', selected_json=?, "
                "selected_at=?, updated_at=? "
                "WHERE id=? AND status='ready' AND target_user_message_id=?",
                (_json(selection), now, now, pending_id, user_message_id),
            )
            await db.commit()
            cur = await db.execute(
                "SELECT * FROM memory_pending_recalls WHERE id=?",
                (pending_id,),
            )
            row = await cur.fetchone()
        if row is None or row["status"] != "selected":
            return None
        if row["target_user_message_id"] != user_message_id:
            return None
        return dict(row)

    async def selected_for_consumed_assistant(
        self,
        assistant_message_id: str,
    ) -> dict | None:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM memory_pending_recalls "
                "WHERE consumed_by_assistant_message_id=? AND status='consumed' "
                "ORDER BY consumed_at DESC LIMIT 1",
                (assistant_message_id,),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    @staticmethod
    async def consume_selected_in_tx(
        db,
        *,
        pending_id: str,
        target_user_message_id: str,
        assistant_message_id: str,
        now: float,
    ) -> bool:
        cur = await db.execute(
            "UPDATE memory_pending_recalls "
            "SET status='consumed', candidate_json='[]', "
            "consumed_by_assistant_message_id=?, consumed_at=?, updated_at=? "
            "WHERE id=? AND status='selected' AND target_user_message_id=?",
            (
                assistant_message_id,
                now,
                now,
                pending_id,
                target_user_message_id,
            ),
        )
        return int(cur.rowcount or 0) > 0

    @staticmethod
    async def create_queued_in_tx(
        db,
        *,
        conv_id: str,
        origin_assistant_message_id: str,
        intent_text: str,
        retrieval_deadline_at: float,
        config: dict,
        created_at: float,
    ) -> str:
        pending_id = new_id("pending_recall")
        await db.execute(
            "UPDATE memory_pending_recalls "
            "SET status='superseded', candidate_json='[]', selected_json='[]', updated_at=? "
            "WHERE conv_id=? AND status IN ('queued','ready','selected')",
            (created_at, conv_id),
        )
        await db.execute(
            "INSERT INTO memory_pending_recalls "
            "(id, conv_id, origin_assistant_message_id, intent_text, status, config_json, "
            "created_at, retrieval_deadline_at, updated_at) "
            "VALUES (?,?,?,?,'queued',?,?,?,?)",
            (
                pending_id,
                conv_id,
                origin_assistant_message_id,
                intent_text,
                _json(config),
                created_at,
                retrieval_deadline_at,
                created_at,
            ),
        )
        return pending_id

    @staticmethod
    async def cancel_for_origin_in_tx(
        db,
        *,
        origin_assistant_message_id: str,
        now: float,
    ) -> int:
        try:
            cur = await db.execute(
                "UPDATE memory_pending_recalls "
                "SET status='cancelled', candidate_json='[]', selected_json='[]', "
                "updated_at=? "
                "WHERE origin_assistant_message_id=? "
                "AND status IN ('queued','ready','selected')",
                (now, origin_assistant_message_id),
            )
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return 0
            raise
        return max(int(cur.rowcount or 0), 0)

    @staticmethod
    async def cancel_for_message_in_tx(
        db,
        *,
        message_id: str,
        now: float,
    ) -> int:
        try:
            cur = await db.execute(
                "UPDATE memory_pending_recalls "
                "SET status='cancelled', candidate_json='[]', selected_json='[]', "
                "updated_at=? "
                "WHERE (origin_assistant_message_id=? OR target_user_message_id=?) "
                "AND status IN ('queued','ready','selected')",
                (now, message_id, message_id),
            )
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return 0
            raise
        return max(int(cur.rowcount or 0), 0)

    @staticmethod
    async def delete_for_conversation_in_tx(db, *, conv_id: str) -> int:
        try:
            cur = await db.execute(
                "DELETE FROM memory_pending_recalls WHERE conv_id=?",
                (conv_id,),
            )
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return 0
            raise
        return max(int(cur.rowcount or 0), 0)


class TimelineRepository:
    async def active(self) -> dict | None:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM memory_timeline_versions WHERE status='active' "
                "ORDER BY version DESC LIMIT 1"
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def latest_attempt(self) -> dict | None:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM memory_timeline_versions "
                "ORDER BY version DESC LIMIT 1"
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def append_active(
        self,
        *,
        window_start_ts: float,
        window_end_ts: float,
        entries: list[dict],
        source_message_ids: list[str],
        source_hash: str,
        prompt_version: str,
        generator_model: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        now = time.time()
        timeline_id = new_id("timeline")
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(
                    "SELECT * FROM memory_timeline_versions WHERE status='active' "
                    "ORDER BY version DESC LIMIT 1"
                )
                previous = await cur.fetchone()
                if (
                    previous
                    and str(previous["source_hash"] or "") == str(source_hash or "")
                    and str(previous["prompt_version"] or "") == str(prompt_version or "")
                ):
                    await db.rollback()
                    return {
                        "id": previous["id"],
                        "version": int(previous["version"]),
                        "status": "active",
                        "created": False,
                        "created_at": float(previous["created_at"]),
                    }
                cur = await db.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM memory_timeline_versions"
                )
                version = int((await cur.fetchone())[0] or 0) + 1
                if previous:
                    await db.execute(
                        "UPDATE memory_timeline_versions "
                        "SET status='superseded', updated_at=? WHERE id=? AND status='active'",
                        (now, previous["id"]),
                    )
                await db.execute(
                    "INSERT INTO memory_timeline_versions "
                    "(id, version, window_start_ts, window_end_ts, entries_json, "
                    "source_message_ids_json, source_hash, status, generator_model, "
                    "prompt_version, metadata_json, failure_reason, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,'active',?,?,?,'',?,?)",
                    (
                        timeline_id,
                        version,
                        float(window_start_ts),
                        float(window_end_ts),
                        _json(entries),
                        _json(source_message_ids),
                        source_hash,
                        generator_model,
                        prompt_version,
                        _json(metadata or {}),
                        now,
                        now,
                    ),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return {
            "id": timeline_id,
            "version": version,
            "status": "active",
            "created": True,
            "created_at": now,
        }

    async def append_invalid(
        self,
        *,
        window_start_ts: float,
        window_end_ts: float,
        source_message_ids: list[str],
        source_hash: str,
        prompt_version: str,
        failure_reason: str,
        generator_model: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        now = time.time()
        timeline_id = new_id("timeline")
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM memory_timeline_versions"
                )
                version = int((await cur.fetchone())[0] or 0) + 1
                await db.execute(
                    "INSERT INTO memory_timeline_versions "
                    "(id, version, window_start_ts, window_end_ts, entries_json, "
                    "source_message_ids_json, source_hash, status, generator_model, "
                    "prompt_version, metadata_json, failure_reason, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,'invalid',?,?,?,?,?,?)",
                    (
                        timeline_id,
                        version,
                        float(window_start_ts),
                        float(window_end_ts),
                        "[]",
                        _json(source_message_ids),
                        source_hash,
                        generator_model,
                        prompt_version,
                        _json(metadata or {}),
                        str(failure_reason or "invalid")[:200],
                        now,
                        now,
                    ),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return {
            "id": timeline_id,
            "version": version,
            "status": "invalid",
            "created_at": now,
        }


class InjectionEventRepository:
    _FIELDS = (
        "request_id",
        "conv_id",
        "user_message_id",
        "assistant_message_id",
        "route",
        "candidate_id",
        "source_chunk_id",
        "memory_item_id",
        "card_id",
        "card_version",
        "score",
        "rank",
        "cooldown_penalty",
        "outcome",
        "reason",
        "rendered_chars",
        "selector_model",
        "selector_input_tokens",
        "selector_output_tokens",
        "selector_latency_ms",
        "response_overlap_proxy",
        "past_reference_proxy",
        "metadata_json",
        "created_at",
    )

    async def record(self, event: dict) -> str:
        data = dict(event)
        data.setdefault("reason", "")
        data.setdefault("rendered_chars", 0)
        data["metadata_json"] = _json(data.pop("metadata", {}))
        data.setdefault("created_at", time.time())
        event_id = str(data.pop("id", "") or new_id("memory_injection"))
        required = ("conv_id", "route", "outcome")
        if any(not str(data.get(field) or "").strip() for field in required):
            raise ValueError("injection event requires conv_id, route, and outcome")
        columns = ("id", *self._FIELDS)
        placeholders = ",".join("?" for _ in columns)
        async with get_db() as db:
            await db.execute(
                f"INSERT INTO memory_injection_events ({','.join(columns)}) "
                f"VALUES ({placeholders})",
                [event_id, *(data.get(field) for field in self._FIELDS)],
            )
            await db.commit()
        return event_id
