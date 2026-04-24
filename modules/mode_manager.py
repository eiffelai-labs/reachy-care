"""
mode_manager.py — Gestionnaire de modes pour Reachy Care.

Utilise ActivityRegistry pour les lookups dynamiques (modes, instructions,
messages d'annonce). Fallback sur les dicts hardcodés si le registry
n'est pas disponible.
"""

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)

MODE_NORMAL   = "normal"
MODE_HISTOIRE = "histoire"
MODE_PRO      = "pro"
MODE_ECHECS   = "echecs"
MODE_MUSIQUE  = "musique"

# Fallback hardcodé (utilisé si ActivityRegistry absent)
_FALLBACK_VALID_MODES = {MODE_NORMAL, MODE_HISTOIRE, MODE_PRO, MODE_ECHECS, MODE_MUSIQUE}

_FALLBACK_INSTRUCTIONS_FILES = {
    MODE_NORMAL:   "instructions.txt",
    MODE_HISTOIRE: "instructions_histoire.txt",
    MODE_PRO:      "instructions_pro.txt",
    MODE_ECHECS:   "instructions_echecs.txt",
    MODE_MUSIQUE:  "instructions_musique.txt",
}

_FALLBACK_ANNOUNCE_MESSAGES = {
    MODE_HISTOIRE: (
        "[Reachy Care] MODE HISTOIRES activé. "
        "Tu entres en mode histoires : lecture de livres du domaine public. "
        "Propose de choisir un type d'histoire."
    ),
    MODE_PRO: (
        "[Reachy Care] MODE EXPOSÉ activé. "
        "Tu entres en mode exposé. "
        "Demande sur quel sujet faire l'exposé si non précisé."
    ),
    MODE_ECHECS: (
        "[Reachy Care] MODE ÉCHECS activé. "
        "Un échiquier est devant toi. Adopte le rôle de coach d'échecs bienveillant."
    ),
    MODE_MUSIQUE: (
        "[Reachy Care] MODE MUSIQUE activé. "
        "Tu écoutes la musique ambiante avec la personne. "
        "Appelle identify_music immédiatement, puis groove sur le rythme. "
        "Reste surtout silencieux — la musique parle."
    ),
    MODE_NORMAL: (
        "[Reachy Care] MODE NORMAL activé. "
        "Reviens à ta personnalité de compagnon bienveillant habituelle."
    ),
}

_SEPARATOR = "=" * 60

# Délai minimum entre deux switches (anti-spam)
_SWITCH_THROTTLE_SEC = 5.0


class ModeManager:
    """Gestionnaire thread-safe des modes de comportement de Reachy."""

    def __init__(self, profiles_dir: str, bridge, registry=None) -> None:
        self._profiles_dir = Path(profiles_dir)
        self._bridge = bridge
        self._registry = registry
        self._lock = threading.Lock()
        self._current_mode = MODE_NORMAL
        self._last_switch_time = 0.0
        self._instructions_cache: dict[str, str] = {}
        self._preload_instructions()

    # ------------------------------------------------------------------
    # Registry-aware lookups
    # ------------------------------------------------------------------

    def _get_valid_modes(self) -> set[str]:
        if self._registry:
            return self._registry.get_valid_modes()
        return _FALLBACK_VALID_MODES

    def _get_instructions_file(self, mode: str) -> str | None:
        if self._registry:
            return self._registry.get_instructions_file(mode)
        return _FALLBACK_INSTRUCTIONS_FILES.get(mode)

    def _get_announce_message(self, mode: str) -> str:
        if self._registry:
            return self._registry.get_announce_message(mode)
        return _FALLBACK_ANNOUNCE_MESSAGES.get(mode, "")

    # ------------------------------------------------------------------
    # Instructions
    # ------------------------------------------------------------------

    def _preload_instructions(self) -> None:
        for mode in self._get_valid_modes():
            filename = self._get_instructions_file(mode)
            if not filename:
                continue
            path = self._profiles_dir / filename
            if path.exists():
                self._instructions_cache[mode] = path.read_text(encoding="utf-8")
                logger.info(
                    "Instructions mode '%s' chargées (%d chars).", mode,
                    len(self._instructions_cache[mode]),
                )
            else:
                logger.warning("Fichier instructions manquant pour mode '%s' : %s", mode, path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_current_mode(self) -> str:
        with self._lock:
            return self._current_mode

    def switch_mode(self, mode: str, context: str = "") -> bool:
        """
        Change le mode actif.

        Returns True si le switch a eu lieu, False si ignoré
        (mode identique, throttle, ou mode inconnu).
        """
        if mode not in self._get_valid_modes():
            logger.warning("Mode inconnu : '%s'", mode)
            return False

        now = time.monotonic()
        with self._lock:
            if self._current_mode == mode:
                return False
            if now - self._last_switch_time < _SWITCH_THROTTLE_SEC:
                logger.debug("Switch trop rapide ignoré.")
                return False
            previous = self._current_mode
            self._current_mode = mode
            self._last_switch_time = now

        logger.info("Switch mode : %s → %s (context=%r)", previous, mode, context)
        self._apply_mode(mode, context)
        return True

    def _apply_mode(self, mode: str, context: str = "") -> None:
        try:
            tz = ZoneInfo(getattr(config, "TIMEZONE", "Europe/Paris"))
        except Exception:
            tz = None
        now_str = datetime.now(tz).strftime("%A %d %B %Y, %Hh%M")

        mode_instructions = self._instructions_cache.get(mode)
        if not mode_instructions:
            logger.error("Instructions manquantes pour mode '%s' — switch annulé.", mode)
            return

        # Construire les instructions complètes :
        # base (normal) + override mode-spécifique si mode != normal
        base_instructions = self._instructions_cache.get(MODE_NORMAL, "")
        if mode == MODE_NORMAL or not base_instructions:
            instructions = mode_instructions
        else:
            instructions = (
                base_instructions
                + "\n\n"
                + _SEPARATOR
                + f"\n## MODE ACTIF : {mode.upper()}\n"
                + "Les règles ci-dessous REMPLACENT les règles générales en cas de conflit.\n"
                + _SEPARATOR
                + "\n\n"
                + mode_instructions
            )

        if mode == MODE_PRO and context:
            instructions = instructions + f"\n\nSujet demandé : {context}"

        # Injection LOCATION, DATETIME, PRIMARY_PERSON, KNOWN_PEOPLE
        # PRIMARY_PERSON / KNOWN_PEOPLE dérivés de known_faces/registry.json
        # via config.OWNER_NAME et config.SECONDARY_PERSONS (édités par le dashboard).
        instructions = instructions.replace("{LOCATION}", config.LOCATION)
        instructions = instructions.replace("{DATETIME}", now_str)
        _owner = getattr(config, "OWNER_NAME", "") or ""
        _secondary = getattr(config, "SECONDARY_PERSONS", []) or []
        _primary_display = _owner.capitalize() if _owner else "non renseigné"
        _known_display = ", ".join(p.capitalize() for p in _secondary) if _secondary else ""
        instructions = instructions.replace("{PRIMARY_PERSON}", _primary_display)
        instructions = instructions.replace("{KNOWN_PEOPLE}", _known_display)

        # 1. Mettre à jour les instructions de session (persistant)
        self._bridge.update_session_instructions(instructions)

        # 2. Injecter un message d'amorçage avec le contexte si disponible
        announce_text = self._get_announce_message(mode)
        if mode == MODE_PRO and context:
            announce_text += f" Sujet : {context}."
        elif mode == MODE_ECHECS and context:
            announce_text += f" Position actuelle de la partie (FEN) : {context}."

        self._bridge.announce_mode_switch(announce_text)
