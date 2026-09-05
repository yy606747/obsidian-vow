import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from brand_compat import migrate_environment


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("project_names_migration", ROOT / "scripts/migrate_project_names.py")
project_names = importlib.util.module_from_spec(spec)
spec.loader.exec_module(project_names)


def test_existing_environment_keeps_auth_and_respects_explicit_new_values():
    env = {"AION_AUTH_TOKEN": "existing-token", "AION_GEMINI_KEY": "old-key",
           "OBSIDIAN_GEMINI_KEY": "new-key", "AION_BIND_HOST": "old-host",
           "OBSIDIAN_BIND_HOST": ""}
    migrate_environment(env)
    assert env["OBSIDIAN_AUTH_TOKEN"] == "existing-token"
    assert env["OBSIDIAN_GEMINI_KEY"] == "new-key"
    assert env["OBSIDIAN_BIND_HOST"] == ""
    assert env["AION_AUTH_TOKEN"] == "existing-token"


def test_old_test_environment_is_isolated_before_config_import(tmp_path):
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LD_LIBRARY_PATH"}}
    env.update({"PYTHONPATH": str(ROOT / "obsidian-chat"), "PYTHONDONTWRITEBYTECODE": "1",
                "AION_TEST_MODE": "1", "AION_DATA_DIR": str(tmp_path)})
    program = """
from pathlib import Path
import runtime_safety
assert runtime_safety.TEST_MODE
assert runtime_safety.resolve_data_dir(Path('.')).is_dir()
runtime_safety.install_test_network_guard()
import socket
try:
    socket.getaddrinfo('example.invalid', 443)
except RuntimeError:
    pass
else:
    raise AssertionError('old test environment lost the network guard')
"""
    result = subprocess.run([sys.executable, "-c", program], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("cookies", ["aion_token=existing-token", "obsidian_token=stale; aion_token=existing-token"])
def test_existing_login_is_accepted_and_receives_current_cookie(monkeypatch, cookies):
    import main

    monkeypatch.setattr(main, "_AUTH_TOKEN", "existing-token")
    request = Request({"type": "http", "scheme": "https", "method": "GET",
                       "path": "/api/settings", "query_string": b"",
                       "headers": [(b"cookie", cookies.encode())]})

    async def next_handler(_request):
        return JSONResponse({"ok": True})

    response = asyncio.run(main.auth_middleware(request, next_handler))
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("obsidian_token=existing-token;")
    assert "HttpOnly" in cookie and "Secure" in cookie


def test_invalid_legacy_cookie_does_not_authenticate(monkeypatch):
    import main

    monkeypatch.setattr(main, "_AUTH_TOKEN", "existing-token")
    request = Request({"type": "http", "headers": [(b"cookie", b"aion_token=wrong")]})
    assert not main._check_token(request)


@pytest.mark.parametrize("value,accepted", [("existing-token", True), ("wrong", False)])
def test_existing_websocket_login_survives_server_restart(monkeypatch, value, accepted):
    import main
    from starlette.websockets import WebSocketDisconnect
    from types import SimpleNamespace

    calls = []

    async def connect(_ws):
        calls.append("connected")

    async def receive_text():
        raise WebSocketDisconnect()

    async def close(code):
        calls.append(code)

    ws = SimpleNamespace(cookies={"aion_token": value}, query_params={}, headers={},
                         receive_text=receive_text, close=close)
    monkeypatch.setattr(main, "_AUTH_TOKEN", "existing-token")
    monkeypatch.setattr(main, "manager", SimpleNamespace(connect=connect, disconnect=lambda _ws: None))
    asyncio.run(main.websocket_endpoint(ws))
    assert calls == (["connected"] if accepted else [4401])


def test_migration_preserves_data_env_permissions_and_container_mount(tmp_path):
    old = tmp_path / "aion-chat"
    (old / "data").mkdir(parents=True)
    content = b"existing database contents"
    (old / "data/chat.db").write_bytes(content)
    env = tmp_path / ".env"
    env.write_text("AION_AUTH_TOKEN=existing-token\nOBSIDIAN_GEMINI_KEY=new-key\nAION_GEMINI_KEY=old-key\n")
    env.chmod(0o600)
    project_names.migrate(tmp_path)
    assert old.is_dir() and not (tmp_path / "obsidian-chat").exists()
    project_names.migrate(tmp_path, apply=True, keep_runtime_alias=True)
    current = tmp_path / "obsidian-chat/data/chat.db"
    assert current.read_bytes() == content
    assert old.is_symlink() and (old / "data/chat.db").samefile(current)
    assert "OBSIDIAN_AUTH_TOKEN=existing-token" in env.read_text()
    assert "\nOBSIDIAN_GEMINI_KEY=old-key" not in env.read_text()
    assert env.stat().st_mode & 0o777 == 0o600
    assert project_names.migrate(tmp_path, apply=True, keep_runtime_alias=True) == []


def test_checkout_migration_keeps_existing_data_and_archives_old_sources(tmp_path):
    old = tmp_path / "aion-chat"
    new = tmp_path / "obsidian-chat"
    (old / "data").mkdir(parents=True)
    (old / "data/chat.db").write_bytes(b"saved history")
    (old / "main.py").write_text("old sources")
    (new / "data/uploads").mkdir(parents=True)
    (new / "data/uploads/.gitkeep").touch()
    (new / "main.py").write_text("new sources")
    project_names.migrate(tmp_path, apply=True)
    assert not old.exists()
    assert (new / "data/chat.db").read_bytes() == b"saved history"
    assert (new / "main.py").read_text() == "new sources"
    assert len(list((tmp_path / ".codex-backups").rglob("main.py"))) == 1


def test_conflicting_data_aborts_before_any_configuration_change(tmp_path):
    for directory in ("aion-chat", "obsidian-chat"):
        data = tmp_path / directory / "data"
        data.mkdir(parents=True)
        (data / "chat.db").write_bytes(directory.encode())
    env = tmp_path / ".env"
    env.write_text("AION_AUTH_TOKEN=existing-token\n")
    with pytest.raises(ValueError, match="Both installations contain data"):
        project_names.migrate(tmp_path, apply=True)
    assert env.read_text() == "AION_AUTH_TOKEN=existing-token\n"
    assert not (tmp_path / ".codex-backups").exists()


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required for browser migration checks")
def test_browser_migration_preserves_choices_identity_and_old_android_bridges():
    script = ROOT / "obsidian-chat/static/brand_compat.js"
    program = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
function storage(values) {
  const entries = new Map(Object.entries(values));
  return {
    get length() { return entries.size; },
    key(i) { return Array.from(entries.keys())[i]; },
    getItem(k) { return entries.has(k) ? entries.get(k) : null; },
    setItem(k, v) { entries.set(k, String(v)); },
    removeItem(k) { entries.delete(k); }
  };
}
const oldBridge = {start() { return true; }};
const currentBridge = {current: true};
const window = {
  localStorage: storage({aion_safeword: 'stop', aion_tts_enabled: 'true',
                         obsidian_tts_enabled: 'false', aion_last_conv: 'thread-7'}),
  sessionStorage: storage({aion_control_owner_client_id: 'owner-9'}),
  AionAudio: oldBridge, AionBle: oldBridge, ObsidianBle: currentBridge,
};
vm.runInNewContext(source, {window});
assert.equal(window.localStorage.getItem('obsidian_safeword'), 'stop');
assert.equal(window.localStorage.getItem('obsidian_tts_enabled'), 'false');
assert.equal(window.localStorage.getItem('obsidian_last_conv'), 'thread-7');
assert.equal(window.sessionStorage.getItem('obsidian_control_owner_client_id'), 'owner-9');
assert.equal(window.ObsidianAudio, oldBridge);
assert.equal(window.ObsidianBle, currentBridge);
window.localStorage.removeItem('obsidian_safeword');
vm.runInNewContext(source, {window});
assert.equal(window.localStorage.getItem('obsidian_safeword'), null);
const blocked = {AionAudio: oldBridge};
Object.defineProperty(blocked, 'localStorage', {get() {throw Error('disabled'); }});
vm.runInNewContext(source, {window: blocked});
assert.equal(blocked.ObsidianAudio, oldBridge);
"""
    result = subprocess.run(["node", "-e", program, str(script)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
