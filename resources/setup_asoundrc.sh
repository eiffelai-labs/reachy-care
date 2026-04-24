#!/bin/bash
# setup_asoundrc.sh — génère ~/.asoundrc en détectant la carte USB de sortie.
#
# Appelé par reachy-care-conv.service (ExecStartPre). Lit le template
# resources/asound.conf, détecte la première carte USB audio qui n'est ni le
# XMOS Reachy Mini (nom "Audio") ni HDMI (vc4hdmi0/1), et substitue le
# placeholder __SPEAKER_CARD__ par le nom détecté.
#
# Échoue volontairement si aucune carte externe détectée → le service ne
# démarre pas → on voit immédiatement le problème au lieu d'avoir du silence.
#
# Cartes supportées (connues) :
#   - S3     → Anker PowerConf S3 (cible Julie)
#   - Device → dongle USB-C DAC + jack (setup  → )
#   - tout autre USB-Audio non-XMOS, non-HDMI → pris automatiquement

set -euo pipefail

REPO_ROOT="${REACHY_CARE_PATH:-/home/pollen/reachy_care}"
TEMPLATE="$REPO_ROOT/resources/asound.conf"
TARGET="/home/pollen/.asoundrc"

if [ ! -f "$TEMPLATE" ]; then
    echo "ERROR: template not found at $TEMPLATE" >&2
    exit 1
fi

SPEAKER=$(aplay -l 2>/dev/null | awk '
    /^card [0-9]+:/ {
        name = $3
        if (name != "Audio" && name !~ /^vc4hdmi/) {
            print name
            exit
        }
    }
')

#  bugfix : si aucune carte externe détectée (Anker débranchée, dongle
# absent, etc.), on FALLBACK sur la carte XMOS interne "Audio" pour permettre
# à conv_app_v2 de démarrer quand même (pas de son audible mais pas de boot
# loop non plus). Avant ce fix, `exit 2` faisait fail ExecStartPre → systemd
# Restart=always → cascade via PartOf → daemon stress → boucle CPU 80%.
# L'absence de carte externe est loggée en warning, la dashboard peut la
# détecter et demander à l'opérateur de brancher l'enceinte.
if [ -z "${SPEAKER:-}" ]; then
    SPEAKER="Audio"
    logger -t reachy-care-audio "WARN: aucune carte USB externe détectée — fallback sur Audio (XMOS interne, 5W, inaudible)"
    echo "reachy-care-audio: WARN pas d'enceinte externe, fallback XMOS interne" >&2
fi

sed "s/__SPEAKER_CARD__/$SPEAKER/g" "$TEMPLATE" > "$TARGET"
logger -t reachy-care-audio "speaker card detected: $SPEAKER -> $TARGET"
echo "reachy-care-audio: speaker card = $SPEAKER" >&2

# Unmute + volume à 85 % sur la carte détectée. Certaines enceintes USB (notamment
# l'Anker PowerConf S3) arrivent PCM=0 % + [off] par défaut à chaque power-on —
# le SDK écrit le signal mais rien ne sort. On force l'état audible ici.
# ||true pour ne pas bloquer si la carte n'expose pas "PCM" (variable selon device).
if amixer -c "$SPEAKER" sset PCM 85% unmute >/dev/null 2>&1; then
    logger -t reachy-care-audio "$SPEAKER PCM set to 85% unmuted"
else
    logger -t reachy-care-audio "WARN: could not set PCM volume on $SPEAKER (mixer control name differs?)"
fi
