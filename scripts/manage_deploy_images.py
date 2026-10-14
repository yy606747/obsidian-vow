#!/usr/bin/env python3
"""Preview or remove old project deployment images, retaining four backups."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys


MANAGED_PREFIXES = ("release-", "recovery-", "build-base-")
GIB = 1024 ** 3


def image_created(value):
    # Docker uses RFC3339Nano; Python 3.8 on the deployment host accepts at
    # most six fractional digits. Preserve the timezone when truncating.
    return datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00")))


def deployment_group(tag: str, repository: str) -> str | None:
    prefix = repository + ":"
    if not tag.startswith(prefix):
        return None
    name = tag[len(prefix):]
    for kind in MANAGED_PREFIXES:
        if name.startswith(kind) and len(name) > len(kind):
            return name[len(kind):]
    return None


def build_plan(images, containers, *, image_name, previous_image_name, keep=4):
    """Keep deployment pairs and at least four distinct historical images."""
    if keep < 4:
        raise ValueError("At least four deployment backups must be retained")
    repository = image_name.rsplit(":", 1)[0]
    by_id = {item["Id"]: item for item in images}
    tags = {
        tag: item["Id"] for item in images for tag in (item.get("RepoTags") or [])
        if tag != "<none>:<none>"
    }
    created = {
        key: image_created(item["Created"])
        for key, item in by_id.items()
    }
    groups = {}
    managed = {}
    for tag, image_id in tags.items():
        group = deployment_group(tag, repository)
        if group is not None:
            managed[tag] = (image_id, group)
            groups[group] = max(groups.get(group, created[image_id]), created[image_id])
    recent_groups = sorted(groups, key=lambda group: (groups[group], group), reverse=True)[:keep]
    # Multiple tags for one image are one backup. The current production image
    # does not count toward the requested four historical versions.
    backup_ids = sorted(
        {image_id for image_id, _ in managed.values()} - {tags.get(image_name)},
        key=lambda key: (created[key], key), reverse=True,
    )[:keep]
    protected = {}

    def protect(image_id, reason):
        if image_id in by_id:
            protected.setdefault(image_id, set()).add(reason)

    for name in (image_name, previous_image_name):
        protect(tags.get(name), name)
    for container in containers:
        protect(container["Image"], "container:" + container["Id"][:12])
    for image_id in backup_ids:
        protect(image_id, "recent distinct backup")
    for tag, image_id in tags.items():
        group = deployment_group(tag, repository)
        if group in recent_groups:
            protect(image_id, "deployment:" + group)
        elif group is None:
            # Preserve manual pins and images also tagged by another project.
            protect(image_id, "unmanaged tag:" + tag)
    removals = [
        {"tag": tag, "image_id": image_id}
        for tag, (image_id, _group) in managed.items() if image_id not in protected
    ]
    removals.sort(key=lambda item: (created[item["image_id"]], item["tag"]), reverse=True)
    return {
        "repository": repository,
        "keep": keep,
        "kept_deployments": recent_groups,
        "historical_image_ids": backup_ids,
        "protected": [
            {"image_id": key, "tags": by_id[key].get("RepoTags") or [], "reasons": sorted(reasons)}
            for key, reasons in protected.items()
            if any(tag.startswith(repository + ":") for tag in (by_id[key].get("RepoTags") or []))
        ],
        "production_present": image_name in tags,
        "remove": removals,
    }


def docker_output(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def inspect_many(kind, ids):
    result = []
    for start in range(0, len(ids), 100):
        result.extend(json.loads(docker_output(kind, "inspect", *ids[start:start + 100])))
    return result


def current_plan(*, image_name, previous_image_name, keep):
    repository = image_name.rsplit(":", 1)[0]
    ids = sorted(set(docker_output("image", "ls", "--no-trunc", "--quiet", repository + ":*").split()))
    container_ids = docker_output("container", "ls", "--all", "--quiet").split()
    return build_plan(
        inspect_many("image", ids), inspect_many("container", container_ids),
        image_name=image_name, previous_image_name=previous_image_name, keep=keep,
    )


def apply_plan(plan, *, refresh, remove):
    if not plan["production_present"]:
        raise ValueError("Production image is missing; no images were removed")
    removed = []
    for candidate in plan["remove"]:
        # Re-check pins and container references immediately before each removal.
        # A retagged image or concurrent deployment invalidates this candidate.
        latest = refresh()
        if not latest["production_present"]:
            raise ValueError("Production image disappeared; cleanup stopped")
        if candidate not in latest["remove"]:
            continue
        try:
            remove(candidate["tag"])
        except subprocess.CalledProcessError as exc:
            print(f"Kept {candidate['tag']}: Docker refused removal ({exc.returncode})", file=sys.stderr)
        else:
            removed.append(candidate["tag"])
    return removed


def disk_space(paths, *, min_free_gb):
    result = []
    for path in dict.fromkeys(str(Path(value).resolve()) for value in paths):
        fs = os.statvfs(path)
        free = fs.f_bavail * fs.f_frsize
        result.append({"path": path, "free_gib": round(free / GIB, 2), "enough": free >= min_free_gb * GIB})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-name", default="obsidian-vow:prod")
    parser.add_argument("--previous-image-name", default="obsidian-vow:previous")
    parser.add_argument("--keep", type=int, default=4, help="Deployment backups to retain (minimum 4)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Remove only the listed old project tags")
    mode.add_argument("--dry-run", action="store_true", help="Preview only (default)")
    parser.add_argument("--data-path", default=".")
    parser.add_argument("--min-free-gb", type=float, default=10, help="Free space floor in GiB")
    parser.add_argument("--require-space", action="store_true", help="Fail below the floor, after any cleanup")
    args = parser.parse_args(argv)
    if args.min_free_gb <= 0:
        parser.error("--min-free-gb must be positive")
    options = {"image_name": args.image_name, "previous_image_name": args.previous_image_name, "keep": args.keep}
    plan = current_plan(**options)
    docker_root = docker_output("info", "--format", "{{.DockerRootDir}}")
    paths = (args.data_path, docker_root)
    before = disk_space(paths, min_free_gb=args.min_free_gb)
    print(json.dumps({"mode": "apply" if args.apply else "preview", "plan": plan, "disk_before": before}, ensure_ascii=False, indent=2), flush=True)
    if args.apply:
        removed = apply_plan(
            plan, refresh=lambda: current_plan(**options),
            remove=lambda tag: subprocess.run(["docker", "image", "rm", tag], check=True),
        )
        print(json.dumps({"removed": removed}, ensure_ascii=False))
    after = disk_space(paths, min_free_gb=args.min_free_gb)
    print(json.dumps({"disk_after": after, "minimum_free_gib": args.min_free_gb}, ensure_ascii=False))
    if args.require_space and not all(item["enough"] for item in after):
        print("Insufficient disk space for deployment; retained backups were not reduced", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Image maintenance failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
