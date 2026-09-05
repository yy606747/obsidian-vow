import importlib

import config


def _isolate_chat_status(tmp_path, monkeypatch):
    path = tmp_path / "chat_status.json"
    monkeypatch.setattr(config, "CHAT_STATUS_PATH", path)
    return path


def test_set_chat_status_line_appends_then_replaces(tmp_path, monkeypatch):
    _isolate_chat_status(tmp_path, monkeypatch)

    config.save_chat_status("阿玖刚睡醒")
    config.set_chat_status_line("[位置]", "[位置] 外出中，当前在：某街")
    assert config.load_chat_status()["status"] == "阿玖刚睡醒\n[位置] 外出中，当前在：某街"

    # 再来一次只替换那一行，不重复追加
    config.set_chat_status_line("[位置]", "[位置] 在家")
    assert config.load_chat_status()["status"] == "阿玖刚睡醒\n[位置] 在家"


def test_digest_overwrite_preserves_location_line(tmp_path, monkeypatch):
    _isolate_chat_status(tmp_path, monkeypatch)

    config.set_chat_status_line("[位置]", "[位置] 外出中")
    # 记忆摘要重新生成整串状态——位置行必须存活
    merged = config.save_chat_status_preserving("阿玖在咖啡馆写代码，心情不错")
    assert merged == "阿玖在咖啡馆写代码，心情不错\n[位置] 外出中"
    assert config.load_chat_status()["status"] == merged


def test_digest_with_own_location_line_does_not_duplicate(tmp_path, monkeypatch):
    _isolate_chat_status(tmp_path, monkeypatch)

    config.set_chat_status_line("[位置]", "[位置] 外出中")
    merged = config.save_chat_status_preserving("新状态\n[位置] 在家")
    assert merged == "新状态\n[位置] 在家"
