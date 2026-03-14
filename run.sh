#!/usr/bin/env bash
set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
# All paths can be overridden by environment variables.
LOG_DIR="${LOG_DIR:-${HOME}/.copilot}"
PID_FILE="${PID_FILE:-${LOG_DIR}/discord_to_copilot_bridge.pid}"
HEARTBEAT_FILE="${HEARTBEAT_FILE:-${LOG_DIR}/discord_to_copilot_bridge.heartbeat.json}"
BRIDGE_SCRIPT="${BRIDGE_SCRIPT:-$(dirname "$(realpath "$0")")/discord_to_copilot_bridge.py}"
BRIDGE_CMD_FRAGMENT="discord_to_copilot_bridge.py"
STALE_AFTER=90
TAKEOVER_WAIT=20

mkdir -p "$LOG_DIR"

read_cmdline() {
  local pid="$1"
  if [[ -r "/proc/$pid/cmdline" ]]; then
    tr '\0' ' ' < "/proc/$pid/cmdline"
    return 0
  fi
  return 1
}

heartbeat_age() {
  if [[ ! -f "$HEARTBEAT_FILE" ]]; then
    echo 999999
    return 0
  fi
  python3 - <<'PY' "$HEARTBEAT_FILE"
import json, sys, datetime
path = sys.argv[1]
try:
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    ts = data.get('timestamp')
    if not ts:
        print(999999)
        raise SystemExit(0)
    now = datetime.datetime.now(datetime.timezone.utc)
    dt = datetime.datetime.fromisoformat(ts)
    print(max(0, int((now - dt).total_seconds())))
except Exception:
    print(999999)
PY
}

if [[ -f "$PID_FILE" ]]; then
  existing_pid="$(tr -d '[:space:]' < "$PID_FILE" || true)"
  existing_cmd="$(read_cmdline "$existing_pid" 2>/dev/null || true)"
  if [[ "$existing_pid" =~ ^[0-9]+$ ]] && kill -0 "$existing_pid" 2>/dev/null && [[ "$existing_cmd" == *"$BRIDGE_CMD_FRAGMENT"* ]]; then
    age="$(heartbeat_age)"
    if [[ "$age" =~ ^[0-9]+$ ]] && (( age > STALE_AFTER )); then
      echo "[bridge-wrapper] stale bridge pid $existing_pid detected (heartbeat age ${age}s), requesting takeover"
      kill "$existing_pid" 2>/dev/null || true
      deadline=$((SECONDS + TAKEOVER_WAIT))
      while kill -0 "$existing_pid" 2>/dev/null; do
        if (( SECONDS >= deadline )); then
          echo "[bridge-wrapper] stale bridge pid $existing_pid did not exit after ${TAKEOVER_WAIT}s"
          exit 1
        fi
        sleep 1
      done
    else
      echo "[bridge-wrapper] waiting for healthy bridge pid $existing_pid to exit"
      while kill -0 "$existing_pid" 2>/dev/null; do
        existing_cmd="$(read_cmdline "$existing_pid" 2>/dev/null || true)"
        if [[ "$existing_cmd" != *"$BRIDGE_CMD_FRAGMENT"* ]]; then
          break
        fi
        sleep 5
      done
    fi
  fi
fi

echo "$$" > "$PID_FILE"
SESSION_ARGS=()
if [[ -n "${DISCORD_COPILOT_SESSION_ID:-}" ]]; then
  SESSION_ARGS=(--session-id "$DISCORD_COPILOT_SESSION_ID")
fi
exec python3 "$BRIDGE_SCRIPT" --watch --interval 10 "${SESSION_ARGS[@]}"
