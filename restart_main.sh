#!/bin/bash
# restart_main.sh — Redémarre main.py proprement (kill-and-wait via PID file)
# Usage: bash restart_main.sh
set -e

CARE_DIR="/home/pollen/reachy_care"
PID_FILE="/tmp/reachy_care.pid"
LOG="$CARE_DIR/logs/reachy_care.log"

export GST_PLUGIN_PATH=/opt/gst-plugins-rs/lib/aarch64-linux-gnu/gstreamer-1.0
export PYTHONPATH="$CARE_DIR:$CARE_DIR/tools_for_conv_app"
export CONV_APP_MODE=v2

# 1. Kill via PID file si disponible
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE" 2>/dev/null)
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[restart_main] Kill PID $OLD_PID..."
        kill "$OLD_PID"
        for i in $(seq 1 20); do
            kill -0 "$OLD_PID" 2>/dev/null || break
            sleep 0.3
        done
        kill -9 "$OLD_PID" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
fi

# 2. Filet de sécurité — tuer tout main.py résiduel
RESIDUAL=$(pgrep -f "$CARE_DIR/main.py" 2>/dev/null || true)
if [ -n "$RESIDUAL" ]; then
    echo "[restart_main] Résidus : $RESIDUAL — kill -9"
    kill -9 $RESIDUAL 2>/dev/null || true
    sleep 1
fi

# 3. Lancement
echo "[restart_main] Lancement main.py..."
cd "$CARE_DIR"
nohup /venvs/apps_venv/bin/python main.py > "$LOG" 2>&1 &
echo "[restart_main] PID=$!"
