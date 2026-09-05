#!/usr/bin/env python3
"""创建、检查和恢复本地源码快照或运行数据备份，不覆盖已有目录。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("obsidian-chat", "pc_agent", "ObsidianApp", "public", "cloudflare-worker", "scripts", "deploy", "docs")
SOURCE_FILES = (
    "AGENTS.md", "README.md", "README.en.md", "LICENSE", ".gitignore", ".gitattributes",
    ".dockerignore", ".env.example", ".python-version",
    "experiments/working_model_gate_spike/cases_v1.json",
)
SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".gradle", "build", "wheels", ".runs",
}
SKIP_FILES = {"encrypted.data", "local.properties", "所需要的API.txt", "NUL"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".log", ".ses", ".pem", ".key", ".apk", ".aab", ".db", ".sqlite", ".sqlite3"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def new_directory(path: Path) -> Path:
    path = path.absolute()
    if path.exists() or path.is_symlink():
        raise ValueError(f"目标已存在，拒绝覆盖：{path}")
    path.mkdir(parents=True, mode=0o700)
    return path


def safe_member(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or "\\" in name or not relative.parts:
        raise ValueError(f"不安全的备份路径：{name}")
    path = root.joinpath(*relative.parts)
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise ValueError(f"备份路径越界或指向符号链接：{name}")
    return path


def copy_regular(source: Path, target: Path, *, private: bool = False) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"只支持普通文件：{source}")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700 if private else 0o755)
    shutil.copy2(source, target)
    if private:
        target.chmod(0o600)


def source_files(root: Path):
    for name in SOURCE_FILES:
        path = root / name
        if path.is_file():
            yield path
    for name in SOURCE_DIRS:
        for directory, children, files in os.walk(root / name, followlinks=False):
            base = Path(directory)
            children[:] = sorted(
                child for child in children
                if child not in SKIP_DIRS and base / child != root / "obsidian-chat" / "data"
            )
            for child in children:
                if (base / child).is_symlink():
                    raise ValueError(f"源码目录包含符号链接，需显式处理：{base / child}")
            for filename in sorted(files):
                path = base / filename
                if filename in SKIP_FILES or path.suffix.lower() in SKIP_SUFFIXES:
                    continue
                if filename.startswith(".env") and filename != ".env.example":
                    continue
                if any(filename.endswith(suffix) for suffix in ("-wal", "-shm", "-journal")):
                    continue
                yield path


def write_manifest(bundle: Path, kind: str, paths: list[Path], **metadata) -> dict:
    manifest = {
        "format": 1, "kind": kind, "created_at": int(time.time()),
        "python": sys.version.split()[0], **metadata,
        "files": {
            path.relative_to(bundle).as_posix(): {"sha256": sha256(path), "size": path.stat().st_size}
            for path in sorted(paths)
        },
    }
    manifest_path = bundle / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest_path.chmod(0o600)
    return manifest


def snapshot(root: Path, output: Path) -> dict:
    root = root.resolve()
    sources = list(source_files(root))
    if output.resolve() == root or (
        output.resolve().is_relative_to(root) and output.resolve().relative_to(root).parts[0] in SOURCE_DIRS
    ):
        raise ValueError("快照不能放入被复制的源码目录。")
    bundle = new_directory(output)
    paths = []
    for source in sources:
        target = bundle / "source" / source.relative_to(root)
        copy_regular(source, target)
        paths.append(target)
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True)
    return write_manifest(bundle, "source", paths, git_head=result.stdout.strip() if result.returncode == 0 else None)


def is_sqlite(path: Path) -> bool:
    with path.open("rb") as stream:
        return stream.read(16) == b"SQLite format 3\x00"


def backup_sqlite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    deadline = time.monotonic() + 30.0

    def progress(_status, _remaining, _total):
        if time.monotonic() > deadline:
            raise TimeoutError(f"数据库备份超时：{source.name}")

    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as origin:
        with sqlite3.connect(target) as destination:
            origin.backup(destination, pages=256, progress=progress, sleep=0.05)
            if destination.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError(f"数据库完整性检查失败：{source.name}")
    target.chmod(0o600)


def referenced_uploads(database: Path) -> set[str]:
    """以冻结的聊天库为准读取本地附件，不导入应用或连接运行数据库。"""
    references = set()
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
        if "attachments" not in columns:
            return references
        for (raw,) in db.execute("SELECT attachments FROM messages WHERE attachments IS NOT NULL"):
            try:
                attachments = json.loads(raw or "[]")
            except (ValueError, TypeError) as exc:
                raise ValueError(f"数据库附件清单无法解析：{database.name}") from exc
            if not isinstance(attachments, list):
                raise ValueError(f"数据库附件清单格式错误：{database.name}")
            for attachment in attachments:
                url = attachment if isinstance(attachment, str) else (
                    attachment.get("url", "") if isinstance(attachment, dict) else ""
                )
                if isinstance(url, str) and url.strip().startswith("/uploads/"):
                    references.add(url.strip().removeprefix("/"))
    return references


def backup_data(data_dir: Path, output: Path, env_files: list[Path]) -> dict:
    data_dir = data_dir.resolve()
    if not data_dir.is_dir():
        raise ValueError("运行数据目录不存在。")
    if output.resolve().is_relative_to(data_dir):
        raise ValueError("备份不能放入运行数据目录。")
    bundle = new_directory(output)
    paths = []
    databases = []
    sources = []
    for source in sorted(data_dir.rglob("*")):
        if source.is_symlink():
            raise ValueError(f"运行数据包含符号链接，需显式处理：{source.relative_to(data_dir)}")
        if not source.is_file() or source.name.endswith(("-wal", "-shm", "-journal")):
            continue
        sources.append((source, is_sqlite(source)))
    # 先冻结数据库，再复制普通文件；初始扫描之后上传的附件按库中引用补齐。
    for source, sqlite in sources:
        if not sqlite:
            continue
        relative = source.relative_to(data_dir)
        target = bundle / "data" / relative
        backup_sqlite(source, target)
        databases.append(target.relative_to(bundle).as_posix())
        paths.append(target)
    for source, sqlite in sources:
        if sqlite:
            continue
        target = bundle / "data" / source.relative_to(data_dir)
        copy_regular(source, target, private=True)
        paths.append(target)
    copied = set(paths)
    for name in databases:
        for reference in sorted(referenced_uploads(bundle / name)):
            source = safe_member(data_dir, reference)
            target = safe_member(bundle / "data", reference)
            if target not in copied:
                if not source.is_file():
                    raise ValueError(f"数据库引用的附件缺失：{reference}")
                copy_regular(source, target, private=True)
                paths.append(target)
                copied.add(target)
    for index, source in enumerate(env_files):
        target = bundle / "env" / f"{index}-{source.name}"
        copy_regular(source, target, private=True)
        paths.append(target)
    return write_manifest(bundle, "data", paths, databases=databases)


def verify(bundle: Path) -> dict:
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or manifest.get("kind") not in {"source", "data"}:
        raise ValueError("无法识别的备份格式。")
    files = manifest["files"]
    if not isinstance(files, dict):
        raise ValueError("备份文件清单格式错误。")
    for name, expected in files.items():
        path = safe_member(bundle, name)
        if not path.is_file() or path.stat().st_size != expected["size"] or sha256(path) != expected["sha256"]:
            raise ValueError(f"备份文件校验失败：{name}")
    for name in manifest.get("databases", []):
        if name not in files:
            raise ValueError("数据库不在校验清单中。")
        path = safe_member(bundle, name)
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError(f"数据库完整性检查失败：{name}")
        if manifest["kind"] == "data":
            for reference in referenced_uploads(path):
                attachment = "data/" + reference
                safe_member(bundle, attachment)
                if attachment not in files:
                    raise ValueError(f"数据库引用的附件未纳入备份：{reference}")
    return manifest


def restore(bundle: Path, target: Path) -> dict:
    bundle = bundle.resolve()
    manifest = verify(bundle)
    # 先校验全部路径，再创建目标，绝不把还原动作指向现有目录。
    names = list(manifest["files"])
    if manifest["kind"] == "source" and any(not name.startswith("source/") for name in names):
        raise ValueError("源码快照的目录格式错误。")
    destination = new_directory(target)
    for name in names:
        relative = name.removeprefix("source/") if manifest["kind"] == "source" else name
        copy_regular(safe_member(bundle, name), safe_member(destination, relative), private=manifest["kind"] == "data")
    return manifest


def check_restored_startup(data_dir: Path) -> None:
    """仅由本次恢复流程调用，在新目录检查初始化；禁用真实网络和后台动作。"""
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL", "TZ"}}
    env.update({
        "OBSIDIAN_TEST_MODE": "1", "OBSIDIAN_DATA_DIR": str(data_dir.resolve()),
        "PYTHONPATH": str(ROOT / "obsidian-chat"), "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    })
    program = (
        "import asyncio\n"
        "import main\n"
        "async def check():\n"
        "    async with main.lifespan(main.app):\n"
        "        pass\n"
        "asyncio.run(check())\n"
    )
    subprocess.run([sys.executable, "-c", program], env=env, cwd=ROOT / "obsidian-chat", timeout=45, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    source_parser = commands.add_parser("snapshot", help="保存源码、未跟踪业务文件和校验清单")
    source_parser.add_argument("--root", type=Path, default=ROOT)
    source_parser.add_argument("--output", type=Path, required=True)
    data_parser = commands.add_parser("backup-data", help="一致性备份全部数据库和附件、配置")
    data_parser.add_argument("--data-dir", type=Path, required=True)
    data_parser.add_argument("--env-file", type=Path, action="append", default=[])
    data_parser.add_argument("--output", type=Path, required=True)
    for command in ("verify", "restore"):
        child = commands.add_parser(command)
        child.add_argument("bundle", type=Path)
        if command == "restore":
            child.add_argument("--target", type=Path, required=True)
            child.add_argument("--check-startup", action="store_true", help="对刚恢复的新数据目录执行无外部动作的启动检查")
    args = parser.parse_args()
    try:
        if args.command == "snapshot":
            manifest = snapshot(args.root, args.output)
        elif args.command == "backup-data":
            manifest = backup_data(args.data_dir, args.output, args.env_file)
        elif args.command == "restore":
            manifest = restore(args.bundle, args.target)
            if args.check_startup:
                if manifest["kind"] != "data":
                    raise ValueError("启动检查只适用于运行数据备份。")
                check_restored_startup(args.target / "data")
        else:
            manifest = verify(args.bundle.resolve())
    except (ValueError, OSError, sqlite3.Error, TimeoutError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"{args.command} 完成：{len(manifest['files'])} 个文件，{len(manifest.get('databases', []))} 个数据库。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
