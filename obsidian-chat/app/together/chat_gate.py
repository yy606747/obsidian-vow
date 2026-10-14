"""Reserve ordinary turns before writes; keep the reservation through generation."""
from contextvars import ContextVar
from functools import wraps
import inspect

from fastapi.responses import JSONResponse

_claim = ContextVar("together_ordinary_turn", default=None)
_running = set()


def ordinary_busy():
    return bool(_running)


async def active_session(db_factory):
    async with db_factory() as db:
        table = await (await db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='together_sessions'")).fetchone()
        if table:
            row = await (await db.execute("SELECT id FROM together_sessions WHERE status='active' LIMIT 1")).fetchone()
            return row[0] if row else None
    return None


def bind_generation(task):
    claim = _claim.get()
    if claim is not None:
        claim["bound"] = True
        task.add_done_callback(lambda _: _running.discard(claim["key"]))


def ordinary_turn(fn):
    @wraps(fn)
    async def guarded(*args, **kwargs):
        from app.control.ownership import control_mode_lock
        async with control_mode_lock:
            session_id = await active_session(fn.__globals__["get_db"])
            if session_id:
                return JSONResponse(status_code=409, content={"ok": False, "code": "together_active",
                    "session_id": session_id, "error": "正在相伴中，请返回相伴；结束后再继续日常聊天"})
            claim = {"key": object(), "bound": False}
            _running.add(claim["key"])
        token = _claim.set(claim)
        try:
            return await fn(*args, **kwargs)
        finally:
            _claim.reset(token)
            if not claim["bound"]:
                _running.discard(claim["key"])
    # FastAPI resolves postponed annotations against the original module.
    guarded.__signature__ = inspect.signature(fn, eval_str=True)
    return guarded
