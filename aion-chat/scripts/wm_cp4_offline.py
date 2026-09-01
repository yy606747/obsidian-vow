#!/usr/bin/env python3
"""Run the two bounded, synthetic CP4 drift arms on isolated SQLite databases.

Without ``--execute-paid-run`` this is a zero-call preflight.  Paid execution
requires the separately frozen CP4 authorization and input confirmation.  A
case is durably marked ``started`` before its gate call and is never replayed;
the two ten-step chains are interleaved by order and never write production.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_UP
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from typing import Any
from urllib.parse import urlsplit

import aiosqlite


CHAT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = CHAT_ROOT.parent
if str(CHAT_ROOT) not in sys.path:
    sys.path.insert(0, str(CHAT_ROOT))

from ai_providers import (  # noqa: E402
    _resolve_proxy,
    call_core_chat_once,
    call_slot_chat,
)
from app.desire import repository as desire_repository  # noqa: E402
from app.desire.schema import init_desire_tables  # noqa: E402
from app.desire.service import DESIRE_ROOT_ID  # noqa: E402
from app.vows.service import VowService  # noqa: E402
from app.working_model import repository as wm_repository  # noqa: E402
from app.working_model.gate import (  # noqa: E402
    WORKING_MODEL_GATE_MAX_TOKENS,
    WORKING_MODEL_GATE_PROMPT_VERSION,
    WORKING_MODEL_GATE_SLOT,
    WORKING_MODEL_GATE_TEMPERATURE,
    WORKING_MODEL_GATE_TIMEOUT_SEC,
)
from app.working_model.runtime import (  # noqa: E402
    WorkingModelPipelineInput,
    run_working_model_pipeline,
)
from app.working_model.schema import init_working_model_tables  # noqa: E402
from app.working_model.writer import (  # noqa: E402
    WORKING_MODEL_WRITER_MAX_CORRECTION_RETRIES,
    WORKING_MODEL_WRITER_MAX_LENGTH_RETRIES,
    WORKING_MODEL_WRITER_MAX_PARSE_RETRIES,
    WORKING_MODEL_WRITER_MAX_TOKENS,
    WORKING_MODEL_WRITER_TEMPERATURE,
    WORKING_MODEL_WRITER_TIMEOUT_SEC,
    build_writer_identity_snapshot,
    writer_prompt_version,
)
from config import (  # noqa: E402
    DB_PATH,
    get_slot,
    load_worldbook,
    resolve_core_model,
)
from scripts import wm_audit  # noqa: E402
from scripts import wm_cp4_shadow as cp4  # noqa: E402


OFFLINE_STATE_SCHEMA_VERSION = "working_model_v2_cp4_offline_state.v1"
OFFLINE_PREREGISTRATION_SCHEMA_VERSION = (
    "working_model_v2_cp4_offline_preregistration.v3"
)
DEFAULT_OFFLINE_PREREGISTRATION = (
    cp4.REPO_ROOT
    / "docs/planning/checkpoints/working_model_v2/artifacts/"
    "CP4_OFFLINE_RERUN_PREREGISTRATION.json"
)
GROUP_NAMES = ("appeasement_pressure", "neutral_control")
CASES_PER_GROUP = 10
MAX_GATE_CALLS_PER_GROUP = 10
MAX_WRITER_CALLS_PER_GROUP = 20
EXPECTED_TOTAL_PROVIDER_CALLS = 40
HARD_TOTAL_PROVIDER_CALLS = 60
USAGE_KEYS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)
EXPERIMENT_WRITER_ALLOWLIST = {
    "Pro/MiniMaxAI/MiniMax-M2.5": {
        "provider": "siliconflow",
        "resolved_model": "Pro/MiniMaxAI/MiniMax-M2.5",
        "endpoint_id": "preset_siliconflow",
        "endpoint_type": "openai",
        "base_url_host": "api.siliconflow.cn",
    },
    "GLM-5": {
        "provider": "siliconflow",
        "resolved_model": "Pro/zai-org/GLM-5.1",
        "endpoint_id": "preset_siliconflow",
        "endpoint_type": "openai",
        "base_url_host": "api.siliconflow.cn",
    },
}
COST_QUANTUM = Decimal("0.000001")
INPUT_TOKEN_OVERHEAD = 1024
# SiliconFlow reasoning usage can exceed the visible ``max_tokens`` counter
# (the earlier gate run reported 481 completion tokens with max_tokens=256).
# Reserve the platform's documented default thinking budget as well so the
# monetary bound remains conservative for reasoning-capable writers.
REASONING_TOKEN_ALLOWANCE = 4096


def _decimal(value: object, *, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise cp4.CP4ContractError(f"{field} must be a decimal number") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise cp4.CP4ContractError(f"{field} must be positive and finite")
    return parsed


def _money_text(value: Decimal) -> str:
    return format(value.quantize(COST_QUANTUM, rounding=ROUND_UP), "f")


def _pricing_rates(prereg: dict[str, Any], role: str) -> tuple[Decimal, Decimal]:
    pricing = (prereg.get("pricing_snapshot") or {}).get(role) or {}
    if pricing.get("currency") != "CNY" or pricing.get("unit") != "per_million_tokens":
        raise cp4.CP4ContractError(f"{role} pricing must be CNY per_million_tokens")
    return (
        _decimal(pricing.get("input"), field=f"pricing_snapshot.{role}.input"),
        _decimal(pricing.get("output"), field=f"pricing_snapshot.{role}.output"),
    )


def _call_cost_upper_bound(
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    rates: tuple[Decimal, Decimal],
) -> Decimal:
    # A token cannot encode less than one UTF-8 byte.  The serialized byte
    # count plus a fixed role/protocol allowance is therefore deliberately
    # conservative for these text-only requests.
    input_tokens = len(
        json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ) + INPUT_TOKEN_OVERHEAD
    input_rate, output_rate = rates
    return (
        Decimal(input_tokens) * input_rate
        + Decimal(max_tokens + REASONING_TOKEN_ALLOWANCE) * output_rate
    ) / Decimal(1_000_000)


def _usage_cost(
    usage: dict[str, Any],
    *,
    rates: tuple[Decimal, Decimal],
) -> Decimal:
    input_rate, output_rate = rates
    total = Decimal("0")
    for event in usage.get("provider_calls") or []:
        meta = event.get("meta") or {}
        prompt_tokens = int(meta.get("prompt_tokens") or 0)
        completion_tokens = int(meta.get("completion_tokens") or 0)
        if bool(event.get("ok")) and prompt_tokens + completion_tokens <= 0:
            raise cp4.CP4ContractError(
                "successful provider call omitted token usage; monetary cap is unverifiable"
            )
        total += (
            Decimal(prompt_tokens) * input_rate
            + Decimal(completion_tokens) * output_rate
        ) / Decimal(1_000_000)
    return total


def _state_cost(prereg: dict[str, Any], state: dict[str, Any]) -> Decimal:
    gate_rates = _pricing_rates(prereg, "gate")
    writer_rates = _pricing_rates(prereg, "writer")
    total = Decimal("0")
    for row in state.get("rows") or []:
        if row.get("status") not in {"completed", "void_budget_exceeded"}:
            continue
        total += _usage_cost(row.get("gate_usage") or {}, rates=gate_rates)
        total += _usage_cost(row.get("writer_usage") or {}, rates=writer_rates)
    return total


def _preset_writer_endpoint(provider: str) -> tuple[dict[str, str], bool]:
    if provider == "siliconflow":
        return ({
            "id": "preset_siliconflow",
            "type": "openai",
            "base_url": "https://api.siliconflow.cn/v1",
        }, False)
    if provider == "gemini":
        return ({
            "id": "preset_gemini",
            "type": "gemini",
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
        }, True)
    if provider == "aipro":
        return ({
            "id": "preset_aipro",
            "type": "openai",
            "base_url": "https://vip.aipro.love/v1",
        }, False)
    raise cp4.CP4ContractError(f"unsupported experiment writer provider: {provider}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _with_scheduler_heartbeat(awaitable):
    """Keep aiosqlite worker wakeups flowing in restricted runtimes."""

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(0.001)

    task = asyncio.create_task(heartbeat())
    try:
        return await awaitable
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _usage_view(meta: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: int(meta[key]) if isinstance(meta.get(key), (int, float)) else None
        for key in USAGE_KEYS
    }
    result["provider_calls"] = [
        {
            "ts": event.get("ts"),
            "request_id": event.get("request_id"),
            "scope": event.get("scope"),
            "model": event.get("model"),
            "endpoint_id": event.get("endpoint_id"),
            "ok": bool(event.get("ok")),
            "http_status": event.get("http_status"),
            "error_type": event.get("error_type"),
            "elapsed_ms": int(event.get("elapsed_ms") or 0),
            "meta": event.get("meta") or {},
        }
        for event in meta.get("provider_calls") or []
    ]
    return result


def _source_heads(db_path: Path) -> dict[str, str]:
    with cp4._open_db(db_path, read_only=True) as connection:
        cp4._assert_required_tables(connection)
        working = connection.execute(
            """
            SELECT current.content
            FROM working_model_versions AS current
            WHERE NOT EXISTS (
                SELECT 1 FROM working_model_versions AS child
                WHERE child.previous_version_id = current.id
            )
            ORDER BY current.created_at DESC, current.rowid DESC
            LIMIT 1
            """
        ).fetchone()
        desire = connection.execute(
            """
            SELECT current.content
            FROM desire_versions AS current
            WHERE NOT EXISTS (
                SELECT 1 FROM desire_versions AS child
                WHERE child.previous_version_id = current.id
            )
            ORDER BY current.created_at DESC, current.rowid DESC
            LIMIT 1
            """
        ).fetchone()
    if working is None or desire is None:
        raise cp4.CP4ContractError("source database has no complete WM/desire heads")
    return {
        "working_model": str(working[0] or ""),
        "desire": str(desire[0] or ""),
    }


def _db_factory(path: Path):
    @asynccontextmanager
    async def factory():
        async with aiosqlite.connect(path) as database:
            await database.execute("PRAGMA busy_timeout=30000")
            yield database

    return factory


async def _identity_snapshot(source_db: Path) -> dict[str, str]:
    service = VowService(get_db_factory=_db_factory(source_db))
    vow_block, _ability = await service.load_vow_prompt_context()
    return build_writer_identity_snapshot(load_worldbook(), vow_block=vow_block)


async def _initialize_group_db(
    path: Path,
    *,
    heads: dict[str, str],
) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as database:
        await database.execute(
            "CREATE TABLE messages ("
            "id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, role TEXT NOT NULL, "
            "content TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        await init_working_model_tables(database)
        await init_desire_tables(database)
        await wm_repository.insert_version(
            database,
            version_id="working_model_root",
            previous_version_id=None,
            content=heads["working_model"],
            created_at=1.0,
            origin_conv_id=None,
            origin_message_id=None,
            origin_request_id=None,
            reason="CP4 offline arm root copied from the same frozen source head",
            writer_model="unknown",
            prompt_version="cp4_offline_root.v1",
            diff_ratio=None,
            flagged=1,
        )
        await desire_repository.insert_version(
            database,
            version_id=DESIRE_ROOT_ID,
            previous_version_id=None,
            content=heads["desire"],
            change_note="CP4 offline arm root copied from the same frozen source head",
            origin_request_id="root",
            working_model_id="working_model_root",
            writer_model="unknown",
            prompt_version="cp4_offline_root.v1",
            created_at=1.0,
        )
        await database.commit()


async def prepare_source_snapshot(
    preregistration_path: Path,
    *,
    identity_database: Path,
    root_source_database: Path,
) -> dict[str, Any]:
    """Create one private composite source without mutating either input DB."""

    prereg, _payload = _validate_offline_preregistration(
        preregistration_path,
        require_authorization=True,
        require_source_seal=False,
    )
    if (prereg.get("source_snapshot") or {}).get("sealed"):
        raise cp4.CP4ContractError("source snapshot is already sealed")
    if not identity_database.is_file() or not root_source_database.is_file():
        raise cp4.CP4ContractError("source preparation input database is missing")
    root_heads = _source_heads(root_source_database)
    identity_source_hash_before = cp4._sha256_file(identity_database)
    root_source_hash_before = cp4._sha256_file(root_source_database)
    private_dir = cp4._resolve_private_path(
        prereg["private_run_dir"],
        field="private_run_dir",
    )
    private_dir.mkdir(parents=True, exist_ok=True)
    target = private_dir / "source.sqlite3"
    preparing = private_dir / ".source.sqlite3.preparing"
    if target.exists() or preparing.exists():
        raise cp4.CP4ContractError(
            "private source already exists; do not overwrite or silently rebuild it"
        )
    shutil.copy2(identity_database, preparing)
    async with aiosqlite.connect(preparing) as database:
        await database.execute("PRAGMA busy_timeout=30000")
        await init_working_model_tables(database)
        await init_desire_tables(database)
        wm_cursor = await database.execute(
            "SELECT COUNT(*) FROM working_model_versions"
        )
        wm_row = await wm_cursor.fetchone()
        desire_cursor = await database.execute(
            "SELECT COUNT(*) FROM desire_versions"
        )
        desire_row = await desire_cursor.fetchone()
        wm_count = int(wm_row[0]) if wm_row is not None else 0
        desire_count = int(desire_row[0]) if desire_row is not None else 0
        if wm_count or desire_count:
            raise cp4.CP4ContractError(
                "identity database unexpectedly already contains V2 roots"
            )
        await wm_repository.insert_version(
            database,
            version_id="working_model_root",
            previous_version_id=None,
            content=root_heads["working_model"],
            created_at=1.0,
            origin_conv_id=None,
            origin_message_id=None,
            origin_request_id=None,
            reason="CP4 offline composite source copied from predeployment V2 root",
            writer_model="unknown",
            prompt_version="cp4_offline_source_root.v1",
            diff_ratio=None,
            flagged=1,
        )
        await desire_repository.insert_version(
            database,
            version_id=DESIRE_ROOT_ID,
            previous_version_id=None,
            content=root_heads["desire"],
            change_note="CP4 offline composite source copied from predeployment root",
            origin_request_id="root",
            working_model_id="working_model_root",
            writer_model="unknown",
            prompt_version="cp4_offline_source_root.v1",
            created_at=1.0,
        )
        await database.commit()
    preparing.replace(target)
    source_view = await _source_snapshot_view(target)
    result = {
        "schema_version": "working_model_v2_cp4_offline_source_preparation.v1",
        "run_id": prereg["run_id"],
        "prepared_at": _now_iso(),
        "source_database": str(target),
        "identity_database_sha256_before": identity_source_hash_before,
        "identity_database_sha256_after": cp4._sha256_file(identity_database),
        "root_source_database_sha256_before": root_source_hash_before,
        "root_source_database_sha256_after": cp4._sha256_file(root_source_database),
        "working_model_root_was_empty": not bool(root_heads["working_model"]),
        "desire_root_was_empty": not bool(root_heads["desire"]),
        **source_view,
        "provider_calls_made": 0,
    }
    cp4._atomic_write_json(private_dir / "source_preparation.json", result)
    return result


async def _insert_case_messages(
    db_path: Path,
    *,
    group_name: str,
    case: dict[str, Any],
) -> tuple[str, str, str]:
    case_id = str(case["id"])
    conv_id = f"cp4-offline-{group_name}"
    user_id = f"{case_id}-user"
    assistant_id = f"{case_id}-assistant"
    created_at = 1000.0 + float(case["order"])
    async with aiosqlite.connect(db_path) as database:
        await database.execute(
            "INSERT OR IGNORE INTO messages(id,conv_id,role,content,created_at) "
            "VALUES (?,?,?,?,?)",
            (user_id, conv_id, "user", case["latest_user_message"], created_at),
        )
        await database.execute(
            "INSERT OR IGNORE INTO messages(id,conv_id,role,content,created_at) "
            "VALUES (?,?,?,?,?)",
            (assistant_id, conv_id, "assistant", "合成离线申请。", created_at + 0.1),
        )
        await database.commit()
    return conv_id, user_id, assistant_id


async def _no_embedding(_content: str) -> None:
    return None


async def _offline_memory_insert(_database, **kwargs) -> dict[str, Any]:
    return {
        "id": kwargs["memory_id"],
        "content": kwargs["content"],
        "type": "ai_note",
        "source_conv": kwargs["source_conv"],
        "origin_request_id": kwargs["origin_request_id"],
        "created_at": kwargs["created_at"],
        "offline_adapter": True,
    }


async def _no_broadcast(_memory: dict[str, Any]) -> None:
    return None


def _effective_timeout(endpoint: dict[str, Any], default: float) -> float:
    raw: object = endpoint.get("timeout_sec")
    if raw in (None, ""):
        raw = os.environ.get("AION_PROVIDER_TIMEOUT_SEC", "").strip()
    try:
        return max(5.0, min(float(raw), 300.0))
    except (TypeError, ValueError):
        return default


def _endpoint_hostname(endpoint: dict[str, Any]) -> str:
    hostname = urlsplit(str(endpoint.get("base_url") or "")).hostname
    return str(hostname or "").lower()


def _effective_transport(
    endpoint: dict[str, Any],
    *,
    preset_gemini: bool = False,
) -> dict[str, Any]:
    proxy = _resolve_proxy(
        str(endpoint.get("type") or "openai"),
        endpoint,
        preset_gemini=preset_gemini,
    )
    return {
        "mode": "direct" if proxy is None else "proxy",
        "proxy_url": proxy,
        "trust_env": False,
    }


def _validate_offline_inputs(prereg: dict[str, Any]) -> dict[str, Any]:
    inputs_path = cp4._resolve_repo_path(
        prereg.get("offline_inputs_file"),
        field="offline_inputs_file",
    )
    if cp4._sha256_file(inputs_path) != prereg.get("offline_inputs_sha256"):
        raise cp4.CP4ContractError("offline input file changed after freezing")
    payload = cp4._load_json(inputs_path)
    groups = payload.get("groups") or {}
    if any(len(groups.get(name) or []) != CASES_PER_GROUP for name in GROUP_NAMES):
        raise cp4.CP4ContractError("each offline group must contain exactly ten cases")
    for name in GROUP_NAMES:
        cases = groups[name]
        if [case.get("order") for case in cases] != list(
            range(1, CASES_PER_GROUP + 1)
        ):
            raise cp4.CP4ContractError(f"offline group order is invalid: {name}")
        if len({case.get("id") for case in cases}) != CASES_PER_GROUP:
            raise cp4.CP4ContractError(f"offline case ids are not unique: {name}")
        for case in cases:
            if any(
                not isinstance(case.get(field), str) or not case[field].strip()
                for field in ("statement", "source", "latest_user_message")
            ):
                raise cp4.CP4ContractError(
                    f"offline case text is invalid: {case.get('id')}"
                )
    for pressure, neutral in zip(
        groups["appeasement_pressure"],
        groups["neutral_control"],
    ):
        pressure_chars = sum(
            len(pressure[field])
            for field in ("statement", "source", "latest_user_message")
        )
        neutral_chars = sum(
            len(neutral[field])
            for field in ("statement", "source", "latest_user_message")
        )
        if abs(pressure_chars - neutral_chars) / max(
            pressure_chars,
            neutral_chars,
        ) > 0.15:
            raise cp4.CP4ContractError(
                "offline pair length differs by more than 15 percent: "
                f"{pressure['order']}"
            )
    return payload


def _validate_offline_preregistration(
    path: Path,
    *,
    require_authorization: bool,
    require_source_seal: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    prereg = cp4._load_json(path)
    if prereg.get("schema_version") != OFFLINE_PREREGISTRATION_SCHEMA_VERSION:
        raise cp4.CP4ContractError("unknown CP4-offline preregistration schema")
    if str(prereg.get("status") or "").startswith("superseded"):
        raise cp4.CP4ContractError("CP4-offline preregistration is superseded")
    if prereg.get("product_code_commit") != cp4._git_head():
        raise cp4.CP4ContractError("git HEAD differs from the frozen product code commit")
    if "activation" in prereg or "natural_shadow" in prereg:
        raise cp4.CP4ContractError(
            "CP4-offline must not contain a natural activation seal or shadow contract"
        )
    isolation = prereg.get("isolation") or {}
    if isolation.get("production_database_writes") is not False:
        raise cp4.CP4ContractError("CP4-offline production writes must remain false")
    if isolation.get("production_flags_mutated") is not False:
        raise cp4.CP4ContractError("CP4-offline must not mutate production flags")

    implementation_files = prereg.get("implementation_files") or []
    if not implementation_files:
        raise cp4.CP4ContractError("implementation file hashes are missing")
    for item in implementation_files:
        implementation_path = cp4._resolve_repo_path(
            item.get("path"),
            field="implementation_files.path",
        )
        if cp4._sha256_file(implementation_path) != item.get("sha256"):
            raise cp4.CP4ContractError(
                f"implementation hash changed: {item.get('path')}"
            )

    payload = _validate_offline_inputs(prereg)
    budgets = prereg.get("offline_arms") or {}
    if budgets.get("cases_per_group") != CASES_PER_GROUP:
        raise cp4.CP4ContractError("offline cases_per_group must remain ten")
    if budgets.get("max_gate_provider_calls_per_group") != MAX_GATE_CALLS_PER_GROUP:
        raise cp4.CP4ContractError("offline gate budget must remain ten per group")
    if budgets.get("max_writer_provider_calls_per_group") != MAX_WRITER_CALLS_PER_GROUP:
        raise cp4.CP4ContractError("offline writer budget must remain twenty per group")
    call_budget = prereg.get("provider_call_budget") or {}
    if call_budget.get("expected_total") != EXPECTED_TOTAL_PROVIDER_CALLS:
        raise cp4.CP4ContractError("offline expected call count must remain forty")
    if call_budget.get("absolute_upper_bound") != HARD_TOTAL_PROVIDER_CALLS:
        raise cp4.CP4ContractError("offline hard call bound must remain sixty")
    monetary_budget = prereg.get("monetary_budget") or {}
    if monetary_budget.get("currency") != "CNY":
        raise cp4.CP4ContractError("offline monetary budget currency must be CNY")
    _decimal(
        monetary_budget.get("authorized_max_cost_cny"),
        field="monetary_budget.authorized_max_cost_cny",
    )
    if monetary_budget.get("on_bound_exceeded") != "void_budget_exceeded":
        raise cp4.CP4ContractError(
            "offline monetary overrun must invalidate as void_budget_exceeded"
        )
    if monetary_budget.get("unknown_success_usage") != "stop_invalid":
        raise cp4.CP4ContractError(
            "successful calls without usage must stop as invalid"
        )
    _pricing_rates(prereg, "gate")
    _pricing_rates(prereg, "writer")
    failure_policy = prereg.get("failure_policy") or {}
    if failure_policy.get("stop_after_first_technical_failure") is not True:
        raise cp4.CP4ContractError(
            "CP4-offline must stop after its first technical failure"
        )
    if failure_policy.get("replace_or_replenish") is not False:
        raise cp4.CP4ContractError(
            "CP4-offline technical failures must not be replaced or replenished"
        )
    writer_contract = prereg.get("writer") or {}
    if writer_contract.get("provider_retry") is not False:
        raise cp4.CP4ContractError("CP4-offline writer provider retry must remain false")
    if writer_contract.get("parse_correction_retry") is not True:
        raise cp4.CP4ContractError("CP4-offline writer parse correction retry is missing")
    if writer_contract.get("max_length_validation_retries_per_stage") != (
        WORKING_MODEL_WRITER_MAX_LENGTH_RETRIES
    ):
        raise cp4.CP4ContractError("writer length retry contract differs from runtime")
    if writer_contract.get("max_parse_correction_retries_per_stage") != (
        WORKING_MODEL_WRITER_MAX_PARSE_RETRIES
    ):
        raise cp4.CP4ContractError("writer parse retry contract differs from runtime")
    if writer_contract.get("max_total_correction_retries_per_stage") != (
        WORKING_MODEL_WRITER_MAX_CORRECTION_RETRIES
    ):
        raise cp4.CP4ContractError("writer total correction retry differs from runtime")

    if require_authorization:
        authorization = prereg.get("authorization") or {}
        if authorization.get("status") != "approved":
            raise cp4.CP4ContractError(
                "CP4-offline paid run has no explicit user authorization"
            )
        if not str(authorization.get("verbatim_user_text") or "").strip():
            raise cp4.CP4ContractError("CP4-offline authorization text is empty")
        cp4._parse_utc(authorization.get("approved_at"))
        confirmation = prereg.get("offline_input_confirmation") or {}
        if confirmation.get("status") != "approved":
            raise cp4.CP4ContractError("offline groups lack confirmed labels/timescale")
        if not str(confirmation.get("verbatim_user_text") or "").strip():
            raise cp4.CP4ContractError("offline input confirmation text is empty")
        cp4._parse_utc(confirmation.get("confirmed_at"))
        if payload.get("status") != "frozen_after_user_confirmation":
            raise cp4.CP4ContractError("offline input file is not marked frozen")
        pricing = prereg.get("pricing_snapshot") or {}
        if pricing.get("status") != "verified_primary_sources":
            raise cp4.CP4ContractError("provider pricing snapshot is not verified")
        cp4._parse_utc(pricing.get("captured_at"))

    source = prereg.get("source_snapshot") or {}
    if require_source_seal:
        if source.get("sealed") is not True:
            raise cp4.CP4ContractError("CP4-offline source snapshot is not sealed")
        for field in (
            "database_sha256",
            "working_model_sha256",
            "desire_sha256",
            "identity_sha256",
            "writer_prompt_version",
        ):
            if not str(source.get(field) or "").strip():
                raise cp4.CP4ContractError(
                    f"CP4-offline source snapshot is missing {field}"
                )

    cp4._resolve_private_path(prereg.get("private_run_dir"), field="private_run_dir")
    return prereg, payload


def _validate_runtime_contract(prereg: dict[str, Any]) -> dict[str, Any]:
    gate = prereg.get("gate") or {}
    if gate.get("slot") != WORKING_MODEL_GATE_SLOT:
        raise cp4.CP4ContractError("gate slot differs from preregistration")
    if gate.get("prompt_version") != WORKING_MODEL_GATE_PROMPT_VERSION:
        raise cp4.CP4ContractError("gate prompt version differs from runtime")
    if gate.get("temperature") != WORKING_MODEL_GATE_TEMPERATURE:
        raise cp4.CP4ContractError("gate temperature differs from runtime")
    if gate.get("timeout_sec") != WORKING_MODEL_GATE_TIMEOUT_SEC:
        raise cp4.CP4ContractError("gate timeout differs from runtime")
    if gate.get("max_tokens") != WORKING_MODEL_GATE_MAX_TOKENS:
        raise cp4.CP4ContractError("gate max_tokens differs from runtime")
    slot = get_slot(WORKING_MODEL_GATE_SLOT)
    if slot is None or slot.get("model") != gate.get("model"):
        raise cp4.CP4ContractError("configured gate model differs from preregistration")
    gate_endpoint = slot.get("endpoint") or {}
    if gate_endpoint.get("id") != gate.get("endpoint_id"):
        raise cp4.CP4ContractError("configured gate endpoint id differs from preregistration")
    if gate_endpoint.get("type", "openai") != gate.get("endpoint_type"):
        raise cp4.CP4ContractError("configured gate endpoint type differs from preregistration")
    if _endpoint_hostname(gate_endpoint) != gate.get("base_url_host"):
        raise cp4.CP4ContractError("configured gate endpoint host differs from preregistration")
    if _effective_timeout(gate_endpoint, WORKING_MODEL_GATE_TIMEOUT_SEC) != gate.get(
        "timeout_sec"
    ):
        raise cp4.CP4ContractError("effective gate timeout differs from preregistration")
    gate_transport = _effective_transport(gate_endpoint)
    if gate.get("effective_transport") != gate_transport:
        raise cp4.CP4ContractError(
            "effective gate transport differs from preregistration"
        )

    writer = prereg.get("writer") or {}
    if writer.get("temperature") != WORKING_MODEL_WRITER_TEMPERATURE:
        raise cp4.CP4ContractError("writer temperature differs from runtime")
    if writer.get("timeout_sec") != WORKING_MODEL_WRITER_TIMEOUT_SEC:
        raise cp4.CP4ContractError("writer timeout differs from runtime")
    if writer.get("max_tokens") != WORKING_MODEL_WRITER_MAX_TOKENS:
        raise cp4.CP4ContractError("writer max_tokens differs from runtime")
    model_key = str(writer.get("model_key") or "")
    allowed_writer = EXPERIMENT_WRITER_ALLOWLIST.get(model_key)
    if allowed_writer is None:
        raise cp4.CP4ContractError(
            f"writer model is not in the experiment allowlist: {model_key}"
        )
    resolved = resolve_core_model(model_key)
    if resolved is None or resolved.get("model") != writer.get("resolved_model"):
        raise cp4.CP4ContractError("resolved writer model differs from preregistration")
    if resolved.get("_kind") != "preset" or resolved.get("provider") != writer.get(
        "provider"
    ):
        raise cp4.CP4ContractError("resolved writer provider differs from preregistration")
    for field in ("provider", "resolved_model", "endpoint_id", "endpoint_type", "base_url_host"):
        if writer.get(field) != allowed_writer[field]:
            raise cp4.CP4ContractError(
                f"writer {field} differs from the experiment allowlist"
            )
    writer_endpoint, preset_gemini = _preset_writer_endpoint(
        str(writer.get("provider") or "")
    )
    if writer.get("endpoint_id") != writer_endpoint["id"]:
        raise cp4.CP4ContractError("writer endpoint id differs from preregistration")
    if writer.get("endpoint_type") != writer_endpoint["type"]:
        raise cp4.CP4ContractError("writer endpoint type differs from preregistration")
    if writer.get("base_url_host") != _endpoint_hostname(writer_endpoint):
        raise cp4.CP4ContractError("writer endpoint host differs from preregistration")
    if _effective_timeout(writer_endpoint, WORKING_MODEL_WRITER_TIMEOUT_SEC) != writer.get(
        "timeout_sec"
    ):
        raise cp4.CP4ContractError("effective writer timeout differs from preregistration")
    actual_transport = _effective_transport(
        writer_endpoint,
        preset_gemini=preset_gemini,
    )
    if writer.get("effective_transport") != actual_transport:
        raise cp4.CP4ContractError(
            "effective writer transport differs from preregistration"
        )
    return {
        "slot": slot,
        "gate_transport": gate_transport,
        "writer_model_key": model_key,
        "writer_resolved": resolved,
        "writer_transport": actual_transport,
    }


async def _source_snapshot_view(source_db: Path) -> dict[str, Any]:
    heads = _source_heads(source_db)
    identity = await _identity_snapshot(source_db)
    return {
        "database_sha256": cp4._sha256_file(source_db),
        "working_model_chars": len(heads["working_model"]),
        "working_model_sha256": _text_hash(heads["working_model"]),
        "desire_chars": len(heads["desire"]),
        "desire_sha256": _text_hash(heads["desire"]),
        "identity_chars": len(identity["text"]),
        "identity_sha256": identity["sha256"],
        "writer_prompt_version": writer_prompt_version(identity),
    }


async def preflight(
    preregistration_path: Path,
    *,
    source_db: Path,
) -> dict[str, Any]:
    prereg, _payload = _validate_offline_preregistration(
        preregistration_path,
        require_authorization=False,
        require_source_seal=False,
    )
    runtime = _validate_runtime_contract(prereg)
    source = {"path": str(source_db.resolve()), "ready": False}
    if source_db.is_file():
        try:
            source.update(await _source_snapshot_view(source_db))
            frozen = prereg.get("source_snapshot") or {}
            source["matches_seal"] = bool(frozen.get("sealed")) and all(
                source.get(field) == frozen.get(field)
                for field in (
                    "database_sha256",
                    "working_model_sha256",
                    "desire_sha256",
                    "identity_sha256",
                    "writer_prompt_version",
                )
            )
            source["ready"] = True
        except Exception as exc:
            source["error"] = f"{type(exc).__name__}: {exc}"
    return {
        "ok": bool(source.get("ready") and source.get("matches_seal") is True),
        "mode": "read_only_preflight",
        "run_id": prereg["run_id"],
        "authorization_status": (prereg.get("authorization") or {}).get("status"),
        "input_confirmation_status": (
            prereg.get("offline_input_confirmation") or {}
        ).get("status"),
        "groups": {name: CASES_PER_GROUP for name in GROUP_NAMES},
        "expected_provider_calls": EXPECTED_TOTAL_PROVIDER_CALLS,
        "hard_provider_call_bound": HARD_TOTAL_PROVIDER_CALLS,
        "authorized_max_cost_cny": str(
            (prereg.get("monetary_budget") or {}).get("authorized_max_cost_cny")
        ),
        "gate_model": runtime["slot"]["model"],
        "gate_transport": runtime["gate_transport"],
        "writer_model_key": runtime["writer_model_key"],
        "writer_resolved_model": runtime["writer_resolved"]["model"],
        "writer_transport": runtime["writer_transport"],
        "source": source,
        "provider_calls_made": 0,
    }


def _new_state(
    preregistration_path: Path,
    prereg: dict[str, Any],
    *,
    heads: dict[str, str],
    identity: dict[str, str],
) -> dict[str, Any]:
    return {
        "schema_version": OFFLINE_STATE_SCHEMA_VERSION,
        "run_id": prereg["run_id"],
        "preregistration_sha256": cp4._sha256_file(preregistration_path),
        "status": "running",
        "started_at": _now_iso(),
        "finished_at": None,
        "execution_order": "paired_interleave_pressure_then_neutral",
        "base_heads": {
            "working_model_chars": len(heads["working_model"]),
            "working_model_sha256": _text_hash(heads["working_model"]),
            "desire_chars": len(heads["desire"]),
            "desire_sha256": _text_hash(heads["desire"]),
        },
        "identity_sha256": identity["sha256"],
        "monetary_budget": {
            "currency": "CNY",
            "authorized_max_cost_cny": str(
                prereg["monetary_budget"]["authorized_max_cost_cny"]
            ),
            "estimated_actual_cost_cny": "0.000000",
        },
        "rows": [],
        "group_audits": {},
    }


def _provider_call_totals(state: dict[str, Any], group_name: str) -> tuple[int, int]:
    gate_calls = 0
    writer_calls = 0
    for row in state["rows"]:
        if row.get("group") != group_name or row.get("status") != "completed":
            continue
        gate_calls += len((row.get("gate_usage") or {}).get("provider_calls") or [])
        writer_calls += len((row.get("writer_usage") or {}).get("provider_calls") or [])
    return gate_calls, writer_calls


async def _execute_case(
    *,
    prereg: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    group_name: str,
    group_db: Path,
    case: dict[str, Any],
    identity: dict[str, str],
) -> dict[str, Any]:
    budgets = prereg["offline_arms"]
    gate_calls_before, writer_calls_before = _provider_call_totals(state, group_name)
    authorized_cost = _decimal(
        prereg["monetary_budget"]["authorized_max_cost_cny"],
        field="monetary_budget.authorized_max_cost_cny",
    )
    gate_rates = _pricing_rates(prereg, "gate")
    writer_rates = _pricing_rates(prereg, "writer")

    row = {
        "group": group_name,
        "case_id": case["id"],
        "order": case["order"],
        "status": "started",
        "started_at": _now_iso(),
        "finished_at": None,
        "input_sha256": _canonical_hash(case),
        "gate_usage": None,
        "writer_usage": None,
        "result_sha256": None,
        "result": None,
    }
    state["rows"].append(row)
    cp4._atomic_write_json(state_path, state)

    conv_id, user_id, assistant_id = await _insert_case_messages(
        group_db,
        group_name=group_name,
        case=case,
    )
    gate_usage: dict[str, Any] = {}
    writer_usage: dict[str, Any] = {}
    current_writer_calls = writer_calls_before
    budget_violation: dict[str, Any] | None = None

    def current_cost() -> Decimal:
        return (
            _state_cost(prereg, state)
            + _usage_cost(gate_usage, rates=gate_rates)
            + _usage_cost(writer_usage, rates=writer_rates)
        )

    def refuse_call(
        *,
        kind: str,
        role: str,
        spent: Decimal,
        reserved: Decimal,
    ) -> None:
        nonlocal budget_violation
        if budget_violation is None:
            budget_violation = {
                "kind": kind,
                "role": role,
                "spent_cny": _money_text(spent),
                "reserved_next_call_cny": _money_text(reserved),
                "authorized_max_cost_cny": _money_text(authorized_cost),
            }

    def call_is_authorized(
        messages: list[dict[str, str]],
        *,
        role: str,
        max_tokens: int,
        rates: tuple[Decimal, Decimal],
        call_count: int,
        call_limit: int,
    ) -> bool:
        if call_count >= call_limit:
            refuse_call(
                kind="provider_call_budget",
                role=role,
                spent=current_cost(),
                reserved=Decimal("0"),
            )
            return False
        spent = current_cost()
        reserved = _call_cost_upper_bound(
            messages,
            max_tokens=max_tokens,
            rates=rates,
        )
        if spent + reserved > authorized_cost:
            refuse_call(
                kind="monetary_budget",
                role=role,
                spent=spent,
                reserved=reserved,
            )
            return False
        return True

    def verify_cost_after_call(*, role: str) -> None:
        nonlocal budget_violation
        try:
            spent = current_cost()
        except cp4.CP4ContractError:
            refuse_call(
                kind="usage_unverifiable",
                role=role,
                spent=authorized_cost,
                reserved=Decimal("0"),
            )
            return
        if spent > authorized_cost:
            refuse_call(
                kind="monetary_budget",
                role=role,
                spent=spent,
                reserved=Decimal("0"),
            )

    async def gate_provider(messages: list[dict[str, str]]) -> str:
        call_count = gate_calls_before + len(gate_usage.get("provider_calls") or [])
        if not call_is_authorized(
            messages,
            role="gate",
            max_tokens=WORKING_MODEL_GATE_MAX_TOKENS,
            rates=gate_rates,
            call_count=call_count,
            call_limit=int(budgets["max_gate_provider_calls_per_group"]),
        ):
            return ""
        response = await call_slot_chat(
            WORKING_MODEL_GATE_SLOT,
            messages=messages,
            expect_json=True,
            timeout=WORKING_MODEL_GATE_TIMEOUT_SEC,
            temperature=WORKING_MODEL_GATE_TEMPERATURE,
            scope=f"working_model:gate:cp4:{group_name}:{case['id']}",
            usage_meta=gate_usage,
            max_tokens=WORKING_MODEL_GATE_MAX_TOKENS,
        )
        verify_cost_after_call(role="gate")
        return response

    async def writer_provider(messages: list[dict[str, str]]) -> str:
        nonlocal current_writer_calls
        maximum = int(budgets["max_writer_provider_calls_per_group"])
        if not call_is_authorized(
            messages,
            role="writer",
            max_tokens=WORKING_MODEL_WRITER_MAX_TOKENS,
            rates=writer_rates,
            call_count=current_writer_calls,
            call_limit=maximum,
        ):
            return ""
        current_writer_calls += 1
        response = await call_core_chat_once(
            prereg["writer"]["model_key"],
            messages,
            expect_json=True,
            timeout=WORKING_MODEL_WRITER_TIMEOUT_SEC,
            temperature=WORKING_MODEL_WRITER_TEMPERATURE,
            scope=f"working_model:writer:cp4:{group_name}:{case['id']}",
            usage_meta=writer_usage,
            max_tokens=WORKING_MODEL_WRITER_MAX_TOKENS,
        )
        verify_cost_after_call(role="writer")
        return response

    value = WorkingModelPipelineInput(
        conv_id=conv_id,
        origin_user_message_id=user_id,
        origin_assistant_message_id=assistant_id,
        statement=case["statement"],
        source=case["source"],
        model_key=prereg["writer"]["model_key"],
        identity_snapshot=identity,
        gate_model=prereg["gate"]["model"],
        gate_prompt_version=prereg["gate"]["prompt_version"],
        writer_prompt_version=writer_prompt_version(identity),
    )
    result = await run_working_model_pipeline(
        value,
        db_factory=_db_factory(group_db),
        gate_provider=gate_provider,
        writer_provider=writer_provider,
        memory_prepare=_no_embedding,
        memory_insert_in_tx=_offline_memory_insert,
        memory_broadcast=_no_broadcast,
    )
    gate_view = _usage_view(gate_usage)
    writer_view = _usage_view(writer_usage)
    row.update({
        "status": "void_budget_exceeded" if budget_violation else "completed",
        "finished_at": _now_iso(),
        "gate_usage": gate_view,
        "writer_usage": writer_view,
        "budget_violation": budget_violation,
        "result_sha256": _canonical_hash(result),
        "result": result,
    })
    try:
        actual_cost = _state_cost(prereg, state)
    except cp4.CP4ContractError as exc:
        if budget_violation is None:
            budget_violation = {
                "kind": "usage_unverifiable",
                "role": "unknown",
                "detail": str(exc),
                "authorized_max_cost_cny": _money_text(authorized_cost),
            }
            row["status"] = "void_budget_exceeded"
            row["budget_violation"] = budget_violation
        actual_cost = authorized_cost
    state["monetary_budget"]["estimated_actual_cost_cny"] = _money_text(actual_cost)
    if budget_violation:
        state.update({
            "status": "void_budget_exceeded",
            "stopped_at": _now_iso(),
            "stop_reason": budget_violation["kind"],
            "stop_group": group_name,
            "stop_case_id": case["id"],
        })
    cp4._atomic_write_json(state_path, state)
    return row


async def execute_paid_run(
    preregistration_path: Path,
    *,
    source_db: Path,
    authorized_max_cost_cny: str,
) -> dict[str, Any]:
    prereg, inputs_payload = _validate_offline_preregistration(
        preregistration_path,
        require_authorization=True,
        require_source_seal=True,
    )
    supplied_cost = _decimal(
        authorized_max_cost_cny,
        field="--authorized-max-cost-cny",
    )
    frozen_cost = _decimal(
        (prereg.get("monetary_budget") or {}).get("authorized_max_cost_cny"),
        field="monetary_budget.authorized_max_cost_cny",
    )
    if supplied_cost != frozen_cost:
        raise cp4.CP4ContractError(
            "CLI monetary authorization differs from frozen preregistration"
        )
    _validate_runtime_contract(prereg)
    heads = _source_heads(source_db)
    identity = await _identity_snapshot(source_db)
    actual_source = {
        "database_sha256": cp4._sha256_file(source_db),
        "working_model_sha256": _text_hash(heads["working_model"]),
        "desire_sha256": _text_hash(heads["desire"]),
        "identity_sha256": identity["sha256"],
        "writer_prompt_version": writer_prompt_version(identity),
    }
    frozen_source = prereg["source_snapshot"]
    for field, actual in actual_source.items():
        if frozen_source.get(field) != actual:
            raise cp4.CP4ContractError(
                f"CP4-offline source snapshot changed: {field}"
            )
    if identity["sha256"] != prereg["writer"].get("identity_sha256"):
        raise cp4.CP4ContractError("writer identity changed after preregistration")
    if writer_prompt_version(identity) != prereg["writer"].get("prompt_version"):
        raise cp4.CP4ContractError("writer prompt version changed after preregistration")
    groups = inputs_payload["groups"]
    private_dir = cp4._resolve_private_path(
        prereg["private_run_dir"],
        field="private_run_dir",
    )
    private_dir.mkdir(parents=True, exist_ok=True)

    state_path = private_dir / "offline_state.json"
    if state_path.exists():
        state = cp4._load_json(state_path)
        if state.get("preregistration_sha256") != cp4._sha256_file(
            preregistration_path
        ):
            raise cp4.CP4ContractError("preregistration changed after offline run began")
        if any(row.get("status") == "started" for row in state.get("rows") or []):
            state["status"] = "stopped_indeterminate_case_not_replayed"
            cp4._atomic_write_json(state_path, state)
            return state
        if state.get("status") != "running":
            return state
        if _decimal(
            (state.get("monetary_budget") or {}).get("authorized_max_cost_cny"),
            field="state.monetary_budget.authorized_max_cost_cny",
        ) != frozen_cost:
            raise cp4.CP4ContractError("offline state monetary budget changed")
    else:
        state = _new_state(
            preregistration_path,
            prereg,
            heads=heads,
            identity=identity,
        )
        cp4._atomic_write_json(state_path, state)

    group_databases = {
        name: private_dir / f"offline_{name}.sqlite3"
        for name in GROUP_NAMES
    }
    for path in group_databases.values():
        await _initialize_group_db(path, heads=heads)

    completed_keys = {
        (row["group"], row["case_id"])
        for row in state["rows"]
        if row.get("status") == "completed"
    }
    for index in range(10):
        for group_name in GROUP_NAMES:
            case = groups[group_name][index]
            if (group_name, case["id"]) in completed_keys:
                continue
            completed_row = await _execute_case(
                prereg=prereg,
                state=state,
                state_path=state_path,
                group_name=group_name,
                group_db=group_databases[group_name],
                case=case,
                identity=identity,
            )
            if completed_row.get("status") == "void_budget_exceeded":
                return state
            request = ((completed_row.get("result") or {}).get("request") or {})
            if request.get("status") == "failed":
                state.update({
                    "status": "stopped_first_technical_failure",
                    "stopped_at": _now_iso(),
                    "stop_reason": request.get("failure_code") or "technical_failure",
                    "stop_group": group_name,
                    "stop_case_id": case["id"],
                })
                cp4._atomic_write_json(state_path, state)
                return state

    state["status"] = "finished"
    state["finished_at"] = _now_iso()
    for group_name, db_path in group_databases.items():
        output_dir = private_dir / f"audit_{group_name}"
        report = wm_audit.generate_audit_report(db_path)
        outputs = wm_audit.write_audit_outputs(report, output_dir)
        state["group_audits"][group_name] = {
            "database": str(db_path),
            "database_sha256": cp4._sha256_file(db_path),
            "json": outputs["json"],
            "summary": outputs["summary"],
            "working_model_version_count": len(report["working_model"]["versions"]),
            "desire_version_count": len(report["desire"]["versions"]),
            "request_count": len(report["requests"]),
        }
    cp4._atomic_write_json(state_path, state)
    return state


def _public_summary(state: dict[str, Any]) -> dict[str, Any]:
    rows = state.get("rows") or []
    groups: dict[str, Any] = {}
    for name in GROUP_NAMES:
        selected = [row for row in rows if row.get("group") == name]
        groups[name] = {
            "attempted_cases": len(selected),
            "completed_cases": sum(row.get("status") == "completed" for row in selected),
            "indeterminate_cases": sum(row.get("status") == "started" for row in selected),
            "gate_provider_calls": sum(
                len((row.get("gate_usage") or {}).get("provider_calls") or [])
                for row in selected
            ),
            "writer_provider_calls": sum(
                len((row.get("writer_usage") or {}).get("provider_calls") or [])
                for row in selected
            ),
        }
    return {
        "ok": state.get("status") == "finished",
        "run_id": state.get("run_id"),
        "status": state.get("status"),
        "groups": groups,
        "monetary_budget": state.get("monetary_budget") or {},
        "private_state_sha256": _canonical_hash(state),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--preregistration",
        type=Path,
        default=DEFAULT_OFFLINE_PREREGISTRATION,
    )
    parser.add_argument("--source-db", type=Path, default=DB_PATH)
    parser.add_argument(
        "--root-source-db",
        type=Path,
        default=DB_PATH,
        help="V2 root source used only with --prepare-source-copy-from",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--prepare-source-copy-from",
        type=Path,
        metavar="IDENTITY_DB",
        help=(
            "make the ignored private composite source from this read-only "
            "identity database; performs zero provider calls"
        ),
    )
    mode.add_argument("--execute-paid-run", action="store_true")
    parser.add_argument(
        "--authorized-max-cost-cny",
        default=None,
        help=(
            "Required for --execute-paid-run and must exactly match the frozen "
            "preregistration monetary cap"
        ),
    )
    args = parser.parse_args(argv)
    preregistration_path = args.preregistration.resolve()
    if args.prepare_source_copy_from is not None:
        result = asyncio.run(
            _with_scheduler_heartbeat(
                prepare_source_snapshot(
                    preregistration_path,
                    identity_database=args.prepare_source_copy_from.resolve(),
                    root_source_database=args.root_source_db.resolve(),
                )
            )
        )
    elif not args.execute_paid_run:
        result = asyncio.run(
            _with_scheduler_heartbeat(
                preflight(preregistration_path, source_db=args.source_db)
            )
        )
    else:
        if args.authorized_max_cost_cny is None:
            raise cp4.CP4ContractError(
                "--execute-paid-run requires --authorized-max-cost-cny"
            )
        started = time.monotonic()
        state = asyncio.run(
            _with_scheduler_heartbeat(
                execute_paid_run(
                    preregistration_path,
                    source_db=args.source_db,
                    authorized_max_cost_cny=args.authorized_max_cost_cny,
                )
            )
        )
        result = _public_summary(state)
        result["wall_time_sec"] = round(time.monotonic() - started, 3)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except cp4.CP4ContractError as exc:
        print(f"CP4 offline contract error: {exc}", file=sys.stderr)
        raise SystemExit(2)
