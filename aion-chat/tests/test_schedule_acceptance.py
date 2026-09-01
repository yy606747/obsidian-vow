import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEDULE_DIR = ROOT / "app" / "schedule"


def test_schedule_v2_files_stay_small_and_within_declared_boundaries():
    files = {
        "__init__.py": 11,
        "store.py": 107,
        "prompt.py": 136,
        "evidence.py": 47,
        "alarm_context.py": 280,  # 闹钟创建时可见上下文的不可变旁表
        "trigger.py": 600,  # 创建快照 + 三天时间线注入；仍以当前行数封顶
        "commands.py": 340,  # 闹钟创建时同步冻结上下文；仍以当前行数封顶
        "manager.py": 120,
    }
    assert sorted(path.name for path in SCHEDULE_DIR.glob("*.py")) == sorted(files)

    allowed_imports = {
        "__init__.py": {"app"},
        "store.py": {"__future__", "logging", "time", "aiosqlite", "database"},
        "prompt.py": {"__future__", "app"},
        "evidence.py": {"__future__", "logging", "activity", "sensing"},
        "alarm_context.py": {
            "__future__", "json", "logging", "time", "datetime", "zoneinfo",
            "aiosqlite", "database",
        },
        "commands.py": {"__future__", "logging", "re", "time", "datetime", "config", "database", "ws"},
        "manager.py": {"__future__", "asyncio", "logging", "threading", "time", "datetime", "ws"},
        "trigger.py": {
            "__future__", "json", "logging", "time", "datetime", "aiosqlite", "ai_providers",
            "app", "config", "database", "music", "routes", "sentinel_runtime", "ws",
        },
    }

    for filename, max_lines in files.items():
        path = SCHEDULE_DIR / filename
        assert len(path.read_text(encoding="utf-8").splitlines()) <= max_lines
        imports = _direct_imports(path)
        assert imports <= allowed_imports[filename]

    assert "activity" not in _direct_imports(SCHEDULE_DIR / "trigger.py")
    assert "sensing" not in _direct_imports(SCHEDULE_DIR / "trigger.py")


def _direct_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imports.add(node.module.split(".", 1)[0])
    return imports
