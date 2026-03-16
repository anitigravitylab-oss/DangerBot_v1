#!/bin/bash
# Discord-to-Codex Bridge 起動ラッパー
# - 既存インスタンスが動いていれば待機
# - 終了後は自動再起動（systemd Restart=always と組み合わせて使用）

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_SCRIPT="${BRIDGE_SCRIPT:-$SCRIPT_DIR/discord_to_codex_bridge.py}"
LOG_DIR="${LOG_DIR:-${HOME}/.dangerbot/logs}"
LOG_FILE="${LOG_FILE:-$LOG_DIR/discord_to_codex_bridge.log}"
CHANNEL="${CODEX_DISCORD_CHANNEL:-${DISCORD_CODEX_CHANNEL_ID:-}}"
CWD="${CODEX_CWD:-$SCRIPT_DIR}"

mkdir -p "$LOG_DIR"

# .env を読み込む (ENV_FILE 環境変数または同ディレクトリの .env)
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/.env}"
# shellcheck disable=SC1090
source "$ENV_FILE" 2>/dev/null || true

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Discord-Codex Bridge 起動 channel=${CHANNEL:-<not set>} cwd=$CWD" >> "$LOG_FILE"

exec python3 -u "$BRIDGE_SCRIPT" \
  ${CHANNEL:+--channel "$CHANNEL"} \
  --cwd "$CWD" \
  --interval 10
