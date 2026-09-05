"""在测试收集前启用隔离，直接运行 pytest 也不会接触默认聊天数据。"""

import atexit
import os
from pathlib import Path
import sys
import tempfile


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["AION_TEST_MODE"] = "1"
if not os.environ.get("AION_DATA_DIR"):
    _test_data = tempfile.TemporaryDirectory(prefix="obsidianvow-tests-")
    os.environ["AION_DATA_DIR"] = _test_data.name
    atexit.register(_test_data.cleanup)

from runtime_safety import install_test_network_guard

install_test_network_guard()
