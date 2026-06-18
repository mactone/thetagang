#!/bin/bash

set -e
set -x

# Container restarts preserve filesystem changes, so a crashed/old Xvfb can leave
# stale display lock/socket files that make the next start fail with:
# "Server is already active for display 1".
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1
mkdir -p /tmp/.X11-unix
chmod 1777 /tmp/.X11-unix

Xvfb :1 -ac -screen 0 1024x768x24 &
XVFB_PID=$!
export DISPLAY=:1

# Fail fast if Xvfb could not start instead of silently continuing without GUI.
sleep 0.5
if ! kill -0 "$XVFB_PID" 2>/dev/null; then
  wait "$XVFB_PID"
fi

# make sure jni path is set
export LD_LIBRARY_PATH="/usr/lib/$(arch)-linux-gnu/jni"

# Strip --bot if present so we can control both processes explicitly.
THETAGANG_ARGS=()
for arg in "$@"; do
  [[ "$arg" == "--bot" ]] && continue
  THETAGANG_ARGS+=("$arg")
done

# Read Telegram credentials from the mounted config file so we can push
# engine lifecycle events without going through the bot process.
_TOML_FILE=""
for arg in "${THETAGANG_ARGS[@]}"; do
  [[ "$_TOML_FILE_NEXT" == "1" ]] && { _TOML_FILE="$arg"; _TOML_FILE_NEXT=0; }
  [[ "$arg" == "--config" ]] && _TOML_FILE_NEXT=1
done
TG_TOKEN=$(grep -E '^\s*bot_token\s*=' "${_TOML_FILE:-/etc/thetagang/thetagang.toml}" 2>/dev/null \
  | head -1 | sed 's/.*= *"\(.*\)".*/\1/')
TG_CHAT=$(grep -E '^\s*chat_id\s*=' "${_TOML_FILE:-/etc/thetagang/thetagang.toml}" 2>/dev/null \
  | head -1 | sed 's/.*= *"\(.*\)".*/\1/')

send_tg() {
  [[ -z "${TG_TOKEN:-}" || -z "${TG_CHAT:-}" ]] && return 0
  curl -s -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    -H "Content-Type: application/json" \
    -d "{\"chat_id\":\"${TG_CHAT}\",\"text\":\"$1\",\"parse_mode\":\"HTML\"}" \
    > /dev/null 2>&1 || true
}

# ---------------------------------------------------------------------------
# Start persistent IBC/TWS daemon.
# IBC launches IB Gateway and keeps it alive via Watchdog (clientId=98).
# The trading engine connects with --without-ibc (clientId=1).
# The Telegram bot also connects independently (clientId=99).
# ---------------------------------------------------------------------------
ibc_daemon_loop() {
  while true; do
    echo "[entrypoint] Starting IBC daemon..."
    set +e
    python /src/scripts/ibc_daemon.py "${THETAGANG_ARGS[@]/#--config/--config}"
    IBC_EXIT=$?
    set -e
    echo "[entrypoint] IBC daemon exited (code $IBC_EXIT), restarting in 30s..."
    sleep 30 || true
  done
}
ibc_daemon_loop &

# Wait for TWS port 7497 to accept connections (up to 120s).
echo "[entrypoint] Waiting for TWS to become ready on port 7497..."
WAIT=0
until nc -z 127.0.0.1 7497 2>/dev/null; do
  sleep 5
  WAIT=$((WAIT + 5))
  if [[ $WAIT -ge 120 ]]; then
    echo "[entrypoint] WARNING: TWS not ready after 120s, proceeding anyway"
    break
  fi
done
echo "[entrypoint] TWS is ready."

# ---------------------------------------------------------------------------
# Trading engine loop: connects to the already-running TWS with --without-ibc,
# runs one portfolio cycle, then sleeps before the next cycle.
# ---------------------------------------------------------------------------
trading_engine_loop() {
  local cycle=0
  send_tg "⚡ <b>ThetaGang container started</b>"
  while true; do
    cycle=$((cycle + 1))
    echo "[entrypoint] Trading engine starting (cycle #${cycle})..."
    set +e
    thetagang "${THETAGANG_ARGS[@]}" --without-ibc
    EXIT_CODE=$?
    set -e
    if [[ $EXIT_CODE -eq 0 ]]; then
      echo "[entrypoint] Trading engine completed (cycle #${cycle}, exit 0). Next run in 3600s."
      send_tg "✅ <b>Engine cycle #${cycle} done</b> — next run in 1h"
    else
      echo "[entrypoint] Trading engine exited with code $EXIT_CODE (cycle #${cycle}). Restarting in 60s..."
      send_tg "🚨 <b>Engine cycle #${cycle} error</b> — exit code ${EXIT_CODE}, restarting in 60s"
    fi
    if [[ $EXIT_CODE -eq 0 ]]; then
      sleep 3600 || true
    else
      sleep 60 || true
    fi
  done
}
trading_engine_loop &

# Telegram bot daemon (foreground — keeps the container alive).
exec thetagang "${THETAGANG_ARGS[@]}" --bot
