"""Shared detection for provider error text that must not be persisted as chat."""

from __future__ import annotations


MODEL_ERROR_PREFIXES = (
    "[请求出错:",
    "[Gemini错误",
    "[硅基流动错误",
    "[中转站错误",
    "[错误]",
)


def looks_like_model_error_text(text: str | None) -> bool:
    value = str(text or "").strip()
    if not value:
        return True
    return value.startswith(MODEL_ERROR_PREFIXES)
