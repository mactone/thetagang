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

# Returns 0 if within the "market near" window: Mon-Fri, 13:00-21:00 UTC
# (NYSE opens 13:30 UTC; window starts 30min early so thetagang's own 1h wait handles
# the gap; ends 1h after close at 20:00. Starting at 09:30 caused 4h of false-positive
# "exchange closed" errors before market open.)
is_market_near() {
  local dow utc_h utc_m total_min
  dow=$(date -u +%w)          # 0=Sun, 6=Sat
  utc_h=$(date -u +%-H)
  utc_m=$(date -u +%-M)
  total_min=$((utc_h * 60 + utc_m))
  local WIN_START=$((13 * 60 + 0))   # 13:00 UTC (30min before NYSE open)
  local WIN_END=$((21 * 60))          # 21:00 UTC
  [[ $dow -ge 1 && $dow -le 5 && $total_min -ge $WIN_START && $total_min -le $WIN_END ]]
}

# Sleeps silently (no Telegram) until the next market-near window if not already near.
market_sleep_if_needed() {
  is_market_near && return 0
  local dow utc_h utc_m total_min sleep_sec rem_today
  dow=$(date -u +%w)
  utc_h=$(date -u +%-H)
  utc_m=$(date -u +%-M)
  total_min=$((utc_h * 60 + utc_m))
  local WIN_START=$((13 * 60 + 0))   # 13:00 UTC
  rem_today=$(( (24 * 60 - total_min) * 60 ))
  if [[ $dow -eq 0 ]]; then
    # Sunday → Monday WIN_START
    sleep_sec=$(( rem_today + WIN_START * 60 ))
  elif [[ $dow -eq 6 ]]; then
    # Saturday → Monday WIN_START
    sleep_sec=$(( rem_today + 24 * 3600 + WIN_START * 60 ))
  elif [[ $total_min -lt $WIN_START ]]; then
    # Weekday, before window today
    sleep_sec=$(( (WIN_START - total_min) * 60 ))
  else
    # Weekday, after window — skip to next trading day
    # Friday after-hours → Monday; otherwise → tomorrow
    local next_days=1
    [[ $dow -eq 5 ]] && next_days=3
    sleep_sec=$(( rem_today + (next_days - 1) * 24 * 3600 + WIN_START * 60 ))
  fi
  echo "[entrypoint] Market not near (dow=$dow, utc=$(date -u +%H:%M)) — sleeping ${sleep_sec}s until next trading window (no Telegram sent)"
  sleep "$sleep_sec" || true
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
    market_sleep_if_needed
    cycle=$((cycle + 1))
    echo "[entrypoint] Trading engine starting (cycle #${cycle})..."
    set +e
    thetagang "${THETAGANG_ARGS[@]}" --without-ibc
    EXIT_CODE=$?
    set -e
    if [[ $EXIT_CODE -eq 0 ]]; then
      echo "[entrypoint] Trading engine completed (cycle #${cycle}, exit 0). Next run in 3600s."
      send_tg "✅ <b>Engine cycle #${cycle} done</b> — next run in 1h"
      sleep 3600 || true
    else
      echo "[entrypoint] Trading engine exited with code $EXIT_CODE (cycle #${cycle})."
      if is_market_near; then
        send_tg "🚨 <b>Engine cycle #${cycle} error</b> — exit code ${EXIT_CODE}, restarting in 60s"
        sleep 60 || true
      else
        echo "[entrypoint] Market not near — suppressing Telegram alert, sleeping until next window."
        market_sleep_if_needed
      fi
    fi
  done
}
trading_engine_loop &

# Telegram bot daemon (foreground — keeps the container alive).
exec thetagang "${THETAGANG_ARGS[@]}" --bot
