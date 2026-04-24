"""
memory_manager.py — Mémoire persistante par personne pour Reachy Care.

Chaque personne connue a un fichier <nom>_memory.json dans known_faces/.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SCHEMA = {
    "name": "",
    "last_seen": "",
    "sessions_count": 0,
    "conversation_summary": "",
    "sessions": [],               # 10 dernières sessions résumées
    "facts": [],                  # faits structurés extraits
    "preferences": {},
    "health_signals": [],
    "family": {},
    "profile": {
        "medications": [],      # ex: ["Doliprane 500mg matin et soir", "Kardégic 75mg à jeun"]
        "schedules": [],        # ex: ["petit-déjeuner 8h", "déjeuner 12h30", "dîner 19h"]
        "emergency_contact": "", # ex: "Fille Marie : 06 12 34 56 78"
        "notes": "",            # informations libres
    },
    "reading_progress": None,    # progression lecture en cours : {book_id, title, authors, offset, source, last_read}
}


class MemoryManager:
    """Charge, met à jour et sauvegarde la mémoire persistante par personne."""

    def __init__(self, known_faces_dir: str) -> None:
        self._dir = Path(known_faces_dir)

    # ------------------------------------------------------------------
    # I/O JSON
    # ------------------------------------------------------------------

    def _path(self, name: str) -> Path:
        return self._dir / f"{name}_memory.json"

    def load(self, name: str) -> dict:
        path = self._path(name)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                for k, v in _SCHEMA.items():
                    data.setdefault(k, v)
                return data
            except Exception as exc:
                logger.warning("MemoryManager: lecture %s échouée : %s", path, exc)
        return {**_SCHEMA, "name": name}

    def save(self, data: dict) -> None:
        name = data.get("name", "unknown")
        try:
            self._path(name).write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.debug("MemoryManager: mémoire sauvegardée pour %s", name)
        except Exception as exc:
            logger.warning("MemoryManager: sauvegarde %s échouée : %s", name, exc)

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def on_seen(self, name: str) -> dict:
        """Met à jour last_seen et sessions_count. Retourne le dict mémoire."""
        data = self.load(name)
        data["name"] = name
        data["last_seen"] = datetime.now().isoformat(timespec="seconds")
        data["sessions_count"] += 1
        self.save(data)
        return data

    def update_summary(self, name: str, summary: str) -> None:
        data = self.load(name)
        data["conversation_summary"] = summary
        self.save(data)

    def update_profile(self, name: str, field: str, value) -> None:
        """Met à jour un champ du profil d'une personne."""
        data = self.load(name)
        if "profile" not in data:
            data["profile"] = {
                "medications": [],
                "schedules": [],
                "emergency_contact": "",
                "notes": "",
            }
        # Champs liste : découper par virgule
        if field in ("medications", "schedules") and isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]
        data["profile"][field] = value
        self.save(data)

    def list_persons(self) -> list[str]:
        return [p.stem.replace("_memory", "") for p in self._dir.glob("*_memory.json")]

    def add_session(self, name: str, session: dict, max_sessions: int = 10) -> None:
        """Ajoute une session à l'historique (tableau tournant). Rétro-compatible avec conversation_summary."""
        data = self.load(name)
        sessions = data.get("sessions", [])
        sessions.append(session)
        data["sessions"] = sessions[-max_sessions:]
        data["conversation_summary"] = session.get("summary", "")
        self.save(data)

    def save_reading_progress(
        self,
        name: str,
        book_id: int | None,
        title: str,
        authors: str,
        offset: int,
        source: str,
        chapter_hint: str = "",
    ) -> None:
        """Sauvegarde la progression de lecture pour une personne."""
        data = self.load(name)
        data["reading_progress"] = {
            "book_id": book_id,
            "title": title,
            "authors": authors,
            "offset": offset,
            "source": source,
            "chapter_hint": chapter_hint,
            "last_read": datetime.now().strftime("%d/%m/%Y"),
        }
        self.save(data)
        logger.debug("Progression lecture sauvegardée pour %s : %s offset=%d", name, title, offset)

    def get_reading_progress(self, name: str) -> dict | None:
        """Retourne la progression de lecture sauvegardée, ou None si aucune."""
        return self.load(name).get("reading_progress")

    def clear_reading_progress(self, name: str) -> None:
        """Efface la progression (livre terminé ou changement de livre)."""
        data = self.load(name)
        data["reading_progress"] = None
        self.save(data)

    def add_facts(self, name: str, facts: list[dict]) -> None:
        """Ajoute des faits structurés, sans doublons exacts sur le champ 'fact'."""
        data = self.load(name)
        existing = data.get("facts", [])
        existing_texts = {f["fact"] for f in existing if isinstance(f, dict) and "fact" in f}
        new_facts = [f for f in facts if isinstance(f, dict) and f.get("fact") not in existing_texts]
        data["facts"] = existing + new_facts
        self.save(data)
