"""Prompt-budget helpers shared by legacy migration and future V2 injection."""

WORKING_MODEL_ACTIVE_MAX_CHARS = 1200


def normalize_prompt_content(content: object) -> str:
    return str(content or "").strip()


def clip_legacy_prompt_content(
    content: object,
    max_chars: int = WORKING_MODEL_ACTIVE_MAX_CHARS,
) -> str:
    """Reproduce the exact text the legacy prompt builder exposed.

    This is intentionally not a generic truncator: migration must preserve the
    legacy ``text[:1197].rstrip() + '...'`` behavior for a 1200-char budget.
    """

    text = normalize_prompt_content(content)
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."
