"""
journal.py — Journal quotidien par personne pour Reachy Care.

Chaque personne a un fichier <nom>_journal_YYYY-MM-DD.json dans known_faces/.
Thread-safe : toutes les écritures sont protégées par un Lock.
"""

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)

VALID_CATEGORIES = {
    "medication", "repas", "visite", "lecture",
    "activite", "humeur", "sante", "sommeil", "note",
}

_VALID_SOURCES = {"llm", "system"}

_CATEGORY_EMOJI = {
    "medication": "💊",
    "repas":      "🍽️",
    "visite":     "👥",
    "lecture":    "📖",
    "activite":   "🎯",
    "humeur":     "😊",
    "sante":      "❤️",
    "sommeil":    "😴",
    "note":       "📝",
}


class Journal:
    """Journal quotidien — un fichier JSON par personne et par jour."""

    def __init__(self, known_faces_dir: str) -> None:
        self._dir = Path(known_faces_dir)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    def _path(self, person: str, date_str: str) -> Path:
        return self._dir / f"{person}_journal_{date_str}.json"

    def _now(self) -> datetime:
        return datetime.now(ZoneInfo(config.TIMEZONE))

    def _today_str(self) -> str:
        return self._now().strftime("%Y-%m-%d")

    def _now_parts(self) -> tuple[str, str]:
        """Returns (date_str, time_str) for the current moment."""
        now = self._now()
        return now.strftime("%Y-%m-%d"), now.strftime("%H:%M")

    # ------------------------------------------------------------------
    # I/O JSON
    # ------------------------------------------------------------------

    def _load(self, person: str, date_str: str) -> dict | None:
        path = self._path(person, date_str)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("journal: impossible de lire %s : %s", path, exc)
            return None

    def _save(self, data: dict, person: str, date_str: str) -> bool:
        path = self._path(person, date_str)
        try:
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return True
        except OSError as exc:
            logger.error("journal: impossible d'écrire %s : %s", path, exc)
            return False

    def _make_empty(self, person: str, date_str: str) -> dict:
        return {
            "person": person,
            "date": date_str,
            "entries": [],
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def log(
        self,
        person: str,
        category: str,
        description: str,
        source: str = "system",
    ) -> bool:
        """Ajoute une entrée au journal du jour. Retourne True si succès."""
        person = person.lower()

        if category not in VALID_CATEGORIES:
            logger.warning("journal: catégorie invalide '%s'", category)
            return False
        if source not in _VALID_SOURCES:
            logger.warning("journal: source invalide '%s'", source)
            return False

        date_str, time_str = self._now_parts()

        entry = {
            "time": time_str,
            "category": category,
            "description": description,
            "source": source,
        }

        with self._lock:
            data = self._load(person, date_str)
            if data is None:
                data = self._make_empty(person, date_str)
            data["entries"].append(entry)
            ok = self._save(data, person, date_str)

        if ok:
            logger.info(
                "journal: [%s] %s %s — %s",
                person, time_str, category, description,
            )
        return ok

    def get_today(self, person: str) -> dict | None:
        """Retourne le journal du jour pour cette personne, ou None."""
        person = person.lower()
        with self._lock:
            return self._load(person, self._today_str())

    def get_date(self, person: str, date_str: str) -> dict | None:
        """Retourne le journal pour une date donnée (YYYY-MM-DD), ou None."""
        person = person.lower()
        with self._lock:
            return self._load(person, date_str)

    def render_text(self, person: str, date_str: str | None = None) -> str:
        """Résumé texte brut du journal (pour email / Telegram)."""
        person = person.lower()
        date_str = date_str or self._today_str()

        with self._lock:
            data = self._load(person, date_str)

        if data is None or not data.get("entries"):
            return "Aucun événement enregistré."

        lines = [f"Journal de {person.capitalize()} — {date_str}", ""]
        for e in data["entries"]:
            emoji = _CATEGORY_EMOJI.get(e["category"], "")
            lines.append(f"  {e['time']}  {emoji} {e['category']} — {e['description']}")
        return "\n".join(lines)

    def render_html(self, person: str, date_str: str | None = None) -> str:
        """Résumé HTML du journal (table inline-CSS pour emails)."""
        person = person.lower()
        date_str = date_str or self._today_str()

        with self._lock:
            data = self._load(person, date_str)

        if data is None or not data.get("entries"):
            return "<p>Aucun événement enregistré.</p>"

        rows = []
        for e in data["entries"]:
            emoji = _CATEGORY_EMOJI.get(e["category"], "")
            rows.append(
                f"<tr>"
                f"<td style=\"padding:4px 8px;border:1px solid #ddd;\">{e['time']}</td>"
                f"<td style=\"padding:4px 8px;border:1px solid #ddd;\">{emoji} {e['category']}</td>"
                f"<td style=\"padding:4px 8px;border:1px solid #ddd;\">{e['description']}</td>"
                f"</tr>"
            )

        return (
            f"<h3 style=\"font-family:sans-serif;\">Journal de {person.capitalize()} — {date_str}</h3>\n"
            f"<table style=\"border-collapse:collapse;font-family:sans-serif;font-size:14px;\">\n"
            f"<tr style=\"background:#f4f4f4;\">"
            f"<th style=\"padding:4px 8px;border:1px solid #ddd;\">Heure</th>"
            f"<th style=\"padding:4px 8px;border:1px solid #ddd;\">Catégorie</th>"
            f"<th style=\"padding:4px 8px;border:1px solid #ddd;\">Description</th>"
            f"</tr>\n"
            + "\n".join(rows)
            + "\n</table>"
        )
