from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CHAT_ROOT = ROOT / "obsidian-chat"
PC_CONTEXT = CHAT_ROOT / "app" / "pc_context"
PC_AGENT = ROOT / "pc_agent"


def test_pc_context_line_count_limits():
    limits = {
        PC_CONTEXT / "__init__.py": 15,
        PC_CONTEXT / "schemas.py": 40,
        PC_CONTEXT / "privacy.py": 200,
        PC_CONTEXT / "app_map.py": 120,
        PC_CONTEXT / "service.py": 150,
        PC_AGENT / "agent.py": 180,
    }
    for path, limit in limits.items():
        assert _line_count(path) <= limit, path.relative_to(ROOT)
    total = sum(
        _line_count(PC_CONTEXT / name)
        for name in ("schemas.py", "privacy.py", "app_map.py", "service.py")
    )
    assert total <= 510


def test_pc_snapshot_contract_has_five_fields():
    from app.pc_context.schemas import PcActivitySnapshot

    assert list(PcActivitySnapshot.__dataclass_fields__) == [
        "observed_at",
        "active_state",
        "last_input_age_sec",
        "foreground_app",
        "foreground_title_sanitized",
    ]


def test_pc_context_import_boundaries():
    forbidden_by_file = {
        PC_CONTEXT / "privacy.py": _project_imports(),
        PC_CONTEXT / "app_map.py": _project_imports(),
        PC_CONTEXT / "schemas.py": _project_imports(),
        PC_CONTEXT / "service.py": {
            "activity", "ws", "database", "ai_providers", "app.sentinel",
            "app.memory_v2",
        },
    }
    for path, forbidden in forbidden_by_file.items():
        imports = _imports(path)
        assert not imports.intersection(forbidden), path.relative_to(ROOT)


def test_pc_agent_import_boundaries_and_no_disallowed_patterns():
    imports = _imports(PC_AGENT / "agent.py")
    assert not any(item.startswith("app.") for item in imports)
    assert "activity" not in imports
    assert {"privacy", "app_map"}.issubset(imports)

    for path in [*PC_CONTEXT.glob("*.py"), PC_AGENT / "agent.py"]:
        source = path.read_text(encoding="utf-8")
        assert "Protocol" not in source
        assert "ABC" not in source
        assert "schema_version" not in source


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def _project_imports() -> set[str]:
    return {
        "activity", "ai_providers", "camera", "database", "location",
        "memory", "music", "schedule", "sensing", "voice", "ws",
        "app.events", "app.legacy_adapters", "app.memory_v2", "app.sentinel",
    }
