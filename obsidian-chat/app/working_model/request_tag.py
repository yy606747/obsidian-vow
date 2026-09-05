"""Strict wire parser for a model-authored Working Model V2 request.

The tag carries a proposal and its source, never a replacement working-model
document.  Its contents are inert data: callers must remove the whole tag
before any tool-command parser sees the remaining assistant text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.chat.commands import (
    ORPHAN_WORKING_MODEL_REQUEST_CLOSE_PATTERN,
    UNFINISHED_WORKING_MODEL_REQUEST_PATTERN,
    WORKING_MODEL_REQUEST_CLOSE,
    WORKING_MODEL_REQUEST_OPEN,
    WORKING_MODEL_REQUEST_PATTERN,
)


WORKING_MODEL_STATEMENT_MAX_CHARS = 240
WORKING_MODEL_SOURCE_MAX_CHARS = 600


@dataclass(frozen=True)
class WorkingModelRequestCandidate:
    statement: str
    source: str


@dataclass(frozen=True)
class WorkingModelRequestExtract:
    found: bool
    candidate: WorkingModelRequestCandidate | None = None
    reject_reason: str | None = None


class WorkingModelRequestParseError(ValueError):
    """The private request marker did not match the frozen JSON contract."""


def _strict_json_object(raw: str) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise WorkingModelRequestParseError("duplicate_key")
            result[key] = value
        return result

    try:
        parsed = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise WorkingModelRequestParseError("invalid_json") from exc
    if not isinstance(parsed, dict):
        raise WorkingModelRequestParseError("not_object")
    if set(parsed) != {"statement", "source"}:
        raise WorkingModelRequestParseError("wrong_fields")
    return parsed


def _normalize_field(value: Any, *, name: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise WorkingModelRequestParseError(f"{name}_not_text")
    text = " ".join(value.split()).strip()
    if not text:
        raise WorkingModelRequestParseError(f"{name}_empty")
    if len(text) > max_chars:
        raise WorkingModelRequestParseError(f"{name}_too_long")
    return text


def parse_working_model_request_payload(raw: str) -> WorkingModelRequestCandidate:
    parsed = _strict_json_object(str(raw or ""))
    return WorkingModelRequestCandidate(
        statement=_normalize_field(
            parsed["statement"],
            name="statement",
            max_chars=WORKING_MODEL_STATEMENT_MAX_CHARS,
        ),
        source=_normalize_field(
            parsed["source"],
            name="source",
            max_chars=WORKING_MODEL_SOURCE_MAX_CHARS,
        ),
    )


def extract_working_model_request(text: str) -> tuple[str, WorkingModelRequestExtract]:
    """Remove every request marker and return at most one valid candidate.

    Multiple markers, an unfinished marker, malformed JSON, or a stray close
    marker all reject the candidate while still hiding all private text.
    """

    raw = str(text or "")
    matches = list(WORKING_MODEL_REQUEST_PATTERN.finditer(raw))
    without_complete = WORKING_MODEL_REQUEST_PATTERN.sub("", raw)
    has_unfinished = bool(UNFINISHED_WORKING_MODEL_REQUEST_PATTERN.search(without_complete))
    has_orphan_close = bool(
        ORPHAN_WORKING_MODEL_REQUEST_CLOSE_PATTERN.search(without_complete)
    )
    cleaned = UNFINISHED_WORKING_MODEL_REQUEST_PATTERN.sub("", without_complete)
    cleaned = ORPHAN_WORKING_MODEL_REQUEST_CLOSE_PATTERN.sub("", cleaned).strip()

    total = len(matches) + (1 if has_unfinished else 0)
    if total == 0 and not has_orphan_close:
        return cleaned, WorkingModelRequestExtract(found=False)
    if total != 1 or has_orphan_close:
        return cleaned, WorkingModelRequestExtract(
            found=True,
            reject_reason="marker_count_invalid",
        )
    if has_unfinished:
        return cleaned, WorkingModelRequestExtract(
            found=True,
            reject_reason="marker_unfinished",
        )
    try:
        candidate = parse_working_model_request_payload(matches[0].group(1))
    except WorkingModelRequestParseError as exc:
        return cleaned, WorkingModelRequestExtract(
            found=True,
            reject_reason=str(exc),
        )
    return cleaned, WorkingModelRequestExtract(found=True, candidate=candidate)


__all__ = [
    "WORKING_MODEL_REQUEST_CLOSE",
    "WORKING_MODEL_REQUEST_OPEN",
    "WORKING_MODEL_SOURCE_MAX_CHARS",
    "WORKING_MODEL_STATEMENT_MAX_CHARS",
    "WorkingModelRequestCandidate",
    "WorkingModelRequestExtract",
    "WorkingModelRequestParseError",
    "extract_working_model_request",
    "parse_working_model_request_payload",
]
