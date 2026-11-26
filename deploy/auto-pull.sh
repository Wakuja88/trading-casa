#!/bin/bash
# Baja los cambios de GitHub. Si cambió el servidor, lo reinicia.
set -euo pipefail
cd /opt/trading
git fetch --quiet origin main
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
[ "$LOCAL" = "$REMOTE" ] && exit 0

CHANGED=$(git diff --name-only "$LOCAL" "$REMOTE")
git reset --hard --quiet origin/main
echo "Actualizado ${LOCAL:0:7} -> ${REMOTE:0:7}: $(echo $CHANGED)"

if echo "$CHANGED" | grep -qE '^(server\.py|deploy/trading\.service)$'; then
  install -m 644 deploy/trading.service /etc/systemd/system/trading.service
  systemctl daemon-reload
  systemctl restart trading
  echo "Servidor reiniciado"
fi
