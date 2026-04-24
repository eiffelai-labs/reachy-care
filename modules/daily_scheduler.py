"""
daily_scheduler.py — Planificateur quotidien léger pour Reachy Care.

Vérifie `datetime.now()` toutes les ~60s (appelé depuis la boucle principale)
et déclenche les actions quotidiennes aux heures configurées.

Actuellement : envoi du récapitulatif journal par email à JOURNAL_EMAIL_HOUR.
"""

import logging
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)


_TZ = ZoneInfo(getattr(config, "TIMEZONE", "Europe/Paris"))
_KNOWN_FACES_DIR: Path = getattr(config, "KNOWN_FACES_DIR", Path("/home/pollen/reachy_care/known_faces"))


class DailyScheduler:
    """Planificateur quotidien appelé depuis la boucle principale (~0.2s)."""

    _CHECK_INTERVAL_SEC = 60.0  # vrai travail toutes les 60s max

    def __init__(self, journal, notifier):
        self._journal = journal
        self._notifier = notifier
        self._last_check_time: float = 0.0
        self._sent_today: str | None = None

    # ------------------------------------------------------------------
    # Public — appelé depuis main loop
    # ------------------------------------------------------------------

    def tick(self) -> None:
        """Appelé toutes les ~0.2s. Throttle interne à 60s."""
        now_mono = time.monotonic()
        if now_mono - self._last_check_time < self._CHECK_INTERVAL_SEC:
            return
        self._last_check_time = now_mono
        self._check_and_send()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _check_and_send(self) -> None:
        """Vérifie si l'heure courante correspond à JOURNAL_EMAIL_HOUR.

        Si oui et pas encore envoyé , envoie le récapitulatif
        journal pour chaque personne ayant des entrées .
        """
        now = datetime.now(_TZ)
        today_str = now.strftime("%Y-%m-%d")

        # Déjà envoyé 
        if self._sent_today == today_str:
            return

        email_hour = getattr(config, "JOURNAL_EMAIL_HOUR", 20)
        if now.hour != email_hour:
            return

        persons = self._get_persons_with_journal_today(today_str)
        if not persons:
            logger.debug("daily_scheduler: aucune entrée journal , rien à envoyer.")
            self._sent_today = today_str
            return

        logger.info("daily_scheduler: envoi récapitulatif journal pour %s personne(s).", len(persons))
        for person in persons:
            try:
                text_body = self._journal.render_text(person, today_str)
                html_body = self._journal.render_html(person, today_str)
                self._notifier.send_journal_recap(person, text_body, html_body)
                logger.info("daily_scheduler: récap envoyé pour %s.", person)
            except Exception:
                logger.exception("daily_scheduler: erreur envoi récap pour %s.", person)

        self._sent_today = today_str

    def _get_persons_with_journal_today(self, today_str: str) -> list[str]:
        """Scanne known_faces/ pour les fichiers *_journal_YYYY-MM-DD.json du jour."""
        suffix = f"_journal_{today_str}.json"
        persons = [
            p.name[: -len(suffix)]
            for p in _KNOWN_FACES_DIR.glob(f"*{suffix}")
            if p.name[: -len(suffix)]
        ]
        return sorted(persons)
