"""
session_memory.py — Mémoire de session pour reachy_mini_conversation_app.

Permet au LLM de sauvegarder et relire des informations clés en cours de session
(position dans un livre, état d'une activité, notes diverses) pour ne pas
perdre le fil quand la fenêtre de contexte OpenAI Realtime se remplit.

La mémoire est stockée dans /tmp/reachy_session_memory.json (session courante).
Elle est relue et ré-injectée dans le contexte par le bridge Reachy Care
toutes les quelques minutes.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies

logger = logging.getLogger(__name__)

MEMORY_FILE = Path("/tmp/reachy_session_memory.json")


def _load_raw() -> dict:
    try:
        return json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_raw(data: dict) -> None:
    MEMORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class SessionMemory(Tool):
    """Sauvegarde et relit des informations clés pour ne pas perdre le fil."""

    name = "session_memory"
    description = (
        "Sauvegarde ou relit des informations importantes pour ne pas les oublier "
        "quand la conversation devient longue. "
        "Utilise 'save' pour mémoriser une information clé (position dans un livre, "
        "état d'une activité, note sur la personne). "
        "Utilise 'load' pour relire tout ce qui a été mémorisé."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["save", "load"],
                "description": "'save' pour mémoriser, 'load' pour relire.",
            },
            "key": {
                "type": "string",
                "description": "Clé de la valeur à sauvegarder. Exemple : 'livre_id', 'livre_offset', 'activite'.",
            },
            "value": {
                "type": "string",
                "description": "Valeur à sauvegarder (texte libre).",
            },
        },
        "required": ["action"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        action = (kwargs.get("action") or "").strip()

        if action == "save":
            key = (kwargs.get("key") or "").strip()
            value = kwargs.get("value", "")
            if not key:
                return {"error": "La clé (key) est obligatoire pour sauvegarder."}
            data = _load_raw()
            data[key] = value
            data["_updated_at"] = time.strftime("%H:%M:%S")
            _save_raw(data)
            logger.info("session_memory: save %r = %r", key, str(value)[:80])
            return {"status": "ok", "saved": {key: value}}

        if action == "load":
            data = _load_raw()
            if not data:
                return {"memory": {}, "note": "Aucune information mémorisée pour cette session."}
            logger.info("session_memory: load → %d clés", len(data))
            return {"memory": data}

        return {"error": f"Action inconnue : {action!r}. Utilise 'save' ou 'load'."}
