from __future__ import annotations

import asyncio
from contextlib import aclosing
from copy import deepcopy
from dataclasses import dataclass, field
import logging
import time
from uuid import uuid4

from app.control.ownership import control_mode_lock
from .cards import CardGenerator
from .compiler import compile_intent
from .config import scene_config
from .context import conversation_model, load_identity
from .experience import Experience
from .memory import SceneMemory
from .models import SceneModels
from .notes import NotesWriter
from .player import LoopPlayer, MockDevices
from .prompts import turn_messages
from .protocol import SceneOutput
from .repository import SceneRepository

logger = logging.getLogger(__name__)
HEARTBEAT_TIMEOUT = 45
FALLBACK_SECONDS = 480
RETAIN_SECONDS = 1800
BROADCAST_TIMEOUT = 2
OUTPUT_TIMEOUT = 5
BRIDGE_READY_TIMEOUT = 20


def _error_code(exc):
    # Keep useful local error codes without carrying provider response prose.
    message = str(exc)
    if message.startswith("scene_") and all(c.isalnum() or c == "_" for c in message):
        return message
    return type(exc).__name__


class SceneConflict(ValueError):
    pass


class SceneOwnerMismatch(PermissionError):
    pass


@dataclass
class Runtime:
    row: dict
    devices: list
    safeword: str
    heartbeat: float
    player: LoopPlayer
    identity: object = None
    card: dict | None = None
    history: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    pending_messages: list = field(default_factory=list)
    pending_wakes: set = field(default_factory=set)
    worker: object = None
    preparation: object = None
    compilers: dict = field(default_factory=dict)
    intent_versions: dict = field(default_factory=dict)
    arrangements: dict = field(default_factory=dict)
    last_wake: float | None = None
    due_at: float | None = None
    last_playback_event: float = 0
    revision: int = 0
    last_output_poll: float | None = None
    hardware_accepted: dict = field(default_factory=dict)
    hardware_positions: dict = field(default_factory=dict)
    experience: Experience = field(default_factory=Experience)
    prepared_intents: dict = field(default_factory=dict)
    recorded_versions: dict = field(default_factory=dict)


async def _broadcast(event):
    from ws import manager
    await manager.broadcast(event)


class SceneService:
    def __init__(self, *, repository=None, config=None, models=None, identity_loader=load_identity,
                 model_resolver=conversation_model, devices=None, now=time.time, broadcast=_broadcast,
                 control_check=None):
        self.repo = repository or SceneRepository(now=now)
        self.config = config or scene_config
        self.models = models or SceneModels()
        self.identity_loader, self.model_resolver = identity_loader, model_resolver
        self.devices = devices or MockDevices()
        self.now, self.broadcast = now, broadcast
        self.cards = CardGenerator(self.repo, self.models, identity_loader)
        self.notes = NotesWriter(self.repo, self.models, identity_loader)
        self.memory = SceneMemory(self.repo, self.models)
        self.control_check = control_check or self._legacy_control_active
        self.active = None
        self.retained = {}
        self.lock = asyncio.Lock()
        self.tasks = set()
        self._pending_events = {}
        self._event_sender = None
        self._resync_events = set()
        self.bridge_ready = {}
        self._last_recent_cleanup = self.now()

    def occupies_control(self):
        return self.active is not None and self.active.row["status"] != "ended"

    def _task(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        def finished(done):
            self.tasks.discard(done)
            if not done.cancelled() and done.exception():
                logger.error("scene task failed: %s", type(done.exception()).__name__)
        task.add_done_callback(finished)
        return task

    async def _legacy_control_active(self):
        from app.control.service import TIMEOUT_AFTER_SECONDS
        async with self.repo.db() as db:
            row = await (await db.execute("""SELECT 1 FROM control_sessions
                WHERE status IN ('active','stale') AND last_heartbeat_at>=? LIMIT 1""", (self.now()-TIMEOUT_AFTER_SECONDS,))).fetchone()
        return row is not None

    def _current(self, rt, generation, *, status="active"):
        return self.active is rt and rt.row["generation"] == generation and rt.row["status"] == status

    def _turn_current(self, rt, generation):
        # A safety pause withdraws device permission, not permission to talk.
        return self.active is rt and rt.row["generation"] == generation and rt.row["status"] in {"active", "paused"}

    def _owner(self, rt, owner_id):
        if rt.row["owner_id"] != owner_id:
            raise SceneOwnerMismatch("scene_owner_mismatch")

    def _runtime(self, session_id, owner_id=None):
        rt = self.retained.get(session_id)
        if rt is None:
            raise LookupError("scene_session_not_found")
        if owner_id is not None:
            self._owner(rt, owner_id)
        return rt

    def playback(self, rt):
        return [{"id": d["id"], "label": d.get("label"), "name": d["name"],
                 "driver": d["driver"],
                 "connected": d["id"] in self.devices.connected,
                 "output": self.actual_output(rt, d),
                 "position": self.actual_position(rt, d)} for d in rt.devices]

    def actual_output(self, rt, device):
        if rt is not self.active:
            return {}
        if device["driver"] == "mock":
            return dict(self.devices.outputs.get(device["id"], {}))
        position = self.actual_position(rt, device)
        if not position["playing"]:
            return {}
        return dict(rt.hardware_accepted[device["id"]]["steps"][position["step"]]["values"])

    def actual_position(self, rt, device):
        key = device["id"]
        if device["driver"] == "mock":
            return rt.player.position(key)
        adopted = rt.hardware_positions.get(key)
        if rt.row["status"] != "active" or not adopted or not adopted["playing"]:
            return {"playing": False, "step": None, "elapsed_ms": 0}
        elapsed = adopted["elapsed_ms"] + max(0, int((self.now() - adopted["at"]) * 1000))
        steps = rt.hardware_accepted[key]["steps"]
        cycle_ms = sum(step["duration_ms"] for step in steps)
        offset = elapsed % cycle_ms
        for index, step in enumerate(steps):
            if offset < step["duration_ms"]:
                return {"playing": True, "step": index, "elapsed_ms": elapsed,
                        "step_elapsed_ms": offset, "cycle": elapsed // cycle_ms}
            offset -= step["duration_ms"]

    def view(self, rt):
        row = rt.row
        result = {key: row.get(key) for key in ("id", "conv_id", "owner_id", "status", "generation",
                  "created_at", "started_at", "ended_at", "reason", "authorized_devices", "bindings", "phase", "revealed_at")}
        result.update(revision=rt.revision, messages=deepcopy(rt.messages), devices=self.playback(rt),
                      next_wake_at=rt.due_at, last_wake_at=rt.last_wake, has_card=bool(rt.card))
        if row["status"] == "ended" and row.get("revealed_at") is not None and rt.card:
            result["card"] = {"id": rt.card["id"], "raw_text": rt.card["raw_text"]}
        return result

    def _emit_locked(self, rt, *, playback=False):
        # Snapshot under the lifecycle lock; socket backpressure must never
        # delay stopping, heartbeat expiry, or the local playback clock.
        rt.revision += 1
        pending = self._pending_events.get(rt.row["id"])
        if rt.row["id"] in self._resync_events or (pending and pending["type"] == "scene_state"):
            playback = False  # A newer playback revision must not erase state.
        value = self.view(rt) if not playback else {"id": rt.row["id"], "generation": rt.row["generation"],
            "revision": rt.revision, "devices": self.playback(rt)}
        self._pending_events[rt.row["id"]] = deepcopy({
            "type": "scene_playback" if playback else "scene_state", "data": value})
        if self._event_sender is None or self._event_sender.done():
            self._event_sender = self._task(self._send_events())

    async def _send_events(self):
        while True:
            async with self.lock:
                if not self._pending_events:
                    return
                key = next(iter(self._pending_events))
                event = self._pending_events.pop(key)
            try:
                async with asyncio.timeout(BROADCAST_TIMEOUT):
                    await self.broadcast(event)
                if event["type"] == "scene_state":
                    self._resync_events.discard(key)
            except Exception as exc:
                logger.warning("scene broadcast failed: %s", type(exc).__name__)
                # Retry a full latest snapshot on the next tick. A partial
                # playback event cannot repair a missed state transition.
                async with self.lock:
                    self._resync_events.add(key)
                    pending = self._pending_events.get(key)
                    rt = self.retained.get(key)
                    if pending and rt:
                        self._pending_events[key] = {"type": "scene_state", "data": deepcopy(self.view(rt))}

    async def current(self):
        return self.view(self.active) if self.active else None

    async def get(self, session_id):
        rt = self.retained.get(session_id)
        if rt:
            result = self.view(rt)
        else:
            row = await self.repo.session(session_id)
            if not row:
                raise LookupError("scene_session_not_found")
            result = {key: row.get(key) for key in ("id", "conv_id", "owner_id", "status", "generation",
                      "created_at", "started_at", "ended_at", "reason", "authorized_devices", "bindings", "phase", "revealed_at")}
            result.update(messages=[], devices=[], revision=0, has_card=bool(row["card_id"]))
            if row["status"] == "ended" and row.get("revealed_at") is not None and row["card_id"]:
                card = await self.repo.card(row["card_id"])
                result["card"] = {"id": card["id"], "raw_text": card["raw_text"]}
        result["feedback"] = await self.repo.feedback(session_id)
        return result

    async def history(self):
        items = []
        for row in await self.repo.history():
            item = await self.get(row["id"])
            # History is the durable card/feedback collection, not a transcript archive.
            items.append({key: item.get(key) for key in ("id", "owner_id", "status", "created_at", "ended_at", "has_card", "revealed_at", "card", "feedback")})
        return items

    async def start(self, *, request_id, conv_id, owner_id, device_ids, safeword):
        if not request_id or not owner_id or not isinstance(safeword, str) or not safeword.strip() or len(safeword) > 64:
            raise ValueError("scene_safeword_required")
        if not device_ids or len(device_ids) != len(set(device_ids)):
            raise ValueError("scene_devices_invalid")
        async with control_mode_lock, self.lock:
            existing = await self.repo.by_request(request_id)
            if existing:
                if existing["owner_id"] != owner_id:
                    raise SceneOwnerMismatch("scene_owner_mismatch")
                if existing["authorized_devices"] != device_ids:
                    raise SceneConflict("scene_request_reused")
                return await self.get(existing["id"])
            cfg = self.config.load()
            if not cfg["enabled"]:
                raise SceneConflict("scene_disabled")
            from app.together.coexist import is_active as together_active
            if await together_active():
                raise SceneConflict("together_active")
            if self.occupies_control() or await self.control_check():
                raise SceneConflict("scene_control_busy")
            catalog = {d["id"]: d for d in cfg["devices"]}
            if not set(device_ids).issubset(catalog) or not set(device_ids).issubset(self.devices.connected):
                raise SceneConflict("scene_device_unavailable")
            self._require_bridge([catalog[key] for key in device_ids], owner_id)
            conv_id, model_key = await self.model_resolver(conv_id)
            row = await self.repo.create_session(request_id=request_id, conv_id=conv_id, model_key=model_key,
                                                  owner_id=owner_id, devices=device_ids)
            rt = Runtime(row, [deepcopy(catalog[key]) for key in device_ids], safeword.strip(), self.now(),
                         LoopPlayer(self.devices, now=self.now))
            if any(d["driver"] != "mock" for d in rt.devices):
                rt.last_output_poll = self.now()
            self.active = rt
            self.retained[row["id"]] = rt
            rt.preparation = self._task(self._prepare(rt))
            self._emit_locked(rt)
            return self.view(rt)

    async def _prepare(self, rt):
        generation = rt.row["generation"]
        try:
            cards = await self.repo.matching(rt.row["authorized_devices"])
            if not cards:
                cards = await self.cards.generate(conv_id=rt.row["conv_id"], model_key=rt.row["model_key"],
                                                  devices=rt.devices, source="entry", count=1)
            card = self.repo.choose(cards)
            identity = await self.identity_loader()
            ordered = card["required_devices"] + [key for key in rt.row["authorized_devices"] if key not in card["required_devices"]]
            bindings = {f"toy{i+1}": key for i, key in enumerate(ordered)}
            async with self.lock:
                if not self._current(rt, generation, status="preparing"):
                    return
                if not set(ordered).issubset(self.devices.connected):
                    raise SceneConflict("scene_device_unavailable")
                if not await self.repo.enter(rt.row["id"], card, bindings, generation=generation):
                    raise SceneConflict("scene_card_already_used")
                rt.row.update(status="active", card_id=card["id"], bindings=bindings, started_at=self.now())
                rt.card, rt.identity = card, identity
                by_id = {d["id"]: d for d in rt.devices}
                rt.devices = [{**by_id[key], "label": label} for label, key in bindings.items()]
                self._wake(rt, "入场")
                self._emit_locked(rt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with self.lock:
                if self._current(rt, generation, status="preparing"):
                    await self._end_locked(rt, "preparation_failed:" + _error_code(exc))

    def _wake(self, rt, reason):
        rt.pending_wakes.add(reason)
        if rt.worker is None or rt.worker.done():
            rt.worker = self._task(self._run(rt, rt.row["generation"]))

    async def _run(self, rt, generation):
        try:
            while True:
                async with self.lock:
                    if not self._turn_current(rt, generation) or not rt.pending_wakes:
                        return
                    trigger = "、".join(sorted(rt.pending_wakes))
                    rt.pending_wakes.clear()
                    rt.history.extend(rt.pending_messages)
                    rt.pending_messages.clear()
                    now = self.now()
                    previous_wake = rt.last_wake if rt.last_wake is not None else now
                    rt.last_wake = now
                    rt.due_at = now + FALLBACK_SECONDS if rt.row["status"] == "active" else None
                    arrangements = "\n".join(f"{d['label']}：{rt.arrangements.get(d['label'], '尚未安排')}；"
                        f"{'正在播放' if self.actual_position(rt, d)['playing'] else '等待手机确认' if d['driver'] != 'mock' and d['id'] in rt.player.sequences else '已停止'}"
                        for d in rt.devices)
                    messages = turn_messages(**vars(rt.identity), card=rt.card["raw_text"], devices=rt.devices,
                        history=list(rt.history), now=now, started_at=rt.row["started_at"], last_wake_at=previous_wake,
                        arrangements=arrangements, trigger=trigger, phase=rt.row["phase"], paused=rt.row["status"] == "paused")
                    display = {"id": uuid4().hex, "role": "assistant", "content": "", "at": now}
                    heart_entry = None
                    heart_filter = None
                    if '按约定时间回来' in trigger:
                        from app.heart_works.entry import attach, IntentStreamFilter
                        heart_entry = attach(messages, source='scene_awake', turn_id=display['id'],
                            conv_id=rt.row['conv_id'], model_key=rt.row['model_key'],
                            user_name=rt.identity.user_name, ai_name=rt.identity.ai_name,
                            identity_text=rt.identity.ai_persona + '\n' + rt.identity.working_model,
                            context_messages=[{'role': 'user', 'content': '当前场景设定：\n' + rt.card['raw_text']},
                                              *rt.history, {'role': 'user', 'content': '这次回来时的情况：\n' + arrangements}])
                        heart_filter = IntentStreamFilter()
                    rt.messages.append(display)
                    rt.experience.message(display, speaker=rt.identity.ai_name)
                    parser = SceneOutput(rt.row["bindings"])
                    raw_parts = []
                async with asyncio.timeout(150), aclosing(self.models.stream(rt.row["model_key"], messages)) as source:
                    async for chunk in source:
                        if not isinstance(chunk, str):
                            raise ValueError("scene_chunk_invalid")
                        raw_parts.append(chunk)
                        visible = parser.feed(heart_filter.feed(chunk) if heart_filter else chunk)
                        async with self.lock:
                            if not self._turn_current(rt, generation):
                                return
                            if visible:
                                display["content"] += visible
                                self._emit_locked(rt)
                if heart_filter:
                    tail = parser.feed(heart_filter.flush())
                    display['content'] += tail
                    heart_entry.extract(''.join(raw_parts))
                parsed = parser.finish()
                async with self.lock:
                    if not self._turn_current(rt, generation):
                        return
                    if not raw_parts or not "".join(raw_parts).strip():
                        raise ValueError("scene_empty_output")
                    rt.history.append({"role": "assistant", "content": heart_entry.extract(''.join(raw_parts)) if heart_entry else ''.join(raw_parts)})
                    rt.history = rt.history[-24:]
                    if not display["content"].strip():
                        rt.messages.remove(display)
                    rt.messages = rt.messages[-200:]
                    if parsed["ended"]:
                        await self._aftercare_locked(rt)
                    permitted = rt.row["status"] == "active"
                    rt.due_at = min(rt.last_wake + FALLBACK_SECONDS,
                                    self.now() + parsed["awake"] if parsed["awake"] is not None else float("inf")) if permitted else None
                    for label, intent in (parsed["intents"].items() if permitted else ()):
                        device_id = rt.row["bindings"][label]
                        version = rt.intent_versions.get(device_id, 0) + 1
                        rt.intent_versions[device_id] = version
                        previous = rt.compilers.get(device_id)
                        if previous and not previous.done():
                            previous.cancel()
                        rt.compilers[device_id] = self._task(self._compile(rt, label, intent, generation, version))
                    self._emit_locked(rt)
                if parsed["notes"]:
                    # Accepted complete turns may save after pause/finish, but
                    # never start memory work inside the lifecycle commit.
                    self._task(self.memory.remember(rt.row["id"], display["id"], parsed["notes"], created_at=self.now()))
                if heart_entry:
                    await heart_entry.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with self.lock:
                if self._turn_current(rt, generation):
                    await self._pause_locked(rt, "model_failed:" + _error_code(exc))
        finally:
            async with self.lock:
                if rt.worker is asyncio.current_task():
                    rt.worker = None
                    if self.active is rt and rt.row["status"] in {"active", "paused"} and rt.pending_wakes:
                        self._wake(rt, "继续处理反馈")

    async def _compile(self, rt, label, intent, generation, version):
        device_id = rt.row["bindings"][label]
        def valid():
            return self._current(rt, generation) and rt.intent_versions.get(device_id) == version
        try:
            device = next(d for d in rt.devices if d["id"] == device_id)
            async with asyncio.timeout(70):
                result = await compile_intent(self.models, intent=intent, device=device,
                    previous=deepcopy(rt.hardware_accepted.get(device_id) if device["driver"] != "mock"
                                      else rt.player.accepted.get(device_id)),
                    position=self.actual_position(rt, device))
            async with self.lock:
                if valid():
                    rt.player.apply(device_id, result)
                    rt.arrangements[label] = intent
                    if result["action"] != "hold":
                        playback_version = rt.player.versions[device_id]
                        rt.prepared_intents[device_id] = (playback_version, intent, result["action"])
                        if device["driver"] == "mock":
                            self._record_adopted(rt, device, playback_version)
                    self._emit_locked(rt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with self.lock:
                if valid():
                    await self._pause_locked(rt, "compile_failed:" + _error_code(exc))

    async def message(self, session_id, *, owner_id, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            raise ValueError("scene_message_invalid")
        async with self.lock:
            rt = self._runtime(session_id, owner_id)
            if rt.row["status"] not in {"active", "paused"}:
                raise SceneConflict("scene_not_active")
            message = {"id": uuid4().hex, "role": "user", "content": text, "at": self.now()}
            rt.experience.message(message, speaker=rt.identity.user_name)
            rt.messages.append(message)
            rt.pending_messages.append({"role": "user", "content": text})
            if rt.safeword.casefold() in text.casefold():
                await self._pause_locked(rt, "safeword")
                self._wake(rt, "安全词已生效，设备保持暂停")
                return self.view(rt)
            rt.heartbeat = self.now()
            self._wake(rt, "收到新反馈")
            self._emit_locked(rt)
            return self.view(rt)

    def _stop_devices(self, rt):
        # A normal wind-down keeps the native session authorization intact.
        # Device revisions, not session generation, fence old compilers/loops.
        for device_id in rt.row["authorized_devices"]:
            rt.intent_versions[device_id] = rt.intent_versions.get(device_id, 0) + 1
            rt.player.stop(device_id)
        for task in rt.compilers.values():
            if task and task is not asyncio.current_task() and not task.done() and not task.cancelling():
                task.cancel()
        rt.hardware_positions.clear()
        rt.prepared_intents.clear()

    async def _aftercare_locked(self, rt):
        if rt.row["phase"] == "aftercare":
            return
        self._stop_devices(rt)
        rt.row["phase"] = "aftercare"
        rt.experience.state(now=self.now(), state="aftercare")
        await self.repo.aftercare(rt.row["id"])

    def _cancel_outputs(self, rt):
        rt.due_at = None
        rt.pending_wakes.clear()
        self._stop_devices(rt)
        current = asyncio.current_task()
        for task in [rt.preparation, rt.worker]:
            # Do not interrupt a cancelled worker's finally block: it hands
            # serialized pending messages to the next authorized generation.
            if task and task is not current and not task.done() and not task.cancelling():
                task.cancel()

    async def _pause_locked(self, rt, reason):
        if rt.row["status"] == "ended":
            return
        if rt.row["status"] == "preparing":
            await self._end_locked(rt, reason)
            return
        rt.row.update(status="paused", generation=rt.row["generation"] + 1, reason=reason)
        self._cancel_outputs(rt)
        rt.experience.state(now=self.now(), state="paused", reason=reason)
        await self.repo.state(rt.row["id"], status="paused", generation=rt.row["generation"], reason=reason)
        self._emit_locked(rt)

    async def _end_locked(self, rt, reason, *, reveal=False):
        if rt.row["status"] == "ended":
            if reveal and rt.row.get("revealed_at") is None:
                await self.repo.reveal(rt.row["id"])
                rt.row["revealed_at"] = self.now()
                rt.row["generation"] += 1
                self._emit_locked(rt)
            return
        rt.row.update(status="ended", generation=rt.row["generation"] + 1, ended_at=self.now(), reason=reason)
        if reveal:
            rt.row["revealed_at"] = self.now()
        self._cancel_outputs(rt)
        rt.pending_messages.clear()
        rt.safeword = ""
        if self.active is rt:
            self.active = None
        recent = rt.experience.snapshot(user_name=rt.identity.user_name, ai_name=rt.identity.ai_name) if rt.identity else None
        await self.repo.state(rt.row["id"], status="ended", generation=rt.row["generation"], reason=reason, recent=recent, reveal=reveal)
        self._emit_locked(rt)
        if recent and recent["items"] and reason != "server_shutdown":
            self._task(self.memory.summarize(dict(rt.row), recent, rt.identity))

    async def action(self, session_id, *, owner_id, action, generation=None):
        async with self.lock:
            # Only this explicit page action reveals a card. In particular the
            # old native notification/panic endpoint "end" is stop-only.
            # Finish is session-scoped, not output-generation-scoped: the local
            # native stop may have already advanced that generation.
            if action == "finish" and session_id not in self.retained:
                row = await self.repo.session(session_id)
                if not row:
                    raise LookupError("scene_session_not_found")
                if row["owner_id"] != owner_id:
                    raise SceneOwnerMismatch("scene_owner_mismatch")
                if row["status"] != "ended":
                    raise SceneConflict("scene_not_active")
                await self.repo.reveal(session_id)
                return await self.get(session_id)
            rt = self._runtime(session_id, owner_id)
            if action == "finish":
                await self._end_locked(rt, "owner_ended", reveal=True)
                return self.view(rt)
            # Native notification/retry commands belong to their original run.
            # A delayed pause must not interrupt a later explicit resume.
            if generation is not None and generation != rt.row["generation"]:
                return self.view(rt)
            if action == "end":
                if rt.row["status"] != "paused":
                    await self._pause_locked(rt, "owner_stopped")
            elif action == "pause":
                if rt.row["status"] != "paused":
                    await self._pause_locked(rt, "owner_paused")
            elif action == "resume":
                if rt.row["status"] != "paused" or rt.card is None:
                    raise SceneConflict("scene_not_paused")
                if not set(rt.row["authorized_devices"]).issubset(self.devices.connected):
                    raise SceneConflict("scene_device_unavailable")
                self._require_bridge(rt.devices, owner_id)
                self._cancel_outputs(rt)
                rt.row.update(status="active", generation=rt.row["generation"]+1, reason=None)
                rt.experience.state(now=self.now(), state="resumed")
                rt.heartbeat = self.now()
                if rt.last_output_poll is not None:
                    rt.last_output_poll = self.now()
                await self.repo.state(rt.row["id"], status="active", generation=rt.row["generation"])
                self._wake(rt, "暂停后重新安排")
                self._emit_locked(rt)
            else:
                raise ValueError("scene_action_invalid")
            return self.view(rt)

    async def heartbeat(self, session_id, *, owner_id):
        async with self.lock:
            rt = self._runtime(session_id, owner_id)
            rt.heartbeat = self.now()
            return {"status": rt.row["status"], "generation": rt.row["generation"]}

    async def panic(self, reason="panic"):
        async with self.lock:
            if self.active:
                await self._pause_locked(self.active, reason)

    async def connect(self, device_id, connected=True):
        async with self.lock:
            device = next((d for d in self.config.load()["devices"] if d["id"] == device_id), None)
            if device is None:
                raise LookupError("scene_device_not_found")
            if device["driver"] != "mock":
                raise SceneConflict("scene_native_bridge_required")
            if connected:
                self.devices.connect(device_id)
            else:
                self.devices.disconnect(device_id)
                if self.active and device_id in self.active.row["authorized_devices"]:
                    await self._pause_locked(self.active, "device_disconnected")
        return self.device_list()

    def _require_bridge(self, devices, owner_id):
        for device in devices:
            if device["driver"] == "mock":
                continue
            ready = self.bridge_ready.get(device["id"])
            if not ready or ready[0] != owner_id or self.now() - ready[1] > BRIDGE_READY_TIMEOUT:
                raise SceneConflict("scene_native_bridge_required")

    async def report_bridge(self, *, owner_id, connected):
        """Readiness belongs to the phone/window, never the global mock-connect API."""
        async with self.lock:
            catalog = {d["id"]: d for d in self.config.load()["devices"] if d["driver"] != "mock"}
            if not set(connected).issubset(catalog):
                raise ValueError("scene_bridge_device_invalid")
            if self.active and self.active.row["owner_id"] != owner_id:
                raise SceneOwnerMismatch("scene_owner_mismatch")
            for key in catalog:
                if key in connected:
                    self.bridge_ready[key] = (owner_id, self.now())
                    self.devices.connect(key)
                elif self.bridge_ready.get(key, (None,))[0] == owner_id:
                    self.bridge_ready.pop(key, None)
                    self.devices.disconnect(key)
            if self.active and self.active.row["status"] in {"preparing", "active"} and not set(self.active.row["authorized_devices"]).issubset(self.devices.connected):
                await self._pause_locked(self.active, "device_disconnected")
            return self.device_list()

    async def output(self, session_id, *, owner_id, generation, connected, adopted):
        """Owner-only sequence transport; UI broadcasts are never hardware commands.

        The phone renews a short native lease using this response. This endpoint
        only copies state under the lock: it cannot wait on a socket or BLE I/O.
        """
        async with self.lock:
            rt = self._runtime(session_id, owner_id)
            result = {"id": session_id, "generation": rt.row["generation"], "status": rt.row["status"], "devices": []}
            if generation != rt.row["generation"] or rt.row["status"] not in {"preparing", "active"}:
                return result
            physical = [d for d in rt.devices if d["driver"] != "mock"]
            if not {d["id"] for d in physical}.issubset(connected):
                await self._pause_locked(rt, "device_disconnected")
                return {**result, "generation": rt.row["generation"], "status": rt.row["status"]}
            rt.last_output_poll = self.now() if physical else None
            if physical:
                # Real playback is owned by the native service, not page visibility
                # or the chat WebSocket. A valid output exchange proves liveness.
                rt.heartbeat = self.now()
            for device in physical:
                key = device["id"]
                self.bridge_ready[key] = (owner_id, self.now())
                self.devices.connect(key)
                version = rt.player.versions.get(key, 0)
                seq = rt.player.sequences.get(key)
                ack = adopted.get(key, {})
                if isinstance(ack, dict) and ack.get("version") == version and type(ack.get("elapsed_ms")) is int and 0 <= ack["elapsed_ms"] <= 86400000:
                    if seq:
                        rt.hardware_accepted[key] = deepcopy(seq)
                        rt.player.started[key] = self.now() - ack["elapsed_ms"] / 1000
                    rt.hardware_positions[key] = {"playing": bool(seq), "elapsed_ms": ack["elapsed_ms"], "at": self.now()}
                    self._record_adopted(rt, device, version)
                result["devices"].append({"id": key, "driver": device["driver"], "version": version,
                    "capabilities": deepcopy(device["capabilities"]),
                    "sequence": deepcopy(seq) if rt.row["status"] == "active" and seq else {"action": "stop"}})
            return result

    def _record_adopted(self, rt, device, version):
        key = device["id"]
        intent = rt.prepared_intents.get(key)
        if not intent or intent[0] != version or rt.recorded_versions.get(key) == version:
            return
        rt.recorded_versions[key] = version
        rt.experience.device(now=self.now(), name=device["name"], intent=intent[1], action=intent[2],
                             simulated=device["driver"] == "mock")

    def device_list(self):
        return [{**d, "connected": d["id"] in self.devices.connected and (d["driver"] == "mock" or
                 self.now() - self.bridge_ready.get(d["id"], (None, -float("inf")))[1] <= BRIDGE_READY_TIMEOUT)}
                for d in self.config.load()["devices"]]

    async def configure(self, value):
        from .config import validate_config
        value = validate_config(value)
        async with self.lock:
            old = self.config.load()
            if self.active and value["enabled"] and old["devices"] != value["devices"]:
                raise SceneConflict("scene_device_config_in_use")
            if not value["enabled"] and self.active:
                await self._end_locked(self.active, "feature_disabled")
            result = self.config.save(value)
            for device_id in list(self.devices.connected):
                if device_id not in {d["id"] for d in value["devices"]}:
                    self.devices.disconnect(device_id)
                    self.bridge_ready.pop(device_id, None)
            return result

    async def feedback(self, session_id, *, rating, reason):
        row = await self.repo.session(session_id)
        if not row or row["status"] != "ended" or row.get("revealed_at") is None or not row["card_id"]:
            raise SceneConflict("scene_feedback_unavailable")
        feedback = await self.repo.save_feedback(session_id, rating=rating, reason=reason)
        if feedback["note_status"] in {"pending", "failed"}:
            self._task(self.notes.update(session_id))
        return feedback

    async def night_cards(self, *, conv_id, model_key):
        cfg = self.config.load()
        if not cfg["enabled"] or not cfg["devices"]:
            return []
        return await self.cards.generate(conv_id=conv_id, model_key=model_key, devices=cfg["devices"], source="night", count=3)

    async def tick(self):
        if self.now() - self._last_recent_cleanup >= 60:
            self._last_recent_cleanup = self.now()
            self._task(self.repo.prune_recent())
        async with self.lock:
            now = self.now()
            rt = self.active
            if rt and rt.row["status"] in {"preparing", "active"}:
                if now - rt.heartbeat > HEARTBEAT_TIMEOUT:
                    await self._pause_locked(rt, "heartbeat_lost")
                elif rt.last_output_poll is not None and now - rt.last_output_poll > OUTPUT_TIMEOUT:
                    await self._pause_locked(rt, "output_link_lost")
                elif not set(rt.row["authorized_devices"]).issubset(self.devices.connected):
                    await self._pause_locked(rt, "device_disconnected")
                elif rt.row["status"] == "active":
                    rt.player.tick()
                    if now - rt.last_playback_event >= 0.25:
                        rt.last_playback_event = now
                        self._emit_locked(rt, playback=True)
                    if rt.due_at is not None and now >= rt.due_at:
                        rt.due_at = None
                        self._wake(rt, "按约定时间回来")
            for key, kept in list(self.retained.items()):
                if kept.row["status"] == "ended" and now - kept.row["ended_at"] > RETAIN_SECONDS:
                    del self.retained[key]
                    self._resync_events.discard(key)

    async def recover(self):
        recovered = await self.repo.recover()
        await self.repo.prune_recent()
        self._task(self.memory.drain())
        return recovered

    async def close(self):
        async with self.lock:
            if self.active:
                await self._end_locked(self.active, "server_shutdown")
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._pending_events.clear()
        self._resync_events.clear()


_service = None


def get_scene_service():
    global _service
    if _service is None:
        _service = SceneService()
    return _service
