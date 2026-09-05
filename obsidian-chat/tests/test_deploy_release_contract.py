from pathlib import Path
from importlib import metadata
import subprocess

import yaml
import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_deploy_script_is_valid_and_preserves_recovery_before_mutation():
    script = ROOT / "scripts/deploy_server.sh"
    assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0
    text = script.read_text()
    calls = text[text.rindex("\nprepare_source_snapshot\n"):]
    assert calls.index("prepare_recovery_image") < calls.index("rsync_apply") < calls.index("runtime_sync")
    assert calls.index("smoke_checks") < calls.index("commit_container")
    assert 'docker commit "$CONTAINER_NAME" "$RECOVERY_IMAGE_NAME"' in text
    assert 'docker image prune' not in text
    assert '"${LOCAL_RELEASE_DIR}/source/"' in text
    assert '"$RESTART" -ne 1 || "$SMOKE" -ne 1' in text


def test_build_and_hot_copy_install_the_same_hash_locked_runtime():
    dockerfile = (ROOT / "obsidian-chat/Dockerfile").read_text()
    script = (ROOT / "scripts/deploy_server.sh").read_text()
    assert "@sha256:" in dockerfile.splitlines()[1]
    assert "apt-get" not in dockerfile
    assert "--require-hashes" in dockerfile
    assert "-r requirements.lock" in dockerfile
    assert "--require-hashes" in script
    assert "-r /app/requirements.lock" in script
    assert "*.lock" in script


def test_container_checks_do_not_depend_on_unpinned_system_packages():
    for relative in ("obsidian-chat/docker-compose.yml", "deploy/docker-compose.prod.yml"):
        document = yaml.safe_load((ROOT / relative).read_text())
        service = next(iter(document["services"].values()))
        assert service["platform"] == "linux/amd64"
        assert service["healthcheck"]["test"][:2] == ["CMD", "python"]
    production = yaml.safe_load((ROOT / "deploy/docker-compose.prod.yml").read_text())
    assert "OBSIDIAN_RELEASE_IMAGE" in production["services"]["app"]["image"]


def test_installed_dependency_versions_match_release_locks():
    for relative in ("obsidian-chat/requirements.lock", "obsidian-chat/requirements-test.lock"):
        for line in (ROOT / relative).read_text().splitlines():
            if "==" not in line or line.startswith("#"):
                continue
            name, version = line.removesuffix(" \\").split("==", 1)
            name = name.split("[", 1)[0]
            assert metadata.version(name) == version, f"{name} 与版本锁不一致"


@pytest.mark.parametrize("health_ok", [True, False])
def test_hot_deploy_smoke_requires_healthz_even_when_static_home_succeeds(health_ok):
    text = (ROOT / "scripts/deploy_server.sh").read_text()
    smoke = text.split("\nsmoke_checks() {", 1)[1].split("<<'REMOTE_SCRIPT'\n", 1)[1].split("\nREMOTE_SCRIPT", 1)[0]
    # 仅执行冒烟片段；所有外部程序换成壳函数，不运行 SSH、容器或网络请求。
    harness = f"""
SERVER_PATH=/nonexistent-obsidianvow-smoke-test
SERVER_HOST=unused.invalid
curl() {{
  echo "curl:$*"
  case "$*" in
    *"/healthz"*) return {0 if health_ok else 22} ;;
    *) return 0 ;;
  esac
}}
python3() {{ echo diagnostic-reached; }}
{smoke}
"""
    result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
    assert "/healthz" in result.stdout
    assert (result.returncode == 0) is health_ok
    assert ("diagnostic-reached" in result.stdout) is health_ok
    production = yaml.safe_load((ROOT / "deploy/docker-compose.prod.yml").read_text())
    assert "/healthz" in production["services"]["app"]["healthcheck"]["test"][-1]
