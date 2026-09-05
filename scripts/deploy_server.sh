#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

SERVER_USER="${SERVER_USER:-deploy}"
SERVER_HOST="${SERVER_HOST:-example.invalid}"
SERVER_PATH="${SERVER_PATH:-/opt/obsidian-vow}"
CONTAINER_NAME="${CONTAINER_NAME:-obsidian-vow}"
IMAGE_NAME="${IMAGE_NAME:-obsidian-vow:prod}"
PREVIOUS_IMAGE_NAME="${PREVIOUS_IMAGE_NAME:-obsidian-vow:previous}"
SSH_CONTROL_PATH="${SSH_CONTROL_PATH:-/tmp/obsidianvow-ssh}"
GRADLE_USER_HOME="${GRADLE_USER_HOME:-/tmp/gradle-home}"
BACKEND_PYTHON="${BACKEND_PYTHON:-${ROOT_DIR}/.venv/bin/python}"
RELEASE_ID="$(date -u +%Y%m%dT%H%M%SZ)-${RANDOM}"
LOCAL_RELEASE_DIR=""
SOURCE_MANIFEST_SHA=""
RECOVERY_IMAGE_NAME="${IMAGE_NAME%:*}:recovery-${RELEASE_ID}"

APPLY=0
BUILD_APK=0
RUNTIME_SYNC=1
RESTART=1
COMMIT_IMAGE=1
SMOKE=1

usage() {
  cat <<'EOF'
Usage:
  scripts/deploy_server.sh [options]

Default mode is dry-run. No server files are changed unless --apply is passed.
The script never uses rsync --delete.

Options:
  --dry-run          Show what would be synced. This is the default.
  --apply            Sync deployable source files to the server.
  --build-apk        Build Android debug APK locally and copy it to obsidian-chat/static/.
  --no-runtime       Do not copy backend files into the running Docker container.
  --no-restart       Do not restart the Docker container.
  --no-commit        Do not commit the running container back to IMAGE_NAME.
  --no-smoke         Skip post-deploy HTTP smoke checks.
  --help             Show this help.

Environment overrides:
  SERVER_USER        SSH user. Default: deploy
  SERVER_HOST        SSH host. Default: example.invalid
  SERVER_PATH        Remote repo path. Default: /opt/obsidian-vow
  CONTAINER_NAME     Docker container. Default: obsidian-vow
  IMAGE_NAME         Docker image tag to commit. Default: obsidian-vow:prod
  PREVIOUS_IMAGE_NAME
                     Docker image tag retained for one-step rollback. Default: obsidian-vow:previous
  SSH_CONTROL_PATH   Optional existing SSH ControlMaster socket.
  GRADLE_USER_HOME   Gradle cache dir for --build-apk. Default: /tmp/gradle-home

Examples:
  scripts/deploy_server.sh
  scripts/deploy_server.sh --apply
  scripts/deploy_server.sh --build-apk --apply
  scripts/deploy_server.sh --apply --no-runtime
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      APPLY=0
      ;;
    --apply)
      APPLY=1
      ;;
    --build-apk)
      BUILD_APK=1
      ;;
    --no-runtime)
      RUNTIME_SYNC=0
      ;;
    --no-restart)
      RESTART=0
      ;;
    --no-commit)
      COMMIT_IMAGE=0
      ;;
    --no-smoke)
      SMOKE=0
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_cmd ssh
require_cmd rsync

SSH_OPTS=()
if [[ -S "$SSH_CONTROL_PATH" ]]; then
  SSH_OPTS=(-S "$SSH_CONTROL_PATH" -o BatchMode=yes)
fi
REMOTE="${SERVER_USER}@${SERVER_HOST}"
RSYNC_RSH="ssh"
if [[ ${#SSH_OPTS[@]} -gt 0 ]]; then
  RSYNC_RSH="ssh ${SSH_OPTS[*]}"
fi

EXCLUDES=(
  # The server is also a source backup for business code. Exclude only local,
  # generated, or reproducible artifacts; keep backend/frontend/Android sources.
  --exclude '.git/'
  --exclude '.venv/'
  --exclude '.gradle/'
  --exclude '.agents/'
  --exclude '.codex/'
  --exclude '.codex-backups/'
  --exclude '.claude/'
  --exclude '.idea/'
  --exclude '.vscode/'
  --exclude 'node_modules/'
  --exclude '__pycache__/'
  --exclude '.pytest_cache/'
  --exclude '.mypy_cache/'
  --exclude '.ruff_cache/'
  --exclude '.runs/'
  --exclude 'test-results/'
  --exclude '*.pyc'
  --exclude '*.pyo'
  --exclude '*.log'
  --exclude '*.ses'
  --exclude 'encrypted.data'
  --exclude '*.pem'
  --exclude '*.key'
  --exclude '.env'
  --exclude 'ObsidianApp/.gradle/'
  --exclude 'ObsidianApp/app/build/'
  --exclude 'ObsidianApp/local.properties'
  --exclude 'obsidian-chat/data/'
  --exclude 'obsidian-chat/.pytest_cache/'
  --exclude 'obsidian-chat/__pycache__/'
  --exclude 'toy/jadx_out/'
  --exclude 'toy/android_adv/build/'
  --exclude 'toy/hci_logs_*/'
  --exclude 'toy/base.apk*'
  --exclude 'migration_dumps/'
  --exclude 'smartring/jadx_out/'
  --exclude 'smartring/base.apk*'
  --exclude 'smartring/bugreport*.zip'
  --exclude 'smartring/bugreport*extract*/'
  --exclude 'research/ble-toy/artifacts/*.zip'
  --exclude 'research/ble-toy/artifacts/*.apk*'
  --exclude 'research/ble-toy/artifacts/output/'
)

remote_ssh() {
  ssh "${SSH_OPTS[@]}" "$REMOTE" "$@"
}

build_apk() {
  echo "Building Android debug APK locally..."
  require_cmd cp
  (cd ObsidianApp && GRADLE_USER_HOME="$GRADLE_USER_HOME" ./gradlew --no-daemon :app:assembleDebug)
  mkdir -p obsidian-chat/static
  cp -f ObsidianApp/app/build/outputs/apk/debug/app-debug.apk obsidian-chat/static/obsidianvow-debug.apk
  echo "APK staged at obsidian-chat/static/obsidianvow-debug.apk"
}

rsync_dry_run() {
  prepare_source_snapshot
  echo "Dry-run rsync to ${REMOTE}:${SERVER_PATH}/"
  rsync -ani --checksum \
    "${EXCLUDES[@]}" \
    -e "$RSYNC_RSH" \
    "${LOCAL_RELEASE_DIR}/source/" "${REMOTE}:${SERVER_PATH}/"
  rsync -ani --checksum -e "$RSYNC_RSH" \
    obsidian-chat/wheels/ "${REMOTE}:${SERVER_PATH}/obsidian-chat/wheels/"
  if [[ -f obsidian-chat/static/obsidianvow-debug.apk ]]; then
    rsync -ani --checksum -e "$RSYNC_RSH" obsidian-chat/static/obsidianvow-debug.apk \
      "${REMOTE}:${SERVER_PATH}/obsidian-chat/static/obsidianvow-debug.apk"
  fi
}

rsync_apply() {
  local backup_dir="${SERVER_PATH}/.codex-backups/rsync-source-${RELEASE_ID}"
  local release_dir="${SERVER_PATH}/.codex-backups/releases/${RELEASE_ID}"
  echo "Remote backup dir: ${backup_dir}"
  remote_ssh "mkdir -p '$backup_dir' '$release_dir'"
  # Retain the existing container's bind-mount path until it is recreated.
  # The migration backs up environment files and preserves all runtime data.
  rsync -a -e "$RSYNC_RSH" "${LOCAL_RELEASE_DIR}/source/scripts/migrate_project_names.py" "${REMOTE}:${release_dir}/migrate_project_names.py"
  remote_ssh "python3 '$release_dir/migrate_project_names.py' --root '$SERVER_PATH' --apply --keep-runtime-alias"
  rsync -aic --backup --backup-dir="$backup_dir" \
    "${EXCLUDES[@]}" \
    -e "$RSYNC_RSH" \
    "${LOCAL_RELEASE_DIR}/source/" "${REMOTE}:${SERVER_PATH}/"
  rsync -aic -e "$RSYNC_RSH" \
    obsidian-chat/wheels/ "${REMOTE}:${SERVER_PATH}/obsidian-chat/wheels/"
  rsync -a -e "$RSYNC_RSH" \
    "${LOCAL_RELEASE_DIR}/manifest.json" "${REMOTE}:${release_dir}/source-manifest.json"
  # 安装包不是源码，单独同步；不改变原有 --build-apk 行为。
  if [[ -f obsidian-chat/static/obsidianvow-debug.apk ]]; then
    rsync -aic -e "$RSYNC_RSH" obsidian-chat/static/obsidianvow-debug.apk \
      "${REMOTE}:${SERVER_PATH}/obsidian-chat/static/obsidianvow-debug.apk"
  fi
}

prepare_source_snapshot() {
  require_cmd "$BACKEND_PYTHON"
  mkdir -p "${ROOT_DIR}/.codex-backups/releases"
  local release_parent
  release_parent="$(mktemp -d "${ROOT_DIR}/.codex-backups/releases/${RELEASE_ID}.XXXXXX")"
  LOCAL_RELEASE_DIR="${release_parent}/snapshot"
  "$BACKEND_PYTHON" scripts/engineering_baseline.py snapshot --output "$LOCAL_RELEASE_DIR"
  SOURCE_MANIFEST_SHA="$(sha256sum "${LOCAL_RELEASE_DIR}/manifest.json" | cut -d ' ' -f 1)"
  echo "本次部署使用冻结源码：${LOCAL_RELEASE_DIR}"
}

prepare_recovery_image() {
  if [[ "$RUNTIME_SYNC" -ne 1 ]]; then
    return
  fi
  echo "修改运行容器前保存恢复镜像：${RECOVERY_IMAGE_NAME}"
  remote_ssh "CONTAINER_NAME='$CONTAINER_NAME' RECOVERY_IMAGE_NAME='$RECOVERY_IMAGE_NAME' PREVIOUS_IMAGE_NAME='$PREVIOUS_IMAGE_NAME' bash -s" <<'REMOTE_SCRIPT'
set -euo pipefail
# 保存容器当前文件系统，包含此前热更新；不能仅保留可能已经过时的标签。
docker commit "$CONTAINER_NAME" "$RECOVERY_IMAGE_NAME"
docker tag "$RECOVERY_IMAGE_NAME" "$PREVIOUS_IMAGE_NAME"
REMOTE_SCRIPT
}

runtime_sync() {
  if [[ "$RUNTIME_SYNC" -ne 1 ]]; then
    echo "Skipping runtime container sync."
    return
  fi

  echo "Copying backend source into Docker container ${CONTAINER_NAME}..."
  remote_ssh "SERVER_PATH='$SERVER_PATH' CONTAINER_NAME='$CONTAINER_NAME' bash -s" <<'REMOTE_SCRIPT'
set -euo pipefail
base="${SERVER_PATH}/obsidian-chat"

for d in app routes static tests scripts wheels; do
  if [[ -d "${base}/${d}" ]]; then
    docker cp "${base}/${d}/." "${CONTAINER_NAME}:/app/${d}/"
  fi
done
if [[ -d "${SERVER_PATH}/public" ]]; then
  docker cp "${SERVER_PATH}/public/." "${CONTAINER_NAME}:/public/"
fi

for f in "${base}"/*.py "${base}"/*.txt "${base}"/*.md "${base}"/*.lock "${base}"/*.in "${base}"/*.sha256 "${base}"/Dockerfile "${base}"/docker-compose.yml; do
  if [[ -f "$f" ]]; then
    docker cp "$f" "${CONTAINER_NAME}:/app/$(basename "$f")"
  fi
done

# Hot-copy deployments do not rebuild the image.  Install from the same
# offline wheelhouse as Dockerfile before restart so newly added runtime
# dependencies are present in an existing container as well.
docker exec --user 0 "$CONTAINER_NAME" python -m pip install \
  --no-index --require-hashes --find-links=/app/wheels -r /app/requirements.lock
REMOTE_SCRIPT
}

restart_container() {
  if [[ "$RUNTIME_SYNC" -ne 1 || "$RESTART" -ne 1 ]]; then
    echo "Skipping container restart."
    return
  fi

  echo "Restarting ${CONTAINER_NAME} and waiting for health..."
  remote_ssh "CONTAINER_NAME='$CONTAINER_NAME' bash -s" <<'REMOTE_SCRIPT'
set -euo pipefail
docker restart "$CONTAINER_NAME"
last_health=""
for _ in $(seq 1 20); do
  last_health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$CONTAINER_NAME" 2>/dev/null || true)"
  echo "health=${last_health}"
  if [[ "$last_health" == "healthy" || "$last_health" == "running" ]]; then
    exit 0
  fi
  sleep 3
done
docker ps --filter "name=${CONTAINER_NAME}" --format '{{.Names}} {{.Status}}'
docker logs --tail 80 --timestamps "$CONTAINER_NAME" 2>&1 || true
exit 1
REMOTE_SCRIPT
}

commit_container() {
  if [[ "$RUNTIME_SYNC" -ne 1 || "$COMMIT_IMAGE" -ne 1 || "$RESTART" -ne 1 || "$SMOKE" -ne 1 ]]; then
    echo "未完成重启和冒烟检查，不更新发布镜像。"
    return
  fi

  echo "检查通过，保存发布镜像与源码对应记录：${RELEASE_ID}"
  remote_ssh "SERVER_PATH='$SERVER_PATH' CONTAINER_NAME='$CONTAINER_NAME' IMAGE_NAME='$IMAGE_NAME' RECOVERY_IMAGE_NAME='$RECOVERY_IMAGE_NAME' RELEASE_ID='$RELEASE_ID' SOURCE_MANIFEST_SHA='$SOURCE_MANIFEST_SHA' bash -s" <<'REMOTE_SCRIPT'
set -euo pipefail
release_image="${IMAGE_NAME%:*}:release-${RELEASE_ID}"
docker commit \
  --change "LABEL org.obsidianvow.release-id=${RELEASE_ID}" \
  --change "LABEL org.obsidianvow.source-manifest-sha256=${SOURCE_MANIFEST_SHA}" \
  "$CONTAINER_NAME" "$release_image"
docker tag "$release_image" "$IMAGE_NAME"
python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path
import subprocess

release_dir = Path(os.environ["SERVER_PATH"]) / ".codex-backups/releases" / os.environ["RELEASE_ID"]
image_id = subprocess.check_output([
    "docker", "image", "inspect", "--format", "{{.Id}}", os.environ["IMAGE_NAME"],
], text=True).strip()
recovery_id = subprocess.check_output([
    "docker", "image", "inspect", "--format", "{{.Id}}", os.environ["RECOVERY_IMAGE_NAME"],
], text=True).strip()
lock_path = Path(os.environ["SERVER_PATH"]) / "obsidian-chat/requirements.lock"
record = {
    "release_id": os.environ["RELEASE_ID"], "image_id": image_id,
    "recovery_image_id": recovery_id, "recovery_image": os.environ["RECOVERY_IMAGE_NAME"],
    "source_manifest_sha256": os.environ["SOURCE_MANIFEST_SHA"],
    "requirements_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
}
record_path = release_dir / "release.json"
record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
record_path.chmod(0o600)
print("发布记录：", record_path)
PY
REMOTE_SCRIPT
}

smoke_checks() {
  if [[ "$SMOKE" -ne 1 ]]; then
    echo "Skipping smoke checks."
    return
  fi

  echo "Running smoke checks..."
  remote_ssh "SERVER_PATH='$SERVER_PATH' SERVER_HOST='$SERVER_HOST' bash -s" <<'REMOTE_SCRIPT'
set -euo pipefail
# 热复制不会更新已有容器的健康配置，发布验收必须直接检查应用和数据库。
curl --retry 10 --retry-delay 1 --retry-connrefused --connect-timeout 2 --max-time 5 \
  -fsS http://127.0.0.1:18080/healthz -o /dev/null
curl -fsS http://127.0.0.1:18080/static/home.html -o /dev/null

python3 - <<'PY'
import json
import hashlib
import pathlib
import urllib.request

root = pathlib.Path(__import__("os").environ["SERVER_PATH"])
token = ""
env_path = root / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("OBSIDIAN_AUTH_TOKEN="):
            token = line.split("=", 1)[1].strip().strip(chr(34)).strip(chr(39))
            break

if not token:
    print("skip diagnostic smoke: missing OBSIDIAN_AUTH_TOKEN")
else:
    body = json.dumps({
        "event": "deploy_script_smoke",
        "provider": "server",
        "message": "deploy script smoke",
        "elapsed_ms": 1,
    }).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:18080/api/location/diagnostic",
        data=body,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        print("diagnostic smoke", resp.status)

    req = urllib.request.Request(
        "http://127.0.0.1:18080/api/push/public-key",
        headers={"Authorization": "Bearer " + token},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        public_key = json.load(resp).get("public_key", "")
        if len(public_key) != 87:
            raise RuntimeError("invalid Web Push application server key")
        digest = hashlib.sha256(public_key.encode()).hexdigest()
        print("push public-key smoke", resp.status, "sha256=" + digest)
PY

if [[ -f "${SERVER_PATH}/obsidian-chat/static/obsidianvow-debug.apk" ]]; then
  curl -kfsSI "https://${SERVER_HOST}/static/obsidianvow-debug.apk" >/dev/null || true
fi
REMOTE_SCRIPT
}

if [[ "$BUILD_APK" -eq 1 ]]; then
  build_apk
fi

if [[ "$APPLY" -ne 1 ]]; then
  rsync_dry_run
  echo
  echo "Dry-run only. Re-run with --apply to sync."
  exit 0
fi

prepare_source_snapshot
prepare_recovery_image
rsync_apply
runtime_sync
restart_container
smoke_checks
commit_container

echo "Deploy finished."
