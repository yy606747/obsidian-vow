import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest


_spec = importlib.util.spec_from_file_location(
    "engineering_baseline", Path(__file__).resolve().parents[2] / "scripts" / "engineering_baseline.py",
)
baseline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(baseline)


def test_source_snapshot_includes_untracked_code_but_not_data_keys_or_builds(tmp_path):
    root = tmp_path / "repo"
    for name in (
        "aion-chat/config.py", "aion-chat/app/new_feature.py", "aion-chat/data/chat.db",
        "aion-chat/.env", "aion-chat/wheels/unused.whl", "AionApp/app/build/generated.txt",
        "pc_agent/agent.py", "scripts/check_backend.py", ".env", ".env.example",
        "cloudflare-worker/gemini-proxy.js", "README.en.md", ".gitattributes", ".python-version",
        "experiments/working_model_gate_spike/cases_v1.json",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("sample", encoding="utf-8")
    bundle = tmp_path / "snapshot"
    baseline.snapshot(root, bundle)
    manifest = baseline.verify(bundle)
    assert set(manifest["files"]) == {
        "source/aion-chat/config.py", "source/aion-chat/app/new_feature.py",
        "source/pc_agent/agent.py", "source/scripts/check_backend.py", "source/.env.example",
        "source/cloudflare-worker/gemini-proxy.js", "source/README.en.md",
        "source/.gitattributes", "source/.python-version",
        "source/experiments/working_model_gate_spike/cases_v1.json",
    }
    baseline.restore(bundle, tmp_path / "restored")
    assert (tmp_path / "restored/aion-chat/app/new_feature.py").read_text() == "sample"


def test_data_backup_restores_live_wal_databases_uploads_and_configuration(tmp_path):
    data = tmp_path / "running"
    (data / "uploads").mkdir(parents=True)
    (data / "uploads/photo.jpg").write_bytes(b"test-image")
    (data / "settings.json").write_text('{"endpoints": []}', encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("AION_AUTH_TOKEN=test-only", encoding="utf-8")
    connections = []
    try:
        for name in ("chat.db", "signal_daily.db", "context_delivery.db"):
            db = sqlite3.connect(data / name)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, attachment TEXT)")
            db.execute("INSERT INTO sample VALUES (1, 'photo.jpg')")
            db.commit()
            connections.append(db)
        bundle = tmp_path / "backup"
        manifest = baseline.backup_data(data, bundle, [env_file])
        assert len(manifest["databases"]) == 3
        assert not any(name.endswith(("-wal", "-shm")) for name in manifest["files"])
        target = tmp_path / "restored"
        baseline.restore(bundle, target)
        for name in manifest["databases"]:
            with sqlite3.connect(target / name) as db:
                attachment = db.execute("SELECT attachment FROM sample").fetchone()[0]
                assert (target / "data/uploads" / attachment).read_bytes() == b"test-image"
        assert (target / "env/0-.env").read_text() == "AION_AUTH_TOKEN=test-only"
        assert (target / "data/settings.json").stat().st_mode & 0o777 == 0o600
        assert (target / "data/settings.json").read_text() == '{"endpoints": []}'
        baseline.check_restored_startup(target / "data")
    finally:
        for db in connections:
            db.close()


def test_restore_refuses_existing_target_or_corrupted_backup(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "settings.json").write_text("{}")
    bundle = tmp_path / "backup"
    baseline.backup_data(data, bundle, [])
    with pytest.raises(ValueError, match="拒绝覆盖"):
        baseline.restore(bundle, data)
    (bundle / "data/settings.json").write_text("bad")
    with pytest.raises(ValueError, match="校验失败"):
        baseline.restore(bundle, tmp_path / "unused")
    assert not (tmp_path / "unused").exists()


def test_restore_refuses_manifest_path_escape(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(json.dumps({
        "format": 1, "kind": "data", "files": {"../../outside": {"size": 0, "sha256": ""}},
    }))
    with pytest.raises(ValueError, match="不安全"):
        baseline.restore(bundle, tmp_path / "unused")
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize("structured", [False, True])
def test_online_backup_includes_upload_added_before_database_snapshot(tmp_path, monkeypatch, structured):
    data = tmp_path / "running"
    (data / "uploads").mkdir(parents=True)
    with sqlite3.connect(data / "chat.db") as db:
        db.execute("CREATE TABLE messages (id TEXT, attachments TEXT)")
    backup_sqlite = baseline.backup_sqlite

    def upload_during_backup(source, target):
        (data / "uploads/late.png").write_bytes(b"late-original")
        attachment = {"url": "/uploads/late.png", "mime_type": "image/png"} if structured else "/uploads/late.png"
        with sqlite3.connect(source) as db:
            db.execute("INSERT INTO messages VALUES ('late', ?)", (json.dumps([attachment]),))
        backup_sqlite(source, target)

    monkeypatch.setattr(baseline, "backup_sqlite", upload_during_backup)
    bundle = tmp_path / "backup"
    baseline.backup_data(data, bundle, [])
    manifest = baseline.verify(bundle)
    target = tmp_path / "restored"
    baseline.restore(bundle, target)
    with sqlite3.connect(target / "data/chat.db") as db:
        assert db.execute("SELECT id FROM messages").fetchall() == [("late",)]
    assert "data/uploads/late.png" in manifest["files"]
    assert (target / "data/uploads/late.png").read_bytes() == b"late-original"


def test_backup_fails_if_snapshot_references_missing_upload(tmp_path):
    data = tmp_path / "running"
    data.mkdir()
    with sqlite3.connect(data / "chat.db") as db:
        db.execute("CREATE TABLE messages (attachments TEXT)")
        db.execute("INSERT INTO messages VALUES (?)", (json.dumps(["/uploads/missing.png"]),))
    bundle = tmp_path / "backup"
    with pytest.raises(ValueError, match="附件"):
        baseline.backup_data(data, bundle, [])
    assert not (bundle / "manifest.json").exists()


def test_verify_and_restore_reject_incomplete_upload_manifest(tmp_path):
    data = tmp_path / "running"
    (data / "uploads").mkdir(parents=True)
    (data / "uploads/photo.png").write_bytes(b"original")
    with sqlite3.connect(data / "chat.db") as db:
        db.execute("CREATE TABLE messages (attachments TEXT)")
        db.execute("INSERT INTO messages VALUES (?)", (json.dumps(["/uploads/photo.png"]),))
    bundle = tmp_path / "backup"
    manifest = baseline.backup_data(data, bundle, [])
    # 模拟旧版备份：数据库与自身校验和正确，但清单漏了被引用附件。
    manifest["files"].pop("data/uploads/photo.png")
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="附件"):
        baseline.verify(bundle)
    with pytest.raises(ValueError, match="附件"):
        baseline.restore(bundle, tmp_path / "unused")
    assert not (tmp_path / "unused").exists()
