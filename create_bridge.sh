#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash create_bridge.sh --name NAME --user-id USER_ID --project-root PATH [options]

Required:
  --name NAME                 Instance name used for local files and logs
  --user-id USER_ID           Discord user ID allowed to issue instructions
  --project-root PATH         Project path Copilot should work in

Channel selection:
  --channel-id ID             Use an existing Discord channel
  --guild-id ID               Create a new channel in this guild
  --channel-name NAME         Name for the new Discord channel
  --category-id ID            Optional parent category when creating a channel

Optional:
  --session-id ID             Use an explicit Copilot session ID
  --model NAME                Override model (default: DISCORD_COPILOT_MODEL or gpt-5.4)
  --base-env PATH             Base env file with DISCORD_BOT_TOKEN (default: ./.env)
  --python PATH               Python executable (default: ./.venv/bin/python)
  --dry-run                   Only print the generated config, do not start the bridge
  --foreground                Run in foreground instead of background
  --help                      Show this help

Examples:
  bash create_bridge.sh \
    --name project-a \
    --channel-id 123456789012345678 \
    --user-id 987654321098765432 \
    --project-root /root/workspace/project-a

  bash create_bridge.sh \
    --name project-b \
    --guild-id 111111111111111111 \
    --channel-name project-b-ops \
    --category-id 222222222222222222 \
    --user-id 987654321098765432 \
    --project-root /root/workspace/project-b
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_ENV="$SCRIPT_DIR/.env"
PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
INSTANCE_NAME=""
CHANNEL_ID=""
GUILD_ID=""
CHANNEL_NAME=""
CATEGORY_ID=""
USER_ID=""
PROJECT_ROOT=""
SESSION_ID=""
MODEL=""
DRY_RUN=0
FOREGROUND=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)
      INSTANCE_NAME="${2:-}"
      shift 2
      ;;
    --channel-id)
      CHANNEL_ID="${2:-}"
      shift 2
      ;;
    --guild-id)
      GUILD_ID="${2:-}"
      shift 2
      ;;
    --channel-name)
      CHANNEL_NAME="${2:-}"
      shift 2
      ;;
    --category-id)
      CATEGORY_ID="${2:-}"
      shift 2
      ;;
    --user-id)
      USER_ID="${2:-}"
      shift 2
      ;;
    --project-root)
      PROJECT_ROOT="${2:-}"
      shift 2
      ;;
    --session-id)
      SESSION_ID="${2:-}"
      shift 2
      ;;
    --model)
      MODEL="${2:-}"
      shift 2
      ;;
    --base-env)
      BASE_ENV="${2:-}"
      shift 2
      ;;
    --python)
      PYTHON_BIN="${2:-}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --foreground)
      FOREGROUND=1
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

if [[ -z "$INSTANCE_NAME" || -z "$USER_ID" || -z "$PROJECT_ROOT" ]]; then
  echo "--name, --user-id, and --project-root are required" >&2
  exit 1
fi

if [[ -n "$CHANNEL_ID" && -n "$GUILD_ID" ]]; then
  echo "Use either --channel-id or --guild-id/--channel-name, not both" >&2
  exit 1
fi

if [[ -z "$CHANNEL_ID" && ( -z "$GUILD_ID" || -z "$CHANNEL_NAME" ) ]]; then
  echo "Provide --channel-id or both --guild-id and --channel-name" >&2
  exit 1
fi

if [[ ! -f "$BASE_ENV" ]]; then
  echo "Base env file not found: $BASE_ENV" >&2
  exit 1
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python executable not found or not executable: $PYTHON_BIN" >&2
  exit 1
fi

set -a
source "$BASE_ENV"
set +a

if [[ -z "${DISCORD_BOT_TOKEN:-}" ]]; then
  echo "DISCORD_BOT_TOKEN is missing in $BASE_ENV" >&2
  exit 1
fi

if [[ -z "$MODEL" ]]; then
  MODEL="${DISCORD_COPILOT_MODEL:-gpt-5.4}"
fi

if [[ -z "$SESSION_ID" ]]; then
  SESSION_ID="$("$PYTHON_BIN" -c 'import uuid; print(uuid.uuid4())')"
fi

INSTANCE_SLUG="$(printf '%s' "$INSTANCE_NAME" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9._-' '-')"
if [[ -z "$INSTANCE_SLUG" ]]; then
  echo "Instance name produced an empty slug" >&2
  exit 1
fi

INSTANCE_DIR="$SCRIPT_DIR/.bridge-instances"
LOG_DIR="${HOME}/.copilot/logs"
mkdir -p "$INSTANCE_DIR" "$LOG_DIR"

create_channel() {
  local payload
  local response
  payload="$("$PYTHON_BIN" - "$CHANNEL_NAME" "$CATEGORY_ID" <<'PY'
import json, sys
name = sys.argv[1]
category_id = sys.argv[2]
payload = {"name": name, "type": 0}
if category_id:
    payload["parent_id"] = category_id
print(json.dumps(payload))
PY
)"
  response="$(curl -fsS \
    -H "Authorization: Bot ${DISCORD_BOT_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$payload" \
    "https://discord.com/api/v10/guilds/${GUILD_ID}/channels")"
  CHANNEL_ID="$("$PYTHON_BIN" -c 'import json,sys; data=json.load(sys.stdin); print(data["id"])' <<<"$response")"
}

if [[ -z "$CHANNEL_ID" ]]; then
  create_channel
fi

STATE_FILE="${HOME}/.copilot/discord_to_copilot_bridge_${INSTANCE_SLUG}.state.json"
LOCK_FILE="${HOME}/.copilot/discord_to_copilot_bridge_${INSTANCE_SLUG}.lock"
HEARTBEAT_FILE="${HOME}/.copilot/discord_to_copilot_bridge_${INSTANCE_SLUG}.heartbeat.json"
PID_FILE="${HOME}/.copilot/discord_to_copilot_bridge_${INSTANCE_SLUG}.pid"
LOG_FILE="${LOG_DIR}/discord_to_copilot_bridge_${INSTANCE_SLUG}.log"
ENV_FILE="${INSTANCE_DIR}/${INSTANCE_SLUG}.env"

cat > "$ENV_FILE" <<EOF
DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN}
DISCORD_CHANNEL_INSTRUCTIONS=${CHANNEL_ID}
DISCORD_INSTRUCTION_USER_ID=${USER_ID}
COPILOT_PROJECT_ROOT=${PROJECT_ROOT}
DISCORD_COPILOT_MODEL=${MODEL}
DISCORD_COPILOT_SESSION_ID=${SESSION_ID}
BRIDGE_STATE_FILE=${STATE_FILE}
BRIDGE_LOCK_FILE=${LOCK_FILE}
BRIDGE_HEARTBEAT_FILE=${HEARTBEAT_FILE}
EOF

echo "instance_name=${INSTANCE_SLUG}"
echo "channel_id=${CHANNEL_ID}"
echo "session_id=${SESSION_ID}"
echo "env_file=${ENV_FILE}"
echo "log_file=${LOG_FILE}"

if (( DRY_RUN )); then
  exit 0
fi

if (( FOREGROUND )); then
  set -a
  source "$ENV_FILE"
  set +a
  exec "$PYTHON_BIN" "$SCRIPT_DIR/discord_to_copilot_bridge.py" --watch --interval 10 --channel-id "$CHANNEL_ID" --user-id "$USER_ID" --session-id "$SESSION_ID" --model "$MODEL"
fi

setsid bash -lc "
  set -euo pipefail
  cd '$SCRIPT_DIR'
  set -a
  source '$ENV_FILE'
  set +a
  exec '$PYTHON_BIN' '$SCRIPT_DIR/discord_to_copilot_bridge.py' --watch --interval 10 --channel-id '$CHANNEL_ID' --user-id '$USER_ID' --session-id '$SESSION_ID' --model '$MODEL'
" >>"$LOG_FILE" 2>&1 < /dev/null &

echo $! > "$PID_FILE"
echo "pid=$!"
