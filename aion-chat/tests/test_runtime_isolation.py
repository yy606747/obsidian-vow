import asyncio
from pathlib import Path
import socket

import httpx
import pytest

import config
import runtime_safety


def test_tests_use_separate_data_directory():
    assert config.TEST_MODE is True
    assert config.DATA_DIR != (config.BASE_DIR / "data").resolve()
    assert config.DB_PATH.parent == config.DATA_DIR
    assert config.UPLOADS_DIR.parent == config.DATA_DIR


def test_test_mode_requires_explicit_nonoverlapping_data_directory(monkeypatch):
    monkeypatch.delenv("AION_DATA_DIR")
    with pytest.raises(RuntimeError, match="显式设置"):
        runtime_safety.resolve_data_dir(config.BASE_DIR)
    for path in (config.BASE_DIR, config.BASE_DIR / "data", config.BASE_DIR / "data" / "tests"):
        monkeypatch.setenv("AION_DATA_DIR", str(path))
        with pytest.raises(RuntimeError, match="重叠"):
            runtime_safety.resolve_data_dir(config.BASE_DIR)


def test_normal_mode_preserves_default_path(monkeypatch):
    monkeypatch.setattr(runtime_safety, "TEST_MODE", False)
    monkeypatch.delenv("AION_DATA_DIR")
    assert runtime_safety.resolve_data_dir(config.BASE_DIR) == (config.BASE_DIR / "data").resolve()


def test_test_mode_does_not_read_dotenv_or_legacy_key_file(monkeypatch, tmp_path):
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: pytest.fail("读取了环境凭据"))
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    original_exists = Path.exists

    def exists(path):
        if path.name in {".env", "所需要的API.txt"}:
            pytest.fail("访问了真实凭据文件")
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", exists)
    config._load_dotenv_safe()
    assert not config.load_settings().get("gemini_key")


def test_real_network_is_blocked_but_mock_transport_works():
    with pytest.raises(RuntimeError, match="禁止真实网络"):
        socket.create_connection(("example.com", 443))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        with pytest.raises(RuntimeError, match="禁止真实网络"):
            connection.connect(("127.0.0.1", 18080))
    with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200))) as client:
        assert client.get("https://example.com").status_code == 200


def test_async_event_loop_still_works():
    async def run():
        await asyncio.sleep(0)
        return 1

    assert asyncio.run(run()) == 1


def test_lifespan_initializes_database_without_starting_background_tasks(monkeypatch):
    import main

    called = []

    async def init_db():
        called.append("database")

    monkeypatch.setattr(main, "init_db", init_db)
    monkeypatch.setattr(main.sentinel_runtime, "start_monitoring", lambda: pytest.fail("启动了哨兵"))
    monkeypatch.setattr(main.schedule_mgr, "start", lambda: pytest.fail("启动了日程"))
    monkeypatch.setattr(main, "run_startup_cleanup", lambda: pytest.fail("执行了数据清理"))

    async def run():
        async with main.lifespan(main.app):
            assert called == ["database"]

    asyncio.run(run())
