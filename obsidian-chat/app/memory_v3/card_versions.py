"""Shared relational-card prompt-version policy.

Generation and readout must use one source of truth: older cards remain in the
database for provenance, while only the currently approved family is allowed
to replace raw chunk presentation in prompts.
"""

from __future__ import annotations


CURRENT_RELATIONAL_CARD_PROMPT_VERSION = "relational-card-v5.2"
READABLE_RELATIONAL_CARD_PROMPT_VERSIONS = frozenset(
    {
        "relational-card-v5.1",
        CURRENT_RELATIONAL_CARD_PROMPT_VERSION,
    }
)


def is_readable_relational_card_prompt_version(value: object) -> bool:
    return str(value or "") in READABLE_RELATIONAL_CARD_PROMPT_VERSIONS


__all__ = [
    "CURRENT_RELATIONAL_CARD_PROMPT_VERSION",
    "READABLE_RELATIONAL_CARD_PROMPT_VERSIONS",
    "is_readable_relational_card_prompt_version",
]
