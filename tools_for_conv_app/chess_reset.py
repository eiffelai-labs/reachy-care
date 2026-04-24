"""
chess_reset.py — Réinitialise la partie d'échecs en cours.

Le LLM appelle cet outil quand le joueur demande une nouvelle partie,
un reset, ou veut recommencer ("nouvelle partie", "on recommence", "reset").
"""

import contextlib
import json
import logging
import os
import tempfile
import time
from typing import Any

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies

logger = logging.getLogger(__name__)

_CMD_DIR = "/tmp/reachy_care_cmds"


def _safe_write_cmd(cmd: dict) -> None:
    os.makedirs(_CMD_DIR, exist_ok=True)
    dest = os.path.join(_CMD_DIR, f"{time.time_ns()}.json")
    with contextlib.suppress(Exception):
        with tempfile.NamedTemporaryFile("w", dir=_CMD_DIR, delete=False, suffix=".tmp") as f:
            json.dump(cmd, f)
            tmp_path = f.name
        os.replace(tmp_path, dest)


class ChessReset(Tool):
    """Réinitialise la partie d'échecs."""

    name = "chess_reset"
    description = (
        "Remet la partie d'échecs à zéro. "
        "À appeler quand le joueur dit 'nouvelle partie', 'on recommence', 'reset'."
    )
    parameters_schema = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        _safe_write_cmd({"cmd": "chess_reset"})
        logger.info("chess_reset: commande envoyée à main.py.")
        return {
            "status": "ok",
            "instruction": "La partie est réinitialisée. Annonce une nouvelle partie en 1 phrase.",
        }
