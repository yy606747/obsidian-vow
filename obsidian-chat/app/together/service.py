from __future__ import annotations

import asyncio
from dataclasses import asdict
import logging
import math
import time

from .config import together_config
from .context import bounded_history, load_identity, resolve_entry
from .media import MediaStore
from .models import TogetherModels, model_available, supports_images
from .prompts import event_text, system_prompt, turn_status
from .protocol import TogetherOutput
from .repository import TogetherRepository

log = logging.getLogger(__name__)


class TogetherConflict(ValueError):
    pass


class TogetherDeviceMismatch(PermissionError):
    pass


def _names():
    from app.chat.worldbook import load_worldbook_names
    return load_worldbook_names()


def _error(exc):
    value = str(exc)
    return value if value.startswith("together_") and all(c.isalnum() or c == "_" for c in value) else type(exc).__name__


async def _device_check(device_id):
    from app.devices import device_service
    result = await device_service.get_device(device_id)
    if not result.get("ok") or result["device"].get("driver_id") != "android_mobile":
        raise ValueError("together_mobile_device_missing")


class TogetherService:
    def __init__(self, *, repository=None, config=None, models=None, media=None, now=time.time,
                 identity_loader=load_identity, names_loader=_names, entry_loader=resolve_entry,
                 image_check=supports_images, model_check=model_available, device_check=_device_check,
                 activity_check=None, writeback_runner=None):
        self.repo = repository or TogetherRepository(now=now)
        self.config = config or together_config
        self.models = models or TogetherModels()
        self.media = media or MediaStore()
        self.now = now
        self.identity_loader, self.names_loader, self.entry_loader = identity_loader, names_loader, entry_loader
        self.image_check, self.model_check, self.device_check = image_check, model_check, device_check
        self.activity_check = activity_check or self._other_activity
        self.writeback_runner = writeback_runner
        self.lock = asyncio.Lock()
        self.workers = {}
        self.running = {}
        self.cancel_reasons = {}
        self.writeback_task = None
        self.closed = False

    async def _other_activity(self):
        # Companion and scene releases can be installed independently.
        tables = {row["name"] for row in await self.repo.read(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('scene_sessions','look_at_me_requests')",
            many=True)}
        scene = await self.repo.read("SELECT id FROM scene_sessions WHERE status!='ended' LIMIT 1") if "scene_sessions" in tables else None
        invitation = await self.repo.read("SELECT id FROM look_at_me_requests WHERE status IN ('accepted','active') LIMIT 1") if "look_at_me_requests" in tables else None
        return bool(scene or invitation)

    def enabled(self):
        return self.config.load()["enabled"]

    def online(self, row):
        return row["status"] == "active" and row["heartbeat_at"]+self.config.load()["lease_seconds"] > self.now()

    async def require(self, session_id, device_id=None, *, active=False):
        row = await self.repo.session(session_id)
        if not row:
            raise LookupError("together_session_missing")
        if device_id is not None and row["device_id"] != device_id:
            raise TogetherDeviceMismatch("together_device_mismatch")
        if active and row["status"] != "active":
            raise TogetherConflict("together_session_ended")
        return row

    async def view(self, row):
        if not row:
            return None
        row, messages, captures, cursor = await self.repo.snapshot(row["id"])
        fields = ("id", "conv_id", "device_id", "model_key", "status", "started_at", "ended_at", "active_seconds",
                  "generation", "source", "source_version", "capture_state", "wake_at", "input_busy")
        result = {key: row[key] for key in fields}
        user_name, ai_name = self.names_loader()
        result.update(user_name=user_name, ai_name=ai_name, server_now=self.now(), enabled=self.enabled(),
                      online=self.online(row), image_supported=self.image_check(row["model_key"]),
                      heartbeat_seconds=self.config.load()["heartbeat_seconds"], lease_seconds=self.config.load()["lease_seconds"])
        result.update(messages=messages, captures=[self.capture_view(c) for c in captures], cursor=cursor)
        result["usage"] = await self.repo.usage(row["id"])
        result["writebacks"] = await self.repo.read("SELECT id,kind,status,error FROM together_writebacks WHERE session_id=?", (row["id"],), many=True)
        return result

    @staticmethod
    def capture_view(row):
        return {key: row.get(key) for key in ("id", "source", "source_version", "purpose", "status", "requested_at",
                                            "deadline", "captured_at", "expires_at", "error")}

    async def current(self):
        return await self.view(await self.repo.current())

    async def get(self, session_id, device_id=None):
        return await self.view(await self.require(session_id, device_id))

    async def start(self, *, request_id, conv_id, device_id, source="screen", history_count=0, model_key=None, wait_for_device=False):
        from app.control.ownership import control_mode_lock
        async with control_mode_lock, self.lock:
            existing = await self.repo.by_request(request_id)
            if existing:
                if existing["device_id"] != device_id:
                    raise TogetherDeviceMismatch("together_device_mismatch")
                return await self.view(existing)
            if not self.enabled():
                raise TogetherConflict("together_disabled")
            current = await self.repo.current()
            if current:
                if current["device_id"] != device_id:
                    raise TogetherConflict("together_active_on_another_device")
                return await self.view(current)
            from .chat_gate import ordinary_busy
            if ordinary_busy():
                raise TogetherConflict("together_chat_busy")
            if source not in {"screen", "camera"}:
                raise ValueError("together_invalid_source")
            await self.device_check(device_id)
            if await self.activity_check():
                raise TogetherConflict("together_other_activity_active")
            resolved, selected, history = await self.entry_loader(conv_id, history_count)
            selected = model_key or selected
            if not self.model_check(selected):
                raise ValueError("together_model_missing")
            # Bound even the on-disk entry snapshot; N remains a maximum, not
            # permission to fill the snapshot from other conversations.
            history = bounded_history(history, [], max_messages=history_count,
                                      max_characters=self.config.load()["context_characters"])
            row = await self.repo.create(request_id=request_id, conv_id=resolved, model_key=selected,
                                         device_id=device_id, source=source, entry_history=history, wait_for_device=wait_for_device)
        self.kick(row["id"])
        return await self.view(row)

    def kick(self, session_id):
        if self.closed or not self.enabled():
            return
        worker = self.workers.get(session_id)
        if worker and not worker.done():
            return
        worker = asyncio.create_task(self._drain(session_id), name="together:"+session_id)
        self.workers[session_id] = worker
        def done(task):
            if self.workers.get(session_id) is task:
                self.workers.pop(session_id, None)
            if not task.cancelled() and task.exception():
                log.error("together worker failed: %s", type(task.exception()).__name__)
        worker.add_done_callback(done)

    def _cancel(self, session_id, reason):
        worker = self.workers.get(session_id)
        if worker and not worker.done():
            self.cancel_reasons[session_id] = reason
            worker.cancel()

    async def message(self, session_id, *, device_id, client_id, text="", capture_id=None):
        if not isinstance(text, str) or len(text) > 8000 or (not text.strip() and not capture_id):
            raise ValueError("together_invalid_message")
        async with self.lock:
            await self.require(session_id, device_id, active=True)
            if not self.enabled():
                raise TogetherConflict("together_disabled")
            message, added = await self.repo.add_message(session_id, client_id=client_id, content=text.strip(), capture_id=capture_id)
            turn = self.running.get(session_id)
            if added and turn and turn["kind"] != "message":
                self._cancel(session_id, "owner_message")
        self.kick(session_id)
        return message

    async def heartbeat(self, session_id, *, device_id, source_version, capture_state="paused", input_busy=False):
        from app.control.ownership import control_mode_lock
        async with control_mode_lock, self.lock:
            row = await self.require(session_id, device_id)
            if row["status"] == "ended":
                return await self.view(row)
            if not self.online(row) and await self.activity_check():
                raise TogetherConflict("together_other_activity_active")
            if row["source_version"] != source_version:
                raise TogetherConflict("together_source_changed")
            if capture_state not in {"ready", "paused", "unavailable"}:
                raise ValueError("together_invalid_capture_state")
            if not self.enabled():
                capture_state = "paused"
            await self.repo.heartbeat(session_id, lease_seconds=self.config.load()["lease_seconds"],
                                      input_busy=input_busy, capture_state=capture_state, enabled=self.enabled())
        self.kick(session_id)
        return await self.get(session_id, device_id)

    async def action(self, session_id, *, device_id, action, source=None, model_key=None, message_id=None, ended_at=None):
        async with self.lock:
            row = await self.require(session_id, device_id)
            if action == "end":
                names = self.names_loader()
                row = await self.repo.finish(session_id, lease_seconds=self.config.load()["lease_seconds"],
                    ended_at=ended_at, event_content=lambda seconds: event_text(user_name=names[0], ai_name=names[1], active_seconds=seconds))
                self._cancel(session_id, "ended")
            elif row["status"] == "ended":
                raise TogetherConflict("together_session_ended")
            elif action in {"switch_source", "pause_capture", "resume_capture"}:
                if action == "switch_source" and source not in {"screen", "camera"}:
                    raise ValueError("together_invalid_source")
                # Incrementing the source version revokes every in-flight frame,
                # while already frozen manual drafts retain their actual origin.
                await self.repo.update(session_id, source=source if action == "switch_source" else row["source"],
                    source_version=row["source_version"]+1, capture_state="paused")
                await self.repo.invalidate_captures(session_id, "source_changed")
                self._cancel(session_id, "source_changed")
            elif action == "set_model":
                if not model_key or not self.model_check(model_key):
                    raise ValueError("together_model_missing")
                await self.repo.update(session_id, model_key=model_key)
            elif action == "retry":
                old = await self.repo.message(message_id) if message_id else None
                if old and old["session_id"] == session_id and old["role"] == "user" and old["status"] == "failed":
                    await self.repo.execute("UPDATE together_messages SET status='queued' WHERE id=?", (message_id,))
                elif not message_id and not self.running.get(session_id):
                    await self.repo.update(session_id, opening_pending=1)
                else:
                    raise ValueError("together_retry_unavailable")
            else:
                raise ValueError("together_invalid_action")
            await self.repo.emit(session_id, "state_changed", {"action": action})
        if action != "end":
            self.kick(session_id)
        return await self.get(session_id, device_id)

    async def configure(self, value):
        async with self.lock:
            result = self.config.save({**self.config.load(), **value})
            row = await self.repo.current()
            if row and not result["enabled"]:
                self._cancel(row["id"], "disabled")
                await self.repo.heartbeat(row["id"], lease_seconds=result["lease_seconds"], input_busy=False, capture_state="paused", enabled=result["enabled"])
                await self.repo.update(row["id"], capture_state="paused", source_version=row["source_version"]+1,
                                       heartbeat_at=0, input_busy=0)
                await self.repo.invalidate_captures(row["id"], "disabled")
                await self.repo.emit(row["id"], "disabled", {})
        return result

    async def request_capture(self, session_id, *, device_id, purpose="manual"):
        async with self.lock:
            row = await self.require(session_id, device_id, active=True)
            if not self.enabled() or not self.online(row):
                raise TogetherConflict("together_offline")
            if not self.image_check(row["model_key"]):
                raise TogetherConflict("together_model_no_vision")
            if row["capture_state"] != "ready":
                raise TogetherConflict("together_capture_paused")
            if purpose != "manual" and row["input_busy"]:
                raise TogetherConflict("together_input_busy")
            return await self.repo.create_capture(row, purpose=purpose, config=self.config.load())

    async def receive_capture(self, session_id, capture_id, *, device_id, source_version, captured_at=None, data=None, error=None):
        async with self.lock:
            row = await self.require(session_id, device_id, active=True)
            capture = await self.repo.capture(capture_id)
            if not capture or capture["session_id"] != session_id:
                raise LookupError("together_capture_missing")
            if row["source_version"] != source_version or capture["source_version"] != source_version:
                raise TogetherConflict("together_source_changed")
            if capture["status"] != "requested":
                return self.capture_view(capture)
            if capture["deadline"] < self.now() or not self.enabled() or not self.online(row):
                raise TogetherConflict("together_capture_expired")
            filename = digest = None
            if error:
                error = error[:80] if error in {"permission_denied", "screen_stopped", "camera_busy", "locked", "capture_timeout", "input_busy", "unavailable"} else "unavailable"
            else:
                if (not data or type(captured_at) not in (float, int) or not math.isfinite(captured_at)
                        or captured_at < max(row["started_at"]-2, capture["requested_at"]-(30 if capture["purpose"] == "manual" else 2))
                        or captured_at > self.now()+2):
                    raise ValueError("together_invalid_capture")
                filename, digest = await self.media.save(capture_id, data, self.config.load())
            accepted = await self.repo.receive_capture(capture_id, filename=filename, sha256=digest, captured_at=captured_at,
                                                       error=error, draft_seconds=self.config.load()["draft_seconds"])
            if not accepted:
                await self.media.delete(filename)
                raise TogetherConflict("together_capture_expired")
        return self.capture_view(await self.repo.capture(capture_id))

    async def transcribe(self, session_id, *, device_id, data):
        await self.require(session_id, device_id, active=True)
        if not self.enabled():
            raise TogetherConflict("together_disabled")
        if len(data) > self.config.load()["recording_seconds"]*32000+4096:
            raise ValueError("together_recording_too_large")
        started, status, code = self.now(), "failed", None
        try:
            text = await self.models.transcribe(data, max_seconds=self.config.load()["recording_seconds"])
            await self.require(session_id, device_id, active=True)
            status = "complete"
            return {"text": text}
        except Exception as exc:
            code = _error(exc)
            raise
        finally:
            await self.repo.record_usage(session_id, kind="asr", started_at=started,
                elapsed_ms=int((self.now()-started)*1000), status=status, error=code)

    async def _wait_capture(self, row, purpose):
        # Do not hide a keyboard or interrupt dictation for an autonomous frame.
        # There is no model polling while waiting for editing to finish.
        if purpose != "manual":
            while row["input_busy"]:
                await asyncio.sleep(.2)
                row = await self.require(row["id"], active=True)
                if not self.online(row):
                    return None, "together_offline"
        try:
            capture = await self.request_capture(row["id"], device_id=row["device_id"], purpose=purpose)
        except (TogetherConflict, ValueError) as exc:
            return None, _error(exc)
        deadline = time.monotonic()+self.config.load()["capture_timeout_seconds"]
        while time.monotonic() < deadline:
            capture = await self.repo.capture(capture["id"])
            if capture["status"] == "ready":
                return capture, ""
            if capture["status"] != "requested":
                return None, capture.get("error") or "unavailable"
            await asyncio.sleep(.1)
        await self.repo.execute("UPDATE together_captures SET status='failed',error='capture_timeout' WHERE id=? AND status='requested'", (capture["id"],))
        return None, "capture_timeout"

    async def _drain(self, session_id):
        while not self.closed and self.enabled():
            async with self.lock:
                turn = await self.repo.claim_turn(session_id, lease_seconds=self.config.load()["lease_seconds"])
                if not turn:
                    return
                self.running[session_id] = turn
            try:
                await self._run_turn(turn)
            finally:
                self.running.pop(session_id, None)

    async def _run_turn(self, turn):
        sid, row, message = turn["session_id"], turn["session"], turn["message"]
        partial, used_captures, completed = "", [], False
        try:
            identity = asdict(await self.identity_loader())
            config = self.config.load()
            capture, capture_error = None, ""
            if message and message.get("capture_id"):
                candidate = await self.repo.capture(message["capture_id"])
                if (candidate and candidate["status"] == "attached" and candidate["filename"]
                        and candidate["expires_at"] > self.now() and self.image_check(row["model_key"])):
                    capture = candidate
                else:
                    capture_error = "together_draft_unavailable"
            elif turn["kind"] == "awake" and row["wake_image"]:
                capture, capture_error = await self._wait_capture(row, "awake")
            history = await self.repo.context_messages(turn, limit=config["context_messages"])
            context_history = []
            for item in history:
                text = item["content"]
                if item.get("capture_id"):
                    image = await self.repo.capture(item["capture_id"])
                    if image and image["captured_at"]:
                        old = "旧图未重新附上。" if not message or item["id"] != message["id"] else ""
                        text += f"\n[附图时间：{image['captured_at']}；来源：{image['source']}。{old}]"
                if item["status"] == "interrupted":
                    text += "\n[这条表达中途被打断。]"
                context_history.append({"role": item["role"], "content": text})
            messages = [{"role": "system", "content": system_prompt(**identity, minimum_wake_seconds=config["minimum_wake_seconds"])}]
            messages += bounded_history(row["entry_history"], context_history, max_messages=config["context_messages"], max_characters=config["context_characters"])
            trigger = {"opening": "刚刚开始陪伴", "message": f"收到{identity['user_name']}的消息", "awake": "按本场安排回来"}[turn["kind"]]
            status = turn_status(user_name=identity["user_name"], ai_name=identity["ai_name"], now=self.now(), started_at=row["started_at"],
                trigger=trigger, source=row["source"], capture_state=row["capture_state"], intent=row["wake_intent"], image=capture, capture_error=capture_error)
            if turn["kind"] == "opening":
                status += f"\n这是首次回应，{identity['ai_name']}先和{identity['user_name']}用文字接上。本轮不取图，可以安排稍后再来看。"
            if message and messages[-1]["role"] == "user":
                # The owner caption and fresh attachment are a single provider
                # message even for providers that do not merge adjacent roles.
                messages[-1]["content"] += "\n\n"+status
            else:
                messages.append({"role": "user", "content": status})
            if capture:
                used_captures.append(capture)
                attachment = await self.media.attachment(capture)
                # Caption and image are one provider message after role normalization.
                messages[-1]["attachments"] = [attachment]
            if turn['kind'] == 'awake':
                from app.heart_works.entry import attach
                turn['_heart_entry'] = attach(messages, source='together_awake', turn_id=turn['id'],
                    conv_id=row['conv_id'], model_key=row['model_key'], user_name=identity['user_name'], ai_name=identity['ai_name'],
                    identity_text='\n'.join(str(identity.get(k) or '') for k in ('ai_persona', 'user_persona', 'working_model')),
                    context_messages=bounded_history(row['entry_history'], context_history, max_messages=config['context_messages'], max_characters=config['context_characters']) + [{'role': 'user', 'content': status}])
            output = await self._model_turn(turn, messages)
            partial = output["visible"]
            if output["look"] and turn["kind"] != "opening":
                fresh = await self.require(sid, active=True)
                extra, failure = await self._wait_capture(fresh, "tool")
                if extra:
                    used_captures.append(extra)
                if partial:
                    messages.append({"role": "assistant", "content": partial})
                # Remove the first image before the follow-up; never resend it.
                for item in messages:
                    item.pop("attachments", None)
                followup = turn_status(user_name=identity["user_name"], ai_name=identity["ai_name"], now=self.now(), started_at=row["started_at"],
                    trigger="本轮主动取图结果，已经用完本轮补看机会", source=fresh["source"], capture_state=fresh["capture_state"], image=extra, capture_error=failure)
                messages.append({"role": "user", "content": followup})
                if extra:
                    messages[-1]["attachments"] = [await self.media.attachment(extra)]
                second = await self._model_turn(turn, messages, prefix=partial)
                output["visible"] = "\n".join(t for t in (partial, second["visible"]) if t)
                output["awake"] = second["awake"]
                output["notes"] += second["notes"]
                output["understandings"] += second["understandings"]
                partial = output["visible"]
            evidence = {"session_id": sid, "turn_id": turn["id"], "owner_messages": [
                {"id": item["id"], "content": item["content"], "created_at": item["created_at"]}
                for item in history if item["role"] == "user" and item["status"] != "queued"][-4:],
                "expression": output["visible"], "images": [self.capture_view(item) for item in used_captures]}
            async with self.lock:
                completed = await self.repo.finish_turn(turn, output=output, evidence=evidence, identity=identity, config=config)
            if completed and turn.get('_heart_entry'):
                await turn['_heart_entry'].commit()
        except asyncio.CancelledError:
            reason = self.cancel_reasons.pop(sid, "interrupted")
            partial = turn.get("partial", partial)
            await self.repo.abort_turn(turn, reason=reason, partial=partial, retry_message=reason == "source_changed")
        except Exception as exc:
            await self.repo.abort_turn(turn, reason=_error(exc), partial=turn.get("partial", partial))
        finally:
            for capture in used_captures:
                # Owner attachments belong to the message, not an individual
                # attempt. Retain them for source-change requeues/manual retry;
                # the cleanup sweep handles expiry, abandonment and session end.
                if capture["purpose"] == "manual" and not completed:
                    continue
                await self.media.delete(capture.get("filename"))
                await self.repo.execute("UPDATE together_captures SET status='consumed',filename=NULL WHERE id=?", (capture["id"],))

    async def _model_turn(self, turn, messages, *, prefix=""):
        from app.heart_works.entry import IntentStreamFilter
        heart_filter = IntentStreamFilter() if turn.get('_heart_entry') else None
        output, meta = TogetherOutput(), {}
        started, status, error = self.now(), "interrupted", None
        last_emit, buffered = time.monotonic(), ""
        try:
            async with asyncio.timeout(self.config.load()["model_timeout_seconds"]):
                async for chunk in self.models.stream(turn["session"]["model_key"], messages, meta):
                    buffered += output.feed(heart_filter.feed(chunk) if heart_filter else chunk)
                    turn["partial"] = "\n".join(x for x in (prefix, output.visible) if x)
                    if buffered and time.monotonic()-last_emit >= .08:
                        async with self.lock:
                            if not await self.repo.turn_current(turn):
                                raise asyncio.CancelledError()
                            await self.repo.emit(turn["session_id"], "reply_delta", {"turn_id": turn["id"], "text": buffered})
                        last_emit, buffered = time.monotonic(), ""
                if heart_filter:
                    buffered += output.feed(heart_filter.flush())
                    turn['_heart_entry'].extract(heart_filter.raw)
                result = output.finish()
                if not output.length and not (turn.get('_heart_entry') and turn['_heart_entry'].intent.strip()):
                    raise ValueError("together_empty_model_output")
                if buffered:
                    async with self.lock:
                        if not await self.repo.turn_current(turn):
                            raise asyncio.CancelledError()
                        await self.repo.emit(turn["session_id"], "reply_delta", {"turn_id": turn["id"], "text": buffered})
                status = "complete"
                return result
        except Exception as exc:
            status, error = "failed", _error(exc)
            raise
        finally:
            await self.repo.record_usage(turn["session_id"], kind="core", turn_id=turn["id"], model_key=turn["session"]["model_key"],
                started_at=started, elapsed_ms=int((self.now()-started)*1000), status=status, meta=meta,
                image_count=sum(len(m.get("attachments", [])) for m in messages), error=error)

    async def cleanup_media(self):
        rows = await self.repo.read("""SELECT c.* FROM together_captures c JOIN together_sessions s ON s.id=c.session_id
            WHERE c.filename IS NOT NULL AND (s.status='ended' OR c.status IN ('failed','consumed')
            OR (c.status='ready' AND c.expires_at<=?)
            OR (c.status='attached' AND NOT EXISTS (
                SELECT 1 FROM together_messages m WHERE m.capture_id=c.id AND m.session_id=c.session_id
                AND (m.status='running' OR (m.status IN ('queued','failed') AND c.expires_at>?)))))""",
            (self.now(), self.now()), many=True)
        for row in rows:
            await self.media.delete(row["filename"])
            await self.repo.execute("UPDATE together_captures SET filename=NULL,status='consumed' WHERE id=?", (row["id"],))
        await self.repo.execute("UPDATE together_captures SET status='failed',error='capture_timeout' WHERE status='requested' AND deadline<?", (self.now(),))
        await self.repo.execute("""DELETE FROM together_captures WHERE filename IS NULL AND status IN ('failed','consumed')
            AND (session_id IN (SELECT id FROM together_sessions WHERE status='ended') OR
                (id NOT IN (SELECT capture_id FROM together_messages WHERE capture_id IS NOT NULL)
                 AND id NOT IN (SELECT id FROM together_captures ORDER BY requested_at DESC LIMIT 200)))""")

    async def tick(self):
        from .coexist import sync_holds
        await sync_holds(now=self.now(), db_factory=self.repo.db, enabled=self.enabled(), lease_seconds=self.config.load()["lease_seconds"])
        from .delivery import drain
        try:
            await drain(db_factory=self.repo.db, now=self.now(), enabled=self.enabled(), lease_seconds=self.config.load()["lease_seconds"])
        except Exception as exc:
            # A previously generated ordinary reply must not block this session
            # or media cleanup if its destination was independently removed.
            log.error("together deferred delivery failed: %s", _error(exc))
        row = await self.repo.current()
        if row:
            if not self.online(row):
                await self.repo.expire_connection(row["id"], lease_seconds=self.config.load()["lease_seconds"])
                self._cancel(row["id"], "connection_lost")
            else:
                self.kick(row["id"])
        await self.cleanup_media()
        if self.writeback_runner and (self.writeback_task is None or self.writeback_task.done()):
            if self.writeback_task and not self.writeback_task.cancelled() and self.writeback_task.exception():
                log.error("together writeback failed: %s", type(self.writeback_task.exception()).__name__)
            self.writeback_task = asyncio.create_task(self.writeback_runner(), name="together:writebacks")

    async def recover(self):
        await self.repo.recover(lease_seconds=self.config.load()["lease_seconds"])
        await self.cleanup_media()
        references = await self.repo.read("SELECT filename FROM together_captures WHERE filename IS NOT NULL", many=True)
        live = {row["filename"] for row in references}
        for path in self.media.root().glob("tc_*.jpg"):
            if path.name not in live:
                await self.media.delete(path.name)

    async def close(self):
        self.closed = True
        tasks = list(self.workers.values())
        if self.writeback_task:
            tasks.append(self.writeback_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


_service = None


def get_together_service():
    global _service
    if _service is None:
        _service = TogetherService()
        from .writeback import Writebacks
        from ws import manager
        writer = Writebacks(_service.repo, broadcast=manager.broadcast)
        _service.writeback_runner = writer.drain
    return _service


async def run_loop(service=None):
    service = service or get_together_service()
    await service.recover()
    while not service.closed:
        try:
            await service.tick()
        except Exception as exc:
            log.error("together tick failed: %s", _error(exc))
        await asyncio.sleep(1)
