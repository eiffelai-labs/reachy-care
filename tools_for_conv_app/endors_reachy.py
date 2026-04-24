"""
endors_reachy — Met Reachy en veille logicielle (mute + tête baissée + loops suspendues).
Envoie la commande sleep_mode via IPC CMD_DIR.
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


class EndorsReatchy(Tool):
    """Met Reachy en sommeil logiciel sur commande vocale."""

    name = "endors_reachy"
    description = (
        "Met Reachy en sommeil physique : tête dans l'habitacle, moteurs en veille, bridge muté. "
        "Le réveil se fait uniquement sur commande vocale 'debout Reachy' / 'lève-toi Reachy' "
        "→ appeler alors reveille_reachy. "
        "À appeler uniquement sur demande explicite ('couche toi', 'dodo', 'bonne nuit', 'à demain')."
    )
    parameters_schema = {"type": "object", "properties": {}, "required": []}

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        _safe_write_cmd({"cmd": "sleep_mode"})
        return {"status": "ok", "info": "Mise en sommeil logiciel envoyée."}
