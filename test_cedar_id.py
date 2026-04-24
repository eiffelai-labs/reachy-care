#!/usr/bin/env python3
"""
test_cedar_id.py — Vérifie que la voix "cedar" est bien identifiée après enrollment.
Génère 2s d'audio via OpenAI Realtime, puis teste SpeakerIdentifier.identify_from_array().
Score attendu > 0.55.
"""

import asyncio
import base64
import json
import logging
import os
import sys

# PI-ONLY : chemins /home/pollen/reachy_care hardcodés.
if not os.path.isdir("/home/pollen/reachy_care"):
    sys.stderr.write("test_cedar_id.py est PI-ONLY (necessite /home/pollen/reachy_care).\n")
    sys.exit(2)

import numpy as np
import websockets
from scipy.signal import resample_poly

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
WS_URL = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview"
KNOWN_FACES_DIR = "/home/pollen/reachy_care/known_faces"
REPO_DIR = "/home/pollen/reachy_care"

TEST_PHRASE = "Bonjour, je suis Julie votre assistante. Comment puis-je vous aider  ?"


async def get_cedar_audio(phrase: str) -> np.ndarray:
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }
    async with websockets.connect(WS_URL, additional_headers=headers) as ws:
        await asyncio.wait_for(ws.recv(), timeout=15)  # welcome

        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "voice": "cedar",
                "modalities": ["audio", "text"],
                "output_audio_format": "pcm16",
                "turn_detection": None,
            }
        }))
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=15)
            ev = json.loads(msg)
            if ev.get("type") == "session.updated":
                break

        await ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": phrase}]
            }
        }))
        await ws.send(json.dumps({
            "type": "response.create",
            "response": {"modalities": ["audio", "text"]}
        }))

        pcm_chunks = []
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=30)
            event = json.loads(msg)
            etype = event.get("type", "")
            if etype == "response.audio.delta":
                delta_b64 = event.get("delta", "")
                if delta_b64:
                    pcm_chunks.append(base64.b64decode(delta_b64))
            elif etype == "response.done":
                break

    raw = b"".join(pcm_chunks)
    audio_int16 = np.frombuffer(raw, dtype=np.int16)
    audio_float32 = audio_int16.astype(np.float32) / 32768.0
    # Rééchantillonner 24kHz → 16kHz
    audio_16k = resample_poly(audio_float32, 2, 3).astype(np.float32)
    return audio_16k  # retourne le audio complet


def main():
    if not OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY non définie.")
        sys.exit(1)

    cedar_npy = os.path.join(KNOWN_FACES_DIR, "cedar_voice.npy")
    if not os.path.exists(cedar_npy):
        logger.error("cedar_voice.npy introuvable dans %s — enrôler d'abord.", KNOWN_FACES_DIR)
        sys.exit(1)

    logger.info("Génération d'un échantillon test cedar...")
    audio_16k = asyncio.run(get_cedar_audio(TEST_PHRASE))
    logger.info("Audio test : %d samples (%.2fs à 16kHz)", len(audio_16k), len(audio_16k) / 16000)

    sys.path.insert(0, REPO_DIR)
    from modules.speaker_id import SpeakerIdentifier

    identifier = SpeakerIdentifier(KNOWN_FACES_DIR, threshold=0.55)
    # Test sur les 2 premières secondes pour référence
    audio = audio_16k[:32000] if len(audio_16k) >= 32000 else audio_16k
    name, score = identifier.identify_from_array(audio)

    logger.info("Résultat identification sur 2s : name=%s, score=%.4f", name, score)

    # Tester aussi tous les segments de 2s et prendre le meilleur score cedar
    best_name, best_score = name, score
    n = len(audio_16k)
    for start in range(0, n - 32000 + 1, 32000):
        seg = audio_16k[start:start + 32000]
        rms = float(np.sqrt(np.mean(seg ** 2)))
        if rms < 0.015:
            continue
        seg_name, seg_score = identifier.identify_from_array(seg)
        if seg_score > best_score:
            best_name, best_score = seg_name, seg_score
        logger.info("  Segment [%ds-%ds] : name=%s, score=%.4f", start // 16000, (start + 32000) // 16000, seg_name, seg_score)

    logger.info("Meilleur score : name=%s, score=%.4f", best_name, best_score)

    if best_name == "cedar" and best_score > 0.55:
        logger.info("TEST PASS — cedar identifié avec score %.4f > 0.55", best_score)
        sys.exit(0)
    else:
        logger.error("TEST FAIL — name=%s, score=%.4f (attendu cedar > 0.55)", best_name, best_score)
        sys.exit(1)


if __name__ == "__main__":
    main()
