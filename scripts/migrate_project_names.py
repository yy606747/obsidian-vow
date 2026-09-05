#!/usr/bin/env python3
"""Migrate project directories and .env keys; preview unless --apply is supplied."""

import argparse
from pathlib import Path
import os
import re
import shutil
import tempfile


DIRECTORIES = (("aion-chat", "obsidian-chat"), ("AionApp", "ObsidianApp"))


def renamed_env(text):
    pattern = re.compile(r"(?m)^(\s*(?:export\s+)?)AION_([A-Z0-9_]+)(\s*=)")
    existing = set(re.findall(r"(?m)^\s*(?:export\s+)?(OBSIDIAN_[A-Z0-9_]+)\s*=", text))

    def replace(match):
        new_name = "OBSIDIAN_" + match[2]
        # An explicitly configured new key takes precedence. Retain the old
        # assignment as a comment so dotenv cannot override the new value.
        prefix = "# migrated legacy value: " if new_name in existing else ""
        return prefix + match[1] + new_name + match[3]

    return pattern.sub(replace, text)


def placeholder(path):
    return path.is_dir() and all(
        child.is_dir() or (child.name == ".gitkeep" and child.stat().st_size == 0)
        for child in path.rglob("*")
    )


def migrate(root, *, apply=False, keep_runtime_alias=False):
    root = Path(root).resolve()
    moves = []
    archives = []
    aliases = []
    for old_name, new_name in DIRECTORIES:
        old, new = root / old_name, root / new_name
        if old.is_symlink():
            if old.resolve() == new.resolve():
                continue
            raise ValueError("Unexpected legacy directory symlink: " + str(old))
        if not old.exists():
            continue
        if new.is_symlink():
            raise ValueError("Unexpected destination symlink: " + str(new))
        if not new.exists():
            moves.append((old, new))
        else:
            # A checkout can already contain the renamed sources while ignored
            # runtime data and machine configuration remain in the old folder.
            for name in ("data", ".env", "local.properties"):
                source, target = old / name, new / name
                if not source.exists():
                    continue
                if target.exists():
                    if name == "data" and placeholder(target):
                        archives.append(target)
                    else:
                        raise ValueError("Both installations contain " + name + "; resolve before migration")
                moves.append((source, target))
            archives.append(old)
        if keep_runtime_alias and old_name == "aion-chat":
            aliases.append((old, new))

    env_changes = []
    for relative in (".env", "aion-chat/.env", "obsidian-chat/.env"):
        path = root / relative
        if not path.is_file():
            continue
        original = path.read_text(encoding="utf-8")
        updated = renamed_env(original)
        if updated != original:
            env_changes.append((path, updated))

    actions = (["move " + str(a.relative_to(root)) + " -> " + str(b.relative_to(root)) for a, b in moves]
               + ["archive " + str(p.relative_to(root)) for p in archives]
               + ["rename environment keys in " + str(p.relative_to(root)) for p, _ in env_changes]
               + ["retain container mount alias " + str(a.relative_to(root)) for a, _ in aliases])
    if not apply or not actions:
        return actions

    backup_parent = root / ".codex-backups"
    backup_parent.mkdir(mode=0o700, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="project-names-", dir=str(backup_parent)))
    os.chmod(backup, 0o700)
    for path, updated in env_changes:
        saved = backup / "env" / path.relative_to(root)
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(path), str(saved))
        saved.chmod(0o600)
        path.write_text(updated, encoding="utf-8")
    # Empty checkout placeholders must move before their data replacements.
    for path in archives:
        if path.name != "data":
            continue
        target = backup / "placeholders" / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        path.rename(target)
    for source, target in moves:
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
    for path in archives:
        if path.name == "data":
            continue
        target = backup / "sources" / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        path.rename(target)
    for old, new in aliases:
        old.symlink_to(new.name, target_is_directory=True)
    return actions + ["backup: " + str(backup)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--keep-runtime-alias", action="store_true",
                        help="Keep an old backend path for existing Docker bind mounts during hot updates")
    args = parser.parse_args()
    for action in migrate(args.root, apply=args.apply, keep_runtime_alias=args.keep_runtime_alias):
        print(action)


if __name__ == "__main__":
    main()
