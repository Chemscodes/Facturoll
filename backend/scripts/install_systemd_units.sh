#!/usr/bin/env bash
set -euo pipefail

BACKEND_DIR="/opt/saas/backend"
RUN_USER="root"
RESTART_UNITS=0
VALIDATE_HEALTH=0
HEALTH_URL="http://127.0.0.1:8001/health"

UNITS=(
  "saas-backend.service"
  "saas-email-worker.service"
  "saas-embedding-worker.service"
  "saas-gmail-sync.service"
  "saas-gmail-sync.timer"
)

usage() {
  cat <<EOF
Usage: install_systemd_units.sh [options]

Options:
  --backend-dir <path>   Backend root path (default: /opt/saas/backend)
  --run-user <user>      Linux user/group for services (default: root)
  --restart              Restart services after install
  --validate-health      Validate backend health URL after restart
  --health-url <url>     Health URL (default: http://127.0.0.1:8001/health)
  --help                 Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend-dir)
      BACKEND_DIR="$2"
      shift 2
      ;;
    --run-user)
      RUN_USER="$2"
      shift 2
      ;;
    --restart)
      RESTART_UNITS=1
      shift
      ;;
    --validate-health)
      VALIDATE_HEALTH=1
      shift
      ;;
    --health-url)
      HEALTH_URL="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ "$(id -u)" -ne 0 ]]; then
  echo "This script must run as root." >&2
  exit 1
fi

if [[ ! -d "$BACKEND_DIR" ]]; then
  echo "Backend directory not found: $BACKEND_DIR" >&2
  exit 1
fi

if ! id "$RUN_USER" >/dev/null 2>&1; then
  echo "Run user does not exist: $RUN_USER" >&2
  exit 1
fi

UNIT_SRC_DIR="$BACKEND_DIR/deploy/systemd"
SYSTEMD_DIR="/etc/systemd/system"
PYTHON_BIN="$BACKEND_DIR/.venv/bin/python"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python not found or not executable: $PYTHON_BIN" >&2
  exit 1
fi

for unit in "${UNITS[@]}"; do
  if [[ ! -f "$UNIT_SRC_DIR/$unit" ]]; then
    echo "Missing unit template: $UNIT_SRC_DIR/$unit" >&2
    exit 1
  fi
done

BACKUP_DIR="$SYSTEMD_DIR/saas-units-backup-$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"

echo "Backing up existing units into: $BACKUP_DIR"
for unit in "${UNITS[@]}"; do
  if [[ -f "$SYSTEMD_DIR/$unit" ]]; then
    cp -a "$SYSTEMD_DIR/$unit" "$BACKUP_DIR/$unit"
  fi
done

rollback() {
  echo "Rolling back systemd units from backup: $BACKUP_DIR"
  for unit in "${UNITS[@]}"; do
    if [[ -f "$BACKUP_DIR/$unit" ]]; then
      cp -a "$BACKUP_DIR/$unit" "$SYSTEMD_DIR/$unit"
    else
      rm -f "$SYSTEMD_DIR/$unit"
    fi
  done
  systemctl daemon-reload
  systemctl restart saas-backend.service || true
  systemctl restart saas-email-worker.service || true
  systemctl restart saas-embedding-worker.service || true
  systemctl restart saas-gmail-sync.timer || true
}

render_unit() {
  local src="$1"
  local dest="$2"
  sed \
    -e "s#__BACKEND_DIR__#$BACKEND_DIR#g" \
    -e "s#__PYTHON_BIN__#$PYTHON_BIN#g" \
    -e "s#__RUN_USER__#$RUN_USER#g" \
    "$src" > "$dest"
}

echo "Installing unit files..."
for unit in "${UNITS[@]}"; do
  render_unit "$UNIT_SRC_DIR/$unit" "$SYSTEMD_DIR/$unit"
done

echo "Reloading systemd..."
systemctl daemon-reload

echo "Enabling units..."
systemctl enable saas-backend.service saas-email-worker.service saas-embedding-worker.service saas-gmail-sync.timer

if [[ "$RESTART_UNITS" -eq 1 ]]; then
  echo "Restarting units..."
  if ! systemctl restart saas-backend.service; then
    rollback
    echo "Failed to restart saas-backend.service" >&2
    exit 1
  fi
  if ! systemctl restart saas-email-worker.service; then
    rollback
    echo "Failed to restart saas-email-worker.service" >&2
    exit 1
  fi
  if ! systemctl restart saas-embedding-worker.service; then
    rollback
    echo "Failed to restart saas-embedding-worker.service" >&2
    exit 1
  fi
  if ! systemctl restart saas-gmail-sync.timer; then
    rollback
    echo "Failed to restart saas-gmail-sync.timer" >&2
    exit 1
  fi
fi

if [[ "$VALIDATE_HEALTH" -eq 1 ]]; then
  echo "Validating health endpoint: $HEALTH_URL"
  ok=0
  for _ in {1..20}; do
    if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
      ok=1
      break
    fi
    sleep 1
  done
  if [[ "$ok" -ne 1 ]]; then
    rollback
    echo "Health validation failed: $HEALTH_URL" >&2
    exit 1
  fi
fi

echo "Systemd units installed successfully."
echo "Backup folder: $BACKUP_DIR"
