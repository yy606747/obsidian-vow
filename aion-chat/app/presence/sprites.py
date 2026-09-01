"""Durable sprite library and atomic daily draw quota."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

from config import DATA_DIR, load_ai_behavior
from database import get_db

from .image_provider import (
    PresenceImageGeneration,
    PresenceImageProvider,
    provider_configured,
)
from .schema import SpriteValidationError, inspect_transparent_png


DEFAULT_DAILY_DRAW_LIMIT = 2
DEFAULT_DRAW_TIMEZONE = "America/Los_Angeles"
DEFAULT_BASE_HEIGHT_DIP = 260.0
ACTIVE_SPRITE_LIMIT = 28
SPRITE_FORMS = frozenset({"unknown", "human", "nonhuman"})
SEED_DIR = Path(__file__).with_name("seed_sprites")
SEED_MANIFEST = SEED_DIR / "manifest.json"


class DrawQuotaExceeded(RuntimeError):
    pass


class SpriteLibraryError(RuntimeError):
    pass


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    return cleaned[:32] or "sprite"


class SpriteLibrary:
    def __init__(
        self,
        *,
        get_db_factory: Callable = get_db,
        storage_dir: Path | None = None,
        provider: Any | None = None,
        now: Callable[[], float] = time.time,
        timezone_name: str | None = None,
        daily_limit: int = DEFAULT_DAILY_DRAW_LIMIT,
    ):
        self._get_db = get_db_factory
        self.storage_dir = storage_dir or DATA_DIR / "presence" / "sprites"
        self.provider = provider or PresenceImageProvider()
        self._now = now
        self.timezone_name = timezone_name or DEFAULT_DRAW_TIMEZONE
        self.daily_limit = max(1, int(daily_limit))

    def _local_date(self, now: float | None = None) -> str:
        try:
            zone = ZoneInfo(self.timezone_name)
        except Exception:
            zone = ZoneInfo(DEFAULT_DRAW_TIMEZONE)
        return datetime.fromtimestamp(self._now() if now is None else now, zone).date().isoformat()

    async def reserve_daily_draw(self) -> dict[str, Any]:
        local_date = self._local_date()
        now = self._now()
        async with self._get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT used_count FROM presence_draw_quota WHERE local_date=?",
                (local_date,),
            )
            row = await cursor.fetchone()
            used = int(row[0]) if row else 0
            if used >= self.daily_limit:
                await db.rollback()
                raise DrawQuotaExceeded("presence_daily_draw_limit")
            used += 1
            await db.execute(
                """
                INSERT INTO presence_draw_quota(local_date, used_count, updated_at)
                VALUES(?,?,?)
                ON CONFLICT(local_date) DO UPDATE SET
                    used_count=excluded.used_count,
                    updated_at=excluded.updated_at
                """,
                (local_date, used, now),
            )
            await db.commit()
        return {"local_date": local_date, "used": used, "limit": self.daily_limit}

    async def quota_snapshot(self) -> dict[str, Any]:
        local_date = self._local_date()
        async with self._get_db() as db:
            cursor = await db.execute(
                "SELECT used_count FROM presence_draw_quota WHERE local_date=?",
                (local_date,),
            )
            row = await cursor.fetchone()
        used = int(row[0]) if row else 0
        return {
            "local_date": local_date,
            "used": used,
            "limit": self.daily_limit,
            "remaining": max(0, self.daily_limit - used),
        }

    async def can_draw(self) -> bool:
        if not provider_configured() and isinstance(
            self.provider, PresenceImageProvider
        ):
            return False
        return bool((await self.quota_snapshot())["remaining"])

    async def draw(
        self,
        *,
        form: str,
        prompt: str,
        description: str,
        base_height_dip: float = DEFAULT_BASE_HEIGHT_DIP,
        context: Any | None = None,
    ) -> dict[str, Any]:
        normalized_form = str(form or "").strip().lower()
        visual_prompt = " ".join(str(prompt or "").split())[:500]
        self_description = " ".join(str(description or "").split())[:2000]
        if normalized_form not in {"human", "nonhuman"}:
            raise SpriteLibraryError("presence_draw_form_invalid")
        if not visual_prompt:
            raise SpriteLibraryError("presence_draw_prompt_required")
        if not self_description:
            raise SpriteLibraryError("presence_draw_description_required")
        if not await self.has_non_seed_sprites() and normalized_form != "human":
            raise SpriteLibraryError("presence_first_sprite_must_be_human")

        reference_png: bytes | None = None
        reference_prompt = ""
        if normalized_form == "human":
            baseline = await self.human_baseline()
            if baseline is not None:
                reference_png, _row = await self.file_for_hash(
                    str(baseline["sprite_hash"])
                )
                reference_prompt = str(baseline.get("prompt") or "")
        preflight = getattr(self.provider, "preflight", None)
        if callable(preflight):
            await preflight()
        quota = await self.reserve_daily_draw()
        generate_with_metadata = getattr(
            self.provider,
            "generate_with_metadata",
            None,
        )
        if callable(generate_with_metadata):
            generated = await generate_with_metadata(
                visual_prompt,
                reference_png=reference_png,
                reference_prompt=reference_prompt,
            )
        else:
            png = await self.provider.generate(visual_prompt)
            generated = PresenceImageGeneration(
                png=bytes(png),
                provider_type=type(self.provider).__name__,
            )
        sprite_id = f"{_slug(visual_prompt)}_{uuid.uuid4().hex[:8]}"
        result = await self.add_sprite(
            sprite_id=sprite_id,
            png=generated.png,
            form=normalized_form,
            description=self_description,
            prompt=visual_prompt,
            provider=type(self.provider).__name__,
            base_height_dip=base_height_dip,
        )
        result["quota"] = quota
        result["provider_type"] = generated.provider_type
        result["provider_degraded"] = generated.degraded
        result["provider_degradation_reason"] = generated.degradation_reason
        if context is not None:
            metadata = getattr(context, "metadata", {}) or {}
            result["source_chain"] = str(metadata.get("source_chain") or "")
        return result

    async def add_sprite(
        self,
        *,
        sprite_id: str,
        png: bytes,
        description: str,
        prompt: str = "",
        form: str = "unknown",
        provider: str = "seed",
        base_height_dip: float = DEFAULT_BASE_HEIGHT_DIP,
        created_at: float | None = None,
    ) -> dict[str, Any]:
        sprite_id = str(sprite_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", sprite_id):
            raise SpriteLibraryError("presence_sprite_id_invalid")
        try:
            base_height = float(base_height_dip)
        except (TypeError, ValueError) as exc:
            raise SpriteLibraryError("presence_base_height_invalid") from exc
        if not 32.0 <= base_height <= 1200.0:
            raise SpriteLibraryError("presence_base_height_invalid")
        normalized_form = str(form or "").strip().lower()
        if normalized_form not in SPRITE_FORMS:
            raise SpriteLibraryError("presence_sprite_form_invalid")
        inspection = inspect_transparent_png(bytes(png))
        hash_hex = inspection.sha256.removeprefix("sha256:")
        file_name = f"{hash_hex}.png"
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        path = self.storage_dir / file_name
        now = self._now() if created_at is None else float(created_at)
        async with self._get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            created_file = False
            try:
                cursor = await db.execute(
                    """
                    INSERT INTO presence_sprites(
                        sprite_id, sprite_hash, file_name, base_height_dip,
                        description, prompt, form, provider, width_px, height_px,
                        active, archived, created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,1,0,?)
                    ON CONFLICT(sprite_id) DO NOTHING
                    """,
                    (
                        sprite_id,
                        inspection.sha256,
                        file_name,
                        base_height,
                        " ".join(str(description or "").split())[:2000],
                        " ".join(str(prompt or "").split())[:500],
                        normalized_form,
                        str(provider or "")[:120],
                        inspection.width_px,
                        inspection.height_px,
                        now,
                    ),
                )
                if cursor.rowcount != 1:
                    raise SpriteLibraryError("presence_sprite_id_conflict")

                # Check the primary-key conflict before touching disk.  This
                # prevents a colliding sprite_id from leaving a file behind
                # for an asset that was never inserted.
                if not path.exists():
                    tmp = self.storage_dir / f".{file_name}.{uuid.uuid4().hex}.tmp"
                    try:
                        tmp.write_bytes(bytes(png))
                        os.replace(tmp, path)
                        created_file = True
                    finally:
                        try:
                            tmp.unlink(missing_ok=True)
                        except OSError:
                            pass
                await db.execute(
                    """
                    INSERT INTO presence_sprite_sync(
                        sprite_hash, device_id, status, synced_at, updated_at
                    ) VALUES(?, 'pc', 'pending', NULL, ?)
                    ON CONFLICT(sprite_hash, device_id) DO NOTHING
                    """,
                    (inspection.sha256, now),
                )
                await self._archive_overflow_in_tx(db)
                await db.commit()
            except (Exception, asyncio.CancelledError):
                # Keep the database/file invariant: a failed transaction may
                # leave neither a sprite row nor a newly-created asset file.
                if created_file:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                await db.rollback()
                raise
        return {
            "ok": True,
            "status": "completed",
            "sprite_id": sprite_id,
            "sprite_hash": inspection.sha256,
            "form": normalized_form,
            "base_height_dip": base_height,
            "width_px": inspection.width_px,
            "height_px": inspection.height_px,
            "available_on_device": False,
        }

    async def _archive_overflow_in_tx(self, db) -> None:
        cursor = await db.execute(
            """
            SELECT sprite_id FROM presence_sprites
            WHERE provider<>'seed' AND form='human'
            ORDER BY created_at, sprite_id
            LIMIT 1
            """
        )
        baseline_row = await cursor.fetchone()
        protected_id = str(baseline_row[0]) if baseline_row is not None else None
        offset = ACTIVE_SPRITE_LIMIT
        where = "active=1 AND archived=0"
        params: list[Any] = []
        if protected_id is not None:
            await db.execute(
                "UPDATE presence_sprites SET active=1, archived=0 WHERE sprite_id=?",
                (protected_id,),
            )
            where += " AND sprite_id<>?"
            params.append(protected_id)
            offset -= 1
        cursor = await db.execute(
            f"""
            SELECT sprite_id FROM presence_sprites
            WHERE {where}
            ORDER BY created_at DESC, sprite_id DESC
            LIMIT -1 OFFSET ?
            """,
            (*params, offset),
        )
        overflow = [str(row[0]) for row in await cursor.fetchall()]
        if overflow:
            placeholders = ",".join("?" for _ in overflow)
            await db.execute(
                f"UPDATE presence_sprites SET active=0, archived=1 "
                f"WHERE sprite_id IN ({placeholders})",
                overflow,
            )

    async def ensure_seed_sprites(self) -> int:
        if not SEED_MANIFEST.exists():
            return 0
        try:
            manifest = json.loads(SEED_MANIFEST.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SpriteLibraryError("presence_seed_manifest_invalid") from exc
        entries = manifest.get("sprites") if isinstance(manifest, dict) else None
        if not isinstance(entries, list):
            raise SpriteLibraryError("presence_seed_manifest_invalid")
        inserted = 0
        for entry in entries:
            if not isinstance(entry, dict):
                raise SpriteLibraryError("presence_seed_manifest_invalid")
            path = SEED_DIR / str(entry.get("file") or "")
            if not path.is_file() or path.parent != SEED_DIR:
                raise SpriteLibraryError("presence_seed_file_missing")
            sprite_id = str(entry.get("sprite_id") or "")
            if await self.get_sprite(sprite_id) is not None:
                continue
            await self.add_sprite(
                sprite_id=sprite_id,
                png=path.read_bytes(),
                description=str(entry.get("description") or ""),
                prompt=str(entry.get("prompt") or ""),
                provider="seed",
                base_height_dip=float(
                    entry.get("base_height_dip") or DEFAULT_BASE_HEIGHT_DIP
                ),
                created_at=float(entry.get("created_at") or self._now()),
            )
            inserted += 1
        return inserted

    async def has_non_seed_sprites(self) -> bool:
        async with self._get_db() as db:
            cursor = await db.execute(
                "SELECT 1 FROM presence_sprites WHERE provider<>'seed' LIMIT 1"
            )
            return await cursor.fetchone() is not None

    async def has_synced_non_seed_sprites(self, *, device_id: str = "pc") -> bool:
        normalized_device = str(device_id or "pc").strip()[:64] or "pc"
        async with self._get_db() as db:
            cursor = await db.execute(
                """
                SELECT 1
                FROM presence_sprites AS s
                JOIN presence_sprite_sync AS ss
                  ON ss.sprite_hash=s.sprite_hash AND ss.device_id=?
                WHERE s.provider<>'seed' AND ss.status='synced'
                LIMIT 1
                """,
                (normalized_device,),
            )
            return await cursor.fetchone() is not None

    async def human_baseline(self) -> dict[str, Any] | None:
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM presence_sprites
                WHERE provider<>'seed' AND form='human'
                ORDER BY created_at, sprite_id
                LIMIT 1
                """
            )
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def non_seed_count(self) -> int:
        async with self._get_db() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM presence_sprites WHERE provider<>'seed'"
            )
            row = await cursor.fetchone()
        return int(row[0]) if row is not None else 0

    async def ensure_seed_fallback(self, *, device_id: str = "pc") -> bool:
        device_id = str(device_id or "pc").strip()[:64] or "pc"
        async with self._get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                restored = await self._ensure_seed_fallback_in_tx(
                    db,
                    device_id=device_id,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return restored

    async def _ensure_seed_fallback_in_tx(
        self,
        db,
        *,
        device_id: str,
    ) -> bool:
        cursor = await db.execute(
            """
            SELECT 1
            FROM presence_sprites AS s
            JOIN presence_sprite_sync AS ss
              ON ss.sprite_hash=s.sprite_hash AND ss.device_id=?
            WHERE s.active=1 AND s.archived=0 AND ss.status='synced'
            LIMIT 1
            """,
            (device_id,),
        )
        if await cursor.fetchone() is not None:
            return False
        cursor = await db.execute(
            "SELECT 1 FROM presence_sprites "
            "WHERE provider='seed' AND archived=1 LIMIT 1"
        )
        if await cursor.fetchone() is None:
            return False
        cursor = await db.execute(
            "UPDATE presence_sprites SET active=1, archived=0 "
            "WHERE provider='seed' AND archived=1"
        )
        return int(cursor.rowcount or 0) > 0

    async def reconcile_seed_lifecycle(self, *, device_id: str = "pc") -> str:
        device_id = str(device_id or "pc").strip()[:64] or "pc"
        async with self._get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """
                    SELECT 1
                    FROM presence_sprites AS s
                    JOIN presence_sprite_sync AS ss
                      ON ss.sprite_hash=s.sprite_hash AND ss.device_id=?
                    WHERE s.provider<>'seed' AND s.active=1 AND s.archived=0
                      AND ss.status='synced'
                    LIMIT 1
                    """,
                    (device_id,),
                )
                if await cursor.fetchone() is not None:
                    await db.execute(
                        "UPDATE presence_sprites SET active=0, archived=1 "
                        "WHERE provider='seed'"
                    )
                    await self._archive_overflow_in_tx(db)
                    outcome = "seeds_archived"
                elif await self._ensure_seed_fallback_in_tx(
                    db,
                    device_id=device_id,
                ):
                    outcome = "seeds_restored"
                else:
                    outcome = "unchanged"
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return outcome

    async def get_sprite(self, sprite_id: str) -> dict[str, Any] | None:
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM presence_sprites WHERE sprite_id=?",
                (str(sprite_id or ""),),
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_sprite_by_hash(self, sprite_hash: str) -> dict[str, Any] | None:
        sprite_hash = self._normalize_hash(sprite_hash)
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM presence_sprites WHERE sprite_hash=?",
                (sprite_hash,),
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    @staticmethod
    def _normalize_hash(sprite_hash: str) -> str:
        value = str(sprite_hash or "").strip().lower()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            raise SpriteLibraryError("presence_sprite_hash_invalid")
        return value

    async def file_for_hash(self, sprite_hash: str) -> tuple[bytes, dict[str, Any]]:
        sprite_hash = self._normalize_hash(sprite_hash)
        row = await self.get_sprite_by_hash(sprite_hash)
        if row is None:
            raise SpriteLibraryError("presence_sprite_not_found")
        file_name = str(row.get("file_name") or "")
        if not re.fullmatch(r"[0-9a-f]{64}\.png", file_name):
            raise SpriteLibraryError("presence_sprite_file_invalid")
        path = self.storage_dir / file_name
        try:
            data = path.read_bytes()
            inspection = inspect_transparent_png(data)
        except (OSError, SpriteValidationError) as exc:
            raise SpriteLibraryError("presence_sprite_file_invalid") from exc
        if inspection.sha256 != sprite_hash:
            raise SpriteLibraryError("presence_sprite_hash_mismatch")
        return data, row

    async def manifest(self, *, device_id: str = "pc") -> list[dict[str, Any]]:
        device_id = str(device_id or "pc").strip()[:64] or "pc"
        await self.ensure_seed_fallback(device_id=device_id)
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT s.sprite_id, s.sprite_hash, s.base_height_dip,
                       s.description, s.prompt, s.form, s.provider,
                       s.width_px, s.height_px,
                       COALESCE(ss.status, 'pending') AS sync_status
                FROM presence_sprites AS s
                LEFT JOIN presence_sprite_sync AS ss
                  ON ss.sprite_hash=s.sprite_hash AND ss.device_id=?
                WHERE s.active=1 AND s.archived=0
                ORDER BY s.created_at DESC, s.sprite_id DESC
                """,
                (device_id,),
            )
            rows = await cursor.fetchall()
        return [
            {
                **dict(row),
                "available_on_device": str(row["sync_status"]) == "synced",
            }
            for row in rows
        ]

    async def available_sprites(self, *, device_id: str = "pc") -> list[dict[str, Any]]:
        return [
            row
            for row in await self.manifest(device_id=device_id)
            if row["available_on_device"]
        ]

    async def has_available_sprites(self, *, device_id: str = "pc") -> bool:
        device_id = str(device_id or "pc").strip()[:64] or "pc"
        await self.ensure_seed_fallback(device_id=device_id)
        async with self._get_db() as db:
            cursor = await db.execute(
                """
                SELECT 1
                FROM presence_sprites AS s
                JOIN presence_sprite_sync AS ss
                  ON ss.sprite_hash=s.sprite_hash AND ss.device_id=?
                WHERE s.active=1 AND s.archived=0 AND ss.status='synced'
                LIMIT 1
                """,
                (device_id,),
            )
            row = await cursor.fetchone()
        return row is not None

    async def mark_synced(self, sprite_hash: str, *, device_id: str = "pc") -> dict[str, Any]:
        sprite_hash = self._normalize_hash(sprite_hash)
        device_id = str(device_id or "pc").strip()[:64] or "pc"
        _data, row = await self.file_for_hash(sprite_hash)
        now = self._now()
        async with self._get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    """
                    INSERT INTO presence_sprite_sync(
                        sprite_hash, device_id, status, synced_at, updated_at
                    ) VALUES(?, ?, 'synced', ?, ?)
                    ON CONFLICT(sprite_hash, device_id) DO UPDATE SET
                        status='synced', synced_at=excluded.synced_at,
                        updated_at=excluded.updated_at
                    """,
                    (sprite_hash, device_id, now, now),
                )
                if str(row.get("provider") or "") != "seed":
                    await db.execute(
                        "UPDATE presence_sprites SET active=0, archived=1 "
                        "WHERE provider='seed'"
                    )
                    await self._archive_overflow_in_tx(db)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return {
            "sprite_id": str(row["sprite_id"]),
            "sprite_hash": sprite_hash,
            "device_id": device_id,
            "status": "synced",
            "available_on_device": True,
        }

    async def mark_missing(self, sprite_hash: str, *, device_id: str = "pc") -> None:
        sprite_hash = self._normalize_hash(sprite_hash)
        device_id = str(device_id or "pc").strip()[:64] or "pc"
        now = self._now()
        async with self._get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    """
                    INSERT INTO presence_sprite_sync(
                        sprite_hash, device_id, status, synced_at, updated_at
                    ) VALUES(?, ?, 'pending', NULL, ?)
                    ON CONFLICT(sprite_hash, device_id) DO UPDATE SET
                        status='pending', synced_at=NULL, updated_at=excluded.updated_at
                    """,
                    (sprite_hash, device_id, now),
                )
                await self._ensure_seed_fallback_in_tx(
                    db,
                    device_id=device_id,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise


sprite_library = SpriteLibrary()


async def execute_presence_draw(intent, context) -> dict[str, Any]:
    if str(intent.arguments.get("parse_error") or ""):
        raise SpriteLibraryError("parse_failed")
    return await sprite_library.draw(
        form=str(intent.arguments.get("form") or ""),
        prompt=str(intent.arguments.get("prompt") or ""),
        description=str(intent.arguments.get("description") or ""),
        base_height_dip=float(
            intent.arguments.get("base_height_dip") or DEFAULT_BASE_HEIGHT_DIP
        ),
        context=context,
    )


__all__ = [
    "ACTIVE_SPRITE_LIMIT",
    "DEFAULT_DAILY_DRAW_LIMIT",
    "DrawQuotaExceeded",
    "SpriteLibrary",
    "SpriteLibraryError",
    "SPRITE_FORMS",
    "execute_presence_draw",
    "sprite_library",
]
