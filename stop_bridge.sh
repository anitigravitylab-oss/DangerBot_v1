#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash stop_bridge.sh --name NAME [--delete-files]

Options:
  --name NAME      Instance name used in create_bridge.sh
  --delete-files   Remove the instance env, pid, lock, heartbeat, and state files
  --help           Show this help
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTANCE_DIR="$SCRIPT_DIR/.bridge-instances"
PYTHON_BIN="${PYTHON_BIN:-$SCRIPT_DIR/.venv/bin/python}"
INSTANCE_NAME=""
DELETE_FILES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)
      INSTANCE_NAME="${2:-}"
      shift 2
      ;;
    --delete-files)
      DELETE_FILES=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "$INSTANCE_NAME" ]]; then
  echo "--name is required" >&2
  exit 1
fi

INSTANCE_SLUG="$(printf '%s' "$INSTANCE_NAME" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9._-' '-')"
ENV_FILE="$INSTANCE_DIR/${INSTANCE_SLUG}.env"
PID_FILE="${HOME}/.copilot/discord_to_copilot_bridge_${INSTANCE_SLUG}.pid"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Instance env not found: $ENV_FILE" >&2
  exit 1
fi

json="$("$PYTHON_BIN" - "$ENV_FILE" <<'PY'
import json, sys
from pathlib import Path

data = {}
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.startswith("#"):
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
print(json.dumps(data))
PY
)"

state_file="$(printf '%s' "$json" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("BRIDGE_STATE_FILE",""))')"
lock_file="$(printf '%s' "$json" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("BRIDGE_LOCK_FILE",""))')"
heartbeat_file="$(printf '%s' "$json" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("BRIDGE_HEARTBEAT_FILE",""))')"

pid=""
if [[ -f "$PID_FILE" ]]; then
  pid="$(tr -d '[:space:]' < "$PID_FILE" || true)"
fi

if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
  kill "$pid"
  for _ in $(seq 1 20); do
    if ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    sleep 1
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -9 "$pid"
  fi
  echo "Stopped instance ${INSTANCE_SLUG} (pid=${pid})"
else
  echo "Instance ${INSTANCE_SLUG} is not running"
fi

rm -f "$PID_FILE"

if (( DELETE_FILES )); then
  rm -f "$ENV_FILE"
  [[ -n "$state_file" ]] && rm -f "$state_file"
  [[ -n "$lock_file" ]] && rm -f "$lock_file"
  [[ -n "$heartbeat_file" ]] && rm -f "$heartbeat_file"
  echo "Deleted files for instance ${INSTANCE_SLUG}"
fi
