"""
log_event.py — Outil de journalisation d'événements quotidiens.

Appelé par le LLM quand il observe un événement notable dans la conversation :
repas mentionné, médicament pris, visite, activité, humeur, remarque santé, sommeil.

Les événements sont écrits dans known_faces/<personne>_journal_YYYY-MM-DD.json,
un fichier par jour par personne. Lecture directe du fichier (pas d'IPC).
"""

import contextlib
import json
import logging
import os
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies

logger = logging.getLogger(__name__)

_SESSION_FILE = Path("/tmp/reachy_session_memory.json")
_LOCK = threading.Lock()

_VALID_CATEGORIES = {
    "medication", "repas", "visite", "lecture",
    "activite", "humeur", "sante", "sommeil", "note",
}

# Config is loaded once at import time from the reachy_care package.
# Falls back to safe defaults if the package is not on sys.path yet.
def _load_config():
    care_path = os.environ.get("REACHY_CARE_PATH", "/home/pollen/reachy_care")
    if care_path not in sys.path:
        sys.path.insert(0, care_path)
    try:
        import config as _rc_config  # noqa: PLC0415
        return _rc_config
    except Exception:
        return None

_config = _load_config()
_KNOWN_FACES_DIR = Path(getattr(_config, "KNOWN_FACES_DIR", "/home/pollen/reachy_care/known_faces"))
_TIMEZONE = getattr(_config, "TIMEZONE", "Europe/Paris")


def _current_person() -> str | None:
    """Lit la personne courante depuis le fichier de session."""
    try:
        data = json.loads(_SESSION_FILE.read_text(encoding="utf-8"))
        return data.get("current_person") or data.get("person")
    except Exception:
        return None


def _journal_path(person: str, date_str: str) -> Path:
    return _KNOWN_FACES_DIR / f"{person}_journal_{date_str}.json"


def _now() -> datetime:
    try:
        return datetime.now(ZoneInfo(_TIMEZONE))
    except Exception:
        return datetime.now()


class LogEvent(Tool):
    """Enregistre un événement notable dans le journal quotidien de la personne."""

    name = "log_event"
    description = (
        "Enregistre un événement notable dans le journal quotidien de la personne. "
        "Appelle cet outil quand tu observes quelque chose de significatif : "
        "repas mentionné, médicament pris, visite de quelqu'un, activité (promenade, jeu, lecture), "
        "changement d'humeur, remarque sur la santé, information sur le sommeil. "
        "NE PAS appeler pour chaque message — uniquement quand un fait concret et utile mérite d'être noté "
        "pour le suivi quotidien de la personne."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": [
                    "medication", "repas", "visite", "lecture",
                    "activite", "humeur", "sante", "sommeil", "note",
                ],
                "description": "Catégorie de l'événement.",
            },
            "description": {
                "type": "string",
                "description": "Ce qui s'est passé — une phrase concise.",
            },
        },
        "required": ["category", "description"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        category = (kwargs.get("category") or "").strip()
        description = (kwargs.get("description") or "").strip()

        if category not in _VALID_CATEGORIES:
            return {"error": f"Catégorie inconnue : {category!r}. Valeurs possibles : {sorted(_VALID_CATEGORIES)}"}
        if not description:
            return {"error": "La description est obligatoire."}

        person = _current_person()
        if not person:
            return {"error": "Personne non identifiée — événement non enregistré."}

        ts = _now()
        date_str = ts.strftime("%Y-%m-%d")
        time_str = ts.strftime("%H:%M")

        entry = {
            "time": time_str,
            "category": category,
            "description": description,
            "source": "llm",
        }

        path = _journal_path(person, date_str)

        try:
            with _LOCK:
                if path.exists():
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                        if not isinstance(data, dict) or "entries" not in data:
                            data = {"person": person, "date": date_str, "entries": []}
                    except (json.JSONDecodeError, ValueError):
                        data = {"person": person, "date": date_str, "entries": []}
                else:
                    data = {"person": person, "date": date_str, "entries": []}

                data["entries"].append(entry)

                # Atomic write: write to a temp file then rename.
                path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", suffix=".tmp",
                    dir=path.parent, delete=False,
                ) as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    tmp_path = f.name
                try:
                    os.replace(tmp_path, path)
                except Exception:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp_path)
                    raise

            logger.info("log_event: %s — %s %s : %s", person, date_str, time_str, description[:80])
            return {"status": "ok", "person": person, "category": category}

        except Exception as exc:
            logger.error("log_event: impossible d'écrire le journal : %s", exc)
            return {"error": "Impossible d'enregistrer l'événement."}
