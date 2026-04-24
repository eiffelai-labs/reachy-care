#!/usr/bin/env python3
"""
generate_cedar_samples.py — Enrôle la voix "cedar" (OpenAI Realtime API) dans WeSpeaker.
Génère ~15 phrases françaises variées, collecte l'audio PCM16 24kHz, rééchantillonne à 16kHz,
découpe en segments de 2s, puis appelle SpeakerIdentifier.enroll("cedar", segments).
"""

import asyncio
import base64
import json
import logging
import os
import sys

# PI-ONLY : chemins /home/pollen/reachy_care hardcodés.
if not os.path.isdir("/home/pollen/reachy_care"):
    sys.stderr.write("generate_cedar_samples.py est PI-ONLY (necessite /home/pollen/reachy_care).\n")
    sys.exit(2)

import numpy as np
import websockets
from scipy.signal import resample_poly

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# --- Config ---
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
WS_URL = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview"
KNOWN_FACES_DIR = "/home/pollen/reachy_care/known_faces"
REPO_DIR = "/home/pollen/reachy_care"

SAMPLE_RATE_REMOTE = 24000  # PCM16 24kHz depuis l'API
SAMPLE_RATE_TARGET = 16000  # WeSpeaker attend du 16kHz
SEGMENT_SAMPLES = SAMPLE_RATE_TARGET * 2  # 2 secondes = 32000 samples
RMS_MIN = 0.015  # filtrer les silences

PHRASES_FR = [
    "Bonjour, je m'appelle Julie et je suis votre assistante robotique.",
    "Il fait beau , j'espère que vous vous portez bien.",
    "Voulez-vous que je vous lise les nouvelles du matin ?",
    "Je suis là pour vous aider dans vos activités quotidiennes.",
    "Avez-vous bien dormi cette nuit ? Comment vous sentez-vous ?",
    "Je peux vous rappeler de prendre vos médicaments si vous le souhaitez.",
    "La température idéale pour une bonne nuit de sommeil est autour de dix-huit degrés.",
    "Votre famille vous a envoyé un message, voulez-vous que je vous le lise ?",
    "Il est important de boire suffisamment d'eau tout au long de la journée.",
    "Je peux vous accompagner dans vos exercices de mobilité si vous en avez envie.",
    "Aujourd'hui c'est mercredi, et le déjeuner sera servi à midi et demi.",
    "N'hésitez pas à me demander si vous avez besoin de quoi que ce soit.",
    "Les fleurs dans le jardin sont magnifiques en ce moment de l'année.",
    "Je vous souhaite une excellente journée remplie de bonheur et de sérénité.",
    "Rappellez-vous que je suis toujours là pour vous, il suffit de m'appeler.",
]


async def collect_audio_for_phrase(ws, phrase: str) -> np.ndarray:
    """Envoie une phrase, collecte tous les deltas audio, retourne un array float32."""
    # Créer l'item de conversation
    await ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": phrase}]
        }
    }))
    # Déclencher la réponse audio
    await ws.send(json.dumps({
        "type": "response.create",
        "response": {
            "modalities": ["audio", "text"],
        }
    }))

    pcm_chunks = []
    while True:
        msg = await asyncio.wait_for(ws.recv(), timeout=30)
        event = json.loads(msg)
        etype = event.get("type", "")

        if etype == "response.audio.delta":
            delta_b64 = event.get("delta", "")
            if delta_b64:
                raw_bytes = base64.b64decode(delta_b64)
                pcm_chunks.append(raw_bytes)

        elif etype == "response.done":
            break

        elif etype == "error":
            logger.error("Erreur API : %s", event)
            break

    if not pcm_chunks:
        return np.array([], dtype=np.float32)

    raw = b"".join(pcm_chunks)
    audio_int16 = np.frombuffer(raw, dtype=np.int16)
    audio_float32 = audio_int16.astype(np.float32) / 32768.0
    return audio_float32


def resample_24k_to_16k(audio: np.ndarray) -> np.ndarray:
    """Rééchantillonne de 24kHz → 16kHz avec resample_poly(up=2, down=3)."""
    if len(audio) == 0:
        return audio
    return resample_poly(audio, 2, 3).astype(np.float32)


def split_into_segments(audio: np.ndarray) -> list[np.ndarray]:
    """Découpe en segments de 2s (32000 samples à 16kHz), filtre les silences."""
    segments = []
    n = len(audio)
    for start in range(0, n - SEGMENT_SAMPLES + 1, SEGMENT_SAMPLES):
        seg = audio[start:start + SEGMENT_SAMPLES]
        rms = float(np.sqrt(np.mean(seg ** 2)))
        if rms >= RMS_MIN:
            segments.append(seg)
        else:
            logger.debug("Segment silence ignoré (RMS=%.4f)", rms)
    return segments


async def main():
    if not OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY non définie. Lancer avec : OPENAI_API_KEY=sk-... python generate_cedar_samples.py")
        sys.exit(1)

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }

    logger.info("Connexion à l'API OpenAI Realtime...")
    all_segments = []

    async with websockets.connect(WS_URL, additional_headers=headers) as ws:
        # Attendre le message de bienvenue
        welcome = await asyncio.wait_for(ws.recv(), timeout=15)
        welcome_event = json.loads(welcome)
        logger.info("Connecté : type=%s", welcome_event.get("type"))

        # Configurer la session : voix cedar, audio seulement, pas de VAD
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "voice": "cedar",
                "modalities": ["audio", "text"],
                "output_audio_format": "pcm16",
                "turn_detection": None,
                "instructions": (
                    "Tu es une assistante robotique bienveillante nommée Julie. "
                    "Tu parles uniquement en français, avec une voix chaleureuse et claire."
                ),
            }
        }))

        # Attendre la confirmation session.updated
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=15)
            ev = json.loads(msg)
            if ev.get("type") == "session.updated":
                logger.info("Session configurée avec voix cedar.")
                break
            elif ev.get("type") == "error":
                logger.error("Erreur session.update : %s", ev)
                sys.exit(1)

        # Générer l'audio pour chaque phrase
        for i, phrase in enumerate(PHRASES_FR):
            logger.info("[%d/%d] Génération audio : %s", i + 1, len(PHRASES_FR), phrase[:60])
            try:
                audio_24k = await collect_audio_for_phrase(ws, phrase)
                if len(audio_24k) == 0:
                    logger.warning("Aucun audio reçu pour la phrase %d", i + 1)
                    continue

                audio_16k = resample_24k_to_16k(audio_24k)
                segments = split_into_segments(audio_16k)
                logger.info("  → %d segments valides (%.1fs audio à 16kHz)", len(segments), len(audio_16k) / SAMPLE_RATE_TARGET)
                all_segments.extend(segments)

            except asyncio.TimeoutError:
                logger.warning("Timeout phrase %d, passage à la suivante.", i + 1)
            except Exception as exc:
                logger.warning("Erreur phrase %d : %s", i + 1, exc)

    logger.info("Total segments collectés : %d", len(all_segments))

    if not all_segments:
        logger.error("Aucun segment audio valide collecté. Abandon.")
        sys.exit(1)

    # Enrôler dans WeSpeaker
    logger.info("Enrôlement dans WeSpeaker...")
    sys.path.insert(0, REPO_DIR)
    from modules.speaker_id import SpeakerIdentifier

    os.makedirs(KNOWN_FACES_DIR, exist_ok=True)
    identifier = SpeakerIdentifier(KNOWN_FACES_DIR)
    success = identifier.enroll("cedar", all_segments)

    if success:
        out_path = os.path.join(KNOWN_FACES_DIR, "cedar_voice.npy")
        if os.path.exists(out_path):
            size = os.path.getsize(out_path)
            logger.info("SUCCESS : %s créé (%d bytes)", out_path, size)
        else:
            logger.error("enroll() a retourné True mais le fichier n'existe pas ?!")
            sys.exit(1)
    else:
        logger.error("enroll() a échoué.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
