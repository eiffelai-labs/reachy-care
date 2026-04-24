#!/bin/bash
# start.sh — démarre Reachy Care avec tous les garde-fous
set -e
VENV="/venvs/apps_venv/bin/python"
MAIN="/home/pollen/reachy_care/main.py"
LOG="/home/pollen/reachy_care/logs/reachy_care.log"

# 1. Vérifier daemon
DAEMON_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
    --max-time 5 http://localhost:8000/api/state/full || echo "000")
if [ "$DAEMON_STATUS" != "200" ]; then
    echo "[start.sh] Daemon absent — restart..."
    sudo systemctl restart reachy-mini-daemon.service && sleep 8
    curl -s -X POST 'http://localhost:8000/api/daemon/start?wake_up=true'
    sleep 8
fi

# 2. Vérifier qu'aucune instance ne tourne déjà
PID_FILE="/tmp/reachy_care.pid"
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[start.sh] Reachy Care déjà en cours (PID $OLD_PID). Arrêtez-le d'abord."
        exit 1
    fi
fi

# 3. GStreamer plugins
export GST_PLUGIN_PATH=/opt/gst-plugins-rs/lib/aarch64-linux-gnu/gstreamer-1.0

# 4. Lancer
mkdir -p /home/pollen/reachy_care/logs
echo "[start.sh] Lancement Reachy Care..."
exec "$VENV" "$MAIN" 2>&1 | tee -a "$LOG"
