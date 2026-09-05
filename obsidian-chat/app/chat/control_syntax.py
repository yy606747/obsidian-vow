"""Width-tolerant syntax for model-authored legacy control markers.

Only known marker envelopes are normalized or hidden.  Payload text and
ordinary Chinese punctuation are left untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


VALUE_MARKERS = {
    name.upper(): name
    for name in (
        "MOBILE_SCREEN_CHECK",
        "SCREEN_CHECK",
        "POI_SEARCH",
        "MUSIC",
        "ALARM",
        "REMINDER",
        "Monitor",
        "SCHEDULE_DEL",
        "TOY",
        "HEART",
        "RING",
        "REMEMBER",
        "VIEW_IMAGE",
        "VOW",
        "UPDATE_MODEL",
        "查看动态",
        "PRESENCE_DRAW",
        "PRESENCE_SHOW",
        "SELF_WAKE",
    )
}

LITERAL_MARKERS = {
    name.upper(): name
    for name in (
        "CAM_CHECK",
        "SCHEDULE_LIST",
        "SELF_WAKE_CANCEL",
        "OPPORTUNITY_NONE",
        "OPPORTUNITY_REFLECT",
        "SELF_WAKE_NONE",
    )
}

PAIRED_MARKERS = {
    name.upper(): name
    for name in (
        "WORKING_MODEL_REQUEST",
        "RECALL_INTENT",
        "WEB_SEARCH_INTENT",
    )
}

# TIDE_INTENT deliberately uses ``[TIDE_INTENT:payload[/TIDE_INTENT]``.
COLON_PAIRED_MARKERS = {"TIDE_INTENT": "TIDE_INTENT"}

_ALL_MARKERS = {
    **VALUE_MARKERS,
    **LITERAL_MARKERS,
    **PAIRED_MARKERS,
    **COLON_PAIRED_MARKERS,
}
_PAIR_KEYS = frozenset({*PAIRED_MARKERS, *COLON_PAIRED_MARKERS})
_NAME_ALTERNATION = "|".join(
    re.escape(name) for name in sorted(_ALL_MARKERS.values(), key=len, reverse=True)
)
_MARKER_HEAD = re.compile(
    rf"[\[【]\s*(?P<slash>/?)\s*(?P<name>{_NAME_ALTERNATION})"
    rf"\s*(?P<colon>[:：]?)\s*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _MarkerSpan:
    end: int
    canonical: str


def _canonical_name(raw: str) -> str:
    return _ALL_MARKERS[str(raw).upper()]


def _close_token_pattern(name: str) -> re.Pattern[str]:
    return re.compile(
        rf"[\[【]\s*/\s*{re.escape(name)}\s*[\]】]",
        re.IGNORECASE,
    )


def _balanced_value_end(text: str, start: int) -> int | None:
    depth = 1
    for index in range(start, len(text)):
        char = text[index]
        if char in "[【":
            depth += 1
        elif char in "]】":
            depth -= 1
            if depth == 0:
                return index
    return None


def _simple_value_end(text: str, start: int) -> int | None:
    positions = [index for index in (text.find("]", start), text.find("】", start)) if index >= 0]
    return min(positions) if positions else None


def _marker_at(text: str, start: int) -> _MarkerSpan | None:
    match = _MARKER_HEAD.match(text, start)
    if match is None:
        return None

    key = match.group("name").upper()
    name = _canonical_name(match.group("name"))
    slash = bool(match.group("slash"))
    colon = bool(match.group("colon"))
    payload_start = match.end()

    if slash:
        if colon or key not in _PAIR_KEYS:
            return None
        if payload_start >= len(text):
            return _MarkerSpan(len(text), f"[/{name}")
        if text[payload_start] not in "]】":
            return None
        return _MarkerSpan(payload_start + 1, f"[/{name}]")

    if colon:
        if key in COLON_PAIRED_MARKERS:
            close = _close_token_pattern(name).search(text, payload_start)
            if close is None:
                return _MarkerSpan(len(text), f"[{name}:" + text[payload_start:])
            payload = text[payload_start:close.start()]
            return _MarkerSpan(close.end(), f"[{name}:{payload}[/{name}]")

        if key not in VALUE_MARKERS:
            return None
        value_end = (
            _balanced_value_end(text, payload_start)
            if key == "VOW"
            else _simple_value_end(text, payload_start)
        )
        if value_end is None:
            return _MarkerSpan(len(text), f"[{name}:" + text[payload_start:])
        payload = text[payload_start:value_end]
        return _MarkerSpan(value_end + 1, f"[{name}:{payload}]")

    if key not in LITERAL_MARKERS and key not in PAIRED_MARKERS:
        return None
    if payload_start >= len(text):
        return _MarkerSpan(len(text), f"[{name}")
    if text[payload_start] not in "]】":
        return None
    opening_end = payload_start + 1
    if key in LITERAL_MARKERS:
        return _MarkerSpan(opening_end, f"[{name}]")

    close = _close_token_pattern(name).search(text, opening_end)
    if close is None:
        return _MarkerSpan(len(text), f"[{name}]" + text[opening_end:])
    payload = text[opening_end:close.start()]
    return _MarkerSpan(close.end(), f"[{name}]{payload}[/{name}]")


def _could_be_marker_prefix(text: str) -> bool:
    if not text or text[0] not in "[【":
        return False
    body = text[1:].lstrip()
    closing = body.startswith("/")
    if closing:
        body = body[1:].lstrip()
    if not body:
        return True

    end = 0
    while end < len(body) and not body[end].isspace() and body[end] not in ":：]】":
        end += 1
    candidate = body[:end].upper()
    if not candidate:
        return True
    names = _PAIR_KEYS if closing else _ALL_MARKERS.keys()
    if not any(name.startswith(candidate) for name in names):
        return False

    remainder = body[end:]
    if not remainder:
        return True
    if candidate in names and not remainder.strip():
        return True
    return False


def canonicalize_control_markers(text: str) -> str:
    """Canonicalize known tag envelopes to ``[NAME:payload]`` syntax."""

    raw = str(text or "")
    output: list[str] = []
    index = 0
    while index < len(raw):
        marker = _marker_at(raw, index) if raw[index] in "[【" else None
        if marker is None:
            output.append(raw[index])
            index += 1
            continue
        output.append(marker.canonical)
        index = marker.end
    return "".join(output)


def strip_control_markers(text: str, *, hide_partial: bool = True) -> str:
    """Remove known complete, malformed-tail, and orphan-close markers."""

    raw = str(text or "")
    output: list[str] = []
    index = 0
    while index < len(raw):
        marker = _marker_at(raw, index) if raw[index] in "[【" else None
        if marker is not None:
            index = marker.end
            continue
        if hide_partial and raw[index] in "[【" and _could_be_marker_prefix(raw[index:]):
            break
        output.append(raw[index])
        index += 1
    return "".join(output).strip()


def contains_control_marker(text: str) -> bool:
    raw = str(text or "")
    for index, char in enumerate(raw):
        if char in "[【" and (
            _marker_at(raw, index) is not None
            or _could_be_marker_prefix(raw[index:])
        ):
            return True
    return False


class ControlMarkerStreamFilter:
    """Project an append-only stream without ever exposing known markers."""

    def __init__(self):
        self._source = ""
        self._visible = ""

    def feed(self, chunk: str) -> str:
        self._source += str(chunk or "")
        visible = strip_control_markers(self._source)
        if not visible.startswith(self._visible):
            # Partial prefixes are withheld, so this should not occur.  If a
            # future syntax breaks that invariant, fail private instead of
            # attempting to retract text already sent over SSE.
            return ""
        delta = visible[len(self._visible):]
        self._visible = visible
        return delta

    def flush(self) -> str:
        return self.feed("")


__all__ = [
    "ControlMarkerStreamFilter",
    "LITERAL_MARKERS",
    "PAIRED_MARKERS",
    "VALUE_MARKERS",
    "canonicalize_control_markers",
    "contains_control_marker",
    "strip_control_markers",
]
