from __future__ import annotations

import ast
from pathlib import Path


OBSIDIAN_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = OBSIDIAN_ROOT.parent
RUNTIME_PACKAGES = (OBSIDIAN_ROOT / "app/memory_v2", OBSIDIAN_ROOT / "app/memory_v3")
EXPERIMENTAL_V2_MODULES = {
    "cards.py",
    "compare_recall.py",
    "gold_eval.py",
    "live_chat_eval.py",
    "migration_compare.py",
    "prompt_block_replay.py",
    "replay_eval.py",
    "trace_recall.py",
    "v2_recall.py",
}


def _import_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_memory_runtime_does_not_import_devtools() -> None:
    violations: list[str] = []
    for package in RUNTIME_PACKAGES:
        for path in package.rglob("*.py"):
            for name in _import_names(path):
                if name == "devtools" or name.startswith("devtools."):
                    violations.append(f"{path.relative_to(OBSIDIAN_ROOT)} -> {name}")
    assert violations == []


def test_memory_v2_runtime_contains_no_experiment_modules() -> None:
    present = {
        path.name
        for path in (OBSIDIAN_ROOT / "app/memory_v2").glob("*.py")
        if path.name in EXPERIMENTAL_V2_MODULES
    }
    assert present == set()


def test_devtools_and_tests_are_excluded_from_production_context() -> None:
    entries = {
        line.strip()
        for line in (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert "obsidian-chat/devtools/" in entries
    assert "obsidian-chat/tests/" in entries
