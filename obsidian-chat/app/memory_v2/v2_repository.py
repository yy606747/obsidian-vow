"""
Memory V2 SQLite repository.

Batch 2.1 只提供新表的薄仓储层，不改变旧 memories 读写行为。
"""

from __future__ import annotations

import json
import time
import uuid

import aiosqlite

from database import get_db


def new_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"


class MemoryRepository:
    ITEM_COLUMNS = [
        "id",
        "legacy_memory_id",
        "origin_type",
        "kind",
        "namespace",
        "content",
        "subject",
        "entities_json",
        "emotion",
        "importance",
        "confidence",
        "status",
        "visibility",
        "embedding",
        "keywords_json",
        "source_conv",
        "source_start_ts",
        "source_end_ts",
        "created_at",
        "updated_at",
        "last_seen_at",
        "last_used_at",
        "expires_at",
        "metadata_json",
    ]

    ITEM_DEFAULTS = {
        "legacy_memory_id": None,
        "origin_type": "manual",
        "kind": "episode",
        "namespace": "normal",
        "subject": "",
        "entities_json": "[]",
        "emotion": "",
        "importance": 0.5,
        "confidence": 0.7,
        "status": "active",
        "visibility": "prompt",
        "embedding": None,
        "keywords_json": "[]",
        "source_conv": None,
        "source_start_ts": None,
        "source_end_ts": None,
        "last_seen_at": None,
        "last_used_at": None,
        "expires_at": None,
        "metadata_json": "{}",
    }

    async def count_legacy_memories(self) -> int:
        async with get_db() as db:
            cur = await db.execute("SELECT COUNT(*) FROM memories")
            row = await cur.fetchone()
        return int(row[0] or 0)

    async def count_migrated_legacy_items(self) -> int:
        async with get_db() as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM memory_items WHERE legacy_memory_id IS NOT NULL"
            )
            row = await cur.fetchone()
        return int(row[0] or 0)

    async def fetch_migrated_legacy_ids(self) -> set[str]:
        async with get_db() as db:
            cur = await db.execute(
                "SELECT legacy_memory_id FROM memory_items WHERE legacy_memory_id IS NOT NULL"
            )
            rows = await cur.fetchall()
        return {row[0] for row in rows if row[0]}

    async def fetch_legacy_memory_ids(self) -> set[str]:
        async with get_db() as db:
            cur = await db.execute("SELECT id FROM memories")
            rows = await cur.fetchall()
        return {row[0] for row in rows if row[0]}

    async def get_legacy_memory(self, legacy_memory_id: str) -> dict | None:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, content, type, created_at, source_conv, embedding, keywords, "
                "importance, source_start_ts, source_end_ts, unresolved "
                "FROM memories WHERE id=?",
                (legacy_memory_id,),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def fetch_legacy_memories(self, limit: int | None = None) -> list[dict]:
        sql = (
            "SELECT id, content, type, created_at, source_conv, embedding, keywords, "
            "importance, source_start_ts, source_end_ts, unresolved "
            "FROM memories ORDER BY created_at ASC"
        )
        params = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(sql, params)
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def get_item_by_legacy_memory_id(self, legacy_memory_id: str) -> dict | None:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM memory_items WHERE legacy_memory_id=?",
                (legacy_memory_id,),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_items(
        self,
        *,
        namespace: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        clauses = []
        params = []
        if namespace:
            clauses.append("namespace=?")
            params.append(namespace)
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                f"SELECT * FROM memory_items {where} ORDER BY updated_at DESC LIMIT ?",
                params,
            )
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def fetch_items_for_recall(
        self,
        *,
        namespaces: list[str] | None = None,
        kinds: list[str] | None = None,
        status: str = "active",
        visibility: str = "prompt",
        limit: int = 500,
    ) -> list[dict]:
        clauses = ["status=?", "visibility=?"]
        params = [status, visibility]
        if namespaces:
            placeholders = ",".join("?" for _ in namespaces)
            clauses.append(f"namespace IN ({placeholders})")
            params.extend(namespaces)
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            clauses.append(f"kind IN ({placeholders})")
            params.extend(kinds)
        params.append(int(limit))
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM memory_items "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY COALESCE(source_end_ts, source_start_ts, created_at) DESC LIMIT ?",
                params,
            )
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def insert_memory_item(self, item: dict, *, ignore_existing: bool = True) -> bool:
        now = time.time()
        data = dict(self.ITEM_DEFAULTS)
        data.update(item)
        data["origin_type"] = self._origin_type(item)
        data.setdefault("id", new_id("memv2"))
        data.setdefault("created_at", now)
        data.setdefault("updated_at", data["created_at"])
        columns = self.ITEM_COLUMNS
        placeholders = ",".join("?" for _ in columns)
        verb = "INSERT OR IGNORE" if ignore_existing else "INSERT"
        async with get_db() as db:
            cur = await db.execute(
                f"{verb} INTO memory_items ({', '.join(columns)}) VALUES ({placeholders})",
                [data.get(col) for col in columns],
            )
            await db.commit()
            return cur.rowcount > 0

    @staticmethod
    def _origin_type(data: dict) -> str:
        explicit = str(data.get("origin_type") or "").strip()
        if explicit:
            return explicit
        try:
            metadata = json.loads(data.get("metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        metadata = metadata if isinstance(metadata, dict) else {}
        source = str(metadata.get("source") or "").strip()
        legacy_type = str(metadata.get("legacy_type") or "").strip()
        if source == "remember_cmd" or legacy_type == "ai_note":
            return "ai_note"
        if source == "digest.multi_note" or legacy_type in {"digest", "digest_note"}:
            return "auto_digest"
        return "legacy" if data.get("legacy_memory_id") else "manual"

    async def update_item_emotion(self, memory_id: str, emotion: str) -> bool:
        now = time.time()
        async with get_db() as db:
            cur = await db.execute(
                "UPDATE memory_items SET emotion=?, updated_at=? WHERE id=?",
                ((emotion or "").strip(), now, memory_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def insert_memory_link(
        self,
        memory_id: str,
        target_id: str,
        target_type: str,
        relation: str,
        *,
        created_at: float | None = None,
    ) -> bool:
        async with get_db() as db:
            cur = await db.execute(
                "INSERT OR IGNORE INTO memory_links "
                "(memory_id, target_id, target_type, relation, created_at) VALUES (?,?,?,?,?)",
                (memory_id, target_id, target_type, relation, created_at or time.time()),
            )
            await db.commit()
            return cur.rowcount > 0

    async def create_event(
        self,
        *,
        source: str,
        content: str,
        namespace: str = "normal",
        conv_id: str | None = None,
        role: str | None = None,
        metadata_json: str = "{}",
        created_at: float | None = None,
    ) -> dict:
        event = {
            "id": new_id("mev"),
            "source": source,
            "namespace": namespace,
            "conv_id": conv_id,
            "role": role,
            "content": content,
            "metadata_json": metadata_json,
            "created_at": created_at or time.time(),
        }
        async with get_db() as db:
            await db.execute(
                "INSERT INTO memory_events "
                "(id, source, namespace, conv_id, role, content, metadata_json, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    event["id"], event["source"], event["namespace"], event["conv_id"],
                    event["role"], event["content"], event["metadata_json"], event["created_at"],
                ),
            )
            await db.commit()
        return event

    async def record_usage(
        self,
        *,
        memory_id: str,
        conv_id: str | None = None,
        request_id: str | None = None,
        reason: str = "",
        score: float | None = None,
        rank: int | None = None,
        touch_last_used: bool = True,
    ) -> dict:
        usage = {
            "id": new_id("muse"),
            "memory_id": memory_id,
            "conv_id": conv_id,
            "request_id": request_id,
            "used_at": time.time(),
            "reason": reason,
            "score": score,
            "rank": rank,
        }
        async with get_db() as db:
            await db.execute(
                "INSERT INTO memory_usage "
                "(id, memory_id, conv_id, request_id, used_at, reason, score, rank) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    usage["id"], usage["memory_id"], usage["conv_id"], usage["request_id"],
                    usage["used_at"], usage["reason"], usage["score"], usage["rank"],
                ),
            )
            if touch_last_used:
                await db.execute(
                    "UPDATE memory_items SET last_used_at=? WHERE id=?",
                    (usage["used_at"], memory_id),
                )
            await db.commit()
        return usage
