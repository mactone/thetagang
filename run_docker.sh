#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Building thetagang Docker image ==="
docker build -t thetagang .

# Stop any previous container
docker rm -f thetagang-bot 2>/dev/null || true

echo ""
echo "=== Starting thetagang-bot (Telegram daemon + IBKR Gateway) ==="

# Run container directly with start-ib-gateway.sh entrypoint
docker run -d --name thetagang-bot \
  --restart unless-stopped \
  -v "$SCRIPT_DIR/thetagang.toml:/etc/thetagang/thetagang.toml" \
  -v "$SCRIPT_DIR/ibc-config.ini:/etc/thetagang/ibc-config.ini" \
  -v "$SCRIPT_DIR/data:/etc/thetagang/data" \
  thetagang \
  --config /etc/thetagang/thetagang.toml --bot

echo ""
echo "=== Done! Checking container status ==="
docker ps | grep thetagang-bot

echo ""
echo "View logs with: docker logs -f thetagang-bot"