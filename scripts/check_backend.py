#!/usr/bin/env python3
"""使用临时数据和无真实凭据的子进程运行后端检查。"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
CORE_TESTS = (
    "test_brand_migration.py",
    "test_runtime_isolation.py",
    "test_runtime_lifecycle.py",
    "test_turn_diagnostics.py",
    "test_engineering_baseline.py",
    "test_deploy_release_contract.py",
    "test_chat_image_history.py",
    "test_image_memory.py",
    "test_chat_basic_actions.py",
    "test_pending_full_corpus.py",
    "test_working_model_gate_cp1.py",
    "test_working_model_v2_cp2.py",
    "test_relationship_prompt_names.py",
    "test_memory_v3_pending_recall.py",
    "test_turn_profiles.py",
    "test_prompt_cache_telemetry.py",
    "test_chat_prompt_boundaries.py",
    "test_chat_streaming_sse.py",
)


def isolated_environment(data_dir: Path) -> dict[str, str]:
    keep = {"PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "WINDIR", "LD_LIBRARY_PATH"}
    env = {key: value for key, value in os.environ.items() if key in keep}
    env.update({
        "OBSIDIAN_TEST_MODE": "1",
        "OBSIDIAN_DATA_DIR": str(data_dir),
        "PYTHONPATH": str(ROOT / "obsidian-chat"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    })
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="运行全部后端单元检查")
    parser.add_argument("--timeout", type=int, default=600, help="检查总时限，单位秒")
    parser.add_argument("tests", nargs="*", help="相对于 obsidian-chat 的测试路径")
    args = parser.parse_args()
    tests = args.tests or (["tests"] if args.all else [f"tests/{name}" for name in CORE_TESTS])
    with tempfile.TemporaryDirectory(prefix="obsidianvow-check-") as task_dir:
        command = [
            sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "-o", "faulthandler_timeout=30", "--tb=short", *tests,
        ]
        print("运行隔离检查：临时数据、禁止真实网络、关闭自主后台任务。", flush=True)
        try:
            return subprocess.run(
                command, cwd=ROOT / "obsidian-chat",
                env=isolated_environment(Path(task_dir)), timeout=args.timeout,
            ).returncode
        except subprocess.TimeoutExpired:
            print("检查超过总时限，已终止；真实运行数据未被使用。", file=sys.stderr)
            return 124


if __name__ == "__main__":
    raise SystemExit(main())
