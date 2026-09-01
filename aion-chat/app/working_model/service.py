"""Working-model compatibility IO and CP0 root migration."""

from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path

import aiosqlite

from app.desire.schema import init_desire_tables
from app.desire.service import ensure_desire_root_in_tx

from . import repository
from .prompt import WORKING_MODEL_ACTIVE_MAX_CHARS, clip_legacy_prompt_content
from .schema import init_working_model_tables

WORKING_MODEL_ROOT_ID = "working_model_root"
LEGACY_WRITER_MODEL = "unknown"
LEGACY_PROMPT_VERSION = "legacy"
LEGACY_CLIP_ALGORITHM = "strip; text[:max_chars-3].rstrip() + '...'"


class WorkingModelMigrationError(RuntimeError):
    """Existing V2 roots disagree with the immutable migration contract."""


class WorkingModelActivationError(RuntimeError):
    """The durable V2 state is not safe to activate yet."""


CP4_NATURAL_TRIGGER_NAME = "wm_cp4_natural_request_cap"


def load_legacy_working_model(path: str | Path) -> dict:
    source = Path(path)
    if source.exists():
        try:
            # Preserve the old config.py behavior exactly: valid JSON is
            # returned as-is, even if a corrupt operator-created file contains
            # a non-object root.  The subsequent legacy save then fails in the
            # same place it did before instead of silently changing semantics.
            return json.loads(source.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"content": "", "updated_at": 0}


def save_legacy_working_model(
    path: str | Path,
    content: str,
    *,
    source_conv: str = "",
    source_msg_id: str = "",
    now: float | None = None,
) -> dict:
    source = Path(path)
    previous = load_legacy_working_model(source)
    now_ts = time.time() if now is None else now
    data = {
        "content": content,
        "updated_at": now_ts,
        "source_conv": source_conv,
        "source_msg_id": source_msg_id,
        "version": int(previous.get("version") or 0) + 1,
        "previous_content": previous.get("content", ""),
        "previous_updated_at": previous.get("updated_at", 0),
    }
    source.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return data


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _inspect_legacy_source(path: Path) -> tuple[dict, bool | None, str | None]:
    if not path.exists():
        return {"content": "", "updated_at": 0}, None, None
    raw_bytes = path.read_bytes()
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("legacy root is not a JSON object")
        parse_ok = True
    except Exception:
        data = {"content": "", "updated_at": 0}
        parse_ok = False
    return data, parse_ok, hashlib.sha256(raw_bytes).hexdigest()


def _root_created_at(data: dict, fallback: float) -> float:
    try:
        value = float(data.get("updated_at") or fallback)
    except (TypeError, ValueError):
        return fallback
    return value if math.isfinite(value) else fallback


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _validate_existing_root(existing: dict) -> None:
    expected = {
        "previous_version_id": None,
        "origin_request_id": None,
        "reason": "",
        "writer_model": LEGACY_WRITER_MODEL,
        "prompt_version": LEGACY_PROMPT_VERSION,
        "diff_ratio": None,
        "flagged": 1,
    }
    mismatches = [key for key, value in expected.items() if existing.get(key) != value]
    content = existing.get("content")
    if not isinstance(content, str):
        mismatches.append("content_type")
    elif len(content) > WORKING_MODEL_ACTIVE_MAX_CHARS:
        mismatches.append("content_length")
    if mismatches:
        raise WorkingModelMigrationError(
            "existing working-model root violates migration contract: "
            + ", ".join(mismatches)
        )


async def migrate_legacy_roots_in_tx(
    db,
    *,
    source_path: str | Path,
    now: float | None = None,
) -> dict:
    """Create immutable working-model and desire roots inside caller's tx."""

    source = Path(source_path)
    source_existed_before = source.exists()
    data, parse_ok, source_file_sha256 = _inspect_legacy_source(source)
    original_content = str(data.get("content") or "")
    normalized_content = original_content.strip()
    root_content = clip_legacy_prompt_content(original_content)
    truncated = len(normalized_content) > WORKING_MODEL_ACTIVE_MAX_CHARS
    fallback_now = time.time() if now is None else now
    candidate_created_at = _root_created_at(data, fallback_now)

    existing = await repository.get_version(db, WORKING_MODEL_ROOT_ID)
    if existing is None:
        if await repository.count_versions(db) != 0:
            raise WorkingModelMigrationError(
                "working_model_versions contains rows but the stable root is missing"
            )
        root = await repository.insert_version(
            db,
            version_id=WORKING_MODEL_ROOT_ID,
            previous_version_id=None,
            content=root_content,
            created_at=candidate_created_at,
            origin_conv_id=_optional_text(data.get("source_conv")),
            origin_message_id=_optional_text(data.get("source_msg_id")),
            origin_request_id=None,
            reason="",
            writer_model=LEGACY_WRITER_MODEL,
            prompt_version=LEGACY_PROMPT_VERSION,
            diff_ratio=None,
            flagged=1,
        )
        action = "created"
    else:
        # The legacy file remains mutable during the CP0/CP1 compatibility
        # window.  Once created, the V2 root is the immutable historical fact;
        # a later file edit is reportable source drift, not a new expected root
        # and not a reason to make application startup fail.
        _validate_existing_root(existing)
        root = existing
        action = "already_migrated"

    desire_action = await ensure_desire_root_in_tx(
        db,
        working_model_id=WORKING_MODEL_ROOT_ID,
        created_at=float(root["created_at"]),
    )

    source_still_exists = source.exists()
    if source_existed_before and not source_still_exists:
        raise WorkingModelMigrationError("legacy working-model file disappeared during migration")

    stored_root_content = str(root["content"])
    legacy_source_drifted = (
        stored_root_content != root_content if parse_ok is True else None
    )

    return {
        "schema_version": "working_model_v2_migration.v1",
        "source_path": str(source),
        "source_exists": source_existed_before,
        "source_parse_ok": parse_ok,
        "source_file_sha256": source_file_sha256,
        "source_content_chars": len(original_content),
        "normalized_content_chars": len(normalized_content),
        "root_content_chars": len(stored_root_content),
        "truncated": truncated,
        "truncation_algorithm": LEGACY_CLIP_ALGORITHM,
        "truncation_position": (
            WORKING_MODEL_ACTIVE_MAX_CHARS - 3 if truncated else None
        ),
        "source_content_sha256": _sha256_text(original_content),
        "root_content_sha256": _sha256_text(stored_root_content),
        "legacy_source_drifted": legacy_source_drifted,
        "root_id": WORKING_MODEL_ROOT_ID,
        "root_created_at": root["created_at"],
        "action": action,
        "desire_root_action": desire_action,
        "legacy_file_preserved": (
            source_still_exists if source_existed_before else None
        ),
    }


def write_migration_report(report: dict, report_path: str | Path) -> None:
    destination = Path(report_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


async def assert_v2_write_path_ready_in_tx(db) -> None:
    """Fail loudly when a bounded CP4 trigger would kill steady-state writes."""

    cursor = await db.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='trigger' AND name=? LIMIT 1",
        (CP4_NATURAL_TRIGGER_NAME,),
    )
    if await cursor.fetchone() is not None:
        raise WorkingModelActivationError(
            f"refusing to activate Working Model V2 while trigger "
            f"{CP4_NATURAL_TRIGGER_NAME!r} still exists"
        )


async def load_v2_prompt_heads(*, db_factory=None) -> tuple[dict, dict]:
    """Load the exact durable heads that CP5 injects into the next prompt."""

    if db_factory is None:
        # Import lazily: database.init_db imports this module for migration.
        from database import get_db

        db_factory = get_db

    from app.desire import repository as desire_repository

    async with db_factory() as db:
        working_model_head = await repository.get_head(db)
        desire_head = await desire_repository.get_head(db)
    if working_model_head is None or desire_head is None:
        missing = []
        if working_model_head is None:
            missing.append("working_model")
        if desire_head is None:
            missing.append("desire")
        raise WorkingModelActivationError(
            "Working Model V2 prompt head missing: " + ", ".join(missing)
        )
    return working_model_head, desire_head


async def run_legacy_root_migration(
    *,
    db_path: str | Path,
    source_path: str | Path,
    report_path: str | Path | None = None,
    now: float | None = None,
) -> dict:
    """Own-transaction migration entrypoint used by tests and operations."""

    async with aiosqlite.connect(db_path) as db:
        await init_working_model_tables(db)
        await init_desire_tables(db)
        await db.commit()
        await db.execute("BEGIN IMMEDIATE")
        try:
            report = await migrate_legacy_roots_in_tx(
                db,
                source_path=source_path,
                now=now,
            )
        except BaseException:
            await db.rollback()
            raise
        await db.commit()
    if report_path is not None:
        write_migration_report(report, report_path)
    return report
