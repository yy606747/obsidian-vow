from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import aiosqlite

from database import get_db

from .ledger import ControlLedger
from .schemas import ControlSession

FIXED_REASONS = frozenset({"panic", "safeword", "device_emergency_stop", "timeout", "superseded", "client_disconnect"})
SAFETY_REASONS = frozenset({"panic", "safeword", "device_emergency_stop"})
DEFAULT_MESSAGE_WINDOW = 12
OutcomeSummarizer = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


async def init_control_outcome_tables(db) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS control_session_outcomes (
            outcome_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL UNIQUE,
            conv_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            close_reason TEXT NOT NULL,
            outcome_status TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            notes_json TEXT NOT NULL DEFAULT '[]',
            source_refs_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_control_outcomes_conv_time ON control_session_outcomes(conv_id, created_at DESC)")


class ControlOutcomeService:
    def __init__(
        self,
        *,
        get_db_factory: Callable = get_db,
        now: Callable[[], float] = time.time,
        ledger: ControlLedger | None = None,
        summarizer: OutcomeSummarizer | None = None,
        message_window_size: int = DEFAULT_MESSAGE_WINDOW,
    ):
        self._get_db = get_db_factory
        self._now = now
        self._ledger = ledger or ControlLedger(get_db_factory=get_db_factory, now=now)
        self._summarizer = summarizer
        self._message_window_size = max(1, int(message_window_size or DEFAULT_MESSAGE_WINDOW))

    async def generate_for_session(self, session_id: str) -> dict[str, Any]:
        session = await self._load_session(session_id)
        if session is None:
            raise LookupError(session_id)
        reason = session.close_reason or "normal"
        if reason in FIXED_REASONS:
            outcome = self._fixed_outcome(session, reason)
        else:
            outcome = await self._normal_outcome(session, reason)
        await self._upsert_outcome(session, outcome)
        await self._ledger.record(
            "control.session.outcome",
            conv_id=session.conv_id,
            session_id=session.session_id,
            content=outcome["summary"] or outcome["outcome_status"],
            metadata={"outcome_id": outcome["outcome_id"], "outcome_status": outcome["outcome_status"], "source_refs": outcome["source_refs"]},
        )
        for note in outcome["notes"][:3]:
            await self._ledger.record("control.note", conv_id=session.conv_id, session_id=session.session_id, content=note, metadata={"source_refs": [outcome["outcome_id"]]})
        await self._refresh_active_agenda(session.conv_id)
        return outcome

    async def _normal_outcome(self, session: ControlSession, reason: str) -> dict[str, Any]:
        context = await self._build_context(session, reason)
        try:
            result = await self._summarize(context)
            return self._outcome_payload(
                session,
                reason,
                status="completed",
                summary=str(result.get("summary") or "本次控制会话已正常结束。").strip(),
                notes=[str(item).strip() for item in (result.get("notes") or []) if str(item).strip()][:3],
                metadata={**context["debug"], "outcome_model": str(result.get("model") or "default")},
            )
        except Exception as exc:
            count = int(context.get("debug", {}).get("message_count_used") or 0)
            summary = "本次控制会话已正常结束。"
            if count:
                summary += f" 已保留本次 {count} 条会话片段，下一次可温和承接。"
            return self._outcome_payload(
                session,
                reason,
                status="completed",
                summary=summary,
                notes=[],
                metadata={**context["debug"], "fallback_used": True, "error_type": type(exc).__name__, "error": str(exc)},
            )

    async def _summarize(self, context: Mapping[str, Any]) -> Mapping[str, Any]:
        if self._summarizer is not None:
            return await self._summarizer(context)
        return await summarize_control_outcome(context)

    def _fixed_outcome(self, session: ControlSession, reason: str) -> dict[str, Any]:
        if reason in SAFETY_REASONS:
            summary = "控制会话因安全停止而结束。"
        else:
            summary = f"控制会话已结束，原因：{reason}。"
        return self._outcome_payload(session, reason, status="completed", summary=summary, notes=[], metadata={"fixed_reason": reason})

    async def _build_context(self, session: ControlSession, reason: str) -> dict[str, Any]:
        messages = await self._recent_messages(session)
        ledger = await self._recent_control_events(session)
        duration = max(0.0, (session.ended_at or self._now()) - session.started_at)
        return {
            "session": session.to_api_dict(),
            "close_reason": reason,
            "duration_sec": duration,
            "messages": messages,
            "control_events": ledger,
            "debug": {"message_window_size": self._message_window_size, "message_count_used": len(messages), "control_event_count": len(ledger)},
        }

    async def _recent_messages(self, session: ControlSession) -> list[dict[str, Any]]:
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, role, content, created_at FROM messages WHERE conv_id=? AND created_at>=? AND created_at<=? ORDER BY created_at DESC LIMIT ?",
                (session.conv_id, session.started_at, session.ended_at or self._now(), self._message_window_size),
            )
            rows = await cur.fetchall()
        return [dict(row) for row in reversed(rows)]

    async def _recent_control_events(self, session: ControlSession) -> list[dict[str, Any]]:
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, content, metadata_json, created_at FROM memory_events WHERE source='control' AND conv_id=? ORDER BY created_at DESC LIMIT 20",
                (session.conv_id,),
            )
            rows = await cur.fetchall()
        events = []
        for row in rows:
            meta = _json_obj(row["metadata_json"])
            if meta.get("session_id") == session.session_id:
                events.append({"event_type": meta.get("event_type"), "content": row["content"], "created_at": row["created_at"]})
        return events

    async def _load_session(self, session_id: str) -> ControlSession | None:
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM control_sessions WHERE session_id=?", (session_id,))
            row = await cur.fetchone()
        return ControlSession.from_row(row) if row else None

    async def _refresh_active_agenda(self, conv_id: str) -> None:
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row; cur = await db.execute("SELECT session_id FROM control_sessions WHERE conv_id=? AND status='active' ORDER BY started_at DESC LIMIT 1", (conv_id,)); row = await cur.fetchone()
        if not row: return
        from .agenda import ControlAgendaService
        await ControlAgendaService(get_db_factory=self._get_db, now=self._now).generate_for_session(row["session_id"])

    def _outcome_payload(self, session: ControlSession, reason: str, *, status: str, summary: str, notes: list[str], metadata: Mapping[str, Any]) -> dict[str, Any]:
        outcome_id = f"ctrl_out_{time.time_ns()}"
        return {
            "outcome_id": outcome_id,
            "session_id": session.session_id,
            "conv_id": session.conv_id,
            "kind": session.kind,
            "close_reason": reason,
            "outcome_status": status,
            "summary": summary,
            "notes": notes[:3],
            "source_refs": [f"session:{session.session_id}"],
            "metadata": dict(metadata),
        }

    async def _upsert_outcome(self, session: ControlSession, outcome: Mapping[str, Any]) -> None:
        now = self._now()
        async with self._get_db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO control_session_outcomes (outcome_id, session_id, conv_id, kind, close_reason, outcome_status, summary, notes_json, source_refs_json, metadata_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (outcome["outcome_id"], session.session_id, session.conv_id, session.kind, outcome["close_reason"], outcome["outcome_status"], outcome["summary"], json.dumps(outcome["notes"], ensure_ascii=False), json.dumps(outcome["source_refs"], ensure_ascii=False), json.dumps(outcome["metadata"], ensure_ascii=False), now, now),
            )
            await db.commit()

async def summarize_control_outcome(context: Mapping[str, Any]) -> Mapping[str, Any]:
    from ai_providers import call_slot_chat
    from config import get_slot

    slot = "control_outcome" if get_slot("control_outcome") else "sentinel"
    messages = [{"role": "system", "content": "你只为亲密控制会话写内部收束摘要。只返回JSON：{\"summary\":\"...\",\"notes\":[\"...\"]}。summary一句话，notes最多3条；不要写设备在线或已执行，除非输入事件明确表示。"}, {"role": "user", "content": json.dumps(_llm_context(context), ensure_ascii=False)}]
    raw = await call_slot_chat(slot, messages, expect_json=True, temperature=0.2)
    data = _json_obj(_strip_json_text(raw))
    summary = str(data.get("summary") or "").strip()
    notes = [str(item).strip() for item in (data.get("notes") or []) if str(item).strip()][:3]
    if not summary: raise RuntimeError("empty outcome summary")
    return {"summary": summary, "notes": notes, "model": f"slot:{slot}"}
def _llm_context(context: Mapping[str, Any]) -> dict[str, Any]:
    session = dict(context.get("session") or {})
    messages = [{"role": m.get("role"), "content": str(m.get("content") or "")[:500]} for m in context.get("messages", [])]
    return {"kind": session.get("kind"), "close_reason": context.get("close_reason"), "duration_sec": int(context.get("duration_sec") or 0), "messages": messages, "control_events": list(context.get("control_events") or [])}
def _strip_json_text(raw: Any) -> str:
    text = re.sub(r"^```(?:json)?|```$", "", str(raw or "").strip(), flags=re.I | re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start >= 0 and end >= start else text
def _json_obj(value: Any) -> dict[str, Any]:
    try: parsed = json.loads(str(value or "{}"))
    except Exception: return {}
    return parsed if isinstance(parsed, dict) else {}
control_outcome_service = ControlOutcomeService()
