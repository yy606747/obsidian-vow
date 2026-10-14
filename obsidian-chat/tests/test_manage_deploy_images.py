from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("manage_deploy_images", ROOT / "scripts/manage_deploy_images.py")
maintenance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(maintenance)


def image(number, *tags):
    return {
        "Id": f"sha256:{number:064x}",
        "Created": datetime(2026, 9, number, tzinfo=timezone.utc).isoformat(),
        "RepoTags": list(tags),
    }


def plan(images, containers=(), keep=4):
    return maintenance.build_plan(
        images, containers, image_name="obsidian-vow:prod",
        previous_image_name="obsidian-vow:previous", keep=keep,
    )


def test_retains_four_complete_deployments_plus_production_previous_and_containers():
    images = []
    for release in range(1, 7):
        images.extend([
            image(release * 2, f"obsidian-vow:recovery-r{release}"),
            image(release * 2 + 1, f"obsidian-vow:release-r{release}"),
        ])
    images[0]["RepoTags"].append("obsidian-vow:previous")
    images[-1]["RepoTags"].append("obsidian-vow:prod")
    containers = [{"Id": "stopped-container", "Image": images[1]["Id"]}]
    result = plan(images, containers)
    assert result["kept_deployments"] == ["r6", "r5", "r4", "r3"]
    assert {item["tag"] for item in result["remove"]} == {
        "obsidian-vow:recovery-r2", "obsidian-vow:release-r2",
    }
    assert len(result["historical_image_ids"]) == 4


def test_aliases_do_not_count_as_backups_and_unknown_tags_are_preserved():
    images = [image(i, f"obsidian-vow:recovery-r{i}") for i in range(1, 8)]
    images[-1]["RepoTags"].append("obsidian-vow:prod")
    images[-2]["RepoTags"] += [
        "obsidian-vow:previous", "obsidian-vow:build-base-r6",
        *[f"obsidian-vow:release-alias{i}" for i in range(10)],
    ]
    images[0]["RepoTags"].append("obsidian-vow:pre-manual-backup")
    images[1]["RepoTags"].append("forecast-agent-service:latest")
    result = plan(images)
    assert set(result["historical_image_ids"]) == {images[i]["Id"] for i in range(2, 6)}
    assert result["remove"] == []
    assert len({item["image_id"] for item in result["protected"]}) == 7


def test_removal_scope_is_exact_repository_and_known_tag_prefixes():
    assert maintenance.deployment_group("other:release-a", "obsidian-vow") is None
    assert maintenance.deployment_group("obsidian-vow-copy:release-a", "obsidian-vow") is None
    assert maintenance.deployment_group("obsidian-vow:pre-keep", "obsidian-vow") is None
    assert maintenance.deployment_group("obsidian-vow:recovery-a", "obsidian-vow") == "a"
    assert maintenance.deployment_group("registry:5000/team/app:release-a", "registry:5000/team/app") == "a"


def test_does_not_clean_when_production_is_missing_or_has_changed():
    images = [image(i, f"obsidian-vow:release-r{i}") for i in range(1, 8)]
    removed = []
    with pytest.raises(ValueError, match="Production image is missing"):
        maintenance.apply_plan(plan(images), refresh=lambda: plan(images), remove=removed.append)
    assert removed == []
    images[-1]["RepoTags"].append("obsidian-vow:prod")
    initial = plan(images)
    assert initial["remove"]
    # An old candidate acquires the rollback pin after the preview.
    candidates = {item["image_id"] for item in initial["remove"]}
    containers = [{"Id": "new-container", "Image": key} for key in candidates]
    maintenance.apply_plan(initial, refresh=lambda: plan(images, containers), remove=removed.append)
    assert removed == []


def test_apply_rechecks_candidates_and_does_not_force_docker_refusals():
    images = [image(i, f"obsidian-vow:release-r{i}") for i in range(1, 8)]
    images[-1]["RepoTags"].append("obsidian-vow:prod")
    initial = plan(images)
    calls = []

    def refuse(tag):
        calls.append(tag)
        raise subprocess.CalledProcessError(1, ["docker", "image", "rm", tag])

    assert maintenance.apply_plan(initial, refresh=lambda: plan(images), remove=refuse) == []
    assert calls == [item["tag"] for item in initial["remove"]]


def test_space_uses_available_bytes_and_keeps_backups_when_floor_not_met(monkeypatch):
    monkeypatch.setattr(maintenance.os, "statvfs", lambda _path: SimpleNamespace(f_bavail=9, f_frsize=maintenance.GIB))
    result = maintenance.disk_space(["/tmp", "/tmp"], min_free_gb=10)
    assert result == [{"path": "/tmp", "free_gib": 9.0, "enough": False}]
    images = [image(i, f"obsidian-vow:release-r{i}") for i in range(1, 6)]
    images[-1]["RepoTags"].append("obsidian-vow:prod")
    monkeypatch.setattr(maintenance, "current_plan", lambda **_kwargs: plan(images))
    monkeypatch.setattr(maintenance, "docker_output", lambda *_args: "/tmp")
    monkeypatch.setattr(maintenance.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("Deleted a retained backup"))
    assert maintenance.main(["--apply", "--data-path", "/tmp", "--require-space"]) == 2
    assert len(plan(images)["historical_image_ids"]) == 4


def test_default_mode_never_removes_images(monkeypatch):
    images = [image(i, f"obsidian-vow:release-r{i}") for i in range(1, 8)]
    images[-1]["RepoTags"].append("obsidian-vow:prod")
    monkeypatch.setattr(maintenance, "current_plan", lambda **_kwargs: plan(images))
    monkeypatch.setattr(maintenance, "docker_output", lambda *_args: "/tmp")
    monkeypatch.setattr(maintenance, "apply_plan", lambda *_args, **_kwargs: pytest.fail("Preview mutated Docker"))
    assert maintenance.main(["--data-path", "/tmp"]) == 0


def test_rejects_retaining_less_than_four_backups():
    with pytest.raises(ValueError, match="At least four"):
        plan([], keep=3)


def test_docker_nanosecond_timestamps_keep_timezone_when_ordering():
    first = maintenance.image_created("2026-09-13T13:03:36.387605603+08:00")
    same = maintenance.image_created("2026-09-13T05:03:36.387605603Z")
    later = maintenance.image_created("2026-09-13T05:04:00Z")
    assert first == same < later
    assert first.microsecond == 387605
