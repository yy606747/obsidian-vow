"""Bounded, source-traceable rolling context for the most recent relationship window."""

from __future__ import annotations

import asyncio
from app.background_tasks import create_tracked_task
import json
import re
import time
from collections.abc import Iterable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite

from database import get_db

from .config import load_memory_v3_config, normalize_memory_v3_config
from .provenance import source_hash_for_messages
from .repository import InjectionEventRepository, TimelineRepository


PROMPT_VERSION = "recent-timeline-v4"
GENERATOR_SCOPE = "memory:recent_timeline"
MAX_SOURCE_MESSAGES = 240
MAX_SOURCE_PROMPT_CHARS = 32_000
MAX_SOURCE_MESSAGE_CHARS = 900
MAX_ENTRIES = 6
MAX_ENTRY_CHARS = 240
TIMELINE_TZ = ZoneInfo("Asia/Shanghai")

_WEEKDAYS_ZH = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
_VOLATILE_TIME_MARKERS = (
    "今天",
    "昨天",
    "前天",
    "明天",
    "今早",
    "今晚",
    "昨晚",
    "刚才",
    "下周",
)
_NON_TEMPORAL_NATURE_PAIR_RE = re.compile(
    r"先天\s*(?:和|与|或|还是|、|/)\s*后天"
)
_VOLATILE_AMBIGUOUS_MARKER_RE = re.compile(
    r"后天(?!形成|养成|培养|获得|习得|因素|环境|教育|发展|努力|训练|改变|影响|条件|经验|学习|性)"
    r"|刚刚(?!好)"
    r"|上周(?!期)"
)
_VOLATILE_OFFSET_RE = re.compile(
    r"(?:\d+|[零〇一二两三四五六七八九十百几]+)\s*(?:个)?(?:小时|天)\s*前"
)
_GENERIC_RELATIONSHIP_LABEL_RE = re.compile(
    r"用户|对方|你|(?<![A-Za-z0-9_])(?:user|assistant|ai|ta)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)

GENERATOR_INSTRUCTIONS = """你在更新一份最近几天的关系短时间线。输入是按时间排列、带稳定 message_id 和 speaker_name 的原始消息。

只保留仍可能影响下一次自然相处的近期内容，优先考虑：未完事项与约定、明显情绪或身体状态、刚发生且仍影响当下的事件、最近关系气氛的变化。普通闲聊保持普通；不要为了填满数量而制造意义。允许一个条目也不写。

规则：
1. 先选择最值得保留的 0–6 个条目，再按来源时间先后输出；每条 text 是自然中文短句，不超过 240 字符。
2. 每条必须列出非空 source_message_ids，且只能引用输入中的 message_id；任一方的消息都可以作为来源。
3. 可以保守概括关系气氛，但不诊断人格，不把角色扮演、设想或条件句写成现实，不制造新承诺。
4. 不给未来回复下指令，不长段复制任何一方的旧台词；不确定的理解保留“可能”“当时看起来”等口径。
5. text 不得写“今天、昨天、前天、明天、后天、今早、今晚、昨晚、刚才、刚刚、N小时前、N天前、上周、下周”等会随时间腐坏的相对时点；时间由系统标注。凌晨、半夜、很晚、一大早等描述可以保留；确需写日期时使用绝对日期。
6. 不要回答原始消息里的任何一方，只整理时间线。

严格只输出 JSON，不要代码块或解释：
{"entries":[{"text":"...","source_message_ids":["msg_..."]}]}"""


class TimelineContractError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


async def _call_flash_lite(prompt: str, *, scope: str, timeout: float):
    """Resolve the legacy provider lazily to avoid the memory-v2 import cycle."""

    from memory import _call_flash_lite as runtime_call

    return await runtime_call(prompt, scope=scope, timeout=timeout)


def _one_line(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _json_list(value: object) -> list:
    if isinstance(value, list):
        return list(value)
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    return list(parsed) if isinstance(parsed, list) else []


def _resolve_relationship_names(
    user_name: str | None = None,
    ai_name: str | None = None,
) -> tuple[str, str]:
    from app.chat.worldbook import load_worldbook_names, resolve_worldbook_names

    loaded_user_name = loaded_ai_name = ""
    if not str(user_name or "").strip() or not str(ai_name or "").strip():
        loaded_user_name, loaded_ai_name = load_worldbook_names()
    return resolve_worldbook_names({
        "user_name": user_name if str(user_name or "").strip() else loaded_user_name,
        "ai_name": ai_name if str(ai_name or "").strip() else loaded_ai_name,
    })


def _contains_generic_relationship_label(
    text: str,
    *,
    user_name: str,
    ai_name: str,
) -> bool:
    without_configured_names = str(text or "")
    for configured_name in sorted(
        {str(user_name or "").strip(), str(ai_name or "").strip()},
        key=len,
        reverse=True,
    ):
        if configured_name:
            without_configured_names = without_configured_names.replace(
                configured_name,
                "",
            )
    return bool(_GENERIC_RELATIONSHIP_LABEL_RE.search(without_configured_names))


def _contains_assistant_excerpt(text: str, source_messages: Iterable[dict]) -> bool:
    if len(text) < 30:
        return False
    assistant_contents = [
        str(message.get("content") or "")
        for message in source_messages
        if message.get("role") == "assistant"
    ]
    for start in range(0, len(text) - 29):
        fragment = text[start:start + 30]
        if any(fragment in content for content in assistant_contents):
            return True
    return False


def _contains_volatile_time(text: str) -> bool:
    normalized = _NON_TEMPORAL_NATURE_PAIR_RE.sub("", _one_line(text))
    return (
        any(marker in normalized for marker in _VOLATILE_TIME_MARKERS)
        or bool(_VOLATILE_AMBIGUOUS_MARKER_RE.search(normalized))
        or bool(_VOLATILE_OFFSET_RE.search(normalized))
    )


def _timeline_datetime(timestamp: float) -> datetime:
    return datetime.fromtimestamp(float(timestamp), TIMELINE_TZ)


def _natural_window_start(now: float) -> float:
    today_start = _timeline_datetime(now).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    return (today_start - timedelta(days=2)).timestamp()


def _format_prompt_time(timestamp: float) -> str:
    return _timeline_datetime(timestamp).strftime("%Y-%m-%d %H:%M")


def _format_clock(timestamp: float) -> str:
    return _timeline_datetime(timestamp).strftime("%H:%M")


def _format_day_with_weekday(value: datetime) -> str:
    return f"{value.month}月{value.day}日 {_WEEKDAYS_ZH[value.weekday()]}"


def _format_now(value: datetime) -> str:
    return f"{_format_day_with_weekday(value)} {value.strftime('%H:%M')}"


def _entry_anchor_ts(
    source_message_ids: Iterable[str],
    source_created_at: dict[str, float],
) -> float | None:
    source_times = [
        float(source_created_at[message_id])
        for message_id in source_message_ids
        if message_id in source_created_at
    ]
    return min(source_times) if source_times else None


def validate_timeline_payload(
    payload: dict,
    *,
    source_messages: list[dict],
    user_name: str | None = None,
    ai_name: str | None = None,
) -> tuple[list[dict], dict]:
    user_name, ai_name = _resolve_relationship_names(user_name, ai_name)
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        raise TimelineContractError("bad_shape", "timeline output requires entries list")
    raw_entries = payload["entries"]
    if len(raw_entries) > MAX_ENTRIES:
        raise TimelineContractError("too_many_entries", "timeline returned too many entries")
    allowed_ids = {str(message.get("id") or "") for message in source_messages}
    entries: list[dict] = []
    seen_text: set[str] = set()
    diagnostics = {"volatile_time_dropped": 0}
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise TimelineContractError("bad_entry", "timeline entry must be an object")
        text = _one_line(raw.get("text"))
        if not text or len(text) > MAX_ENTRY_CHARS:
            raise TimelineContractError("bad_entry_text", "timeline entry text is invalid")
        if text in seen_text:
            continue
        source_ids = list(dict.fromkeys(
            str(value or "").strip()
            for value in (raw.get("source_message_ids") or [])
            if str(value or "").strip()
        ))
        if not source_ids or any(message_id not in allowed_ids for message_id in source_ids):
            raise TimelineContractError("bad_source", "timeline entry cites unknown source")
        if _contains_assistant_excerpt(text, source_messages):
            raise TimelineContractError(
                "assistant_excerpt",
                "timeline copied a long assistant excerpt",
            )
        if _contains_generic_relationship_label(
            text,
            user_name=user_name,
            ai_name=ai_name,
        ):
            raise TimelineContractError(
                "generic_relationship_label",
                "timeline entry used a generic relationship label",
            )
        if _contains_volatile_time(text):
            diagnostics["volatile_time_dropped"] += 1
            continue
        entries.append({"text": text, "source_message_ids": source_ids})
        seen_text.add(text)
    return entries, diagnostics


def build_generation_prompt(
    source_messages: list[dict],
    *,
    now: float,
    window_start: float,
    user_name: str | None = None,
    ai_name: str | None = None,
) -> str:
    user_name, ai_name = _resolve_relationship_names(user_name, ai_name)
    payload = {
        "timezone": "Asia/Shanghai",
        "now": _format_prompt_time(now),
        "window_start": _format_prompt_time(window_start),
        "window_end": _format_prompt_time(now),
        "source_messages": [
            {
                "message_id": str(message.get("id") or ""),
                "speaker_name": (
                    user_name if message.get("role") == "user" else ai_name
                ),
                "time": _timeline_datetime(
                    float(message.get("created_at") or 0)
                ).strftime("%m-%d %H:%M"),
                "content": str(message.get("content") or "")[:MAX_SOURCE_MESSAGE_CHARS],
            }
            for message in source_messages
        ],
    }
    return (
        f"你在整理{ai_name}和{user_name}之间的近期互动。"
        f"每条原始消息已经用 speaker_name 标明说话人。"
        f"条目需要指代双方时，只用{ai_name}、{user_name}或自然的我/她；"
        "不能使用协议角色名或泛称。\n\n"
        f"{GENERATOR_INSTRUCTIONS}\n\n"
        f"【近期原始消息】\n{json.dumps(payload, ensure_ascii=False)}"
    )


async def _recent_source_messages(*, now: float, hours: int) -> tuple[list[dict], dict]:
    window_start = _natural_window_start(now)
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, conv_id, role, content, created_at FROM ("
            "SELECT id, conv_id, role, content, created_at FROM messages "
            "WHERE role IN ('user','assistant') AND created_at>=? AND created_at<=? "
            "ORDER BY created_at DESC, id DESC LIMIT ?"
            ") ORDER BY created_at ASC, id ASC",
            (window_start, now, MAX_SOURCE_MESSAGES),
        )
        rows = [dict(row) for row in await cur.fetchall()]

    selected_reversed: list[dict] = []
    used_chars = 0
    for row in reversed(rows):
        cost = min(len(str(row.get("content") or "")), MAX_SOURCE_MESSAGE_CHARS) + 96
        if selected_reversed and used_chars + cost > MAX_SOURCE_PROMPT_CHARS:
            break
        selected_reversed.append(row)
        used_chars += cost
    selected = list(reversed(selected_reversed))
    return selected, {
        "requested_window_start_ts": window_start,
        "source_truncated": len(selected) < len(rows),
        "source_rows_before_char_budget": len(rows),
        "source_rows_used": len(selected),
        "source_prompt_chars_estimate": used_chars,
        "configured_hours_ignored_for_window": int(hours),
    }


async def _messages_by_ids(message_ids: Iterable[str]) -> list[dict]:
    ids = list(dict.fromkeys(str(value or "") for value in message_ids if str(value or "")))
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, conv_id, role, content, created_at FROM messages "
            f"WHERE id IN ({placeholders}) ORDER BY created_at ASC, id ASC",
            ids,
        )
        return [dict(row) for row in await cur.fetchall()]


def build_timeline_prompt_block(
    timeline: dict,
    *,
    visible_message_ids: Iterable[str],
    source_created_at: dict[str, float],
    reference_ts: float,
    max_chars: int,
    user_name: str | None = None,
    ai_name: str | None = None,
) -> dict:
    user_name, ai_name = _resolve_relationship_names(user_name, ai_name)
    entries = [entry for entry in _json_list(timeline.get("entries_json")) if isinstance(entry, dict)]
    visible_ids = {str(value or "") for value in visible_message_ids if str(value or "")}
    reference = _timeline_datetime(reference_ts)
    today = reference.date()
    allowed_dates = {
        today - timedelta(days=2),
        today - timedelta(days=1),
        today,
    }
    candidates: list[dict] = []
    deduped_indices: list[int] = []
    out_of_window_indices: list[int] = []
    missing_timestamp_indices: list[int] = []
    generic_relationship_label_indices: list[int] = []
    for index, entry in enumerate(entries):
        try:
            source_index = int(entry.get("_timeline_index", index))
        except (TypeError, ValueError):
            source_index = index
        source_ids = {
            str(value or "")
            for value in (entry.get("source_message_ids") or [])
            if str(value or "")
        }
        if source_ids and source_ids.issubset(visible_ids):
            deduped_indices.append(source_index)
            continue
        text = _one_line(entry.get("text"))
        if not text:
            continue
        if _contains_generic_relationship_label(
            text,
            user_name=user_name,
            ai_name=ai_name,
        ):
            generic_relationship_label_indices.append(source_index)
            continue
        anchor_ts = _entry_anchor_ts(source_ids, source_created_at)
        if anchor_ts is None:
            missing_timestamp_indices.append(source_index)
            continue
        anchor_date = _timeline_datetime(anchor_ts).date()
        if anchor_date not in allowed_dates:
            out_of_window_indices.append(source_index)
            continue
        candidates.append({
            "index": source_index,
            "text": text,
            "source_message_ids": sorted(source_ids),
            "anchor_ts": anchor_ts,
            "anchor_date": anchor_date,
        })

    header = f"[最近三天的事]（现在 {_format_now(reference)}）"
    footer = (
        "使用规则：把它当柔性背景，不要逐条复述或声称它绝对正确；"
        f"{user_name}眼前的新表达与这里冲突时，始终以眼前表达为准。"
    )

    def render(selected: list[dict]) -> str:
        chronological = sorted(
            selected,
            key=lambda item: (item["anchor_ts"], item["index"]),
        )
        buckets: dict[object, list[dict]] = {}
        for item in chronological:
            buckets.setdefault(item["anchor_date"], []).append(item)
        lines = [header]
        for offset, label in ((2, "前天"), (1, "昨天"), (0, "今天")):
            bucket_date = today - timedelta(days=offset)
            bucket = buckets.get(bucket_date) or []
            if not bucket:
                continue
            bucket_dt = datetime.combine(
                bucket_date,
                datetime.min.time(),
                tzinfo=TIMELINE_TZ,
            )
            title = label if offset == 0 else f"{label}（{_format_day_with_weekday(bucket_dt)}）"
            lines.extend(["", title])
            lines.extend(
                f"· {_format_clock(item['anchor_ts'])} {item['text']}"
                for item in bucket
            )
        lines.extend(["", footer])
        return "\n".join(lines)

    selected: list[dict] = []
    budget_dropped_indices: list[int] = []
    for item in sorted(
        candidates,
        key=lambda candidate: (candidate["anchor_ts"], candidate["index"]),
        reverse=True,
    ):
        trial = [*selected, item]
        if len(render(trial)) > max(int(max_chars), 0):
            budget_dropped_indices.append(item["index"])
            continue
        selected = trial
    if not selected:
        return {
            "enabled": False,
            "content": "",
            "entries": [],
            "deduped_indices": deduped_indices,
            "budget_dropped_indices": sorted(budget_dropped_indices),
            "out_of_window_indices": out_of_window_indices,
            "missing_timestamp_indices": missing_timestamp_indices,
            "generic_relationship_label_indices": generic_relationship_label_indices,
            "reason": "all_visible_out_of_window_or_budget_dropped",
        }
    injected = [
        {
            "index": item["index"],
            "text": item["text"],
            "source_message_ids": item["source_message_ids"],
            "anchor_ts": item["anchor_ts"],
        }
        for item in sorted(
            selected,
            key=lambda candidate: (candidate["anchor_ts"], candidate["index"]),
        )
    ]
    content = render(selected)
    return {
        "enabled": True,
        "content": content,
        "entries": injected,
        "deduped_indices": deduped_indices,
        "budget_dropped_indices": sorted(budget_dropped_indices),
        "out_of_window_indices": out_of_window_indices,
        "missing_timestamp_indices": missing_timestamp_indices,
        "generic_relationship_label_indices": generic_relationship_label_indices,
        "rendered_chars": len(content),
        "reason": "injected",
    }


def _response_overlap_proxy(memory_text: str, response_text: str) -> float:
    left = _one_line(memory_text)
    right = _one_line(response_text)
    if len(left) < 4 or len(right) < 4:
        return 0.0
    grams = {left[index:index + 4] for index in range(len(left) - 3)}
    if not grams:
        return 0.0
    return round(sum(gram in right for gram in grams) / len(grams), 4)


class TimelineService:
    def __init__(self, repository: TimelineRepository | None = None):
        self.repository = repository or TimelineRepository()
        self._task: asyncio.Task | None = None
        self._request_serial = 0
        self._latest_config: dict | None = None

    def start_background_refresh(self, config_snapshot: dict | None = None) -> None:
        config = normalize_memory_v3_config(
            config_snapshot if config_snapshot is not None else load_memory_v3_config()
        )
        self._request_serial += 1
        self._latest_config = config
        if not config["timeline_enabled"]:
            if self._task is not None and not self._task.done():
                self._task.cancel()
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if (
            self._task is None
            or self._task.done()
            or bool(getattr(self._task, "cancelling", lambda: 0)())
        ):
            self._task = create_tracked_task(
                self._run_refresh_loop(),
                name="memory_recent_timeline",
            )

    async def _run_refresh_loop(self) -> None:
        try:
            while True:
                config = normalize_memory_v3_config(self._latest_config)
                latest = await self.repository.latest_attempt()
                if latest is not None:
                    wait_for = (
                        float(config["timeline_generation_min_interval_sec"])
                        - (time.time() - float(latest.get("created_at") or 0))
                    )
                    if wait_for > 0:
                        await asyncio.sleep(wait_for)
                target_serial = self._request_serial
                await self.refresh_now(config_snapshot=self._latest_config)
                if target_serial == self._request_serial:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            # Background timeline failure is fail-open by contract.  A future
            # assistant message will schedule another bounded attempt.
            return

    async def refresh_now(self, *, config_snapshot: dict | None = None) -> dict:
        config = normalize_memory_v3_config(
            config_snapshot if config_snapshot is not None else load_memory_v3_config()
        )
        if not config["timeline_enabled"]:
            return {"status": "disabled", "provider_calls": 0}

        now = time.time()
        source_messages, source_meta = await _recent_source_messages(
            now=now,
            hours=config["timeline_hours"],
        )
        source_hash = source_hash_for_messages(source_messages)
        active = await self.repository.active()
        if (
            active
            and str(active.get("source_hash") or "") == source_hash
            and str(active.get("prompt_version") or "") == PROMPT_VERSION
        ):
            return {"status": "unchanged", "provider_calls": 0, "timeline_id": active["id"]}

        window_start = float(source_meta["requested_window_start_ts"])
        source_ids = [str(message.get("id") or "") for message in source_messages]
        metadata = {
            **source_meta,
            "generation_scope": GENERATOR_SCOPE,
            "configured_hours": config["timeline_hours"],
            "configured_hours_affects_window": False,
            "configured_max_chars": config["timeline_max_chars"],
        }
        if not source_messages:
            created = await self.repository.append_active(
                window_start_ts=window_start,
                window_end_ts=now,
                entries=[],
                source_message_ids=[],
                source_hash=source_hash,
                prompt_version=PROMPT_VERSION,
                generator_model=None,
                metadata={**metadata, "empty_window": True},
            )
            return {"status": "empty", "provider_calls": 0, "timeline_id": created["id"]}

        user_name, ai_name = _resolve_relationship_names()
        prompt = build_generation_prompt(
            source_messages,
            now=now,
            window_start=window_start,
            user_name=user_name,
            ai_name=ai_name,
        )
        deadline = time.monotonic() + config["timeline_generation_timeout_sec"]
        entries: list[dict] | None = None
        validation_diagnostics: dict = {}
        contract_error: TimelineContractError | None = None
        provider_calls = 0
        for _attempt in range(config["timeline_generation_attempts"]):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            provider_calls += 1
            try:
                raw = await asyncio.wait_for(
                    _call_flash_lite(
                        prompt,
                        scope=GENERATOR_SCOPE,
                        timeout=remaining,
                    ),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                break
            if raw is None:
                continue
            try:
                entries, validation_diagnostics = validate_timeline_payload(
                    raw,
                    source_messages=source_messages,
                    user_name=user_name,
                    ai_name=ai_name,
                )
                contract_error = None
                break
            except TimelineContractError as exc:
                contract_error = exc

        if entries is None:
            reason = contract_error.code if contract_error else "provider_or_json_failure"
            invalid = await self.repository.append_invalid(
                window_start_ts=window_start,
                window_end_ts=now,
                source_message_ids=source_ids,
                source_hash=source_hash,
                prompt_version=PROMPT_VERSION,
                failure_reason=reason,
                generator_model="memory_digest_slot",
                metadata=metadata,
            )
            return {
                "status": "failed",
                "reason": reason,
                "provider_calls": provider_calls,
                "timeline_id": invalid["id"],
            }

        created = await self.repository.append_active(
            window_start_ts=window_start,
            window_end_ts=now,
            entries=entries,
            source_message_ids=source_ids,
            source_hash=source_hash,
            prompt_version=PROMPT_VERSION,
            generator_model="memory_digest_slot",
            metadata={**metadata, **validation_diagnostics},
        )
        return {
            "status": "created" if created.get("created") else "unchanged",
            "provider_calls": provider_calls,
            "timeline_id": created["id"],
            "entry_count": len(entries),
        }

    async def prompt_context(
        self,
        *,
        visible_messages: Iterable[dict],
        config_snapshot: dict | None = None,
        now: float | None = None,
        user_name: str | None = None,
    ) -> dict:
        config = normalize_memory_v3_config(
            config_snapshot if config_snapshot is not None else load_memory_v3_config()
        )
        if not config["timeline_enabled"]:
            return {"status": "disabled", "block": "", "entries": []}
        active = await self.repository.active()
        if active is None:
            return {"status": "missing", "block": "", "entries": []}
        current = float(now if now is not None else time.time())
        natural_window_start = _natural_window_start(current)
        if float(active.get("window_end_ts") or 0) < natural_window_start:
            return {
                "status": "stale",
                "timeline_id": active["id"],
                "block": "",
                "entries": [],
            }

        source_ids = _json_list(active.get("source_message_ids_json"))
        source_rows = await _messages_by_ids(source_ids)
        if (
            len(source_rows) != len(set(str(value) for value in source_ids))
            or source_hash_for_messages(source_rows) != str(active.get("source_hash") or "")
        ):
            return {
                "status": "source_changed",
                "timeline_id": active["id"],
                "block": "",
                "entries": [],
            }

        source_created_at = {
            str(row.get("id") or ""): float(row.get("created_at") or 0)
            for row in source_rows
        }
        entry_cutoff = natural_window_start
        current_entries: list[dict] = []
        stale_entry_indices: list[int] = []
        for index, entry in enumerate(_json_list(active.get("entries_json"))):
            if not isinstance(entry, dict):
                continue
            entry_source_ids = [
                str(value or "")
                for value in (entry.get("source_message_ids") or [])
                if str(value or "")
            ]
            entry_anchor_ts = _entry_anchor_ts(entry_source_ids, source_created_at)
            if entry_source_ids and (
                entry_anchor_ts is None or entry_anchor_ts < entry_cutoff
            ):
                stale_entry_indices.append(index)
                continue
            current_entries.append({**entry, "_timeline_index": index})
        visible_ids = [
            str(message.get("id") or "")
            for message in visible_messages
            if str(message.get("id") or "")
        ]
        rendered = build_timeline_prompt_block(
            {**active, "entries_json": json.dumps(current_entries, ensure_ascii=False)},
            visible_message_ids=visible_ids,
            source_created_at=source_created_at,
            reference_ts=current,
            max_chars=config["timeline_max_chars"],
            user_name=user_name,
        )
        return {
            "status": "injected" if rendered["enabled"] else rendered["reason"],
            "timeline_id": active["id"],
            "timeline_version": active.get("version"),
            "prompt_version": active.get("prompt_version"),
            "block": rendered["content"],
            "entries": rendered["entries"],
            "rendered_chars": rendered.get("rendered_chars", 0),
            "deduped_indices": rendered["deduped_indices"],
            "budget_dropped_indices": rendered["budget_dropped_indices"],
            "out_of_window_indices": rendered["out_of_window_indices"],
            "missing_timestamp_indices": rendered["missing_timestamp_indices"],
            "generic_relationship_label_indices": rendered[
                "generic_relationship_label_indices"
            ],
            "stale_entry_indices": stale_entry_indices,
        }

    async def record_injection_usage(
        self,
        timeline_meta: dict | None,
        *,
        conv_id: str,
        assistant_message_id: str,
        response_text: str,
    ) -> dict:
        meta = timeline_meta if isinstance(timeline_meta, dict) else {}
        entries = [entry for entry in (meta.get("entries") or []) if isinstance(entry, dict)]
        if meta.get("status") != "injected" or not entries:
            return {"status": "skipped", "count": 0}
        repository = InjectionEventRepository()
        event_ids: list[str] = []
        errors = 0
        for rank, entry in enumerate(entries, 1):
            text = str(entry.get("text") or "")
            try:
                event_ids.append(await repository.record({
                    "request_id": assistant_message_id,
                    "conv_id": conv_id,
                    "assistant_message_id": assistant_message_id,
                    "route": "timeline",
                    "candidate_id": (
                        f"{meta.get('timeline_id')}:entry:{entry.get('index')}"
                    ),
                    "rank": rank,
                    "outcome": "injected",
                    "reason": "recent_timeline",
                    "rendered_chars": len(text),
                    "response_overlap_proxy": _response_overlap_proxy(text, response_text),
                    "past_reference_proxy": int(any(
                        marker in str(response_text or "")
                        for marker in ("之前", "上次", "昨天", "前几天", "记得")
                    )),
                    "metadata": {
                        "timeline_version": meta.get("timeline_version"),
                        "prompt_version": meta.get("prompt_version"),
                        "source_message_ids": entry.get("source_message_ids") or [],
                    },
                }))
            except Exception:
                errors += 1
        return {
            "status": "recorded" if event_ids else "error",
            "count": len(event_ids),
            "event_ids": event_ids,
            "errors": errors,
        }


timeline_service = TimelineService()


__all__ = [
    "GENERATOR_INSTRUCTIONS",
    "PROMPT_VERSION",
    "TimelineContractError",
    "TimelineService",
    "build_generation_prompt",
    "build_timeline_prompt_block",
    "timeline_service",
    "validate_timeline_payload",
]
