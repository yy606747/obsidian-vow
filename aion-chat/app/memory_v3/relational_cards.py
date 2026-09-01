"""Mechanical contract for free-form relational memory cards.

The validator proves provenance and bounded shape only.  It deliberately does
not claim that a relationship interpretation is correct or worth retaining.
"""

from __future__ import annotations

import re

from .provenance import source_hash_for_messages


MAX_NOTE_CHARS = 200
MAX_QUOTE_CHARS = 100
MAX_TOTAL_QUOTE_CHARS = 300
ASSISTANT_COPY_WINDOW = 20
CARD_KINDS = {"shared_moment", "relational_reading"}
ABSTAIN_REASONS = {
    "raw_or_digest_sufficient",
    "no_grounded_extra_memory",
    "not_self_contained",
    "no_durable_relation",
}
LONGITUDINAL_MARKERS = (
    "第一次",
    "头一次",
    "又",
    "再一次",
    "一直",
    "从来",
    "越来越",
    "终于",
    "重新",
)


class RelationalCardContractError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _one_line(value: object) -> str:
    return " ".join(str(value or "").split())


def _assistant_copy_detected(note: str, messages: list[dict]) -> bool:
    normalized_note = _one_line(note)
    for message in messages:
        if message.get("role") != "assistant":
            continue
        source = _one_line(message.get("content"))
        if len(source) < ASSISTANT_COPY_WINDOW:
            continue
        for start in range(0, len(source) - ASSISTANT_COPY_WINDOW + 1):
            if source[start: start + ASSISTANT_COPY_WINDOW] in normalized_note:
                return True
    return False


def longitudinal_marker_warning(note: str, messages: list[dict]) -> list[str]:
    """Return unsupported-looking longitudinal markers without rejecting.

    This is deliberately a review warning, not semantic validation: seeing the
    same word in the source does not prove that the note's longitudinal claim
    is supported.
    """

    source_text = "\n".join(str(message.get("content") or "") for message in messages)
    return [
        marker
        for marker in LONGITUDINAL_MARKERS
        if marker in note and marker not in source_text
    ]


def validate_relational_card(payload: dict, chunk_messages: list[dict]) -> dict:
    if not isinstance(payload, dict):
        raise RelationalCardContractError("invalid_payload", "payload must be an object")
    decision = str(payload.get("decision") or "").strip().lower()
    if decision == "abstain":
        reason = str(payload.get("reason_code") or "").strip().lower()
        if reason not in ABSTAIN_REASONS:
            raise RelationalCardContractError(
                "invalid_abstain_reason",
                "reason_code must name one allowed abstain reason",
            )
        return {
            "schema_version": 1,
            "decision": "abstain",
            "reason_code": reason,
        }
    if decision != "create":
        raise RelationalCardContractError("invalid_decision", "decision must be create or abstain")

    kind = str(payload.get("kind") or "").strip().lower()
    if kind not in CARD_KINDS:
        raise RelationalCardContractError(
            "invalid_card_kind",
            "kind must be shared_moment or relational_reading",
        )

    raw_note = str(payload.get("note") or "")
    if "\n" in raw_note or "\r" in raw_note:
        raise RelationalCardContractError(
            "note_not_single_paragraph", "note must be one paragraph"
        )
    note = _one_line(raw_note)
    if not note:
        raise RelationalCardContractError("empty_note", "note is required")
    if len(note) > MAX_NOTE_CHARS:
        raise RelationalCardContractError("note_too_long", "note exceeds character limit")

    by_id = {str(message.get("id") or ""): dict(message) for message in chunk_messages}
    declared_ids = [str(value) for value in payload.get("source_message_ids") or [] if str(value)]
    declared_ids = list(dict.fromkeys(declared_ids))
    if not declared_ids:
        raise RelationalCardContractError("missing_sources", "source_message_ids is required")
    if any(source_id not in by_id for source_id in declared_ids):
        raise RelationalCardContractError("source_outside_chunk", "source is not part of this chunk")

    raw_quotes = payload.get("quotes")
    if not isinstance(raw_quotes, list) or not raw_quotes:
        raise RelationalCardContractError("missing_quote", "at least one exact quote is required")
    quotes: list[dict] = []
    seen_quotes: set[tuple[str, str]] = set()
    has_user_quote = False
    total_quote_chars = 0
    for raw in raw_quotes:
        if not isinstance(raw, dict):
            raise RelationalCardContractError("invalid_quote", "quote must be an object")
        source_id = str(raw.get("source_message_id") or "")
        quote = str(raw.get("quote") or "")
        if source_id not in declared_ids:
            raise RelationalCardContractError(
                "quote_source_not_declared", "quote source must be declared by the card"
            )
        total_quote_chars += len(quote)
        if len(quote) > MAX_QUOTE_CHARS or total_quote_chars > MAX_TOTAL_QUOTE_CHARS:
            raise RelationalCardContractError(
                "quote_too_long",
                f"each quote must be at most {MAX_QUOTE_CHARS} characters and "
                f"all quotes at most {MAX_TOTAL_QUOTE_CHARS}",
            )
        if not quote or quote not in str(by_id[source_id].get("content") or ""):
            raise RelationalCardContractError(
                "quote_not_in_message", "quote must be a verbatim substring of messages.content"
            )
        key = (source_id, quote)
        if key in seen_quotes:
            raise RelationalCardContractError("duplicate_quote", "duplicate quote")
        seen_quotes.add(key)
        quotes.append({"source_message_id": source_id, "quote": quote})
        if str(by_id[source_id].get("role") or "") == "user":
            has_user_quote = True

    # The writer sees the whole chunk, so undeclared assistant messages can be
    # copied too.  Scan the complete input rather than trusting declared source
    # IDs to define the only possible voice-contamination surface.
    if _assistant_copy_detected(note, list(by_id.values())):
        raise RelationalCardContractError(
            "assistant_verbatim_copy",
            f"note copies at least {ASSISTANT_COPY_WINDOW} normalized assistant characters",
        )

    if not has_user_quote:
        raise RelationalCardContractError(
            "missing_user_quote",
            "at least one verbatim user quote must support this relationship memory",
        )

    source_rows = [by_id[source_id] for source_id in declared_ids]
    return {
        "schema_version": 1,
        "decision": "create",
        "kind": kind,
        "note": note,
        "source_message_ids": declared_ids,
        "quotes": quotes,
        "source_hash": source_hash_for_messages(source_rows),
        "longitudinal_marker_warning": longitudinal_marker_warning(
            note, list(by_id.values())
        ),
    }


__all__ = [
    "ABSTAIN_REASONS",
    "ASSISTANT_COPY_WINDOW",
    "CARD_KINDS",
    "LONGITUDINAL_MARKERS",
    "MAX_NOTE_CHARS",
    "MAX_QUOTE_CHARS",
    "MAX_TOTAL_QUOTE_CHARS",
    "RelationalCardContractError",
    "longitudinal_marker_warning",
    "validate_relational_card",
]
