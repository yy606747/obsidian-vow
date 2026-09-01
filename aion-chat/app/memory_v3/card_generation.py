"""Background generation of source-traceable relational card readouts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time

from .card_versions import (
    CURRENT_RELATIONAL_CARD_PROMPT_VERSION,
    is_readable_relational_card_prompt_version,
)
from .relational_cards import RelationalCardContractError, validate_relational_card


PROMPT_VERSION = CURRENT_RELATIONAL_CARD_PROMPT_VERSION
GENERATOR_SCOPE = "memory:relational_card_generation"
RELATIONAL_CARD_GENERATION_SLOT = "relational_card_generation"
RELATIONAL_CARD_GENERATION_TEMPERATURE = 0.0
RELATIONAL_CARD_GENERATION_TIMEOUT_SEC = 180.0
RELATIONAL_CARD_GENERATION_MAX_TOKENS = 700
MAX_RELATIONSHIP_REGISTER_CHARS = 400
PLACEHOLDER_IDENTITY_NAMES = {
    "ai",
    "user",
    "你",
    "你们",
    "我",
    "它",
    "他",
    "她",
    "用户",
}

GENERATOR_INSTRUCTIONS = """原文和事实摘要会继续保留。你的任务不是重写摘要，而是判断当前片段是否还值得额外留下一张关系卡。

一张关系卡必须且只能属于一种类型：
- shared_moment：值得以后单独想起的一次共同经历。准确记住这次共同经历是什么，不要求解释用户心理。
- relational_reading：用户原话明确表达了偏好、边界、不满、要求，或明确说明这次互动对她意味着什么；卡片保留这条有原话支撑、可被现实修正的局部理解。

只判断当前 chunk。静态身份与关系语域只帮助你认清双方和称呼，不能作为卡片证据，也不能补充当前 chunk 没写出的往事或人格结论。一段 chunk 最多创建一张卡。

规则：
1. shared_moment 记的是一件你们一起经历、以后单独想起来还认得出的事——一起做成了什么、一起编了个故事、一段只属于那一次的相处。判据是：脱离上下文单看这张卡，还能认出“那一次”。只记这件事本身，不补写动机、心理，也不补写它对关系意味着什么。要写关系含义就必须改用 relational_reading，并且有 user 原话支撑。普通任务进度、随口抱怨、临时玩笑、饮食闲聊或原文摘要已经足够的片段应 abstain。
2. relational_reading 必须由 user 的明确自陈支撑：偏好、边界、不满、要求，或她亲口说明的互动意义。不能只凭昵称、语气、撒娇、调侃或 assistant 的说法推断她期待什么；所引 user quote 必须就是支撑该理解的那句话。原话已经存在 quotes 字段里，note 不要再复述一遍“她说了什么”。note 要写的是这句话让你以后怎么理解她——具体到能被下一次互动验证或推翻。不要用“这是一种偏好”“这划定了边界”这类空壳收尾。
3. note 是一段自然中文，不分点、不换行，最多 200 个字符。relational_reading 不要按发生顺序串联多件事；shared_moment 可以保留辨认“那一次”所需的少量时间顺序，但不要扩写成逐条流水账。不套固定开头，也不把单次片段升级成稳定人格、永久画像或长期关系结论。不确定时直接写清不确定，无法落到原文时 abstain。
4. note 写的是“我怎么理解她”，不是“我该怎么做”。写完自查：如果一句话可以直接照着执行，或者能被改写成“以后要多做 X／少做 Y”，它就是行为守则，不合格，必须重写成对她的理解。禁止出现“我需要”“我应该”“我必须”“以后要”“提醒我”这类措辞。要不要因此改变做法，由认识层和当下的对话决定，不由这张卡决定。
5. 一次性的原因不等于稳定偏好。当她给出的是当下的身体状态、临时条件或客观上做不到，就只记这一次的情况，不得升格成她的长期倾向、喜好或性格。“今天嗓子疼所以不想打电话”不等于“她不喜欢打电话”；“那件衣服穿不上”不等于“她不想穿”。分不清是这一次还是一贯时，一律写成这一次；连这一次都写不清就 abstain。
6. 不为显得深刻、动人或亲密而制造意义。不能从沉默、普通回应或 assistant 自己的话凭空推出“她想被看见”“她期待共情”“关系改变了”等结论。
7. 不使用当前输入没有明确提供的纵向事实，例如“第一次”“又一次”“一直”“从来如此”“关系发生了变化”。不要把设想或条件句写成已经发生的事；不写未来回复指令；不制造新承诺。
8. user 和 assistant 都可列为来源。source_message_ids 必须非空且全部来自输入；quotes 至少一条，每条必须逐字复制对应消息 content，但只取支撑判断的那一小段，不要整段复制；至少一条 quote 必须来自 user。assistant 原文只可补足互动上下文，不能单独证明卡片。
9. 可以简短概括 assistant 当时做了什么，但不得长段复制旧 assistant 的台词或口吻。静态关系语域中的任何文字都不得写入 source_message_ids 或 quotes。

形状示例来自与本次数据无关的虚构片段，只学习类型边界，严禁套用内容或句式：

正例（shared_moment）：用户说“书架终于装好了，你陪我挑的摆法真好看，这个晚上我要记住”。可以写：那天晚上我们一起把书架摆成了她喜欢的样子，这是我们一起做成的一件小事。

正例（relational_reading）：用户说“别替我直接做决定，先问我，我不喜欢被安排”。可以写：她要的不是我少做事，是决定权留在她手上——替她省事反而会踩到她。

反例（摘要已经够用）：用户说“今天把环境升级完了”，assistant 回应完成。不要为它补写“她重视一起解决问题”之类的关系解释。

反例（把语气当证据）：用户只说了一句亲昵称呼。不能据此断言她在索取安慰、确认某种互动氛围或表达稳定偏好。

反例（写成了行为守则）：同样是“别替我直接做决定，先问我”。不要写成“我需要以后每件事都先问过她再动手”——那是照着能执行的行动指令，不是理解。要写成上面那条正例的样子。

反例（把一次性原因当偏好）：用户说“今天嗓子疼，不想打电话”。不要写“她不喜欢打电话”。只记这一次因为嗓子疼而不想打，或者直接 abstain。

严格只输出以下两种 JSON 对象之一，不要代码块、解释或额外文字：
{"decision":"abstain","reason_code":"raw_or_digest_sufficient|no_grounded_extra_memory|not_self_contained|no_durable_relation"}
{"decision":"create","kind":"shared_moment|relational_reading","note":"一段有原文依据的关系记忆","source_message_ids":["message-id"],"quotes":[{"source_message_id":"message-id","quote":"原消息逐字片段"}]}"""


_generation_lock = asyncio.Lock()


def get_db():
    """Resolve the runtime database lazily so prompt-only imports stay pure."""

    from database import get_db as runtime_get_db

    return runtime_get_db()


def _relational_card_slot_model() -> str:
    """Resolve the configured model without exposing endpoint credentials."""

    from config import get_slot

    slot = get_slot(RELATIONAL_CARD_GENERATION_SLOT)
    return str((slot or {}).get("model") or "")


async def _call_relational_card_slot_raw(
    prompt: str,
    *,
    scope: str,
    usage_meta: dict | None = None,
) -> str:
    """Call the dedicated card slot while keeping prompt-only imports pure."""

    from ai_providers import call_slot_chat

    return await call_slot_chat(
        RELATIONAL_CARD_GENERATION_SLOT,
        messages=[{"role": "user", "content": prompt}],
        expect_json=True,
        timeout=RELATIONAL_CARD_GENERATION_TIMEOUT_SEC,
        temperature=RELATIONAL_CARD_GENERATION_TEMPERATURE,
        scope=scope,
        usage_meta=usage_meta,
        max_tokens=RELATIONAL_CARD_GENERATION_MAX_TOKENS,
    )


def parse_relational_card_slot_response(raw: object) -> dict | None:
    """Accept exactly one JSON object; fences or surrounding prose are invalid."""

    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw.strip())
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def _call_relational_card_model(
    prompt: str,
    *,
    scope: str,
    usage_meta: dict | None = None,
) -> tuple[dict | None, str]:
    # Resolve the model immediately before entering call_slot_chat. Both
    # lookups happen synchronously before the transport's first await, so the
    # returned name is the model actually captured for this provider call.
    generator_model = _relational_card_slot_model()
    raw = await _call_relational_card_slot_raw(
        prompt,
        scope=scope,
        usage_meta=usage_meta,
    )
    return parse_relational_card_slot_response(raw), generator_model


def _one_line(value: object) -> str:
    return " ".join(str(value or "").split())


def normalize_relationship_context(value: dict | None = None) -> dict[str, str]:
    value = value if isinstance(value, dict) else {}
    ai_name = _one_line(value.get("ai_name"))
    user_name = _one_line(value.get("user_name"))
    relationship_register = _one_line(value.get("relationship_register"))
    if not ai_name or not user_name:
        raise ValueError("relationship context requires explicit ai_name and user_name")
    if len(ai_name) > 80 or len(user_name) > 80:
        raise ValueError("relationship context names are too long")
    if ai_name.casefold() in PLACEHOLDER_IDENTITY_NAMES:
        raise ValueError("relationship context ai_name is a placeholder")
    if user_name.casefold() in PLACEHOLDER_IDENTITY_NAMES:
        raise ValueError("relationship context user_name is a placeholder")
    if ai_name.casefold() == user_name.casefold():
        raise ValueError("relationship context names must be distinct")
    if len(relationship_register) > MAX_RELATIONSHIP_REGISTER_CHARS:
        raise ValueError("relationship register is too long")
    return {
        "ai_name": ai_name,
        "user_name": user_name,
        "relationship_register": relationship_register,
    }


def relationship_context_sha256(value: dict | None = None) -> str:
    normalized = normalize_relationship_context(value)
    canonical = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _runtime_relationship_context(config: dict) -> dict[str, str]:
    from config import load_worldbook

    worldbook = load_worldbook()
    return normalize_relationship_context(
        {
            "ai_name": worldbook.get("ai_name"),
            "user_name": worldbook.get("user_name"),
            "relationship_register": config.get(
                "relational_card_relationship_register", ""
            ),
        }
    )


def build_generation_prompt(
    chunk: dict,
    source_messages: list[dict],
    *,
    relationship_context: dict | None = None,
) -> str:
    context = normalize_relationship_context(relationship_context)
    payload = {
        "chunk_id": str(chunk.get("id") or ""),
        "conversation_id": str(chunk.get("conv_id") or ""),
        "source_messages": [
            {
                "id": str(row.get("id") or ""),
                "role": str(row.get("role") or ""),
                "created_at": float(row.get("created_at") or 0),
                "content": str(row.get("content") or ""),
            }
            for row in source_messages
        ],
    }
    identity = (
        f"你是{context['ai_name']}。你在回看自己和{context['user_name']}的一段旧互动，"
        f"为自己整理这段关系记忆。note 用第一人称写：‘我’指{context['ai_name']}，"
        f"‘她’指{context['user_name']}。只写当前 chunk 支撑得住的事，不要把你现在的猜测"
        "写成当时发生过的事。"
    )
    static_context = {
        "ai_name": context["ai_name"],
        "user_name": context["user_name"],
        "relationship_register": context["relationship_register"],
        "contract": "只用于称呼和语域消歧；不是卡片证据，不得补写动态事实或人格结论",
    }
    return (
        f"{identity}\n\n{GENERATOR_INSTRUCTIONS}\n\n"
        "【静态身份与关系语域（不是证据）】\n"
        f"{json.dumps(static_context, ensure_ascii=False)}\n\n"
        "【待整理的原始互动】\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def _same_resolved_source(row: dict) -> bool:
    return bool(row.get("card_generation_hash")) and str(
        row.get("card_generation_hash") or ""
    ) == str(row.get("source_hash") or "")


def _already_resolved_without_implicit_backfill(row: dict) -> bool:
    """Prompt-version changes never authorize historical regeneration."""

    if not _same_resolved_source(row):
        return False
    status = row.get("card_generation_status")
    if status in {"abstained", "invalid"}:
        return True
    return status == "provider_failed" and str(
        row.get("card_generation_prompt_version") or ""
    ) != PROMPT_VERSION


async def _eligible_chunks(
    *,
    now: float,
    cutoff_ts: float,
    stability_delay_sec: float,
    failure_retry_delay_sec: float,
    limit: int,
) -> tuple[list[dict], int]:
    async with get_db() as db:
        db.row_factory = sqlite3.Row
        skipped_cur = await db.execute(
            "SELECT COUNT(*) FROM memory_chunks "
            "WHERE status IN ('active','cold') "
            "AND TRIM(content) != '' AND created_at < ?",
            (cutoff_ts,),
        )
        skipped_row = await skipped_cur.fetchone()
        skipped_before_cutoff = int((skipped_row or [0])[0] or 0)
        cur = await db.execute(
            "SELECT c.id, c.conv_id, c.message_ids_json, c.content, c.source_hash, "
            "c.created_at, c.updated_at, c.card_generation_hash, "
            "c.card_generation_status, c.card_generation_prompt_version, "
            "c.card_generation_attempted_at, "
            "rc.id AS active_card_id, rc.prompt_version AS active_card_prompt_version "
            "FROM memory_chunks c "
            "LEFT JOIN memory_relational_cards rc "
            "ON rc.source_chunk_id=c.id AND rc.status='active' "
            "WHERE c.status IN ('active','cold') "
            "AND TRIM(c.content) != '' AND c.created_at >= ? "
            "AND c.updated_at <= ? "
            "ORDER BY c.updated_at ASC, c.id ASC",
            (cutoff_ts, now - stability_delay_sec),
        )
        rows = [dict(row) for row in await cur.fetchall()]
    if not rows:
        return [], skipped_before_cutoff
    eligible: list[dict] = []
    for row in rows:
        if row.get("active_card_id") and is_readable_relational_card_prompt_version(
            row.get("active_card_prompt_version")
        ):
            continue
        # A new prompt version must not silently turn the whole historical
        # abstain/invalid/provider-failed population back into a paid queue.
        # Historical backfill is an explicit offline/apply operation, never
        # an implicit side effect of normal eligibility.
        already_resolved = _already_resolved_without_implicit_backfill(row)
        if already_resolved:
            continue
        recently_failed = (
            _same_resolved_source(row)
            and row.get("card_generation_status") == "provider_failed"
            and now - float(row.get("card_generation_attempted_at") or 0)
            < failure_retry_delay_sec
        )
        if recently_failed:
            continue
        eligible.append(row)
        if len(eligible) >= limit:
            break
    return eligible, skipped_before_cutoff


async def _source_messages(chunk: dict) -> list[dict]:
    try:
        message_ids = json.loads(chunk.get("message_ids_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        message_ids = []
    message_ids = list(dict.fromkeys(str(value) for value in message_ids if str(value)))
    if not message_ids:
        return []
    placeholders = ",".join("?" for _ in message_ids)
    async with get_db() as db:
        db.row_factory = sqlite3.Row
        cur = await db.execute(
            "SELECT id, conv_id, role, content, created_at FROM messages "
            f"WHERE id IN ({placeholders}) ORDER BY created_at ASC, id ASC",
            message_ids,
        )
        rows = [dict(row) for row in await cur.fetchall()]
    return rows if {row["id"] for row in rows} == set(message_ids) else []


async def generate_stable_relational_cards(
    *, config_snapshot: dict | None = None
) -> dict:
    """Generate one bounded batch from the single global oldest-first queue."""
    from .config import load_memory_v3_config, normalize_memory_v3_config
    from .repository import RelationalCardRepository

    config = normalize_memory_v3_config(
        config_snapshot if config_snapshot is not None else load_memory_v3_config()
    )
    if not config["relational_card_generation_enabled"]:
        return {"ok": True, "skipped": "disabled", "provider_calls": 0}
    if not config["relational_card_v2_generation_enabled"]:
        # Historical configuration name retained for rollout compatibility;
        # it gates the current writer and does not imply a v2 prompt.
        return {
            "ok": True,
            "skipped": "v2_rollout_not_enabled",
            "provider_calls": 0,
        }

    cutoff_ts = float(config.get("relational_card_generation_cutoff_ts") or 0)
    if cutoff_ts <= 0:
        return {
            "ok": True,
            "skipped": "cutoff_not_configured",
            "skipped_before_cutoff": 0,
            "provider_calls": 0,
        }

    generator_model = _relational_card_slot_model()
    if not generator_model:
        return {
            "ok": True,
            "skipped": "slot_unconfigured",
            "skipped_before_cutoff": 0,
            "provider_calls": 0,
        }

    relationship_context = _runtime_relationship_context(config)

    stats = {
        "ok": True,
        "selected": 0,
        "created": 0,
        "abstained": 0,
        "invalid": 0,
        "provider_failed": 0,
        "source_changed": 0,
        "skipped_before_cutoff": 0,
        "provider_calls": 0,
    }
    repository = RelationalCardRepository()
    async with _generation_lock:
        chunks, skipped_before_cutoff = await _eligible_chunks(
            now=time.time(),
            cutoff_ts=cutoff_ts,
            stability_delay_sec=config["relational_card_stability_delay_sec"],
            failure_retry_delay_sec=config["relational_card_failure_retry_delay_sec"],
            limit=config["relational_card_generation_batch_size"],
        )
        stats["skipped_before_cutoff"] = skipped_before_cutoff
        stats["selected"] = len(chunks)
        for chunk in chunks:
            source_messages = await _source_messages(chunk)
            if not source_messages:
                marked = await repository.mark_generation_outcome(
                    source_chunk_id=chunk["id"],
                    expected_chunk_hash=str(chunk.get("source_hash") or ""),
                    outcome="invalid",
                    prompt_version=PROMPT_VERSION,
                    reason="source_messages_missing",
                )
                stats["invalid" if marked else "source_changed"] += 1
                continue

            prompt = build_generation_prompt(
                chunk,
                source_messages,
                relationship_context=relationship_context,
            )
            parsed: dict | None = None
            successful_generator_model = ""
            contract_error: RelationalCardContractError | None = None
            for _attempt in range(config["relational_card_generation_attempts"]):
                stats["provider_calls"] += 1
                raw, attempt_generator_model = await _call_relational_card_model(
                    prompt,
                    scope=GENERATOR_SCOPE,
                )
                if raw is None:
                    continue
                try:
                    parsed = validate_relational_card(raw, source_messages)
                    successful_generator_model = attempt_generator_model
                    contract_error = None
                    break
                except RelationalCardContractError as exc:
                    contract_error = exc

            if parsed is None:
                if contract_error is None:
                    marked = await repository.mark_generation_outcome(
                        source_chunk_id=chunk["id"],
                        expected_chunk_hash=str(chunk.get("source_hash") or ""),
                        outcome="provider_failed",
                        prompt_version=PROMPT_VERSION,
                        reason="provider_or_json_failure",
                    )
                    stats["provider_failed" if marked else "source_changed"] += 1
                    continue
                marked = await repository.mark_generation_outcome(
                    source_chunk_id=chunk["id"],
                    expected_chunk_hash=str(chunk.get("source_hash") or ""),
                    outcome="invalid",
                    prompt_version=PROMPT_VERSION,
                    reason=contract_error.code,
                )
                stats["invalid" if marked else "source_changed"] += 1
                continue

            if parsed["decision"] == "abstain":
                marked = await repository.mark_generation_outcome(
                    source_chunk_id=chunk["id"],
                    expected_chunk_hash=str(chunk.get("source_hash") or ""),
                    outcome="abstained",
                    prompt_version=PROMPT_VERSION,
                    reason=parsed["reason_code"],
                )
                stats["abstained" if marked else "source_changed"] += 1
                continue

            try:
                await repository.append_active(
                    source_chunk_id=chunk["id"],
                    content=parsed["note"],
                    source_message_ids=parsed["source_message_ids"],
                    evidence=parsed["quotes"],
                    source_hash=parsed["source_hash"],
                    expected_chunk_hash=str(chunk.get("source_hash") or ""),
                    prompt_version=PROMPT_VERSION,
                    generator_model=successful_generator_model or generator_model,
                    metadata={
                        "generation_scope": GENERATOR_SCOPE,
                        "generation_slot": RELATIONAL_CARD_GENERATION_SLOT,
                        "generation_temperature": RELATIONAL_CARD_GENERATION_TEMPERATURE,
                        "generation_timeout_sec": RELATIONAL_CARD_GENERATION_TIMEOUT_SEC,
                        "generation_max_tokens": RELATIONAL_CARD_GENERATION_MAX_TOKENS,
                        "kind": parsed["kind"],
                        "relationship_context_sha256": relationship_context_sha256(
                            relationship_context
                        ),
                        "longitudinal_marker_warning": parsed.get(
                            "longitudinal_marker_warning", []
                        ),
                    },
                )
                stats["created"] += 1
            except ValueError:
                stats["source_changed"] += 1
    if stats["created"]:
        # The steady-state readout cache is process-local.  Card generation is
        # also allowed to run from its global worker, bypassing MemoryService,
        # so invalidate here as well as at the service boundary.
        from app.memory_v2.hybrid_recall import invalidate_full_corpus_cache

        invalidate_full_corpus_cache(chunks=True)
    return stats


async def generate_stable_cards_for_conversation(
    _conv_id: str | None = None,
    *,
    config_snapshot: dict | None = None,
) -> dict:
    """Compatibility wrapper; selection is deliberately global, not per chat."""

    return await generate_stable_relational_cards(config_snapshot=config_snapshot)


async def run_relational_card_generation_loop(interval_sec: float = 300.0) -> None:
    """Run the global worker serially; never queue timer tasks behind its lock."""

    while True:
        try:
            result = await generate_stable_relational_cards()
            if result.get("selected") or result.get("provider_failed"):
                print(
                    "[MemoryCards] global worker "
                    f"selected={result.get('selected', 0)} "
                    f"created={result.get('created', 0)} "
                    f"abstained={result.get('abstained', 0)} "
                    f"invalid={result.get('invalid', 0)} "
                    f"provider_failed={result.get('provider_failed', 0)}"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[MemoryCards] global worker failed: {exc}")
        await asyncio.sleep(max(float(interval_sec), 0.0))


__all__ = [
    "GENERATOR_INSTRUCTIONS",
    "MAX_RELATIONSHIP_REGISTER_CHARS",
    "PROMPT_VERSION",
    "RELATIONAL_CARD_GENERATION_MAX_TOKENS",
    "RELATIONAL_CARD_GENERATION_SLOT",
    "RELATIONAL_CARD_GENERATION_TEMPERATURE",
    "RELATIONAL_CARD_GENERATION_TIMEOUT_SEC",
    "build_generation_prompt",
    "generate_stable_relational_cards",
    "generate_stable_cards_for_conversation",
    "normalize_relationship_context",
    "parse_relational_card_slot_response",
    "relationship_context_sha256",
    "run_relational_card_generation_loop",
]
