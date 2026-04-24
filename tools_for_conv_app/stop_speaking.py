"""
stop_speaking.py — Coupe immédiatement l'audio de Reachy et active le mode silencieux.

Le LLM appelle cet outil dès que l'utilisateur demande de se taire
("tais-toi", "silence", "chut", "stop", "arrête de parler"…).

L'outil :
1. Flush la queue audio GStreamer (coupe la parole en cours)
2. Écrit {"cmd": "mute"} dans /tmp/reachy_care_cmds/ → main.py mute le bridge
3. Le mode silencieux persiste jusqu'au wake word "Hey Reachy"
"""

import contextlib
import json
import logging
import os
import sys
import tempfile
import time
import urllib.request
from typing import Any

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies

logger = logging.getLogger(__name__)

_CMD_DIR = "/tmp/reachy_care_cmds"


def _safe_write_cmd(cmd: dict) -> None:
    """Écriture atomique dans la queue répertoire : tmp → os.replace() → <timestamp_ns>.json."""
    os.makedirs(_CMD_DIR, exist_ok=True)
    dest = os.path.join(_CMD_DIR, f"{time.time_ns()}.json")
    with contextlib.suppress(Exception):
        with tempfile.NamedTemporaryFile("w", dir=_CMD_DIR, delete=False, suffix=".tmp") as f:
            json.dump(cmd, f)
            tmp_path = f.name
        os.replace(tmp_path, dest)


def _get_handler():
    for mod in sys.modules.values():
        handler = getattr(mod, "_reachy_care_handler", None)
        if handler is not None:
            return handler
    return None


class StopSpeaking(Tool):
    """Coupe immédiatement la parole de Reachy."""

    name = "stop_speaking"
    description = (
        "Coupe la parole de Reachy. "
        "UNIQUEMENT si la personne dit EXPLICITEMENT et CLAIREMENT un de ces mots : "
        "'tais-toi', 'silence', 'chut', 'ferme-la', 'arrête de parler', 'stop'. "
        "NE PAS appeler si tu entends du bruit, des mots incomplets, ou du son ambiant. "
        "NE PAS appeler si la personne parle à quelqu'un d'autre. "
        "NE PAS appeler pendant une partie d'échecs. "
        "En cas de doute : NE PAS appeler."
    )
    parameters_schema = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        handler = _get_handler()

        # 1. Couper l'audio en cours
        if handler is not None and callable(getattr(handler, "_clear_queue", None)):
            try:
                handler._clear_queue()
                logger.info("stop_speaking: audio coupé.")
            except Exception as exc:
                logger.warning("stop_speaking: _clear_queue échoué : %s", exc)
        else:
            logger.warning("stop_speaking: handler ou _clear_queue indisponible.")

        # 2. Signaler à main.py de muter le bridge (stoppe tous les futurs événements LLM)
        try:
            _safe_write_cmd({"cmd": "mute"})
            logger.info("stop_speaking: commande mute envoyée à main.py.")
        except Exception as exc:
            logger.warning("stop_speaking: écriture cmd mute échouée : %s", exc)

        # 3. Couper le VAD immédiatement (privacy — micro quasi sourd)
        #    Sans ça, le LLM continue d'entendre et de répondre pendant les ~3s
        #    avant que main.py traite la commande mute.
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:8766/sleep",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=1)
            logger.info("stop_speaking: VAD → 0.99 (silence immédiat).")
        except Exception:
            pass

        # 4. Annuler la réponse OpenAI Realtime en cours (coupe le stream côté serveur)
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:8766/cancel",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=1)
            logger.info("stop_speaking: réponse Realtime annulée via /cancel.")
        except Exception:
            pass

        return {
            "status": "ok",
            "instruction": (
                "SILENCE ABSOLU PERMANENT. Tu ne produis AUCUNE réponse vocale. "
                "Tu n'écoutes pas, tu n'analyses pas, tu ne commentes pas. "
                "Tu attends que l'utilisateur dise 'Hey Reachy' pour reprendre. "
                "Même si tu entends quelque chose, tu restes MUET."
            ),
        }
