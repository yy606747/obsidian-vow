"""Replay source adapter for Sentinel Attention eval.

This adapter only converts already-loaded replay dictionaries into normalized
EvidenceRecord objects. It has no file, database, model, network, or runtime
side effects.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from app.location_geofence import (
    LOCATION_GEOFENCE_PAYLOAD_SCHEMA_VERSION,
    location_geofence_tags,
    normalize_location_geofence_payload,
)

from .types import (
    EvidenceRecord,
    ReplayEvidenceBundle,
    frozen_tags,
    require_no_decision_fields,
)


_REQUIRED_SIGNAL_FIELDS = ("kind", "source", "text")


def adapt_replay_input(input_payload: Mapping[str, Any]) -> ReplayEvidenceBundle:
    """Normalize one replay input payload into Attention evidence."""
    if not isinstance(input_payload, Mapping):
        raise ValueError("input must be an object")
    require_no_decision_fields("input", input_payload)

    raw_signals = _validated_raw_signals(input_payload.get("raw_signals"))
    recent_chat = _validated_recent_chat(input_payload.get("recent_chat", []))
    reference_time = str(input_payload.get("reference_time") or "").strip()
    evidence = tuple(_adapt_signal(signal) for signal in raw_signals)
    context_tags = frozen_tags(
        tag
        for message in recent_chat
        for tag in _tags_for_text(message)
    )
    return ReplayEvidenceBundle(
        evidence=evidence,
        recent_chat=tuple(recent_chat),
        reference_time=reference_time,
        context_tags=context_tags,
    )


def _validated_raw_signals(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("input.raw_signals must be a list")

    records: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValueError(f"raw_signals[{index}] must be an object")
        require_no_decision_fields(f"raw_signals[{index}]", item)
        record: dict[str, Any] = {}
        for field in _REQUIRED_SIGNAL_FIELDS:
            text = str(item.get(field) or "").strip()
            if not text:
                raise ValueError(f"raw_signals[{index}] requires {field}")
            record[field] = text
        payload = item.get("payload")
        if payload is not None:
            if not isinstance(payload, Mapping):
                raise ValueError(f"raw_signals[{index}].payload must be an object")
            record["payload"] = dict(payload)
        records.append(record)
    return records


def _validated_recent_chat(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("input.recent_chat must be a list")
    chat: list[str] = []
    for index, item in enumerate(value):
        text = str(item or "").strip()
        if not text:
            raise ValueError(f"recent_chat[{index}] must be non-empty text")
        chat.append(text)
    return chat


def _adapt_signal(signal: Mapping[str, Any]) -> EvidenceRecord:
    kind = signal["kind"]
    source = signal["source"]
    text = signal["text"]
    payload = dict(signal.get("payload") or {})
    tags = _tags_for_signal(kind=kind, source=source, text=text, payload=payload)
    return EvidenceRecord(
        kind=kind,
        source=source,
        text=text,
        tags=frozen_tags(tags),
        payload=payload,
    )


def _tags_for_signal(
    *,
    kind: str,
    source: str,
    text: str,
    payload: Mapping[str, Any],
) -> list[str]:
    tags = set(_tags_for_record(kind=kind, text=text, payload=payload))
    tags.add(f"kind:{kind}")
    tags.add(f"source:{source}")

    if "camera" in kind or "camera" in source:
        tags.add("source_family:camera")
    elif "pc" in source or "pc" in kind:
        tags.add("source_family:pc_activity")
    elif "music" in source or "music" in kind:
        tags.add("source_family:music")
    elif kind.startswith("chat."):
        tags.add("source_family:chat")
    elif kind.startswith("sensing."):
        tags.add("source_family:sensing")
    elif kind.startswith("activity."):
        tags.add("source_family:activity")
    elif kind.startswith("location."):
        tags.add("source_family:location")
    elif kind.startswith("schedule."):
        tags.add("source_family:schedule")
    elif kind.startswith("sentinel."):
        tags.add("source_family:sentinel")

    if "camera_disabled" in tags or "pc_disabled" in tags:
        tags.add("source_status:disabled")
    if source == "pc.activity":
        tags.update(_pc_tags_for_text(text))

    return sorted(tags)


def _tags_for_record(*, kind: str, text: str, payload: Mapping[str, Any]) -> set[str]:
    payload_schema = payload.get("payload_schema")
    if not kind.startswith("location."):
        return _tags_for_text(text)
    if payload_schema is None:
        # Historical replay records predate structured Evidence.
        return _tags_for_text(text)
    if payload_schema != LOCATION_GEOFENCE_PAYLOAD_SCHEMA_VERSION:
        raise ValueError(f"unknown location payload_schema: {payload_schema!r}")
    normalized = normalize_location_geofence_payload(payload)
    return set(location_geofence_tags(normalized))


def _pc_tags_for_text(text: str) -> set[str]:
    tags: set[str] = set()
    for state in ("active", "idle", "locked", "offline", "unknown"):
        if f"pc_{state}" in text or f"pc_state={state}" in text:
            tags.add(f"pc_{state}")
    for tag in (
        "pc_foreground_dev_tool",
        "pc_foreground_browser",
        "pc_foreground_media",
    ):
        if tag in text:
            tags.add(tag)
    return tags


def _tags_for_text(text: str) -> set[str]:
    tags: set[str] = set()
    if "先别打扰" in text or "一会回来" in text:
        tags.add("recent_chat_cooldown")
    if "刚正常聊天" in text or "最近互动正常" in text:
        tags.update({"recent_chat_cooldown", "recent_chat_positive"})
    if "争执" in text or "不想说了" in text or "未收束" in text:
        tags.add("relationship_unsettled")
    if "承诺" in text or "作息节点" in text or "前睡觉" in text:
        tags.add("routine_risk")
    if "开会" in text or "忙正事" in text:
        tags.add("busy_declared")
    if "频繁点亮" in text or "手机活跃" in text:
        tags.add("phone_active")
    if (
        "at_home 变为 outside" in text
        or "从在家变为外出" in text
        or "围栏内变为围栏外" in text
        or "范围里走到了范围外" in text
    ):
        tags.add("left_home_transition")
    if (
        "outside 变为 at_home" in text
        or "从外出变为在家" in text
        or "围栏外变为围栏内" in text
        or "范围外回到了范围里" in text
    ):
        tags.add("return_home_transition")
    if "位置数据过期" in text or "旧位置" in text or _stale_location_minutes(text):
        tags.add("stale_location")
    if "持续 outside" in text or (
        ("设备定位" in text or "定位" in text)
        and ("围栏外" in text or "范围外" in text)
        and ("没有观测到新的围栏边界变化" in text or "之后没有新的进出记录" in text)
    ):
        tags.add("outside_continuous")
    if (
        "没有新位置变化" in text
        or "没有观测到新的围栏边界变化" in text
        or "之后没有新的进出记录" in text
    ):
        tags.add("no_location_change")
    if "最后一次解锁" in text:
        tags.add("last_unlock_old")
    if "解锁" in text:
        tags.add("unlock_event")
    if "环境很暗" in text or "环境光较暗" in text:
        tags.add("dark_environment")
    if "充电" in text:
        tags.add("charging")
    if "没有连续使用记录" in text or "没有连续 activity" in text:
        tags.add("no_continuous_activity")
    if "专心写作业" in text or ("IDE" in text and "低频解锁" in text):
        tags.add("busy_study")
    if "motion=running" in text or ("running" in text and "单次" in text):
        tags.add("motion_uncertain")
    if "通知明显增多" in text or "通知变多" in text:
        tags.add("notification_uncertain")
    if "摄像头来源关闭" in text:
        tags.add("camera_disabled")
    if "PC activity source disabled" in text or "PC activity 来源关闭" in text:
        tags.add("pc_disabled")
    if "location_state=at_home" in text or "在家" in text:
        tags.add("at_home")
    if "活动较低" in text:
        tags.add("low_activity")
    if "伤感歌单" in text or "伤感" in text:
        tags.add("mood_uncertain")
    if "6小时前" in text or "长时间没有互动" in text:
        tags.add("long_silence")
    if "社交" in text:
        tags.add("social_activity")
    if "视频" in text:
        tags.add("video_activity")
    if "社交和视频" in text:
        tags.add("social_video_activity")
    return tags


def _stale_location_minutes(text: str) -> bool:
    minutes = _first_match(text, r"更新时间(\d+)分钟前", default="")
    return bool(minutes and int(minutes) >= 60)


def _first_match(text: str, pattern: str, *, default: str) -> str:
    match = re.search(pattern, text)
    if not match:
        return default
    return match.group(1)


__all__ = [
    "adapt_replay_input",
]
