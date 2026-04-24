"""
enroll_face.py — Outil d'enrôlement facial pour reachy_mini_conversation_app.

Déclenché par l'IA quand l'utilisateur demande à être mémorisé.
Écrit un fichier dans /tmp/reachy_care_cmds/, lu par main.py.
L'enrôlement dure ~3 secondes (10 photos automatiques) puis s'arrête seul.
"""

import contextlib
import json
import logging
import os
import tempfile
import time
import unicodedata
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


def _normalize_name(name: str) -> str:
    """Minuscules, suppression accents, espaces→underscores.

    Note : copie de main.py._normalize_name() — duplication intentionnelle
    car ce fichier tourne dans conv_app (process séparé, pas d'import croisé).
    """
    name = name.strip().lower()
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    name = name.replace(" ", "_")
    return name


class EnrollFace(Tool):
    """Mémorise le visage d'une personne pour la reconnaître dans le futur."""

    name = "enroll_face"
    description = (
        "Mémorise le visage de la personne devant Reachy pour la reconnaître lors des prochaines visites. "
        "IMPORTANT : n'utilise cet outil QUE si la personne te demande EXPLICITEMENT d'être mémorisée "
        "(ex: 'mémorise mon visage', 'enregistre-moi', 'souviens-toi de moi'). "
        "Ne l'appelle JAMAIS suite à un événement [Reachy Care] de reconnaissance faciale, "
        "ni de ta propre initiative. En cas de doute : ne pas enrôler. "
        "L'enrôlement dure environ 3 secondes — dis à la personne de rester face à la caméra."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Prénom de la personne à mémoriser. Exemple : 'Alexandre', 'Marie'.",
            },
        },
        "required": ["name"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        name = _normalize_name(kwargs.get("name") or "")

        if not name:
            return {"error": "Je n'ai pas compris le prénom. Peux-tu le répéter ?"}

        cmd = {"cmd": "enroll", "name": name}

        try:
            _safe_write_cmd(cmd)
            logger.info("enroll_face: commande écrite → name=%r", name)
            return {"status": "ok", "name": name}
        except Exception as exc:
            logger.error("enroll_face: impossible d'écrire la commande : %s", exc)
            return {"error": "Impossible de lancer l'enrôlement."}
