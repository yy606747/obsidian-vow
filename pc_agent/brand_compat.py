"""Compatibility with environment configuration of older desktop installs."""

import os


def migrate_environment():
    for name, value in tuple(os.environ.items()):
        if name.startswith("AION_"):
            os.environ.setdefault("OBSIDIAN_" + name[len("AION_"):], value)
