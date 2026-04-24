#!/bin/bash
# install.sh — installer les services systemd Reachy Care sur le Pi
# Usage : bash install.sh
# Idempotent : peut être relancé sans danger.
set -euo pipefail

SRC=$(dirname "$(readlink -f "$0")")
SYSTEMD_DIR=/etc/systemd/system
JOURNALD_DIR=/etc/systemd/journald.conf.d

echo "[install] Copie services systemd → $SYSTEMD_DIR"
sudo cp "$SRC/reachy-care-aec.service"  "$SYSTEMD_DIR/"
sudo cp "$SRC/reachy-care-conv.service" "$SYSTEMD_DIR/"
sudo cp "$SRC/reachy-care-main.service" "$SYSTEMD_DIR/"

echo "[install] Copie rotation journald → $JOURNALD_DIR"
sudo mkdir -p "$JOURNALD_DIR"
sudo cp "$SRC/journald-reachy-care.conf" "$JOURNALD_DIR/reachy-care.conf"

echo "[install] daemon-reload + enable"
sudo systemctl daemon-reload
sudo systemctl restart systemd-journald
sudo systemctl enable reachy-care-aec.service
sudo systemctl enable reachy-care-conv.service
sudo systemctl enable reachy-care-main.service

echo "[install] Services installés et activés. Démarrer avec :"
echo "   sudo systemctl start reachy-care-main"
echo "   (démarre conv + aec automatiquement via dépendances)"
echo ""
echo "[install] Logs temps réel :"
echo "   journalctl -u reachy-care-main -f"
echo "   journalctl -u reachy-care-conv -f"
