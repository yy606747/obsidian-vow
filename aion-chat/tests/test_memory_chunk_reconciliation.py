import asyncio
from contextlib import asynccontextmanager
import json

import aiosqlite

import app.memory_v2.chunks as chunks
from app.memory_v3.schema import init_memory_v3_tables


async def _create_db(path) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute("""
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                conv_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE memory_chunks (
                id TEXT PRIMARY KEY,
                conv_id TEXT NOT NULL,
                message_ids_json TEXT NOT NULL DEFAULT '[]',
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                embedding BLOB,
                keywords_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
        """)
        await db.execute("""
            CREATE TABLE memory_items (
                id TEXT PRIMARY KEY,
                legacy_memory_id TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
        """)
        await init_memory_v3_tables(db)
        await db.commit()


def _get_db_factory(path):
    @asynccontextmanager
    async def factory():
        async with aiosqlite.connect(path) as db:
            yield db

    return factory


def test_split_parts_keep_only_the_messages_their_text_contains(monkeypatch):
    monkeypatch.setattr(
        chunks,
        "load_worldbook",
        lambda: {"user_name": "User", "ai_name": "AI"},
    )
    messages = [
        {
            "id": f"m{index}",
            "conv_id": "conv",
            "role": "user",
            "content": str(index) * 1100,
            "created_at": float(index),
        }
        for index in range(1, 4)
    ]

    built = chunks.build_chunks_from_messages(messages)

    assert len(built) == 2
    assert json.loads(built[0]["message_ids_json"]) == ["m1", "m2"]
    assert json.loads(built[1]["message_ids_json"]) == ["m3"]
    assert "1" * 100 in built[0]["content"]
    assert "3" * 100 not in built[0]["content"]
    assert "3" * 100 in built[1]["content"]
    assert built[0]["source_hash"] != built[1]["source_hash"]


def test_edit_updates_same_chunk_and_clears_stale_embedding(tmp_path, monkeypatch):
    db_path = tmp_path / "chunks.db"
    asyncio.run(_create_db(db_path))
    monkeypatch.setattr(chunks, "get_db", _get_db_factory(db_path))
    monkeypatch.setattr(
        chunks,
        "load_worldbook",
        lambda: {"user_name": "User", "ai_name": "AI"},
    )

    async def scenario():
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO messages VALUES ('m1','conv','user','旧内容',1)"
            )
            await db.commit()
        first = await chunks.ensure_conversation_chunks("conv", embed=False)
        async with aiosqlite.connect(db_path) as db:
            cur = await db.execute(
                "SELECT id, source_hash FROM memory_chunks WHERE status='active'"
            )
            chunk_id, old_hash = await cur.fetchone()
            await db.execute(
                "UPDATE memory_chunks SET embedding=? WHERE id=?",
                (b"old-vector", chunk_id),
            )
            await db.execute("UPDATE messages SET content='新内容' WHERE id='m1'")
            await db.commit()

        second = await chunks.ensure_conversation_chunks("conv", embed=False)
        async with aiosqlite.connect(db_path) as db:
            cur = await db.execute(
                "SELECT id, content, source_hash, embedding, status FROM memory_chunks"
            )
            row = await cur.fetchone()
        return first, second, chunk_id, old_hash, row

    first, second, chunk_id, old_hash, row = asyncio.run(scenario())

    assert first["inserted_chunks"] == 1
    assert second["updated_chunks"] == 1
    assert second["retired_chunks"] == 0
    assert row[0] == chunk_id
    assert "新内容" in row[1]
    assert row[2] != old_hash
    assert row[3] is None
    assert row[4] == "active"


def test_new_grouping_retires_old_chunk_and_invalidates_card(tmp_path, monkeypatch):
    db_path = tmp_path / "retire.db"
    asyncio.run(_create_db(db_path))
    monkeypatch.setattr(chunks, "get_db", _get_db_factory(db_path))
    monkeypatch.setattr(
        chunks,
        "load_worldbook",
        lambda: {"user_name": "User", "ai_name": "AI"},
    )

    async def scenario():
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO messages VALUES ('m1','conv','user','第一条',1)"
            )
            await db.commit()
        await chunks.ensure_conversation_chunks("conv", embed=False)
        async with aiosqlite.connect(db_path) as db:
            old_chunk_id = (
                await (
                    await db.execute(
                        "SELECT id FROM memory_chunks WHERE status='active'"
                    )
                ).fetchone()
            )[0]
            await db.execute(
                "INSERT INTO memory_relational_cards "
                "(id, source_chunk_id, version, content, source_hash, status, prompt_version, created_at, updated_at) "
                "SELECT 'card', id, 1, '摘要', source_hash, 'active', 'v1', 1, 1 "
                "FROM memory_chunks WHERE id=?",
                (old_chunk_id,),
            )
            await db.execute(
                "INSERT INTO messages VALUES ('m2','conv','assistant','第二条',2)"
            )
            await db.commit()

        result = await chunks.ensure_conversation_chunks("conv", embed=False)
        async with aiosqlite.connect(db_path) as db:
            rows = await (
                await db.execute(
                    "SELECT id, status FROM memory_chunks ORDER BY status, id"
                )
            ).fetchall()
            card_status = (
                await (
                    await db.execute(
                        "SELECT status FROM memory_relational_cards WHERE id='card'"
                    )
                ).fetchone()
            )[0]
        return result, old_chunk_id, rows, card_status

    result, old_chunk_id, rows, card_status = asyncio.run(scenario())

    assert result["inserted_chunks"] == 1
    assert result["retired_chunks"] == 1
    assert result["cards_invalidated"] == 1
    assert (old_chunk_id, "retired") in rows
    assert len([row for row in rows if row[1] == "active"]) == 1
    assert card_status == "invalid"


def test_read_only_preview_matches_delete_apply_and_does_not_mutate(tmp_path, monkeypatch):
    db_path = tmp_path / "preview-delete.db"
    asyncio.run(_create_db(db_path))
    monkeypatch.setattr(chunks, "get_db", _get_db_factory(db_path))
    monkeypatch.setattr(
        chunks,
        "load_worldbook",
        lambda: {"user_name": "User", "ai_name": "AI"},
    )

    async def scenario():
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO messages VALUES ('m1','conv','user','会被删除的内容',1)"
            )
            await db.commit()
        await chunks.ensure_conversation_chunks("conv", embed=False)
        async with aiosqlite.connect(db_path) as db:
            chunk_id, source_hash = await (
                await db.execute(
                    "SELECT id, source_hash FROM memory_chunks WHERE status='active'"
                )
            ).fetchone()
            await db.execute(
                "INSERT INTO memory_relational_cards "
                "(id, source_chunk_id, version, content, source_hash, status, "
                "prompt_version, created_at, updated_at) "
                "VALUES ('card',?,1,'摘要',?,'active','v1',1,1)",
                (chunk_id, source_hash),
            )
            await db.execute("DELETE FROM messages WHERE id='m1'")
            await db.commit()

        preview = await chunks.preview_conversation_chunk_reconciliation("conv")
        async with aiosqlite.connect(db_path) as db:
            before_apply = await (
                await db.execute(
                    "SELECT "
                    "(SELECT status FROM memory_chunks WHERE id=?), "
                    "(SELECT status FROM memory_relational_cards WHERE id='card')",
                    (chunk_id,),
                )
            ).fetchone()

        applied = await chunks.ensure_conversation_chunks("conv", embed=False)
        async with aiosqlite.connect(db_path) as db:
            after_apply = await (
                await db.execute(
                    "SELECT "
                    "(SELECT status FROM memory_chunks WHERE id=?), "
                    "(SELECT status FROM memory_relational_cards WHERE id='card')",
                    (chunk_id,),
                )
            ).fetchone()
        return preview, before_apply, applied, after_apply, chunk_id

    preview, before_apply, applied, after_apply, chunk_id = asyncio.run(scenario())

    assert preview["apply"] is False
    assert preview["would_retire"] == 1
    assert preview["would_invalidate_cards"] == 1
    assert preview["retire_ids"] == [chunk_id]
    assert before_apply == ("active", "active")
    assert applied["retired_chunks"] == preview["would_retire"]
    assert applied["cards_invalidated"] == preview["would_invalidate_cards"]
    assert after_apply == ("retired", "invalid")
