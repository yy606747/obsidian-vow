"""隔离检查使用的进程级保护；普通运行不改变网络行为。"""

from __future__ import annotations

import os
from pathlib import Path
import socket


TEST_MODE = os.environ.get("AION_TEST_MODE", "").strip().lower() in {
    "1", "true", "yes", "on",
}
_guard_installed = False


def resolve_data_dir(base_dir: Path) -> Path:
    default = (base_dir / "data").resolve()
    configured = os.environ.get("AION_DATA_DIR", "").strip()
    if TEST_MODE and not configured:
        raise RuntimeError("测试模式必须显式设置 AION_DATA_DIR，不能使用真实数据目录。")
    selected = Path(configured).expanduser().resolve() if configured else default
    if TEST_MODE and (
        selected == default or default in selected.parents or selected in default.parents
    ):
        raise RuntimeError("测试数据目录不能与默认运行数据目录重叠。")
    return selected


def install_test_network_guard() -> None:
    """阻止真实网络连接，保留本地套接字和模拟传输，供异步测试使用。"""
    global _guard_installed
    if not TEST_MODE or _guard_installed:
        return

    # 测试中的假凭据可在安装保护后注入；不继承启动进程的真实凭据。
    for name in tuple(os.environ):
        upper = name.upper()
        if upper.endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_KEY_ID")) or upper in {
            "GOOGLE_APPLICATION_CREDENTIALS", "AWS_SHARED_CREDENTIALS_FILE",
        }:
            os.environ.pop(name, None)

    def blocked(*_args, **_kwargs):
        raise RuntimeError("AION_TEST_MODE 禁止真实网络请求，请使用模拟传输。")

    for method in ("connect", "connect_ex", "sendto"):
        original = getattr(socket.socket, method)

        def guarded(sock, *args, _original=original, **kwargs):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                return blocked()
            return _original(sock, *args, **kwargs)

        setattr(socket.socket, method, guarded)
    socket.create_connection = blocked
    socket.getaddrinfo = blocked
    _guard_installed = True
