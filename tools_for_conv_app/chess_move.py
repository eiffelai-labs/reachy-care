"""
chess_move.py — Enregistre le coup du joueur et retourne la réponse de Stockfish.

Tool SYNCHRONE : envoie le coup à main.py, attend la réponse Stockfish,
retourne tout d'un bloc (coup humain + coup Reachy + phrase exacte à dire).
"""

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import time
from typing import Any

import sys
sys.path.insert(0, "/home/pollen/reachy_care")
from modules.chess_parser import san_to_french  # noqa: E402

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies

logger = logging.getLogger(__name__)

_CMD_DIR = "/tmp/reachy_care_cmds"
_RESPONSE_FILE = "/tmp/reachy_chess_response.json"
_POLL_INTERVAL = 0.3
_POLL_TIMEOUT = 8.0
_ANTI_SPAM_SEC = 10.0

# Anti-spam : empêcher le LLM d'appeler le tool en rafale
_last_call_time: float = 0.0


def _safe_write_cmd(cmd: dict) -> None:
    os.makedirs(_CMD_DIR, exist_ok=True)
    dest = os.path.join(_CMD_DIR, f"{time.time_ns()}.json")
    with contextlib.suppress(Exception):
        with tempfile.NamedTemporaryFile("w", dir=_CMD_DIR, delete=False, suffix=".tmp") as f:
            json.dump(cmd, f)
            tmp_path = f.name
        os.replace(tmp_path, dest)


class ChessMove(Tool):
    """Enregistre le coup du joueur et retourne la réponse Stockfish."""

    name = "chess_move"
    description = (
        "À appeler UNE SEULE FOIS après confirmation du joueur. "
        "Retourne le coup de Reachy. "
        "INTERDIT d'appeler ce tool deux fois de suite sans que le joueur ait parlé entre les deux. "
        "INTERDIT d'inventer un coup — utilise UNIQUEMENT le résultat de ce tool."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "move": {
                "type": "string",
                "description": "Coup du joueur en notation UCI (ex: 'e2e4') ou SAN (ex: 'e4').",
            },
        },
        "required": ["move"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        global _last_call_time
        move = (kwargs.get("move") or "").strip()
        if not move:
            return {"error": "Paramètre 'move' manquant."}

        # Anti-spam : rejeter si appelé trop vite
        now = time.monotonic()
        if now - _last_call_time < _ANTI_SPAM_SEC:
            elapsed = now - _last_call_time
            logger.warning("chess_move: anti-spam — appelé %.1fs après le dernier appel, rejeté.", elapsed)
            return {
                "status": "rejected",
                "instruction": (
                    "STOP. Tu viens déjà d'envoyer un coup. "
                    "C'est au JOUEUR de parler maintenant. "
                    "ATTENDS qu'il annonce son prochain coup. NE FAIS RIEN."
                ),
            }
        _last_call_time = now

        with contextlib.suppress(FileNotFoundError):
            os.unlink(_RESPONSE_FILE)

        _safe_write_cmd({"cmd": "chess_human_move", "move": move})
        logger.info("chess_move: coup '%s' envoyé, attente réponse...", move)

        start = time.monotonic()
        while time.monotonic() - start < _POLL_TIMEOUT:
            await asyncio.sleep(_POLL_INTERVAL)
            try:
                with open(_RESPONSE_FILE) as f:
                    response = json.load(f)
                logger.info("chess_move: réponse reçue en %.1fs", time.monotonic() - start)
                with contextlib.suppress(Exception):
                    os.unlink(_RESPONSE_FILE)
                return self._format_response(response)
            except (FileNotFoundError, json.JSONDecodeError):
                continue

        logger.warning("chess_move: timeout après %.1fs", _POLL_TIMEOUT)
        return {"error": "Timeout — Stockfish n'a pas répondu. Redemande son coup au joueur."}

    @staticmethod
    def _format_response(resp: dict) -> dict[str, Any]:
        status = resp.get("status", "error")

        if status == "ok":
            reachy_move = resp.get("reachy_move", "?")
            reachy_from = resp.get("reachy_from", "?")
            reachy_to = resp.get("reachy_to", "?")
            french = san_to_french(reachy_move)

            if resp.get("game_over"):
                return {
                    "status": "game_over",
                    "say_exactly": f"Partie terminée : {resp['game_over']}. On en refait une ?",
                    "instruction": "Dis EXACTEMENT la phrase dans 'say_exactly'. Rien d'autre.",
                }

            history_pgn = resp.get("history_pgn", "")
            check_suffix = " Échec !" if resp.get("is_check") else ""

            return {
                "status": "ok",
                "human_move": resp.get("human_move", "?"),
                "reachy_move": reachy_move,
                "game_history": history_pgn,
                "move_number": resp.get("move_number", "?"),
                "say_exactly": (
                    f"Mon coup : {french}. "
                    f"Déplace ma pièce de {reachy_from} vers {reachy_to}."
                    f"{check_suffix} "
                    "À toi."
                ),
                "instruction": (
                    "Dis EXACTEMENT la phrase dans 'say_exactly'. Mot pour mot. "
                    "Ensuite SILENCE TOTAL. C'est au joueur de parler. "
                    "N'appelle PAS chess_move à nouveau — attends que le joueur annonce son coup. "
                    f"RAPPEL — voici tous les coups joués dans cette partie : {history_pgn}"
                ),
            }

        if status == "illegal":
            return {
                "status": "illegal",
                "say_exactly": f"Ce coup est illégal : {resp.get('reason', 'interdit')}. Quel est ton coup ?",
                "instruction": "Dis EXACTEMENT la phrase dans 'say_exactly'. Attends le joueur.",
            }

        if status == "invalid":
            return {
                "status": "invalid",
                "say_exactly": "Je n'ai pas compris ce coup. Peux-tu le répéter ?",
                "instruction": "Dis EXACTEMENT la phrase dans 'say_exactly'. Attends le joueur.",
            }

        return {
            "status": "error",
            "say_exactly": resp.get("error", "Erreur. Répète ton coup."),
            "instruction": "Dis EXACTEMENT la phrase dans 'say_exactly'. Attends le joueur.",
        }
