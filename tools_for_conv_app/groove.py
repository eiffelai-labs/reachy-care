"""
groove.py — Mouvement rythmique synchronisé sur la musique.

Détecte le BPM depuis le micro (autocorrélation RMS) ou utilise le BPM
fourni par identify_music, puis fait bouger la tête et les antennes de
Reachy sur le rythme, en continu, jusqu'à l'appel avec stop=True.
"""

import logging
import threading
import time
from typing import Any

import numpy as np

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies

logger = logging.getLogger(__name__)

_SAMPLE_RATE  = 16000
_DETECT_SECS  = 4        # secondes d'écoute pour la détection BPM

# Thread global (un seul groove actif)
_groove_stop  = threading.Event()
_groove_thread: threading.Thread | None = None


def _detect_bpm_from_audio(frames: list[bytes]) -> int:
    """Détecte le BPM par autocorrélation de l'enveloppe RMS."""
    audio = np.frombuffer(b"".join(frames), dtype=np.int16).astype(np.float32) / 32768.0
    hop = _SAMPLE_RATE // 100          # 10ms par frame
    env = np.array([
        np.sqrt(np.mean(audio[i: i + hop] ** 2))
        for i in range(0, len(audio) - hop, hop)
    ])
    env -= env.mean()
    corr = np.correlate(env, env, "full")[len(env) - 1:]
    # 60 BPM = 1 beat/s = 100 frames @ 10ms | 200 BPM = 0.3s = 30 frames
    lo, hi = 30, 100
    if len(corr) > hi:
        peak = int(np.argmax(corr[lo: hi + 1])) + lo
        bpm = round(60.0 / (peak * 0.01))
        return max(60, min(200, bpm))
    return 120


def _groove_loop(
    deps: ToolDependencies,
    bpm: int,
    style: str,
    stop_event: threading.Event,
) -> None:
    """Boucle de mouvement rythmique — tourne dans un thread daemon."""
    try:
        from reachy_mini.utils import create_head_pose
        from reachy_mini_conversation_app.dance_emotion_moves import GotoQueueMove
    except ImportError as e:
        logger.warning("groove: import échoué : %s", e)
        return

    beat = 60.0 / bpm          # durée d'un beat en secondes
    move_dur = beat * 0.85     # légèrement sous le beat pour fluidité

    # Amplitudes selon l'énergie du morceau
    amp = {"doux": 0.25, "modere": 0.45, "energique": 0.70}.get(style, 0.45)
    yaw_max    = amp * 25   # degrés
    ant_high   =  amp
    ant_low    = -amp * 0.5

    beat_idx = 0
    last_beat = time.monotonic()

    while not stop_event.is_set():
        # Alterner gauche / droite à chaque beat
        if beat_idx % 2 == 0:
            yaw              =  yaw_max
            ant_left, ant_right = ant_low, ant_high
        else:
            yaw              = -yaw_max
            ant_left, ant_right = ant_high, ant_low

        # Accentuer les temps forts (1 et 3 dans un )
        is_strong = (beat_idx % 2 == 0)
        pitch = -5 if is_strong else 0   # légère inclinaison vers l'avant sur les temps forts

        try:
            target = create_head_pose(yaw=yaw, pitch=pitch, degrees=True)
            move = GotoQueueMove(
                target_head_pose=target,
                target_antennas=(ant_left, ant_right),
                duration=move_dur,
            )
            deps.movement_manager.queue_move(move)
        except Exception as exc:
            logger.debug("groove: queue_move échoué : %s", exc)

        beat_idx += 1

        # Attendre le prochain beat (précis)
        next_beat = last_beat + beat
        wait = next_beat - time.monotonic()
        if wait > 0:
            stop_event.wait(wait)
        last_beat = next_beat

    # Retour position neutre
    try:
        neutral = create_head_pose(yaw=0, pitch=0, degrees=True)
        deps.movement_manager.queue_move(
            GotoQueueMove(target_head_pose=neutral, target_antennas=(0, 0), duration=0.5)
        )
    except Exception:
        pass

    logger.info("groove: arrêté (BPM=%d)", bpm)


class Groove(Tool):
    """Fait bouger Reachy sur le rythme de la musique ambiante."""

    name = "groove"
    description = (
        "Démarre un mouvement rythmique (tête + antennes) synchronisé sur le BPM de la musique. "
        "Reachy se balance sur le rythme en continu. "
        "Appelle avec stop=True pour arrêter. "
        "Utilise le BPM de identify_music si disponible, sinon le détecte automatiquement."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "bpm": {
                "type": "integer",
                "description": "BPM du morceau (issu de identify_music). Optionnel — détecté automatiquement si absent.",
            },
            "style": {
                "type": "string",
                "enum": ["doux", "modere", "energique"],
                "description": "Intensité du mouvement. 'doux' pour ballade, 'energique' pour rythme rapide.",
            },
            "stop": {
                "type": "boolean",
                "description": "Si True, arrête le groove en cours.",
            },
        },
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        global _groove_thread, _groove_stop

        stop = bool(kwargs.get("stop", False))

        # Arrêter le groove existant si demandé ou pour repartir proprement
        if _groove_thread is not None and _groove_thread.is_alive():
            _groove_stop.set()
            _groove_thread.join(timeout=3)
            _groove_thread = None
            _groove_stop.clear()
            if stop:
                return {"status": "stopped"}

        if stop:
            return {"status": "already_stopped"}

        bpm: int = int(kwargs.get("bpm") or 0)
        style: str = str(kwargs.get("style") or "modere")

        # Détecter le BPM depuis le micro si non fourni
        if not bpm:
            import asyncio
            import pyaudio

            def _record() -> list[bytes]:
                pa = pyaudio.PyAudio()
                frames = []
                stream = None
                try:
                    stream = pa.open(
                        format=pyaudio.paInt16,
                        channels=1,
                        rate=_SAMPLE_RATE,
                        input=True,
                        frames_per_buffer=1024,
                    )
                    for _ in range(int(_SAMPLE_RATE / 1024 * _DETECT_SECS)):
                        frames.append(stream.read(1024, exception_on_overflow=False))
                finally:
                    if stream:
                        stream.stop_stream()
                        stream.close()
                    pa.terminate()
                return frames

            try:
                frames = await asyncio.get_running_loop().run_in_executor(None, _record)
                bpm = _detect_bpm_from_audio(frames)
                logger.info("groove: BPM détecté = %d", bpm)
            except Exception as exc:
                bpm = 120
                logger.warning("groove: détection BPM échouée (%s) → 120 BPM par défaut", exc)

        # Lancer le thread de groove
        _groove_stop.clear()
        _groove_thread = threading.Thread(
            target=_groove_loop,
            args=(deps, bpm, style, _groove_stop),
            daemon=True,
            name="groove-thread",
        )
        _groove_thread.start()

        return {
            "status": "grooving",
            "bpm": bpm,
            "style": style,
            "beat_interval_sec": round(60.0 / bpm, 2),
        }
