import argparse
import asyncio
import sqlite3

import pytest

from app.memory_v2 import embedding
from scripts import reembed_memory


def _create_business_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE memory_chunks (
            id TEXT PRIMARY KEY, content TEXT NOT NULL,
            status TEXT NOT NULL, embedding BLOB
        );
        CREATE TABLE memory_items (
            id TEXT PRIMARY KEY, content TEXT NOT NULL,
            status TEXT NOT NULL, visibility TEXT NOT NULL, embedding BLOB
        );
        CREATE TABLE memories (
            id TEXT PRIMARY KEY, content TEXT NOT NULL, embedding BLOB
        );
        INSERT INTO memory_chunks VALUES ('chunk','chunk text','active',NULL);
        INSERT INTO memory_items VALUES ('item','item text','active','prompt',NULL);
        INSERT INTO memories VALUES ('legacy','legacy text',NULL);
        """
    )
    conn.commit()
    conn.close()


def _args(tmp_path, command, *, yes=False):
    return argparse.Namespace(
        command=command,
        run_dir=str(tmp_path / "run"),
        provider="gemini",
        model="gemini-embedding-2",
        dimensions=128,
        batch_size=2,
        request_interval_sec=0.0,
        timeout_sec=10.0,
        max_retries=2,
        initial_backoff_sec=1.0,
        max_backoff_sec=10.0,
        jitter_ratio=0.0,
        max_active_chunks=5000,
        usd_per_million_tokens=0.20,
        yes=yes,
    )


def test_stage_is_resumable_and_apply_is_atomic(tmp_path, monkeypatch):
    db_path = tmp_path / "business.db"
    _create_business_db(db_path)
    monkeypatch.setattr(reembed_memory, "DB_PATH", db_path)

    calls = []

    async def fake_embed(texts, *, purpose, config):
        calls.extend(texts)
        return embedding.EmbeddingBatchResult(
            vectors=[[float(index + 1)] * 128 for index, _ in enumerate(texts)],
            prompt_tokens=len(texts) * 10,
            requests=1,
        )

    monkeypatch.setattr(embedding, "embed_texts_detailed", fake_embed)
    args = _args(tmp_path, "stage")

    first = asyncio.run(reembed_memory._stage(args))
    second = asyncio.run(reembed_memory._stage(args))

    assert first["ok"] is True
    assert first["stats"]["stage_rows_after"] == 3
    assert second["stats"]["pending_at_start"] == 0
    assert len(calls) == 3

    applied = reembed_memory._apply(_args(tmp_path, "apply", yes=True))
    verified = reembed_memory._verify(_args(tmp_path, "verify"))

    assert applied["updated"] == {
        "memory_chunks": 1,
        "memory_items": 1,
        "memories": 1,
    }
    assert verified["ok"] is True
    assert all(item["missing"] == 0 for item in verified["tables"].values())


def test_apply_refuses_content_drift_without_partial_updates(tmp_path, monkeypatch):
    db_path = tmp_path / "business.db"
    _create_business_db(db_path)
    monkeypatch.setattr(reembed_memory, "DB_PATH", db_path)

    async def fake_embed(texts, *, purpose, config):
        return embedding.EmbeddingBatchResult(vectors=[[0.1] * 128 for _ in texts])

    monkeypatch.setattr(embedding, "embed_texts_detailed", fake_embed)
    asyncio.run(reembed_memory._stage(_args(tmp_path, "stage")))

    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE memory_items SET content='changed after stage' WHERE id='item'")
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="target set drifted|content drift"):
        reembed_memory._apply(_args(tmp_path, "apply", yes=True))

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT embedding FROM memory_chunks UNION ALL "
        "SELECT embedding FROM memory_items UNION ALL SELECT embedding FROM memories"
    ).fetchall()
    conn.close()
    assert rows == [(None,), (None,), (None,)]


def test_stage_prunes_rows_that_left_the_business_target_set(tmp_path, monkeypatch):
    db_path = tmp_path / "business.db"
    _create_business_db(db_path)
    monkeypatch.setattr(reembed_memory, "DB_PATH", db_path)

    calls = []

    async def fake_embed(texts, *, purpose, config):
        calls.extend(texts)
        return embedding.EmbeddingBatchResult(vectors=[[0.2] * 128 for _ in texts])

    monkeypatch.setattr(embedding, "embed_texts_detailed", fake_embed)
    args = _args(tmp_path, "stage")
    first = asyncio.run(reembed_memory._stage(args))

    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM memory_chunks WHERE id='chunk'")
    conn.commit()
    conn.close()

    second = asyncio.run(reembed_memory._stage(args))

    assert first["stats"]["stage_rows_after"] == 3
    assert second["ok"] is True
    assert second["stats"]["pruned_stale"] == 1
    assert second["stats"]["pending_at_start"] == 0
    assert second["stats"]["stage_rows_after"] == 2
    assert len(calls) == 3
