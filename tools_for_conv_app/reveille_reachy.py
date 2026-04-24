"""
reveille_reachy — Réveille Reachy de son mode veille physique.
Envoie la commande wake via IPC CMD_DIR → main.py réactive les moteurs et démute le bridge.
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


class ReveilleReatchy(Tool):
    """Réveille Reachy de son mode veille physique sur commande vocale."""

    name = "reveille_reachy"
    description = (
        "Réveille Reachy de son mode veille physique (habitacle). "
        "À appeler quand la personne dit 'debout Reachy', 'lève-toi Reachy', 'réveille-toi'. "
        "Réactive les moteurs et démute le bridge."
    )
    parameters_schema = {"type": "object", "properties": {}, "required": []}

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        _safe_write_cmd({"cmd": "wake_motors"})
        return {"status": "ok", "info": "Réveil envoyé."}
