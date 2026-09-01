import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

PROTECTED_MODULE_DIRS = [
    ROOT / "app" / "api" / "control",
    ROOT / "app" / "api" / "location",
    ROOT / "app" / "api" / "sentinel",
    ROOT / "app" / "background",
    ROOT / "app" / "sentinel",
    ROOT / "app" / "location",
]

PROTECTED_ROUTE_FILES = [
    ROOT / "routes" / "events.py",
    ROOT / "routes" / "sentinel.py",
]

ALLOWED_MUTABLE_ROUTES = {
    "/api/sentinel/config": {"PUT"},
    "/api/sentinel/context-trigger-shadow/{evaluation_id}/label": {"PUT"},
}

FORBIDDEN_LEGACY_IMPORTS = {
    "activity",
    "ai_providers",
    "camera",
    "database",
    "location",
    "memory",
    "music",
    "schedule",
    "sensing",
    "voice",
    "ws",
}

FORBIDDEN_SIDE_EFFECT_CALLS = {
    "append_activity_log",
    "append_monitor_log",
    "append_sensing_entry",
    "broadcast",
    "create_task",
    "execute",
    "get_db",
    "open",
    "perform_cam_check",
    "process_heartbeat",
    "process_schedule_commands",
    "save_chat_status",
    "stream_ai",
    "write_text",
}

FORBIDDEN_DURABLE_EVIDENCE_CALLS = {
    "connect",
    "execute",
    "get_db",
    "open",
    "write_text",
}


def test_future_modules_do_not_import_legacy_big_files_directly():
    violations = []
    for path in _protected_files():
        for imported in _direct_imports(path):
            if imported in FORBIDDEN_LEGACY_IMPORTS:
                violations.append(f"{path.relative_to(ROOT)} imports {imported}")

    assert violations == []


def test_future_modules_do_not_call_side_effects_directly():
    violations = []
    for path in _protected_files():
        for call_name in _direct_calls(path):
            if call_name in FORBIDDEN_SIDE_EFFECT_CALLS:
                violations.append(f"{path.relative_to(ROOT)} calls {call_name}")

    assert violations == []


def test_phase8_evidence_foundation_does_not_persist_records_yet():
    violations = []
    for path in sorted((ROOT / "app" / "events").rglob("*.py")):
        for call_name in _direct_calls(path):
            if call_name in FORBIDDEN_DURABLE_EVIDENCE_CALLS:
                violations.append(f"{path.relative_to(ROOT)} calls {call_name}")

    assert violations == []


def test_phase8_diagnostic_routes_are_read_only():
    from routes import events, sentinel

    routes = [*events.router.routes]
    assert routes
    for route in routes:
        assert route.methods == {"GET"}

    for route in sentinel.router.routes:
        if route.path in ALLOWED_MUTABLE_ROUTES:
            assert route.methods == ALLOWED_MUTABLE_ROUTES[route.path]
        else:
            assert route.methods == {"GET"}


def _direct_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.level == 0:
                imports.add(node.module.split(".", 1)[0])
    return imports


def _direct_calls(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            calls.add(func.id)
        elif isinstance(func, ast.Attribute):
            calls.add(func.attr)
    return calls


def _protected_files() -> list[Path]:
    paths = []
    for directory in PROTECTED_MODULE_DIRS:
        if directory.exists():
            paths.extend(sorted(directory.rglob("*.py")))
    paths.extend(path for path in PROTECTED_ROUTE_FILES if path.exists())
    return paths
