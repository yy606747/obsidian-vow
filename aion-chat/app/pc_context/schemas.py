"""PC activity snapshot contract."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PcActivitySnapshot:
    observed_at: float
    active_state: str
    last_input_age_sec: int | None
    foreground_app: str | None
    foreground_title_sanitized: str | None


__all__ = ["PcActivitySnapshot"]
