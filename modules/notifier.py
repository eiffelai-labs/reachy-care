"""Notification module — Telegram + Email alerts and journal recaps.

Extracted from main.py to be reusable across modules.
All sends happen in daemon threads to avoid blocking the caller.
"""

import logging
import smtplib
import threading
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import requests

import config

logger = logging.getLogger("reachy.notifier")


class Notifier:
    """Unified notification sender (Telegram + Email)."""

    def __init__(self) -> None:
        # Telegram
        self._tg_enabled: bool = getattr(config, "TELEGRAM_ENABLED", False)
        self._tg_token: str = getattr(config, "TELEGRAM_BOT_TOKEN", "")
        self._tg_chat_id: str = getattr(config, "TELEGRAM_CHAT_ID", "")

        # Email — alerts
        self._email_enabled: bool = getattr(config, "ALERT_EMAIL_ENABLED", False)
        self._email_from: str = getattr(config, "ALERT_EMAIL_FROM", "")
        self._email_password: str = getattr(config, "ALERT_EMAIL_PASSWORD", "")
        self._email_to: str = getattr(config, "ALERT_EMAIL_TO", "")

        # Email — journal (defaults to alert recipient if not set)
        self._journal_email_to: str = getattr(config, "JOURNAL_EMAIL_TO", "") or self._email_to
        self._journal_tg_enabled: bool = getattr(config, "JOURNAL_TELEGRAM_ENABLED", True)

    # ------------------------------------------------------------------
    # Telegram
    # ------------------------------------------------------------------

    def send_telegram(self, text: str, parse_mode: str = "Markdown") -> None:
        """Send a Telegram message in a background thread. No-op if not configured."""
        if not self._tg_enabled or not self._tg_token or not self._tg_chat_id:
            return

        def _send() -> None:
            try:
                resp = requests.post(
                    f"https://api.telegram.org/bot{self._tg_token}/sendMessage",
                    json={
                        "chat_id": self._tg_chat_id,
                        "text": text,
                        "parse_mode": parse_mode,
                    },
                    timeout=10,
                )
                if not resp.ok:
                    logger.error("Telegram: %s %s", resp.status_code, resp.text)
            except Exception as exc:
                logger.error("Échec envoi Telegram : %s", exc)

        threading.Thread(target=_send, name="telegram", daemon=True).start()

    # ------------------------------------------------------------------
    # Email
    # ------------------------------------------------------------------

    def send_email(
        self,
        subject: str,
        body: str,
        html: str | None = None,
        recipient: str | None = None,
    ) -> None:
        """Send an email in a background thread. Supports plain text and optional HTML.

        If *recipient* is None, uses ALERT_EMAIL_TO.
        No-op if email is not configured.
        """
        if not self._email_enabled:
            return
        if not self._email_from or not self._email_password:
            logger.warning("Email : identifiants non configurés (ALERT_EMAIL_FROM / ALERT_EMAIL_PASSWORD).")
            return

        to_addr = recipient or self._email_to
        if not to_addr:
            logger.warning("Email : destinataire non configuré.")
            return

        # Build message
        if html:
            msg = MIMEMultipart("alternative")
            msg.attach(MIMEText(body, "plain", "utf-8"))
            msg.attach(MIMEText(html, "html", "utf-8"))
        else:
            msg = MIMEText(body, "plain", "utf-8")

        msg["Subject"] = subject
        msg["From"] = self._email_from
        msg["To"] = to_addr

        def _send() -> None:
            try:
                with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as smtp:
                    smtp.login(self._email_from, self._email_password)
                    smtp.send_message(msg)
                logger.info("Email envoyé à %s : %s", to_addr, subject)
            except Exception as exc:
                logger.error("Échec envoi email : %s", exc)

        threading.Thread(target=_send, name="email", daemon=True).start()

    # ------------------------------------------------------------------
    # Convenience — Fall alert
    # ------------------------------------------------------------------

    def send_fall_alert(self, person_name: str | None) -> None:
        """Send both Telegram + email alert for a detected fall."""
        who = person_name.capitalize() if person_name else "une personne inconnue"
        tz = ZoneInfo(getattr(config, "TIMEZONE", "Europe/Paris"))
        now = datetime.now(tz).strftime("%d/%m/%Y à %Hh%M")

        # Telegram
        self.send_telegram(
            f"⚠️ *Reachy Care — Chute détectée*\n\n"
            f"🕐 {now}\n"
            f"👤 Personne : {who}\n\n"
            f"Reachy a réagi vocalement. Vérifiez la situation."
        )

        # Email
        self.send_email(
            subject=f"⚠️ Reachy Care — Chute détectée ({who})",
            body=(
                f"Bonjour,\n\n"
                f"Le robot Reachy a détecté une chute le {now}.\n\n"
                f"Personne concernée : {who}\n\n"
                f"Reachy a immédiatement réagi vocalement. Veuillez vérifier la situation.\n\n"
                f"— Reachy Care"
            ),
        )

    # ------------------------------------------------------------------
    # Convenience — Journal recap
    # ------------------------------------------------------------------

    def send_journal_recap(
        self,
        person: str,
        text_body: str,
        html_body: str | None = None,
    ) -> None:
        """Send daily journal recap via email and/or Telegram."""
        subject = f"📋 Reachy Care — Récap journalier ({person})"

        # Email
        if self._email_enabled and self._journal_email_to:
            self.send_email(
                subject=subject,
                body=text_body,
                html=html_body,
                recipient=self._journal_email_to,
            )

        # Telegram
        if self._journal_tg_enabled:
            # Telegram messages have a 4096 char limit; truncate if needed
            tg_text = f"*{subject}*\n\n{text_body}"
            if len(tg_text) > 4000:
                tg_text = tg_text[:3997] + "…"
            self.send_telegram(tg_text)
