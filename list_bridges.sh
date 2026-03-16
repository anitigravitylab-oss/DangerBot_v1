#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTANCE_DIR="$SCRIPT_DIR/.bridge-instances"
PYTHON_BIN="${PYTHON_BIN:-$SCRIPT_DIR/.venv/bin/python}"

if [[ ! -d "$INSTANCE_DIR" ]]; then
  echo "No bridge instances found."
  exit 0
fi

shopt -s nullglob
files=("$INSTANCE_DIR"/*.env)
shopt -u nullglob

if [[ ${#files[@]} -eq 0 ]]; then
  echo "No bridge instances found."
  exit 0
fi

printf '%-24s %-20s %-38s %-10s %s\n' "INSTANCE" "CHANNEL" "SESSION" "STATUS" "PID"

for env_file in "${files[@]}"; do
  instance_name="$(basename "$env_file" .env)"
  json="$("$PYTHON_BIN" - "$env_file" <<'PY'
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

  channel_id="$(printf '%s' "$json" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("DISCORD_CHANNEL_INSTRUCTIONS",""))')"
  session_id="$(printf '%s' "$json" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("DISCORD_COPILOT_SESSION_ID",""))')"
  heartbeat_file="$(printf '%s' "$json" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("BRIDGE_HEARTBEAT_FILE",""))')"
  pid_file="${HOME}/.copilot/discord_to_copilot_bridge_${instance_name}.pid"
  pid=""
  status="stopped"

  if [[ -f "$pid_file" ]]; then
    pid="$(tr -d '[:space:]' < "$pid_file" || true)"
  fi

  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    status="running"
  fi

  if [[ -n "$heartbeat_file" && -f "$heartbeat_file" ]]; then
    hb_status="$("$PYTHON_BIN" - "$heartbeat_file" <<'PY'
import json, sys
from pathlib import Path
try:
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(data.get("status", "unknown"))
except Exception:
    print("unknown")
PY
)"
    status="$status:$hb_status"
  fi

  printf '%-24s %-20s %-38s %-10s %s\n' "$instance_name" "$channel_id" "$session_id" "$status" "${pid:-"-"}"
done
