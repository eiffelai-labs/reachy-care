"""
confirm_identity — Confirme l'identité d'une personne ambiguë.
Utilisé quand Reachy hésite entre deux personnes enrôlées et a posé la question.
"""

import contextlib
import json
import os
import tempfile
import time
from typing import Any

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies

_CMD_DIR = "/tmp/reachy_care_cmds"


def _safe_write_cmd(cmd: dict) -> None:
    os.makedirs(_CMD_DIR, exist_ok=True)
    dest = os.path.join(_CMD_DIR, f"{time.time_ns()}.json")
    with contextlib.suppress(Exception):
        with tempfile.NamedTemporaryFile("w", dir=_CMD_DIR, delete=False, suffix=".tmp") as f:
            json.dump(cmd, f)
            tmp_path = f.name
        os.replace(tmp_path, dest)


class ConfirmIdentity(Tool):
    """Confirme l'identité d'une personne après une question de désambiguïsation."""

    name = "confirm_identity"
    description = (
        "Confirme le prénom d'une personne ambiguë après avoir posé la question. "
        "À appeler uniquement après un message [Reachy Care] Ambiguïté identité "
        "et après que la personne a répondu. "
        "Passer le prénom exact tel qu'il est enrôlé (minuscules)."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Prénom confirmé de la personne (ex: 'alexandre', 'galileo').",
            }
        },
        "required": ["name"],
    }

    async def __call__(self, deps: ToolDependencies, name: str = "", **kwargs: Any) -> dict[str, Any]:
        confirmed = name.strip().lower()
        if not confirmed:
            return {"status": "error", "info": "Prénom vide."}
        _safe_write_cmd({"cmd": "confirm_identity", "name": confirmed})
        return {"status": "ok", "name": confirmed}
