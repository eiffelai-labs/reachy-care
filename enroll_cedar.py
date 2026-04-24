#!/usr/bin/env python3
"""
enroll_cedar.py — Enrôle la voix TTS cedar (OpenAI) via le mic XMOS.

Lancer pendant que Reachy est en boucle de parole (l'écho capture cedar).
Enregistre N secondes depuis dsnoop_xmos, coupe en segments de 2s,
filtre les silences, enrôle comme "cedar" dans WeSpeaker.

Usage :
    /venvs/apps_venv/bin/python /home/pollen/reachy_care/enroll_cedar.py
    /venvs/apps_venv/bin/python /home/pollen/reachy_care/enroll_cedar.py --duration 60
"""

import argparse
import os
import sys
import time

# PI-ONLY : ce script utilise dsnoop_xmos et les chemins du Pi.
if not os.path.isdir("/home/pollen/reachy_care"):
    sys.stderr.write("enroll_cedar.py est PI-ONLY (necessite /home/pollen/reachy_care).\n")
    sys.exit(2)

import numpy as np

sys.path.insert(0, "/home/pollen/reachy_care")

SAMPLE_RATE = 16000
SEGMENT_SEC = 2
RMS_MIN = 0.015  # seuil silence (un peu plus bas que speaker_id pour l'écho BT)
DEVICE_NAME = "dsnoop_xmos"  # device ALSA XMOS (index 9 en général)


def record_audio(duration_s: int) -> np.ndarray:
    """Enregistre depuis dsnoop_xmos. Retourne float32 array [-1, 1]."""
    try:
        import sounddevice as sd
        # Chercher l'index du device dsnoop_xmos
        devices = sd.query_devices()
        dev_idx = None
        for i, d in enumerate(devices):
            if DEVICE_NAME in d["name"] and d["max_input_channels"] > 0:
                dev_idx = i
                break
        if dev_idx is None:
            print(f"[enroll_cedar] Device '{DEVICE_NAME}' non trouvé — devices disponibles :")
            for i, d in enumerate(devices):
                if d["max_input_channels"] > 0:
                    print(f"  [{i}] {d['name']}")
            dev_idx = None  # utilise le défaut

        print(f"[enroll_cedar] Enregistrement {duration_s}s depuis device={dev_idx or 'défaut'}...")
        audio = sd.rec(
            int(duration_s * SAMPLE_RATE),
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            device=dev_idx,
        )
        for i in range(duration_s):
            time.sleep(1)
            remaining = duration_s - i - 1
            if remaining % 5 == 0 or remaining <= 3:
                print(f"  {remaining}s restantes...")
        sd.wait()
        return audio.flatten()

    except ImportError:
        print("[enroll_cedar] sounddevice non dispo — utilisation de pyaudio...")
        return _record_pyaudio(duration_s)


def _record_pyaudio(duration_s: int) -> np.ndarray:
    import pyaudio
    import wave
    import tempfile
    import subprocess

    # Fallback : arecord via subprocess
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    cmd = [
        "arecord",
        "-D", "dsnoop_xmos",
        "-f", "S16_LE",
        "-r", str(SAMPLE_RATE),
        "-c", "1",
        "-d", str(duration_s),
        tmp.name,
    ]
    print(f"[enroll_cedar] Commande : {' '.join(cmd)}")
    subprocess.run(cmd, check=True)
    with wave.open(tmp.name, "rb") as wf:
        frames = wf.readframes(wf.getnframes())
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    import os
    os.unlink(tmp.name)
    return audio


def split_segments(audio: np.ndarray, seg_sec: int = SEGMENT_SEC) -> list[np.ndarray]:
    """Découpe en segments de seg_sec secondes, filtre les silences."""
    seg_len = seg_sec * SAMPLE_RATE
    segments = []
    for start in range(0, len(audio) - seg_len, seg_len):
        seg = audio[start:start + seg_len]
        rms = float(np.sqrt(np.mean(seg ** 2)))
        if rms >= RMS_MIN:
            segments.append(seg)
            print(f"  segment {len(segments)} : RMS={rms:.3f} ✓")
        else:
            print(f"  segment — : RMS={rms:.3f} (silence, ignoré)")
    return segments


def main():
    parser = argparse.ArgumentParser(description="Enrôle la voix cedar depuis mic XMOS")
    parser.add_argument("--duration", type=int, default=40,
                        help="Durée d'enregistrement en secondes (défaut: 40)")
    parser.add_argument("--name", type=str, default="cedar",
                        help="Nom du locuteur à enrôler (défaut: cedar)")
    args = parser.parse_args()

    print("=" * 60)
    print(f"  enroll_cedar.py — enrollment '{args.name}' ({args.duration}s)")
    print("=" * 60)
    print("  Lance Reachy en boucle de parole MAINTENANT, puis appuie Entrée.")
    input()

    audio = record_audio(args.duration)
    print(f"\n[enroll_cedar] {len(audio)/SAMPLE_RATE:.1f}s enregistrés. Découpage en segments...")

    segments = split_segments(audio)
    print(f"\n[enroll_cedar] {len(segments)} segments valides sur {len(audio)//SAMPLE_RATE//SEGMENT_SEC} total.")

    if len(segments) < 3:
        print("[enroll_cedar] Moins de 3 segments — enrollment annulé. Relance avec --duration plus long.")
        sys.exit(1)

    from modules.speaker_id import SpeakerIdentifier
    import config
    sid = SpeakerIdentifier(str(config.KNOWN_FACES_DIR))
    ok = sid.enroll(args.name, segments)
    if ok:
        print(f"\n[enroll_cedar] ✅ '{args.name}' enrôlé ({len(segments)} segments).")
        print(f"  Fichier : {config.KNOWN_FACES_DIR}/{args.name}_voice.npy")
        print("\n  Prochaine étape : ajouter le gate speaker ID dans openai_realtime.py")
    else:
        print(f"\n[enroll_cedar] Enrollment échoué.")
        sys.exit(1)


if __name__ == "__main__":
    main()
