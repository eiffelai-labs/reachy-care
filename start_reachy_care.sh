#!/bin/bash
# start_reachy_care.sh — Script unique de démarrage Reachy Care
# Lance tout dans le bon ordre, sans rien casser.

set -e

CARE_DIR="/home/pollen/reachy_care"
VENV="/venvs/apps_venv/bin/python3"
LOG_DIR="$CARE_DIR/logs"
BT_MAC="2C:81:BF:11:FA:C7"

echo "=== Reachy Care startup ==="

# 1. Kill tout ce qui traîne
echo "[1/6] Nettoyage..."
# Kill ALL our python processes (including controller-launched main.py)
pkill -9 -f "conv_app_v2/main.py" 2>/dev/null || true
pkill -9 -f "reachy_care/main.py" 2>/dev/null || true
# Also kill by exact path (controller launches with full path)
pkill -9 -f "/home/pollen/reachy_care/main.py" 2>/dev/null || true
# Kill our aplay processes (BT playback + AEC ref) — NOT bluealsa-aplay system service
for pid in $(pgrep -f "aplay -D" 2>/dev/null); do
    kill -9 "$pid" 2>/dev/null || true
done
sleep 2

# 2. Bluetooth : disconnect + reconnect (crée un PCM bluealsa frais)
echo "[2/6] Bluetooth..."
bluetoothctl disconnect "$BT_MAC" 2>/dev/null || true
sleep 1
bluetoothctl connect "$BT_MAC" 2>/dev/null
sleep 3
# Verify BT (retry once if D-Bus flaky)
if ! bluetoothctl info "$BT_MAC" 2>/dev/null | grep -q "Connected: yes"; then
    echo "BT check failed, retrying..."
    sleep 2
    bluetoothctl connect "$BT_MAC" 2>/dev/null
    sleep 3
fi
echo "BT connectée: $BT_MAC"

# 3. Volume USB à 0 (son BT uniquement, XMOS AEC ref inaudible)
echo "[3/6] Volume USB → 0..."
amixer -c 0 sset PCM 0 2>/dev/null

# 4. Lancer conv_app_v2 (prend le daemon, doit démarrer en premier)
echo "[4/6] Conv_app_v2..."
rm -f "$LOG_DIR/conv_app_v2.log"
cd "$CARE_DIR/conv_app_v2"
nohup $VENV main.py > "$LOG_DIR/conv_app_v2.log" 2>&1 &
CONV_PID=$!
echo "conv_app_v2 PID: $CONV_PID"

# 5. Attendre que conv_app_v2 soit online avant de lancer main.py
echo "[5/6] Attente conv_app_v2 online..."
for i in $(seq 1 30); do
    if grep -q "ConversationEngine started" "$LOG_DIR/conv_app_v2.log" 2>/dev/null; then
        echo "conv_app_v2 online après ${i}s"
        break
    fi
    sleep 1
done
if ! grep -q "ConversationEngine started" "$LOG_DIR/conv_app_v2.log" 2>/dev/null; then
    echo "ATTENTION: conv_app_v2 pas encore online après 30s"
fi

# 6. Lancer main.py (vision, wake word, face recog — n'utilise pas le daemon)
echo "[6/6] Main.py..."
rm -f "$LOG_DIR/main.log"
cd "$CARE_DIR"
nohup $VENV main.py > "$LOG_DIR/main.log" 2>&1 &
MAIN_PID=$!
echo "main.py PID: $MAIN_PID"

echo ""
echo "=== Reachy Care démarré ==="
echo "conv_app_v2: $CONV_PID"
echo "main.py: $MAIN_PID"
echo "Logs: $LOG_DIR/conv_app_v2.log, $LOG_DIR/main.log"
