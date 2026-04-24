"""
report_wellbeing.py — Outil de retour de vérification de bien-être.

Appelé par le LLM après avoir posé une question de check-in (suite à une
suspicion de chute). Écrit un fichier dans /tmp/reachy_care_cmds/,
lu par main.py pour décider d'escalader ou non l'alerte.
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
    """Écriture atomique dans la queue répertoire : tmp → os.replace() → <timestamp_ns>.json."""
    os.makedirs(_CMD_DIR, exist_ok=True)
    dest = os.path.join(_CMD_DIR, f"{time.time_ns()}.json")
    with contextlib.suppress(Exception):
        with tempfile.NamedTemporaryFile("w", dir=_CMD_DIR, delete=False, suffix=".tmp") as f:
            json.dump(cmd, f)
            tmp_path = f.name
        os.replace(tmp_path, dest)


class ReportWellbeing(Tool):
    """Signale l'issue d'une vérification de bien-être après suspicion de chute."""

    name = "report_wellbeing"
    description = (
        "UNIQUEMENT si tu as reçu exactement le message système '[Reachy Care] Suspicion de chute' "
        "ET que tu as posé une question de vérification ET obtenu une réponse (ou silence prolongé). "
        "JAMAIS spontanément. JAMAIS parce que la personne est silencieuse, concentrée, ou dit 'Non'. "
        "JAMAIS parce que tu n'as pas eu de réponse depuis quelques minutes. "
        "Le silence, c'est normal — la personne travaille, réfléchit, dort. "
        "Appelle UNIQUEMENT sur déclenchement système [Reachy Care]."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["ok", "problem", "no_response"],
                "description": (
                    "'ok' si la personne a répondu positivement au check-in système, "
                    "'problem' si elle a explicitement dit avoir besoin d'aide (au secours, j'ai mal, je suis tombé), "
                    "'no_response' si aucune réponse après 45s LORS D'UN CHECK-IN SYSTÈME."
                ),
            },
        },
        "required": ["status"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        # Guard : vérifier qu'un check-in chute est réellement actif
        _FALL_CHECKIN_FILE = "/tmp/reachy_care_fall_checkin.json"
        _CHECKIN_EXPIRY_S = 120  # secondes
        try:
            with open(_FALL_CHECKIN_FILE, "r", encoding="utf-8") as f:
                checkin_data = json.load(f)
            ts = checkin_data.get("timestamp", 0)
            if time.time() - ts > _CHECKIN_EXPIRY_S:
                logger.warning("report_wellbeing ignoré — check-in expiré (%.0fs)", time.time() - ts)
                return {"error": "Aucune suspicion de chute active — report_wellbeing ignoré."}
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            logger.warning("report_wellbeing ignoré — aucun fichier de check-in actif")
            return {"error": "Aucune suspicion de chute active — report_wellbeing ignoré."}

        raw = kwargs.get("status", "")
        status = raw.strip() if isinstance(raw, str) else ""
        if status not in {"ok", "problem", "no_response"}:
            status = "no_response"

        cmd = {"cmd": "wellbeing_response", "status": status}

        try:
            _safe_write_cmd(cmd)
            logger.info("report_wellbeing: status=%r écrit dans cmd file", status)
            return {"acknowledged": True, "status": status}
        except Exception as exc:
            logger.error("report_wellbeing: impossible d'écrire la commande : %s", exc)
            return {"error": "Impossible de transmettre le résultat."}
