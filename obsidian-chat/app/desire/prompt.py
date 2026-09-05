"""Desire prompt-budget helper; CP0 does not inject this content."""

DESIRE_MAX_CHARS = 200


def normalize_desire_prompt_content(content: object) -> str:
    text = str(content or "").strip()
    if len(text) > DESIRE_MAX_CHARS:
        raise ValueError(f"desire content exceeds {DESIRE_MAX_CHARS} characters")
    return text
