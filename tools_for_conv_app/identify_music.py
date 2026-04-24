"""
identify_music.py — Reconnaissance musicale en temps réel.

Capture 8s de micro et interroge audd.io pour identifier le morceau en cours.
Retourne titre, artiste et paroles pour permettre à Reachy d'écouter vraiment.

Clé API : s'inscrire sur audd.io (500 identifications/mois gratuites).
Configurer AUDD_API_TOKEN dans config_local.py sur le Pi.
"""

import asyncio
import io
import logging
import wave
from typing import Any

import requests

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies

logger = logging.getLogger(__name__)

_AUDD_URL       = "https://api.audd.io/"
_SAMPLE_RATE    = 16000
_CHANNELS       = 1
_RECORD_SECONDS = 8
_INPUT_DEVICE   = None   # None = défaut ALSA (reachymini_audio_src via dsnoop)

try:
    import sys
    sys.path.insert(0, "/home/pollen/reachy_care")
    import config as _cfg
    _AUDD_TOKEN = getattr(_cfg, "AUDD_API_TOKEN", "test")
except Exception:
    _AUDD_TOKEN = "test"


class IdentifyMusic(Tool):
    """Écoute la musique ambiante et identifie le morceau en cours."""

    name = "identify_music"
    description = (
        "Écoute la musique ambiante pendant 8 secondes et identifie le morceau. "
        "Retourne le titre, l'artiste, le genre et un extrait des paroles si disponible. "
        "À utiliser dès que de la musique joue dans la pièce et que tu veux savoir ce que c'est. "
        "Appelle aussi régulièrement pendant le MODE_MUSIQUE pour suivre les changements de morceaux."
    )
    parameters_schema = {
        "type": "object",
        "properties": {},
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:

        def _record_and_identify() -> dict[str, Any]:
            try:
                import pyaudio
            except ImportError:
                return {"error": "pyaudio non disponible."}

            pa = pyaudio.PyAudio()
            stream = None
            frames = []

            try:
                stream = pa.open(
                    format=pyaudio.paInt16,
                    channels=_CHANNELS,
                    rate=_SAMPLE_RATE,
                    input=True,
                    input_device_index=_INPUT_DEVICE,
                    frames_per_buffer=1024,
                )
                n_frames = int(_SAMPLE_RATE / 1024 * _RECORD_SECONDS)
                for _ in range(n_frames):
                    frames.append(stream.read(1024, exception_on_overflow=False))
            except Exception as exc:
                return {"error": f"Erreur capture audio : {exc}"}
            finally:
                if stream:
                    stream.stop_stream()
                    stream.close()
                pa.terminate()

            # WAV en mémoire
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(_CHANNELS)
                wf.setsampwidth(2)
                wf.setframerate(_SAMPLE_RATE)
                wf.writeframes(b"".join(frames))

            # Envoi à audd.io
            try:
                resp = requests.post(
                    _AUDD_URL,
                    data={"api_token": _AUDD_TOKEN, "return": "lyrics,spotify"},
                    files={"file": ("audio.wav", buf.getvalue(), "audio/wav")},
                    timeout=20,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                return {"error": f"Erreur API audd.io : {exc}"}

            if data.get("status") != "success" or not data.get("result"):
                return {
                    "identified": False,
                    "message": "Morceau non reconnu — continue d'écouter, rappelle identify_music dans 15s.",
                }

            r = data["result"]
            out: dict[str, Any] = {
                "identified": True,
                "title":  r.get("title", ""),
                "artist": r.get("artist", ""),
                "album":  r.get("album", ""),
            }

            # Paroles (extrait court — pour chanter avec)
            lyrics_data = r.get("lyrics") or {}
            if isinstance(lyrics_data, dict):
                full = lyrics_data.get("lyrics", "")
            elif isinstance(lyrics_data, str):
                full = lyrics_data
            else:
                full = ""
            if full:
                out["lyrics_excerpt"] = full[:400]

            # Spotify (tempo/énergie si dispo)
            spotify = r.get("spotify") or {}
            if isinstance(spotify, dict):
                af = spotify.get("audio_features") or {}
                if isinstance(af, dict):
                    if af.get("tempo"):
                        out["bpm"] = round(af["tempo"])
                    if af.get("valence") is not None:
                        out["mood"] = "joyeux" if af["valence"] > 0.6 else ("mélancolique" if af["valence"] < 0.3 else "doux")
                    if af.get("energy") is not None:
                        out["energy"] = "énergique" if af["energy"] > 0.7 else ("calme" if af["energy"] < 0.3 else "modéré")

            logger.info("identify_music: '%s' — %s", out.get("title"), out.get("artist"))
            return out

        return await asyncio.get_running_loop().run_in_executor(None, _record_and_identify)
