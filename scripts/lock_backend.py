#!/usr/bin/env python3
"""从已验收环境锁定后端依赖，不解析或升级到远端最新版。"""

from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
from pathlib import Path
import sys

from packaging.requirements import Requirement
from packaging.tags import sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "aion-chat"


def read_requirements(path: Path) -> list[Requirement]:
    return [
        Requirement(line.split("#", 1)[0].strip())
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.split("#", 1)[0].strip()
    ]


def installed_closure(requirements: list[Requirement]) -> dict[str, tuple[str, set[str]]]:
    result: dict[str, tuple[str, set[str]]] = {}
    pending = list(requirements)
    while pending:
        requirement = pending.pop()
        name = canonicalize_name(requirement.name)
        dist = metadata.distribution(name)
        if requirement.specifier and dist.version not in requirement.specifier:
            raise ValueError(f"当前安装版本不满足声明：{requirement}，实际为 {dist.version}")
        extras = set(requirement.extras)
        if name in result:
            previous_extras = result[name][1]
            if extras <= previous_extras:
                continue
            extras |= previous_extras
        result[name] = (dist.version, extras)
        for text in dist.requires or ():
            child = Requirement(text)
            if child.marker is None or any(child.marker.evaluate({"extra": extra}) for extra in {"", *extras}):
                pending.append(child)
    return result


def pin(name: str, version: str, extras: set[str]) -> str:
    suffix = f"[{','.join(sorted(extras))}]" if extras else ""
    return f"{name}{suffix}=={version}"


def matching_wheels(wheel_dir: Path, name: str, version: str) -> list[Path]:
    supported = set(sys_tags())
    matches = []
    for path in sorted(wheel_dir.glob("*.whl")):
        wheel_name, wheel_version, _, tags = parse_wheel_filename(path.name)
        if wheel_name == name and str(wheel_version) == version and tags & supported:
            matches.append(path)
    return matches


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pins-only", type=Path, help="输出精确版本，供补齐离线安装包使用")
    parser.add_argument("--check", action="store_true", help="只检查锁定文件与当前环境是否一致")
    parser.add_argument("--wheel-dir", type=Path, default=BACKEND / "wheels")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 11) or sys.platform != "linux":
        parser.error("当前部署锁针对 Linux 与 Python 3.11，请在同一平台生成。")
    runtime = installed_closure(read_requirements(BACKEND / "requirements.in"))
    combined = installed_closure(
        read_requirements(BACKEND / "requirements.in") + read_requirements(BACKEND / "requirements-test.in")
    )
    if args.pins_only:
        args.pins_only.write_text(
            "\n".join(pin(name, version, extras) for name, (version, extras) in sorted(combined.items())) + "\n",
            encoding="utf-8",
        )
        print(f"已记录 {len(combined)} 个精确版本：{args.pins_only}")
        return 0
    header = "# 由 scripts/lock_backend.py 从已验收环境生成；Linux / Python 3.11。\n"
    header += "# 安装时使用 --require-hashes --no-index --find-links=wheels。\n"
    runtime_lines = [header]
    test_lines = [header, "-r requirements.lock\n"]
    wheel_hashes: dict[str, str] = {}
    missing = []
    for name, (version, extras) in sorted(combined.items()):
        wheels = matching_wheels(args.wheel_dir, name, version)
        if not wheels:
            missing.append(f"{name}=={version}")
            continue
        hashes = []
        for wheel in wheels:
            wheel_hashes[wheel.name] = digest(wheel)
            hashes.append(f"    --hash=sha256:{wheel_hashes[wheel.name]}")
        line = pin(name, version, extras) + " \\\n" + " \\\n".join(hashes) + "\n"
        (runtime_lines if name in runtime else test_lines).append(line)
    if missing:
        print("缺少与已验收环境一致的离线安装包：\n" + "\n".join(missing), file=sys.stderr)
        return 1
    outputs = {
        BACKEND / "requirements.lock": "".join(runtime_lines),
        BACKEND / "requirements-test.lock": "".join(test_lines),
        BACKEND / "wheels.sha256": "".join(f"{value}  {name}\n" for name, value in sorted(wheel_hashes.items())),
    }
    for path, content in outputs.items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                print(f"锁定文件与当前环境不同：{path}", file=sys.stderr)
                return 1
        else:
            path.write_text(content, encoding="utf-8")
    print(f"运行依赖 {len(runtime)} 项，含测试共 {len(combined)} 项；版本与文件校验值一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
