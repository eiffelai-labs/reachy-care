"""
switch_mode.py — Outil de changement de mode pour reachy_mini_conversation_app.

Appelé par l'IA quand elle détecte une intention de changement de mode.

Deux actions en parallèle :
  1. Met à jour la session OpenAI Realtime DIRECTEMENT via await (synchrone avant
     de retourner le résultat au LLM, pour que les nouvelles instructions soient
     actives dès la prochaine réponse).
  2. Écrit un fichier dans /tmp/reachy_care_cmds/ pour que main.py mette à jour
     son état interne (queue répertoire — zéro perte de commande).
"""

import contextlib
import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies

logger = logging.getLogger(__name__)

_CMD_DIR = "/tmp/reachy_care_cmds"
_SEPARATOR = "=" * 60

# Fallback statique (utilisé si ActivityRegistry n'est pas chargeable)
_FALLBACK_INSTRUCTIONS_FILES = {
    "normal":   "instructions.txt",
    "histoire": "instructions_histoire.txt",
    "pro":      "instructions_pro.txt",
    "echecs":   "instructions_echecs.txt",
    "musique":  "instructions_musique.txt",
}


# ---------------------------------------------------------------------------
# Config + Registry helpers
# ---------------------------------------------------------------------------

def _get_config():
    care_path = os.environ.get("REACHY_CARE_PATH", "/home/pollen/reachy_care")
    try:
        if care_path not in sys.path:
            sys.path.insert(0, care_path)
        import config as _rc_config  # noqa: PLC0415
        return _rc_config
    except Exception:
        return None


def _get_registry(cfg=None):
    """Charge ActivityRegistry si disponible. Accepte un config déjà chargé."""
    if cfg is None:
        cfg = _get_config()
    if cfg is None:
        return None
    try:
        from modules.activity_registry import ActivityRegistry
        activities_dir = getattr(cfg, "ACTIVITIES_DIR", None)
        if activities_dir and Path(activities_dir).is_dir():
            return ActivityRegistry(activities_dir)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# IPC write
# ---------------------------------------------------------------------------

def _safe_write_cmd(cmd: dict) -> None:
    """Écriture atomique dans la queue répertoire : tmp → os.replace() → <timestamp_ns>.json."""
    os.makedirs(_CMD_DIR, exist_ok=True)
    dest = os.path.join(_CMD_DIR, f"{time.time_ns()}.json")
    with contextlib.suppress(Exception):
        with tempfile.NamedTemporaryFile("w", dir=_CMD_DIR, delete=False, suffix=".tmp") as f:
            json.dump(cmd, f)
            tmp_path = f.name
        os.replace(tmp_path, dest)


# ---------------------------------------------------------------------------
# Instructions
# ---------------------------------------------------------------------------

def _get_profiles_dir() -> Path | None:
    base = os.environ.get("REACHY_MINI_EXTERNAL_PROFILES_DIRECTORY", "")
    profile = os.environ.get("REACHY_MINI_CUSTOM_PROFILE", "reachy_care")
    if base:
        d = Path(base) / profile
        if d.exists():
            return d
    fallback = Path("/home/pollen/reachy_care/external_profiles/reachy_care")
    return fallback if fallback.exists() else None


def _load_file(profiles_dir: Path, mode: str, registry=None) -> str | None:
    if registry:
        filename = registry.get_instructions_file(mode)
    else:
        filename = _FALLBACK_INSTRUCTIONS_FILES.get(mode)
    if not filename:
        return None
    path = profiles_dir / filename
    if path.exists():
        return path.read_text(encoding="utf-8")
    logger.warning("switch_mode: fichier instructions manquant : %s", path)
    return None


def _build_instructions(mode: str, topic: str = "", registry=None, cfg=None) -> str | None:
    """Construit les instructions fusionnées base + mode-spécifique."""
    profiles_dir = _get_profiles_dir()
    if not profiles_dir:
        logger.error("switch_mode: répertoire de profils introuvable.")
        return None

    mode_txt = _load_file(profiles_dir, mode, registry)
    if not mode_txt:
        return None

    if mode == "normal":
        instructions = mode_txt
    else:
        base = _load_file(profiles_dir, "normal", registry) or ""
        if base:
            instructions = (
                _SEPARATOR
                + "\n## MODE ACTIF : " + mode.upper() + "\n"
                + "Les regles de ce bloc ont PRIORITE ABSOLUE sur tout ce qui suit.\n"
                + _SEPARATOR
                + "\n\n"
                + mode_txt
                + "\n\n"
                + _SEPARATOR
                + "\n## REGLES GENERALES (s appliquent sauf si contredites ci-dessus)\n"
                + _SEPARATOR
                + "\n\n"
                + base
            )
        else:
            instructions = mode_txt

    if mode == "pro" and topic:
        instructions += f"\n\nSujet demandé : {topic}"

    location = getattr(cfg, "LOCATION", "Paris, France") if cfg else "Paris, France"
    timezone_str = getattr(cfg, "TIMEZONE", "Europe/Paris") if cfg else "Europe/Paris"
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(timezone_str)
    except Exception:
        tz = None
    now_str = datetime.now(tz).strftime("%A %d %B %Y, %Hh%M")
    instructions = instructions.replace("{LOCATION}", location)
    instructions = instructions.replace("{DATETIME}", now_str)
    return instructions


# ---------------------------------------------------------------------------
# Handler direct access
# ---------------------------------------------------------------------------

def _get_handler():
    """Récupère le handler OpenAI Realtime depuis le module patché."""
    for mod in sys.modules.values():
        if hasattr(mod, "_reachy_care_handler") and getattr(mod, "_reachy_care_handler") is not None:
            return mod._reachy_care_handler
    return None


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

class SwitchMode(Tool):
    """Change le mode de comportement de Reachy."""

    name = "switch_mode"
    description = (
        "Change le mode de comportement de Reachy. "
        "Utilise 'histoire' pour lire un livre du domaine public, "
        "'musique' pour écouter la musique ambiante avec la personne (identification + réactions), "
        "'pro' pour faire un exposé sur un sujet, "
        "'echecs' pour jouer aux échecs, "
        "'normal' pour revenir à la conversation habituelle."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["normal", "histoire", "pro", "echecs", "musique"],
                "description": "Mode cible.",
            },
            "topic": {
                "type": "string",
                "description": "Sujet pour le mode exposé. Exemple : 'les étoiles', 'la Tour Eiffel'.",
            },
        },
        "required": ["mode"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        mode = (kwargs.get("mode") or "").strip()
        topic = (kwargs.get("topic") or "").strip()

        # Load config and registry once for this call
        cfg = _get_config()
        registry = _get_registry(cfg)

        valid = registry.get_valid_modes() if registry else set(_FALLBACK_INSTRUCTIONS_FILES.keys())
        if mode not in valid:
            return {"error": f"Mode inconnu : {mode}. Modes disponibles : {sorted(valid)}"}

        # 1. Construire les instructions fusionnées
        instructions = _build_instructions(mode, topic, registry=registry, cfg=cfg)

        # 2. Mettre à jour la session DIRECTEMENT (avant de retourner le résultat au LLM)
        #    → le LLM génère sa première réponse avec les nouvelles instructions déjà actives
        if instructions:
            handler = _get_handler()
            if handler and hasattr(handler, "schedule_session_update"):
                try:
                    # cancel la response en cours AVANT le session.update
                    # pour éviter la collision avec la response auto-créée par
                    # semantic_vad sur speech_started pendant les ms du push 32KB
                    # → error "active response in progress" → blanc 14s + besoin de rappeler.
                    if hasattr(handler, "cancel_response"):
                        await handler.cancel_response()
                    await handler.schedule_session_update(instructions)
                    logger.info(
                        "switch_mode: session update direct OK (mode=%s, %d chars)",
                        mode, len(instructions),
                    )
                except Exception as exc:
                    logger.warning("switch_mode: session update direct échoué : %s", exc)
            else:
                logger.warning(
                    "switch_mode: handler non disponible — session update ignoré "
                    "(conv_app pas encore connecté ?)"
                )
        else:
            logger.warning("switch_mode: instructions introuvables pour mode=%s", mode)

        # 2b. Arrêter le groove si on quitte le mode musique
        if mode != "musique":
            _groove = sys.modules.get("groove")
            if _groove is not None:
                _stop = getattr(_groove, "_groove_stop", None)
                _thread = getattr(_groove, "_groove_thread", None)
                if _stop is not None:
                    _stop.set()
                if _thread is not None and _thread.is_alive():
                    _thread.join(timeout=1)
                if _stop is not None:
                    _stop.clear()
                    setattr(_groove, "_groove_thread", None)
                logger.info("switch_mode: groove arrêté (mode=%s)", mode)

        # 3. Écrire la commande pour main.py (mise à jour de l'état interne Reachy Care)
        cmd: dict[str, Any] = {"cmd": "switch_mode", "mode": mode}
        if topic:
            cmd["topic"] = topic
        try:
            _safe_write_cmd(cmd)
            logger.info("switch_mode: commande main.py écrite → mode=%s", mode)
        except Exception as exc:
            logger.warning("switch_mode: impossible d'écrire la commande : %s", exc)

        return {"status": "ok", "mode": mode}
