"""Presence sprite-sync, delivery, and durable ACK worker."""

from __future__ import annotations

import logging
import queue
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from activity_worker import heartbeat_due, retry_delay, sleep_with_gap_log
from presence_protocol import (
    AckJournal,
    PresenceContractError,
    SpriteCache,
    normalize_hash,
    validate_trajectory,
)
from transport import get_bytes, get_json, post_json


POLL_TIMEOUT_SEC = 5
SYNC_INTERVAL_SEC = 60
log = logging.getLogger("pc_agent")


def run_presence_loop(
    server_url: str,
    token: str,
    controller,
    ack_queue: queue.Queue,
    state_dir: Path,
    lock_checker: Callable[[], bool | None],
    journal: AckJournal | None = None,
) -> None:
    cache = SpriteCache(Path(state_dir) / "presence_sprites")
    journal = journal or AckJournal(Path(state_dir) / "presence_ack_journal.json")
    recovered = journal.recover_incomplete(now=time.time())
    if recovered:
        log.info("recovered %s incomplete Presence playback(s)", recovered)
    active: set[str] = set()
    last_sync = 0.0
    failures = 0
    last_heartbeat_at: float | None = None
    while True:
        _drain_player_acks(ack_queue, journal, active)
        _flush_acks(server_url, token, journal)
        try:
            now = time.monotonic()
            if now - last_sync >= SYNC_INTERVAL_SEC:
                sync_sprites(server_url, token, cache)
                last_sync = now
            delivery = get_json(
                f"{server_url}/api/presence/pending?timeout={POLL_TIMEOUT_SEC}&device_id=pc",
                token=token,
                timeout=POLL_TIMEOUT_SEC + 10,
            )
            if failures:
                log.info("Presence poll recovered after %s failed attempt(s)", failures)
            failures = 0
            heartbeat_at = time.monotonic()
            if heartbeat_due(last_heartbeat_at, heartbeat_at):
                log.info("Presence poll heartbeat")
                last_heartbeat_at = heartbeat_at
            if delivery:
                try:
                    _handle_delivery(
                        server_url,
                        token,
                        delivery,
                        controller,
                        cache,
                        journal,
                        active,
                        lock_checker,
                    )
                except PresenceContractError as exc:
                    event_id = str(delivery.get("event_id") or "").strip()
                    if not event_id:
                        raise
                    journal.record(
                        event_id,
                        "rejected",
                        reason=str(exc)[:120] or "client_contract_invalid",
                        now=time.time(),
                    )
                    _flush_acks(server_url, token, journal)
        except Exception as exc:
            failures += 1
            delay = retry_delay(failures, 2, 15)
            log.warning(
                "Presence poll failed; attempt=%s retry_in=%.1fs error=%s: %s",
                failures, delay, type(exc).__name__, exc,
            )
            sleep_with_gap_log(delay, "Presence poll retry")


def sync_sprites(server_url: str, token: str, cache: SpriteCache) -> int:
    manifest = get_json(
        f"{server_url}/api/presence/sprites/manifest?device_id=pc",
        token=token,
        timeout=20,
    )
    entries = manifest.get("sprites") if isinstance(manifest, dict) else None
    if not isinstance(entries, list):
        raise PresenceContractError("sprite_manifest:invalid")
    synced = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            sprite_hash = normalize_hash(str(entry.get("sprite_hash") or ""))
            path = cache.valid_path(sprite_hash)
            downloaded = path is None
            if downloaded:
                data = get_bytes(
                    f"{server_url}/api/presence/sprites/{quote(sprite_hash, safe='')}",
                    token=token,
                    timeout=40,
                )
                cache.store(sprite_hash, data)
            if downloaded or str(entry.get("sync_status") or "") != "synced":
                post_json(
                    f"{server_url}/api/presence/sprites/{quote(sprite_hash, safe='')}/synced",
                    {"device_id": "pc"},
                    token=token,
                )
                synced += 1
        except Exception as exc:
            log.warning("Presence sprite sync failed: %s: %s", type(exc).__name__, exc)
    return synced


def _handle_delivery(
    server_url: str,
    token: str,
    delivery: dict[str, Any],
    controller,
    cache: SpriteCache,
    journal: AckJournal,
    active: set[str],
    lock_checker: Callable[[], bool | None],
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    received_at = monotonic()
    event_id = str(delivery.get("event_id") or "").strip()
    if not event_id:
        raise PresenceContractError("delivery:event_id")
    existing = journal.get(event_id)
    if existing:
        journal.record(
            event_id,
            existing["status"],
            reason=str(existing.get("reason") or ""),
            actual_playback_ms=existing.get("actual_playback_ms"),
            now=time.time(),
        )
        _flush_acks(server_url, token, journal)
        if existing["status"] in {"played", "rejected", "expired"} or event_id in active:
            return

    remaining_ttl_ms = delivery.get("remaining_ttl_ms")
    if isinstance(remaining_ttl_ms, bool) or not isinstance(remaining_ttl_ms, int):
        raise PresenceContractError("delivery:ttl")
    if remaining_ttl_ms <= 0:
        journal.record(event_id, "expired", reason="start_before_elapsed", now=time.time())
        _flush_acks(server_url, token, journal)
        return
    local_start_deadline = received_at + remaining_ttl_ms / 1000.0
    if lock_checker() is True:
        journal.record(event_id, "expired", reason="locked", now=time.time())
        _flush_acks(server_url, token, journal)
        return
    sprite_hash = normalize_hash(str(delivery.get("sprite_hash") or ""))
    sprite_path = cache.valid_path(sprite_hash)
    if sprite_path is None:
        journal.record(event_id, "rejected", reason="sprite_missing", now=time.time())
        _flush_acks(server_url, token, journal)
        return
    trajectory = validate_trajectory(delivery.get("trajectory"))
    if trajectory["sprite_id"] != str(delivery.get("sprite_id") or ""):
        raise PresenceContractError("delivery:sprite_id_mismatch")
    base_height = float(delivery.get("base_height_dip") or 0)
    if not 32.0 <= base_height <= 1200.0:
        raise PresenceContractError("delivery:base_height")

    journal.record(event_id, "accepted", now=time.time())
    _flush_acks(server_url, token, journal)
    saved = journal.get(event_id) or {}
    if saved.get("status") in {"played", "rejected", "expired"}:
        return
    if monotonic() >= local_start_deadline:
        journal.record(event_id, "expired", reason="start_before_elapsed", now=time.time())
        _flush_acks(server_url, token, journal)
        return
    request = dict(delivery)
    request.update(
        trajectory=trajectory,
        sprite_path=str(sprite_path),
        local_start_deadline=local_start_deadline,
    )
    active.add(event_id)
    controller.submit(request)


def _drain_player_acks(
    ack_queue: queue.Queue,
    journal: AckJournal,
    active: set[str],
) -> None:
    while True:
        try:
            ack = ack_queue.get_nowait()
        except queue.Empty:
            return
        event_id = str(ack.get("event_id") or "")
        active.discard(event_id)
        saved = journal.get(event_id) or {}
        if not (
            saved.get("status") == str(ack.get("status") or "")
            and saved.get("reason") == " ".join(str(ack.get("reason") or "").split())[:240]
            and saved.get("actual_playback_ms") == ack.get("actual_playback_ms")
        ):
            journal.record_ack(ack, now=time.time())


def _flush_acks(server_url: str, token: str, journal: AckJournal) -> None:
    for ack in journal.pending():
        event_id = str(ack["event_id"])
        payload = {
            "status": ack["status"],
            "reason": str(ack.get("reason") or ""),
            "actual_playback_ms": ack.get("actual_playback_ms"),
            "device_id": "pc",
        }
        try:
            response = post_json(
                f"{server_url}/api/presence/{quote(event_id, safe='')}/ack",
                payload,
                token=token,
            )
        except Exception:
            continue
        journal.mark_sent(event_id, response, now=time.time())


__all__ = ["run_presence_loop", "sync_sprites"]
