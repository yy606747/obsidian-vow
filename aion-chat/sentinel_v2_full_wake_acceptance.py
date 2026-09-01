"""Local acceptance harness for Sentinel V2 full-wake primary runtime.

This script executes the production runtime wiring with local fake ports only:
temporary sqlite, fake websocket broadcast, fake monitor log writer, fake
Sentinel provider, and fake Core stream. It is intended as a loud local
acceptance gate before enabling the full primary path in real runtime config.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import sentinel_runtime
from app.sentinel import build_sentinel_runtime_context
from sentinel_core_wake_adapters import LegacyCoreWakePorts


SENTINEL_V2_FULL_WAKE_ACCEPTANCE_SCHEMA_VERSION = "sentinel_v2_full_wake_acceptance.v1"

LOCAL_ACCEPTANCE_SIDE_EFFECTS = [
    "temp_sqlite_db",
    "fake_websocket_broadcast",
    "fake_core_stream",
    "fake_monitor_log",
]


@dataclass(frozen=True)
class AcceptanceScenario:
    name: str
    runtime_context: Mapping[str, Any]
    judgment: Mapping[str, Any] | None = None
    core_chunks: Sequence[str] | None = None
    provider_error: Exception | None = None
    expected_statuses: Sequence[str] = ()
    expected_roles: Sequence[str] = ()
    expected_stream_calls: int = 0
    expected_toy_commands: Sequence[str] = ()
    expected_logged_toy_commands: Sequence[str] = ()
    expected_toy_delivery_status: str = ""
    expected_toy_delivery_reason: str = ""


class _NoControlSessions:
    async def get_current(self, *, conv_id: str) -> Any | None:
        return None

    async def get_session(self, session_id: str) -> Any | None:
        return None

    async def recent_safety_tombstone(self, conv_id: str) -> Any | None:
        return None


def _wake_judgment(**overrides: Any) -> dict[str, Any]:
    payload = {
        "monitoringlog": "新链路看到用户刚空下来，适合轻轻出现。",
        "summary": "当前像一个低打扰轻唤醒窗口。",
        "score": 8,
        "confidence": 0.82,
        "wake_intent": True,
        "call_core": True,
        "core_reason": "V2 判断她刚忙完，适合轻轻出现。",
        "restraint_reason": "",
        "uncertainty": "不知道她是否愿意展开聊天。",
        "suggested_next_check_sec": 600,
        "tone_hint": "轻轻问一句刚忙完了吗",
    }
    payload.update(overrides)
    return payload


DEFAULT_RUNTIME_CONTEXT = {
    "recent_chat": [{"role": "user", "content": "我刚忙完。"}],
    "last_user_message_age_sec": 3600,
    "last_wake_age_sec": 3600,
    "user_name": "用户",
    "ai_name": "Aion",
}

ACCEPTANCE_SCENARIOS = {
    "success": AcceptanceScenario(
        name="success",
        runtime_context=DEFAULT_RUNTIME_CONTEXT,
        judgment=_wake_judgment(),
        core_chunks=["刚好想到你。"],
        expected_statuses=["core_wake_requested", "core_succeeded"],
        expected_roles=["user", "system", "assistant"],
        expected_stream_calls=1,
    ),
    "toy_command": AcceptanceScenario(
        name="toy_command",
        runtime_context=DEFAULT_RUNTIME_CONTEXT,
        judgment=_wake_judgment(core_reason="V2 判断现在适合用更贴近的方式出现。"),
        core_chunks=["靠近一点 [TOY:2]", "，我在。"],
        expected_statuses=["core_wake_requested", "core_succeeded"],
        expected_roles=["user", "system", "assistant"],
        expected_stream_calls=1,
        expected_logged_toy_commands=["2"],
        expected_toy_delivery_status="gateway_rejected",
        expected_toy_delivery_reason="capability_not_frozen",
    ),
    "core_empty": AcceptanceScenario(
        name="core_empty",
        runtime_context=DEFAULT_RUNTIME_CONTEXT,
        judgment=_wake_judgment(core_reason="V2 判断现在适合轻轻出现，但 Core 返回为空。"),
        core_chunks=[],
        expected_statuses=["core_wake_requested", "core_empty"],
        expected_roles=["user"],
        expected_stream_calls=2,
    ),
    "gate_block": AcceptanceScenario(
        name="gate_block",
        runtime_context={
            **DEFAULT_RUNTIME_CONTEXT,
            "quiet_hours_active": True,
            "clear_sleep": False,
            "device_effect_requested": False,
            "device_effect_allowed": False,
        },
        judgment=_wake_judgment(core_reason="V2 判断想出现，但硬边界应该拦住。"),
        core_chunks=["不应该被调用。"],
        expected_statuses=["sentinel_v2_gate_blocked"],
        expected_roles=["user"],
        expected_stream_calls=0,
    ),
    "provider_failure": AcceptanceScenario(
        name="provider_failure",
        runtime_context=DEFAULT_RUNTIME_CONTEXT,
        provider_error=RuntimeError("fake sentinel provider down"),
        core_chunks=["不应该被调用。"],
        expected_statuses=["sentinel_v2_primary_failed"],
        expected_roles=["user"],
        expected_stream_calls=0,
    ),
}


class _AsyncCursor:
    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor

    async def fetchone(self) -> sqlite3.Row | None:
        return self._cursor.fetchone()

    async def fetchall(self) -> list[sqlite3.Row]:
        return self._cursor.fetchall()


class _AsyncSqliteConn:
    def __init__(self, path: Path) -> None:
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row

    async def __aenter__(self) -> "_AsyncSqliteConn":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self._conn.close()
        return False

    @property
    def row_factory(self) -> Any:
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value: Any) -> None:
        self._conn.row_factory = value

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> _AsyncCursor:
        return _AsyncCursor(self._conn.execute(sql, params))

    async def commit(self) -> None:
        self._conn.commit()


def _init_core_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                model TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                conv_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                attachments TEXT DEFAULT ''
            )
            """
        )
        conn.execute(
            "INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
            ("conv_sentinel", "Sentinel", "mock-model", 1.0, 2.0),
        )
        conn.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            ("msg_user", "conv_sentinel", "user", "我刚忙完", 1.0, "[]"),
        )
        conn.commit()
    finally:
        conn.close()


def _fetch_messages(path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT role, content FROM messages ORDER BY created_at"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


@contextmanager
def _patched_signal_modules() -> Iterator[None]:
    missing = object()
    originals = {
        name: sys.modules.get(name, missing)
        for name in ("location", "activity", "sensing")
    }
    location_module = ModuleType("location")
    location_module.format_location_for_prompt = lambda: "当前位置：本地验收假信号。"
    activity_module = ModuleType("activity")
    activity_module.get_activity_summary_for_prompt = lambda _hours: "近一小时本地验收假活动。"
    sensing_module = ModuleType("sensing")
    sensing_module.format_sensing_for_prompt = lambda **_kwargs: "本地验收假体感信号。"
    sys.modules["location"] = location_module
    sys.modules["activity"] = activity_module
    sys.modules["sensing"] = sensing_module
    try:
        yield
    finally:
        for name, original in originals.items():
            if original is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


@contextmanager
def _patched_runtime(
    *,
    scenario: AcceptanceScenario,
    db_path: Path,
    logs: list[dict[str, Any]],
    broadcasts: list[dict[str, Any]],
    stream_calls: list[dict[str, Any]],
    slot_call_kinds: list[str],
) -> Iterator[None]:
    originals: dict[str, Any] = {}

    def patch(name: str, value: Any) -> None:
        originals[name] = getattr(sentinel_runtime, name)
        setattr(sentinel_runtime, name, value)

    @asynccontextmanager
    async def fake_get_db() -> AsyncIterator[_AsyncSqliteConn]:
        async with _AsyncSqliteConn(db_path) as db:
            yield db

    async def fake_append_and_broadcast(entry: Mapping[str, Any]) -> bool:
        logs.append(dict(entry))
        broadcasts.append({"type": "monitor_log", "data": dict(entry)})
        return True

    async def fake_broadcast(payload: Mapping[str, Any]) -> None:
        broadcasts.append(dict(payload))

    async def fake_last_user_msg_time() -> int:
        return 0

    async def fake_read_runtime_context(**_kwargs: Any) -> dict[str, Any]:
        return build_sentinel_runtime_context(scenario.runtime_context)

    async def fake_read_core_wake_execution_context(**_kwargs: Any) -> dict[str, Any]:
        return {
            "conv_id": "conv_sentinel",
            "model_key": "mock-model",
            "recent_messages": [{"id": "m1", "role": "user", "content": "我刚忙完。"}],
            "last_user_message_age_sec": 3600,
            "user_name": "用户",
            "ai_name": "Aion",
            "toy_capability_allowed": False,
            "toy_capability_reason": "no_active_session",
        }

    async def fake_call_slot_chat(_slot_name: str, *, messages: Sequence[Mapping[str, str]], **_kwargs: Any) -> str:
        prompt_text = "\n".join(str(message.get("content") or "") for message in messages)
        if "wake_intent" in prompt_text:
            slot_call_kinds.append("v2")
            if scenario.provider_error is not None:
                raise scenario.provider_error
            return json.dumps(scenario.judgment or _wake_judgment(), ensure_ascii=False)
        slot_call_kinds.append("legacy")
        return json.dumps({
            "monitoringlog": "旧链路不应该在 primary 验收中被调用。",
            "summary": "legacy path called",
            "score": 1,
            "core_reason": "",
        }, ensure_ascii=False)

    async def fake_stream_core(messages: Sequence[Mapping[str, str]], model_key: str, temperature: Any = None):
        prompt_text = "\n".join(str(message.get("content") or "") for message in messages)
        stream_calls.append({
            "message_count": len(messages),
            "model_key": model_key,
            "temperature": temperature,
            "prompt_text": prompt_text,
        })
        for chunk in scenario.core_chunks if scenario.core_chunks is not None else []:
            yield chunk

    async def fake_sleep(_seconds: float) -> None:
        return None

    clock_value = 1_700_000_000.0

    def fake_clock() -> float:
        nonlocal clock_value
        clock_value += 1
        return clock_value

    ports = LegacyCoreWakePorts(
        db_factory=fake_get_db,
        broadcaster=fake_broadcast,
        core_streamer=fake_stream_core,
        monitor_log_writer=fake_append_and_broadcast,
        clock=fake_clock,
        default_temperature=0.4,
        sleeper=fake_sleep,
        control_session_service_obj=_NoControlSessions(),
    )

    async def empty_relationship_context() -> tuple[str, str]:
        return "", ""

    async def fake_timeline_context(*, visible_message_ids: Sequence[str], now: float | None = None) -> dict[str, Any]:
        return {
            "status": "injected",
            "block": "[最近三天的事]\n· 今天 21:00 用户说忙完了。",
            "entries": [{"text": "用户说忙完了。"}],
        }

    async def skipped_timeline_usage(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "skipped", "count": 0}

    async def disabled_web_search(**_kwargs: Any) -> dict[str, Any]:
        return {"status": "disabled", "block": ""}

    ports.load_vow_prompt_context = empty_relationship_context
    ports.load_working_model_prompt_context = empty_relationship_context
    ports.load_timeline_prompt_context = fake_timeline_context
    ports.record_timeline_injection_usage = skipped_timeline_usage
    ports.prepare_web_search_turn = disabled_web_search
    ports.finalize_web_search_turn = disabled_web_search

    class _NoopToolLedger:
        def __init__(self) -> None:
            self.serial = 0

        def new_invocation_id(self, kind: str) -> str:
            self.serial += 1
            return f"acceptance-{kind}-{self.serial}"

        async def record_model_request(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def record_model_output(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def record_turn(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def record_visible_message(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def record_marker(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    patch("cleanup_old_logs", lambda _keep_days=3: None)
    patch("load_cam_config", lambda: {})
    patch("load_worldbook", lambda: {"user_name": "用户", "ai_name": "Aion"})
    patch("load_chat_status", lambda: {"status": ""})
    patch("load_ai_behavior", lambda: {
        "sentinel_call_core_criteria": "score >= 7 时可以唤醒 Core。",
        "sentinel_wake_threshold": 7,
        "sentinel_v2_provider_enabled": True,
        "sentinel_v2_full_wake_enabled": True,
        "sentinel_v2_full_wake_legacy_fallback_enabled": False,
    })
    patch("async_get_last_user_msg_time", fake_last_user_msg_time)
    patch("read_logs_since", lambda _since: [])
    patch("get_db", fake_get_db)
    patch("append_and_broadcast_monitor_log", fake_append_and_broadcast)
    patch("manager", SimpleNamespace(broadcast=fake_broadcast))
    patch("read_sentinel_runtime_context", fake_read_runtime_context)
    patch("read_core_wake_execution_context", fake_read_core_wake_execution_context)
    patch("build_legacy_core_wake_ports", lambda **_kwargs: ports)
    patch("call_slot_chat", fake_call_slot_chat)
    patch("aiosqlite", SimpleNamespace(Row=sqlite3.Row))
    patch("tool_invocation_ledger", _NoopToolLedger())

    original_timeline_refresh = sentinel_runtime.timeline_service.start_background_refresh
    sentinel_runtime.timeline_service.start_background_refresh = lambda *_args, **_kwargs: None

    try:
        with _patched_signal_modules():
            yield
    finally:
        sentinel_runtime.timeline_service.start_background_refresh = original_timeline_refresh
        for name, original in reversed(list(originals.items())):
            setattr(sentinel_runtime, name, original)


async def run_acceptance_scenario(name: str) -> dict[str, Any]:
    if name not in ACCEPTANCE_SCENARIOS:
        raise ValueError(f"unknown acceptance scenario {name!r}")

    scenario = ACCEPTANCE_SCENARIOS[name]
    logs: list[dict[str, Any]] = []
    broadcasts: list[dict[str, Any]] = []
    stream_calls: list[dict[str, Any]] = []
    slot_call_kinds: list[str] = []
    failures: list[str] = []
    exception: str = ""

    with tempfile.TemporaryDirectory(prefix="sentinel_v2_acceptance_") as tmp:
        db_path = Path(tmp) / "sentinel_v2_full_wake.db"
        _init_core_db(db_path)
        try:
            with _patched_runtime(
                scenario=scenario,
                db_path=db_path,
                logs=logs,
                broadcasts=broadcasts,
                stream_calls=stream_calls,
                slot_call_kinds=slot_call_kinds,
            ):
                await sentinel_runtime.SentinelRuntime()._analyze_and_log()
        except Exception as exc:
            exception = f"{type(exc).__name__}: {exc}"
            failures.append(f"unexpected_exception={exception}")

        messages = _fetch_messages(db_path)

    statuses = [str(entry.get("status") or "") for entry in logs]
    roles = [str(message.get("role") or "") for message in messages]
    toy_commands = _toy_commands_from_broadcasts(broadcasts)
    logged_toy_commands = _toy_commands_from_logs(logs)
    toy_delivery = _toy_delivery_from_logs(logs)
    assistant_text = "\n".join(
        str(message.get("content") or "")
        for message in messages
        if message.get("role") == "assistant"
    )

    if statuses != list(scenario.expected_statuses):
        failures.append(f"statuses expected {list(scenario.expected_statuses)!r}, got {statuses!r}")
    if roles != list(scenario.expected_roles):
        failures.append(f"roles expected {list(scenario.expected_roles)!r}, got {roles!r}")
    if len(stream_calls) != scenario.expected_stream_calls:
        failures.append(f"stream_calls expected {scenario.expected_stream_calls}, got {len(stream_calls)}")
    for index, call in enumerate(stream_calls):
        prompt_text = str(call.get("prompt_text") or "")
        if "[最近三天的事]" not in prompt_text:
            failures.append(f"stream_calls[{index}] missing Timeline block")
        for legacy_marker in ("[相关记忆]", "可参考记忆"):
            if legacy_marker in prompt_text:
                failures.append(
                    f"stream_calls[{index}] contains legacy memory marker {legacy_marker!r}"
                )
    if slot_call_kinds != ["v2"]:
        failures.append(f"slot_call_kinds expected ['v2'], got {slot_call_kinds!r}")
    if toy_commands != list(scenario.expected_toy_commands):
        failures.append(f"toy_commands expected {list(scenario.expected_toy_commands)!r}, got {toy_commands!r}")
    if logged_toy_commands != list(scenario.expected_logged_toy_commands):
        failures.append(
            "logged_toy_commands expected "
            f"{list(scenario.expected_logged_toy_commands)!r}, got {logged_toy_commands!r}"
        )
    if scenario.expected_toy_delivery_status:
        status = str(toy_delivery.get("status") or "") if isinstance(toy_delivery, Mapping) else ""
        if status != scenario.expected_toy_delivery_status:
            failures.append(f"toy_delivery.status expected {scenario.expected_toy_delivery_status!r}, got {status!r}")
    if scenario.expected_toy_delivery_reason:
        reason = str(toy_delivery.get("reason") or "") if isinstance(toy_delivery, Mapping) else ""
        if reason != scenario.expected_toy_delivery_reason:
            failures.append(f"toy_delivery.reason expected {scenario.expected_toy_delivery_reason!r}, got {reason!r}")
    if "[TOY:" in assistant_text:
        failures.append("assistant message still contains a toy command marker")
    if any(entry.get("fallback_used") is True for entry in logs):
        failures.append("fallback_used=true appeared in monitor logs")

    return {
        "name": name,
        "passed": not failures,
        "failures": failures,
        "exception": exception,
        "statuses": statuses,
        "roles": roles,
        "slot_call_kinds": list(slot_call_kinds),
        "stream_calls": len(stream_calls),
        "toy_commands": toy_commands,
        "logged_toy_commands": logged_toy_commands,
        "toy_command_delivery": dict(toy_delivery) if isinstance(toy_delivery, Mapping) else {},
        "assistant_text": assistant_text,
        "broadcast_types": [str(payload.get("type") or "") for payload in broadcasts],
        "log_count": len(logs),
    }


async def run_acceptance_scenarios(scenarios: Sequence[str] | None = None) -> dict[str, Any]:
    selected = list(scenarios or ACCEPTANCE_SCENARIOS)
    unknown = sorted(set(selected).difference(ACCEPTANCE_SCENARIOS))
    if unknown:
        raise ValueError(f"unknown acceptance scenarios: {unknown!r}")

    results = [await run_acceptance_scenario(name) for name in selected]
    failed = [result for result in results if not result["passed"]]
    return {
        "schema_version": SENTINEL_V2_FULL_WAKE_ACCEPTANCE_SCHEMA_VERSION,
        "runtime_mode": "local_acceptance",
        "side_effects": list(LOCAL_ACCEPTANCE_SIDE_EFFECTS),
        "production_side_effects": [],
        "fallback_used": False,
        "fallback_reason": "",
        "metrics": {
            "total": len(results),
            "passed": len(results) - len(failed),
            "failed": len(failed),
        },
        "scenarios": results,
    }


def _toy_commands_from_broadcasts(broadcasts: Sequence[Mapping[str, Any]]) -> list[str]:
    commands: list[str] = []
    for payload in broadcasts:
        if payload.get("type") != "toy_command":
            continue
        data = payload.get("data")
        if not isinstance(data, Mapping):
            continue
        value = data.get("commands")
        if isinstance(value, Sequence) and not isinstance(value, str | bytes):
            commands.extend(str(item) for item in value)
    return commands


def _toy_commands_from_logs(logs: Sequence[Mapping[str, Any]]) -> list[str]:
    commands: list[str] = []
    for entry in logs:
        value = entry.get("toy_commands")
        if isinstance(value, Sequence) and not isinstance(value, str | bytes):
            commands = [str(item) for item in value]
    return commands


def _toy_delivery_from_logs(logs: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    delivery: Mapping[str, Any] = {}
    for entry in logs:
        value = entry.get("toy_command_delivery")
        if isinstance(value, Mapping):
            delivery = value
    return delivery


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local Sentinel V2 full-wake acceptance scenarios.")
    parser.add_argument(
        "--scenario",
        action="append",
        choices=sorted(ACCEPTANCE_SCENARIOS),
        help="Run one scenario. Repeat to run multiple. Defaults to all scenarios.",
    )
    parser.add_argument("--output-json", type=Path, help="Write the JSON result to this path.")
    parser.add_argument("--quiet", action="store_true", help="Do not print JSON to stdout.")
    parser.add_argument("--no-fail", action="store_true", help="Always exit 0 after writing the result.")
    return parser.parse_args(argv)


async def _async_main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    result = await run_acceptance_scenarios(args.scenario)
    text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(text + "\n", encoding="utf-8")
    if not args.quiet:
        print(text)
    return 1 if result["metrics"]["failed"] and not args.no_fail else 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
