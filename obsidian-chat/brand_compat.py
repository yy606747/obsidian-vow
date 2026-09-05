"""Upgrade entry points for persisted identifiers from earlier installations."""

from __future__ import annotations

import os
from collections.abc import MutableMapping


LEGACY_AUTH_COOKIE = "aion_token"
LEGACY_STRUCTURED_FENCE = "```aion"


def migrate_environment(environ: MutableMapping[str, str] | None = None) -> None:
    """Accept old deployment variables while giving explicit new values priority."""
    target = os.environ if environ is None else environ
    for name, value in tuple(target.items()):
        if name.startswith("AION_"):
            target.setdefault("OBSIDIAN_" + name[len("AION_"):], value)
