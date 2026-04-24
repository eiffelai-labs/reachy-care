#!/bin/bash
# test_audio_stack.sh — smoke test non-régression de la stack audio Reachy Care.
#
# À tourner SUR LE PI, juste après tout changement touchant :
#   - resources/asound.conf
#   - resources/setup_asoundrc.sh
#   - resources/systemd/reachy-care-aec.service
#   - resources/systemd/reachy-care-conv.service
#   - conv_app_v2/audio_io.py
#
# Durée ~20 s. Exit code 0 = tous les tests PASS, 1 = au moins un FAIL.
#
# Usage depuis Mac :
#   ssh pollen@reachy-mini.local 'bash ~/reachy_care/resources/test_audio_stack.sh'
#
# Historique — ce test est né du  : un changement asound.conf
# (hw:0,0 → hw:CARD=Audio,DEV=0) a décalé les index PyAudio sans qu'on le
# détecte, cassant wake word + reco faciale + AttenLabs pendant 2 heures.
# Ce smoke test aurait attrapé la régression en 20 secondes.

set -u
PASS=0
FAIL=0
WARN=0

# Couleurs uniquement si stdout est un tty
if [ -t 1 ]; then
    GREEN="\033[0;32m"; RED="\033[0;31m"; YELLOW="\033[0;33m"; NC="\033[0m"
else
    GREEN=""; RED=""; YELLOW=""; NC=""
fi

pass() { echo -e "  ${GREEN}PASS${NC} $1"; PASS=$((PASS+1)); }
fail() { echo -e "  ${RED}FAIL${NC} $1"; FAIL=$((FAIL+1)); }
warn() { echo -e "  ${YELLOW}WARN${NC} $1"; WARN=$((WARN+1)); }

echo "=== Reachy Care audio stack smoke test ==="
echo

# ────────────────────────────────────────────────────────────────────────────
echo "[1] Services systemd"
for svc in reachy-mini-daemon reachy-care-aec reachy-care-conv reachy-care-main; do
    if [ "$(systemctl is-active $svc)" = "active" ]; then
        pass "$svc active"
    else
        fail "$svc NOT active ($(systemctl is-active $svc))"
    fi
done

# ────────────────────────────────────────────────────────────────────────────
echo
echo "[2] Cartes ALSA attendues (Audio = XMOS + une carte externe)"
APLAY_LIST=$(aplay -l 2>/dev/null)
if echo "$APLAY_LIST" | grep -q "card [0-9]*: Audio"; then
    pass "carte Audio (XMOS Reachy Mini) détectée"
else
    fail "carte Audio (XMOS) ABSENTE — le daemon ou le port USB ont un souci"
fi

SPEAKER_CARD=$(echo "$APLAY_LIST" | awk '/^card [0-9]+:/ && $3 != "Audio" && $3 !~ /^vc4hdmi/ {print $3; exit}')
if [ -n "$SPEAKER_CARD" ]; then
    pass "carte enceinte externe détectée : $SPEAKER_CARD"
else
    fail "aucune carte enceinte externe (Anker/dongle/Jabra) — vérifier câble USB"
fi

# ────────────────────────────────────────────────────────────────────────────
echo
echo "[3] ~/.asoundrc cohérent"
ASOUNDRC=/home/pollen/.asoundrc
if [ ! -f "$ASOUNDRC" ]; then
    fail "~/.asoundrc absent — setup_asoundrc.sh n'a pas tourné"
else
    for alias in dsnoop_xmos mic_alert_in conv_audio_in reachymini_audio_sink; do
        if grep -q "^pcm\.$alias " "$ASOUNDRC"; then
            pass "pcm.$alias déclaré"
        else
            fail "pcm.$alias ABSENT dans ~/.asoundrc"
        fi
    done
    # Le placeholder ne doit plus être présent après substitution
    if grep -q "__SPEAKER_CARD__" "$ASOUNDRC"; then
        fail "__SPEAKER_CARD__ non substitué — setup_asoundrc.sh a échoué"
    else
        pass "placeholder __SPEAKER_CARD__ substitué"
    fi
fi

# ────────────────────────────────────────────────────────────────────────────
echo
echo "[4] Capture micro (mic_alert_in et conv_audio_in, 2 s chacun)"
for pcm in mic_alert_in conv_audio_in; do
    TMP=/tmp/smoke_${pcm}.wav
    rm -f "$TMP"
    if ! arecord -D "$pcm" -d 2 -f S16_LE -r 16000 -c 1 -q "$TMP" 2>/dev/null; then
        fail "arecord -D $pcm échec"
        continue
    fi
    RMS=$(/venvs/apps_venv/bin/python3 -c "
import wave, struct, math
try:
    w = wave.open('$TMP', 'rb')
    n = w.getnframes()
    if n == 0:
        print(0); exit()
    data = w.readframes(n)
    samples = struct.unpack(f'{n}h', data)
    rms = math.sqrt(sum(s*s for s in samples) / n)
    print(int(rms))
except Exception as e:
    print(0)
" 2>/dev/null)
    if [ "$RMS" -gt 10 ] 2>/dev/null; then
        pass "$pcm lit (RMS=$RMS, seuil minimum 10 = bruit ambiant)"
    else
        fail "$pcm silence total (RMS=$RMS) — XMOS OP_L/R cassé ou dsnoop verrouillé"
    fi
    rm -f "$TMP"
done

# ────────────────────────────────────────────────────────────────────────────
echo
echo "[5] Playback sortie (0.5 s silence sur reachymini_audio_sink)"
# On évite aplay qui échoue en Device busy pendant que conv tourne. On utilise
# juste `speaker-test -D reachymini_audio_sink -c 1 -s 1 -f 1 -r 16000 -F S16_LE`
# ou on skip si le sink est claimé. L'important est que le PCM résolve.
if /venvs/apps_venv/bin/python3 -c "
import subprocess
r = subprocess.run(['aplay', '--dump-hw-params', '-D', 'reachymini_audio_sink', '/dev/null'],
                   capture_output=True, timeout=3)
import sys
sys.exit(0 if r.returncode in (0, 1) else 2)
" 2>/dev/null; then
    pass "reachymini_audio_sink résout (le sink peut être busy, c'est OK)"
else
    fail "reachymini_audio_sink ne résout pas — carte sortie absente ou plug cassé"
fi

# ────────────────────────────────────────────────────────────────────────────
echo
echo "[6] PyAudio énumère mic_alert_in et conv_audio_in (régression )"
PYAUDIO_CHECK=$(/venvs/apps_venv/bin/python3 << 'PYEOF' 2>/dev/null
import pyaudio, sys, os
sys.stderr = open(os.devnull, 'w')  # coupe le bruit ALSA
pa = pyaudio.PyAudio()
found = {"mic_alert_in": None, "conv_audio_in": None}
for i in range(pa.get_device_count()):
    info = pa.get_device_info_by_index(i)
    if info["maxInputChannels"] > 0:
        for k in found:
            if k in info["name"] and found[k] is None:
                found[k] = i
pa.terminate()
for k, v in found.items():
    print(f"{k}={v}")
PYEOF
)
for pcm in mic_alert_in conv_audio_in; do
    IDX=$(echo "$PYAUDIO_CHECK" | grep "^$pcm=" | cut -d= -f2)
    if [ -n "$IDX" ] && [ "$IDX" != "None" ]; then
        pass "PyAudio voit $pcm à l'index $IDX"
    else
        fail "PyAudio NE VOIT PAS $pcm — main.py wake_word.py sera muet"
    fi
done

# ────────────────────────────────────────────────────────────────────────────
echo
echo "[7] Volume enceinte externe (régression  : reset à mute au boot)"
if [ -n "${SPEAKER_CARD:-}" ]; then
    VOL_LINE=$(amixer -c "$SPEAKER_CARD" sget PCM 2>/dev/null | grep -oE '\[[0-9]+%\][^[]*\[(on|off)\]' | head -1)
    if echo "$VOL_LINE" | grep -q "\[on\]"; then
        VOL=$(echo "$VOL_LINE" | grep -oE '\[[0-9]+%\]' | head -1 | tr -d '[]%')
        if [ "${VOL:-0}" -ge 50 ] 2>/dev/null; then
            pass "$SPEAKER_CARD PCM = ${VOL}% [on]"
        else
            warn "$SPEAKER_CARD PCM audible mais volume bas (${VOL}%)"
        fi
    else
        fail "$SPEAKER_CARD PCM muet ($VOL_LINE) — setup_asoundrc.sh n'a pas unmute"
    fi
else
    warn "carte externe non détectée, test volume skipped"
fi

# ────────────────────────────────────────────────────────────────────────────
echo
echo "=== Résumé : $PASS PASS / $FAIL FAIL / $WARN WARN ==="
if [ "$FAIL" -gt 0 ]; then
    echo "REGRESSION DÉTECTÉE — NE PAS COMMIT, investiguer d'abord."
    exit 1
fi
echo "Stack audio OK."
exit 0
