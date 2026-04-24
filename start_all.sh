#!/bin/bash
# start_all.sh — Lance conv_app + Reachy Care ensemble en une seule commande
set -e

VENV="/venvs/apps_venv/bin/python"
CARE_DIR="/home/pollen/reachy_care"
LOG_DIR="$CARE_DIR/logs"
CONV_PID_FILE="/tmp/conv_app.pid"

# ── Mode conv_app ─────────────────────────────────────────────────────────
# "v2"     = conv_app_v2/main.py (custom, mode julie-demo-clean)
# "pollen" = binaire Pollen patche via patch_source.py (rollback)
CONV_APP_MODE="${CONV_APP_MODE:-v2}"
echo "[start_all] CONV_APP_MODE=$CONV_APP_MODE"

# Fix .asoundrc — supprime toute config corrompue, ALSA utilise ses défauts système
rm -f ~/.asoundrc

# Fuseau horaire — survit au reflash SD card (fix)
sudo timedatectl set-timezone Europe/Paris 2>/dev/null || true

export GST_PLUGIN_PATH=/opt/gst-plugins-rs/lib/aarch64-linux-gnu/gstreamer-1.0
export REACHY_CARE_PATH="$CARE_DIR"
# Outils conv_app sur le PYTHONPATH pour qu'ils soient importables par le conv_app
export PYTHONPATH="${CARE_DIR}:${CARE_DIR}/tools_for_conv_app:${PYTHONPATH}"

# Charger les variables depuis le .env du projet reachy_care (copie depuis conv_app Pollen)
# Contient OPENAI_API_KEY (cadeau HuggingFace pour Reachy Mini) + BRAVE_API_KEY.
ENV_FILE="$CARE_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    set -o allexport
    source "$ENV_FILE"
    set +o allexport
fi

# P1-C : s'assurer que OPENAI_API_KEY est exportée pour main.py (résumé session, extraction faits)
# Fallback au cas où source .env ne suffit pas (format .env non-standard, valeur vide, etc.)
if [ -z "${OPENAI_API_KEY:-}" ] && [ -f "$ENV_FILE" ]; then
    _oai_key=$(grep -m1 '^OPENAI_API_KEY=' "$ENV_FILE" 2>/dev/null \
        | cut -d= -f2- | sed "s/^['\"]//; s/['\"]$//")
    [ -n "$_oai_key" ] && export OPENAI_API_KEY="$_oai_key"
    unset _oai_key
fi

# Profil Reachy Care (priorité sur le .env)
export REACHY_MINI_CUSTOM_PROFILE=reachy_care
export REACHY_MINI_EXTERNAL_PROFILES_DIRECTORY="$CARE_DIR/external_profiles"
export REACHY_MINI_EXTERNAL_TOOLS_DIRECTORY="$CARE_DIR/tools_for_conv_app"
export AUTOLOAD_EXTERNAL_TOOLS=true

mkdir -p "$LOG_DIR"

# ── Volume système au maximum (ALSA pur — PulseAudio INTERDIT) ────────────
# DECISIONS.md / CLAUDE.md : pactl/pulse cassent PortAudio → micro main.py mort.
amixer sset Master 100% unmute 2>/dev/null || true
echo "[start_all] Volume système configuré au maximum"

# ── 1. Cleanup au Ctrl+C (enregistré tôt pour couvrir toutes les étapes) ──
cleanup() {
    echo ""
    echo "[start_all] Arrêt..."
    if [ -f "$CONV_PID_FILE" ]; then
        kill "$(cat "$CONV_PID_FILE")" 2>/dev/null || true
        rm -f "$CONV_PID_FILE"
    fi
    echo "[start_all] Terminé."
}
#   EXIT retiré : /api/power/start utilise _background_op(timeout=90), et
#   start_all.sh doit pouvoir exit 0 SANS tuer conv_app_v2 (lancé avec &).
#   Le trap reste sur INT/TERM pour le cas "Ctrl+C depuis terminal".
trap cleanup INT TERM

# ── 2. Daemon ──────────────────────────────────────────────────────────────
# Restart seulement si daemon absent ou moteurs défaillants (control_mode != enabled).
# head_joints est toujours null dans l'API daemon même quand les moteurs fonctionnent.
# Un restart inconditionnel détruit l'initialisation des servos Dynamixel et
# provoque son propre état corrompu.
# on force aussi le restart si wide_dynamic_range IMX708 n'est pas à 1,
# car le control est flag `grabbed`/`modify-layout` — impossible à poser à chaud
# tant que le pipeline GStreamer du daemon tient le sensor.
HEAD_STATE=$(curl -s --max-time 5 http://localhost:8000/api/state/full 2>/dev/null \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('ok' if d.get('control_mode') == 'enabled' else 'bad')" 2>/dev/null \
  || echo "bad")
# HDR IMX708 : détecter le subdev sensor et lire l'état courant. Seul un des
# subdev expose `wide_dynamic_range` (probe order kernel non déterministe).
HDR_SUBDEV=""
HDR_CURRENT=""
for _sd in /dev/v4l-subdev0 /dev/v4l-subdev1 /dev/v4l-subdev2 /dev/v4l-subdev3; do
    if [ -e "$_sd" ]; then
        _val=$(v4l2-ctl --get-ctrl wide_dynamic_range -d "$_sd" 2>/dev/null | awk -F': ' '/wide_dynamic_range/ {print $2}')
        if [ -n "$_val" ]; then
            HDR_SUBDEV="$_sd"
            HDR_CURRENT="$_val"
            break
        fi
    fi
done
unset _sd _val
if [ -n "$HDR_SUBDEV" ]; then
    echo "[start_all] HDR IMX708 subdev=$HDR_SUBDEV, current=$HDR_CURRENT (cible=1)"
    if [ "$HDR_CURRENT" != "1" ]; then
        echo "[start_all] HDR à poser → force restart daemon pour libérer le sensor."
        HEAD_STATE="bad"
    fi
else
    echo "[start_all] AVERTISSEMENT: aucun subdev IMX708 avec wide_dynamic_range trouvé — HDR skippé."
fi
if [ "$HEAD_STATE" != "ok" ]; then
    echo "[start_all] Daemon absent ou moteurs défaillants — redémarrage complet..."
    if [ -f "$CONV_PID_FILE" ] && kill -0 "$(cat "$CONV_PID_FILE")" 2>/dev/null; then
        echo "[start_all] Arrêt conv_app avant restart daemon..."
        kill "$(cat "$CONV_PID_FILE")" 2>/dev/null
        rm -f "$CONV_PID_FILE"
        sleep 2
    fi
    # Arrêter le daemon au niveau API avant systemctl restart
    curl -s -X POST 'http://localhost:8000/api/daemon/stop?goto_sleep=false' --max-time 10 || true
    sleep 2
    sudo systemctl set-environment GST_PLUGIN_PATH=/opt/gst-plugins-rs/lib/aarch64-linux-gnu/gstreamer-1.0
    # Stop explicite pour libérer le sensor IMX708 (flag V4L2 `grabbed`)
    # avant de poser wide_dynamic_range=1 pendant la fenêtre sans pipeline.
    sudo systemctl stop reachy-mini-daemon.service
    sleep 2
    if [ -n "$HDR_SUBDEV" ] && [ "$HDR_CURRENT" != "1" ]; then
        echo "[start_all] HDR IMX708 : set wide_dynamic_range=1 sur $HDR_SUBDEV (sensor libre)..."
        if v4l2-ctl --set-ctrl wide_dynamic_range=1 -d "$HDR_SUBDEV" 2>&1; then
            _new=$(v4l2-ctl --get-ctrl wide_dynamic_range -d "$HDR_SUBDEV" 2>/dev/null | awk -F': ' '/wide_dynamic_range/ {print $2}')
            echo "[start_all] HDR IMX708 readback=$_new"
            unset _new
        else
            echo "[start_all] AVERTISSEMENT: set HDR échoué — AE restera en mode moyenne image (silhouette contre-jour)."
        fi
    fi
    sudo systemctl start reachy-mini-daemon.service
    sleep 8
    # Après systemctl restart, le daemon démarre en état "running" mais le backend
    # n'est pas initialisé (ready=false, last_alive=null). Il faut OBLIGATOIREMENT :
    # 1) stop au niveau API pour passer en "stopped"
    # 2) start?wake_up=true pour initialiser le backend + lever la tête
    # Sans ça, daemon/start est rejeté avec "Daemon is already running".
    curl -s -X POST 'http://localhost:8000/api/daemon/stop?goto_sleep=false' --max-time 10 || true
    sleep 3
    curl -s -X POST 'http://localhost:8000/api/daemon/start?wake_up=false' --max-time 15 || true
    sleep 8
else
    echo "[start_all] Daemon OK — moteurs activés (main.py fera le wake_up)..."
    curl -s -X POST 'http://localhost:8000/api/motors/set_mode/enabled' --max-time 5 || true
    sleep 2
fi
echo "[start_all] Daemon prêt."

# ── 2b. Activer l'AEC hardware XMOS XVF3800 (test ) ─────────────
# Le firmware Pollen livre le chip AEC DÉSACTIVÉE (FAR_END_DSP_ENABLE=0,
# FAR_EXTGAIN=-60dB). Résultat : aucune annulation écho → boucle infinie
# Reachy s'écoute lui-même. Le daemon reset ces valeurs à chaque start,
# donc on les active APRÈS que le daemon soit stable.
# Pré-requis : audio_io.py écrit déjà la référence vers plughw:0,0 (commit ).
# Pré-requis : speaker hw:0,0 muté (sinon double son USB + XMOS interne).
XMOS_CTL="/venvs/mini_daemon/lib/python3.12/site-packages/reachy_mini/media/audio_control_utils.py"
XMOS_PY="/venvs/mini_daemon/bin/python3"
if [ -f "$XMOS_CTL" ]; then
    echo "[start_all] Activation AEC XMOS (far-end DSP + gain 0dB)..."
    "$XMOS_PY" "$XMOS_CTL" AUDIO_MGR_FAR_END_DSP_ENABLE --values 1 2>&1 | tail -1
    "$XMOS_PY" "$XMOS_CTL" AEC_FAR_EXTGAIN --values 0.0 2>&1 | tail -1
    # Désactivation AGC XMOS (test ) : l'AGC compresse la voix avec le bruit
    # ambiant (dsnoop reachymini_audio_src) → wake word openwakeword ~1/100.
    # On garde MIC_GAIN=90 (défaut Pollen, interdit >90), on coupe juste l'AGC post.
    "$XMOS_PY" "$XMOS_CTL" PP_AGCONOFF --values 0 2>&1 | tail -1
    # ─── SPLIT L/R canaux USB XMOS ─────────────────────────────
    # Contexte :  on avait mis AUDIO_MGR_OP_L/R=[7,3] (ASR sur les 2).
    # Effet secondaire : conv_app_v2 captait les voix distantes (pièce voisine),
    # hallucinations Whisper, Reachy répondait à tout → boucle d'écho gate AttenLabs.
    #
    # Correction : séparer les canaux par rôle.
    #   L=[7,3] → ASR beamformer (pre-PP) → exposé via pcm.mic_alert_in dans
    #     asound.conf → consommé par wake_word.py et sound_detector.py.
    #     Sensible aux voix/cris même de loin ou de côté. Fait pour alerte.
    #   R=[8,0] → post-PP défaut Pollen (AGC + noise gate + equalization) →
    #     exposé via pcm.conv_audio_in → consommé par conv_app_v2/audio_io.py.
    #     Voix propre, noise gate coupe les voix faibles (pièce voisine).
    #
    # Rollback atomique : remettre L=R=[8,0] (tout post-PP, wake word à 0.001)
    # OU L=R=[7,3] (état, hallucinations conv).
    "$XMOS_PY" "$XMOS_CTL" AEC_ASROUTONOFF --values 1 2>&1 | tail -1
    "$XMOS_PY" "$XMOS_CTL" AEC_ASROUTGAIN --values 1.0 2>&1 | tail -1
    "$XMOS_PY" "$XMOS_CTL" AUDIO_MGR_OP_L --values 7 3 2>&1 | tail -1
    "$XMOS_PY" "$XMOS_CTL" AUDIO_MGR_OP_R --values 8 0 2>&1 | tail -1
    # Mute speaker interne hw:0,0 (pas audible mais signal passe pour AEC ref)
    amixer -c 0 sset 'PCM',0 0% mute 2>/dev/null | tail -1 >/dev/null
    echo "[start_all] AEC XMOS activée, AGC désactivée, canal L→ASR (alerte) / canal R→post-PP (conv), speaker hw:0,0 muté."
else
    echo "[start_all] AVERTISSEMENT: audio_control_utils.py absent — AEC non activable."
fi

# ── 3. Tuer toute instance orpheline avant de relancer ────────────────────
# Fix : conv_app orpheline → vibration moteurs.
# Fix : tuer aussi l'ancien main.py et les anciens start_all.sh
#   pour éviter les doubles instances (deux voix, deux main.py).
_KILLED=0
if pkill -f reachy-mini-conversation-app 2>/dev/null; then _KILLED=1; fi
if pkill -f "python.*conv_app_v2/main.py" 2>/dev/null; then _KILLED=1; fi
if pkill -f "python.*${CARE_DIR}/main.py" 2>/dev/null; then _KILLED=1; fi
# Tuer les anciens start_all.sh (pas le processus courant $$)
for _pid in $(pgrep -f "bash.*start_all.sh" 2>/dev/null); do
    [ "$_pid" -ne "$$" ] && kill -9 "$_pid" 2>/dev/null && _KILLED=1
done
if [ "$_KILLED" -eq 1 ]; then
    echo "[start_all] Anciens processus tués — attente arrêt..."
    sleep 2
fi
rm -f "$CONV_PID_FILE"

# ── 4. Attendre que le daemon soit prêt (backend ready) ──────────────────
echo "[start_all] Attente daemon backend ready..."
for i in $(seq 1 15); do
    READY=$(curl -s http://localhost:8000/api/daemon/status --max-time 2 | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('backend_status',{}).get('ready','false'))" 2>/dev/null)
    if [ "$READY" = "True" ]; then
        echo "[start_all] Daemon backend ready apres ${i}s."
        break
    fi
    sleep 1
done

# ── 4b. Sortie audio : enceinte USB-C filaire (dongle DAC, basculé ) ────
# Le sink audio ne passe plus par Bluetooth mais par une carte USB Audio Class
# (dongle USB-C → jack 3.5 mm avec DAC intégré + enceinte mini-jack), branchée
# sur le port USB-C arrière du Reachy Mini. Détectée par ALSA sous le nom "Device"
# (carte 3). Avantages : XMOS AEC redevient viable (signal de référence sur hw:0,0),
# pas de bluealsa single-client, pas de subprocess aplay GIL contention, latence <20 ms.
USB_CARD_NAME="Device"   # GeneralPlus USB Audio Device (CARD= dans aplay -l)
echo "[start_all] Sortie audio : carte USB '$USB_CARD_NAME' (filaire)"
if ! aplay -l 2>/dev/null | grep -q "$USB_CARD_NAME"; then
    echo "[start_all] AVERTISSEMENT : carte USB '$USB_CARD_NAME' non détectée — vérifier le dongle."
fi

# ── 4c. ~/.asoundrc NON réécrit ───────────────────────────────────
# /etc/asound.conf définit désormais reachymini_audio_sink + reachymini_audio_src
# en USB (installé via `sudo cp resources/asound.conf /etc/asound.conf`).
# Redéfinir les mêmes alias dans ~/.asoundrc provoque une collision ALSA
# (_toplevel_ parse error "Invalid argument"). Donc on laisse ~/.asoundrc vide.
# Le `rm -f ~/.asoundrc` ligne 17 du script garantit qu'aucune version stale ne subsiste.

# ── 4d. Attendre stabilisation du device ALSA USB ─────────────────────────
echo "[start_all] Vérification device reachymini_audio_sink (via /etc/asound.conf)..."
if aplay -L 2>/dev/null | grep -q "reachymini_audio_sink"; then
    echo "[start_all] Device ALSA reachymini_audio_sink prêt (→ carte USB $USB_CARD_NAME)."
else
    echo "[start_all] ERREUR: device reachymini_audio_sink absent — vérifier /etc/asound.conf"
fi

# ── 4e. Vérification présence alias via aplay -L (USB, pas ~/.asoundrc) ──
# Avant : on appelait has_reachymini_asoundrc() + fallback write_asoundrc_to_home()
# du SDK Pollen. Ce fallback écrit un template canonical "hw:0,0" dans ~/.asoundrc,
# ce qui entre en conflit avec notre /etc/asound.conf USB et casse le playback
# (ALSA "Invalid argument" sur le 2ème bloc pcm redéfini).
# Maintenant : on ne touche pas ~/.asoundrc. /etc/asound.conf est la source de vérité.
# On vérifie juste que aplay -L voit bien l'alias reachymini_audio_sink.
if aplay -L 2>/dev/null | grep -q "reachymini_audio_sink"; then
    echo "[start_all] aplay -L : reachymini_audio_sink présent (via /etc/asound.conf USB)."
else
    echo "[start_all] ERREUR: reachymini_audio_sink absent de aplay -L — /etc/asound.conf mal installé ?"
fi

# ── 4f. Patch GStreamer camera: emit("try-pull-sample") au lieu de .try_pull_sample()
# Le SDK reachy_mini 1.5.1 utilise appsink.try_pull_sample() qui lève AttributeError
# dans le contexte GStreamer de conv_app_v2. Le même fichier utilise emit() dans open()
# qui fonctionne. Ce sed idempotent aligne _get_sample() sur le même pattern.
_CAM_GST="/venvs/apps_venv/lib/python3.12/site-packages/reachy_mini/media/camera_gstreamer.py"
if [ -f "$_CAM_GST" ] && grep -q 'appsink.try_pull_sample' "$_CAM_GST" 2>/dev/null; then
    sed -i 's/sample = appsink.try_pull_sample(20_000_000)/sample = appsink.emit("try-pull-sample", 20_000_000)  # reachy-care-fix/' "$_CAM_GST"
    find "$(dirname "$_CAM_GST")/__pycache__" -delete 2>/dev/null || true
    echo "[start_all] GStreamer camera fix appliqué (emit au lieu de try_pull_sample)"
else
    echo "[start_all] GStreamer camera fix déjà en place ou fichier absent"
fi

# ── 4g. Patches MediaPipe HeadTracker anti-hallucination
#
# Deux bugs cumulés qui faisaient partir la tête en l'air en contre-jour :
# (1) frame BGR envoyée à MediaPipe qui attend RGB → couleurs peau faussées,
#     visages fantômes sur zones lumineuses (plafond, fenêtre).
# (2) min_detection_confidence=0.05 trop permissif → MediaPipe détecte des "visages"
#     dans n'importe quelle zone de luminance, s'accroche en mode tracking sur fantôme.
#
# Fix 1 : frame[:, :, ::-1] avant get_head_position().
# Fix 2 : seuil 0.05 → 0.5 (standard MediaPipe), dans reachy_mini_toolbox.
# Testé terrain  : disparition des tête en l'air contre-jour.

# Fix 1 — BGR→RGB dans camera_worker (conv_app_v2 venv)
_CAM_WORKER="/venvs/mini_daemon/lib/python3.12/site-packages/reachy_mini_conversation_app/camera_worker.py"
if [ -f "$_CAM_WORKER" ] && grep -q 'get_head_position(frame)' "$_CAM_WORKER" 2>/dev/null; then
    sed -i 's|self\.head_tracker\.get_head_position(frame)|self.head_tracker.get_head_position(frame[:, :, ::-1])  # reachy-care-fix BGR→RGB|' "$_CAM_WORKER"
    find "$(dirname "$_CAM_WORKER")/__pycache__" -delete 2>/dev/null || true
    echo "[start_all] HeadTracker BGR→RGB fix appliqué (camera_worker venv mini_daemon)"
else
    echo "[start_all] HeadTracker BGR→RGB fix déjà en place ou fichier absent"
fi

# Fix 1bis — Clamp pitch face_tracking_offsets à max 8.6° vers le haut ()
# look_at_image retourne un pitch aberrant quand le sujet est trop proche →
# tête part au plafond systématiquement dès détection. Le clamp garantit que
# le pitch ne peut jamais monter au-delà de -0.15 rad, indépendant de tout.
# Ligne ciblée dans la liste face_tracking_offsets (rotation[1] seul sur une ligne).
if [ -f "$_CAM_WORKER" ] && grep -q '^[[:space:]]*rotation\[1\],$' "$_CAM_WORKER" 2>/dev/null; then
    sed -i '/^[[:space:]]*rotation\[1\],$/s|rotation\[1\],|max(rotation[1], 0.0),  # reachy-care-fix anti-plafond (asymétrique)|' "$_CAM_WORKER"
    # Idempotent : couvre aussi la version précédente du clamp (-0.15)
    sed -i 's|max(rotation\[1\], -0\.15)|max(rotation[1], 0.0)|g' "$_CAM_WORKER"
    find "$(dirname "$_CAM_WORKER")/__pycache__" -delete 2>/dev/null || true
    echo "[start_all] Pitch clamp anti-plafond appliqué (camera_worker venv mini_daemon)"
else
    echo "[start_all] Pitch clamp déjà en place ou fichier absent"
fi

# Fix 2 — min_detection_confidence 0.05 → 0.5 dans head_tracker (les 2 venv)
for _HT in \
    /venvs/mini_daemon/lib/python3.12/site-packages/reachy_mini_toolbox/vision/head_tracker.py \
    /venvs/apps_venv/lib/python3.12/site-packages/reachy_mini_toolbox/vision/head_tracker.py; do
    if [ -f "$_HT" ] && grep -q 'min_detection_confidence=0.05' "$_HT" 2>/dev/null; then
        sed -i 's/min_detection_confidence=0\.05,/min_detection_confidence=0.6,  # reachy-care-fix anti-hallu (compromis 0.6 + clamp pitch 0.0)/' "$_HT"
        # Idempotent : couvre aussi les valeurs intermédiaires des versions précédentes
        sed -i 's/min_detection_confidence=0\.5,/min_detection_confidence=0.6,/' "$_HT"
        sed -i 's/min_detection_confidence=0\.85,/min_detection_confidence=0.6,/' "$_HT"
        find "$(dirname "$_HT")/__pycache__" -delete 2>/dev/null || true
        echo "[start_all] HeadTracker confidence 0.05→0.5 appliqué ($_HT)"
    else
        echo "[start_all] HeadTracker confidence déjà en place ou fichier absent : $_HT"
    fi
done

# ── 5. Lancer conv_app_v2 + main.py via systemd (supervision + auto-restart) ──
# Depuis  les 3 services sont enable/start via systemd :
#   reachy-care-aec.service  : active AEC XMOS (oneshot, après daemon)
#   reachy-care-conv.service : conv_app_v2 (LLM voice, IPC :8766)
#   reachy-care-main.service : main.py (face reco + wake word + AttenLabs)
# Restart=always, RestartSec=5s : si un process meurt, systemd le relance auto.
# Dépendances : main dépend de conv dépend de aec dépend de reachy-mini-daemon.
# Voir docs/BACKLOG_CONV.md §1 et resources/systemd/
rm -rf /tmp/reachy_care_cmds
rm -f /tmp/reachy_care_fall_checkin.json
mkdir -p /tmp/reachy_care_cmds
echo "[start_all] Queue de commandes réinitialisée."

if [ "$CONV_APP_MODE" = "v2" ]; then
    echo "[start_all] Démarrage services systemd (reachy-care-main pull conv + aec)..."
    # restart plutôt que start : idempotent + recharge env si .env changé
    sudo systemctl restart reachy-care-main.service
    MAIN_PID=$(systemctl show -p MainPID reachy-care-main.service | cut -d= -f2)
    CONV_PID=$(systemctl show -p MainPID reachy-care-conv.service | cut -d= -f2)
    echo "[start_all] main.py PID=$MAIN_PID — log: journalctl -u reachy-care-main -f"
    echo "[start_all] conv_app_v2 PID=$CONV_PID — log: journalctl -u reachy-care-conv -f"
else
    # Mode pollen legacy (rollback) : garder nohup, systemd ne gère que v2
    echo "[start_all] Mode pollen : fallback nohup (pas de supervision systemd)..."
    /venvs/apps_venv/bin/reachy-mini-conversation-app >> "$LOG_DIR/conv_app.log" 2>&1 &
    echo $! > "$CONV_PID_FILE"
    sleep 9
    nohup "$VENV" "$CARE_DIR/main.py" "$@" >> "$LOG_DIR/reachy_care.log" 2>&1 &
    MAIN_PID=$!
    disown $MAIN_PID 2>/dev/null || true
    echo "[start_all] main.py PID=$MAIN_PID (nohup, pas supervisé)"
fi

# Attendre jusqu'à 30s que main.py écrive son PID file (preuve de startup).
for i in $(seq 1 30); do
    if [ -f /tmp/reachy_care.pid ]; then
        READY_PID=$(cat /tmp/reachy_care.pid 2>/dev/null || echo "")
        if [ -n "$READY_PID" ] && kill -0 "$READY_PID" 2>/dev/null; then
            echo "[start_all] main.py ready (pid $READY_PID alive après ${i}s)"
            exit 0
        fi
    fi
    sleep 1
done

echo "[start_all] WARN: /tmp/reachy_care.pid non écrit après 30s — main.py lent ou crashé, voir $LOG_DIR/reachy_care.log"
# On exit 0 quand même : main.py peut encore remonter, et surtout on ne veut
# pas que le controller flag "error" tant que conv_app_v2 tourne.
exit 0
