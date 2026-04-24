"""
activity_registry.py — Registre dynamique des activités/modes pour Reachy Care.

Scanne le répertoire `activities/`, charge les manifest.json,
et fournit des lookups pour les gates, instructions, outils et modes valides.

Le mode "normal" est toujours disponible (pas besoin de manifest).
"""

import json
import logging
from pathlib import Path

import config

logger = logging.getLogger(__name__)

# Gates par défaut (mode normal — tout actif, rien supprimé)
_DEFAULT_GATES = {
    "suppress_face_recognition": False,
    "silent_face_recognition": False,
    "suppress_cry_detection": False,
    "suppress_fall_detection": False,
    "head_pitch_deg": None,
    "wake_word_interrupts_reading": False,
}

# Announce message pour le mode normal (pas dans un manifest)
_NORMAL_ANNOUNCE = (
    "[Reachy Care] MODE NORMAL activé. "
    "Reviens à ta personnalité de compagnon bienveillant habituelle."
)


class ActivityRegistry:
    """Registre des activités chargé depuis activities/*/manifest.json."""

    def __init__(self, activities_dir: str | Path | None = None) -> None:
        self._dir = Path(activities_dir) if activities_dir else getattr(config, "ACTIVITIES_DIR", config.BASE_DIR / "activities")
        self._manifests: dict[str, dict] = {}
        self._scan()

    def _scan(self) -> None:
        """Charge tous les manifest.json trouvés dans activities/."""
        self._manifests.clear()
        if not self._dir.is_dir():
            logger.warning("activity_registry: répertoire introuvable : %s", self._dir)
            return

        for manifest_path in sorted(self._dir.glob("*/manifest.json")):
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                name = data.get("name", "")
                if not name:
                    logger.warning("activity_registry: manifest sans 'name' : %s", manifest_path)
                    continue
                self._manifests[name] = data
                logger.info(
                    "activity_registry: activité '%s' chargée (%s).",
                    name, data.get("display_name", name),
                )
            except (json.JSONDecodeError, OSError) as exc:
                logger.error("activity_registry: erreur chargement %s : %s", manifest_path, exc)

        logger.info("activity_registry: %d activité(s) chargée(s).", len(self._manifests))

    # ------------------------------------------------------------------
    # Lookups publics
    # ------------------------------------------------------------------

    def get_valid_modes(self) -> set[str]:
        """Retourne l'ensemble des modes valides (normal + activités)."""
        return {"normal"} | set(self._manifests.keys())

    def get_mode_list(self) -> list[str]:
        """Liste ordonnée des modes (normal en premier)."""
        return ["normal"] + sorted(self._manifests.keys())

    def get_gates(self, mode: str) -> dict:
        """Retourne les gates pour un mode. Dict vide = mode normal."""
        manifest = self._manifests.get(mode)
        if manifest is None:
            return dict(_DEFAULT_GATES)
        gates = dict(_DEFAULT_GATES)
        gates.update(manifest.get("gates", {}))
        return gates

    def get_instructions_file(self, mode: str) -> str | None:
        """Retourne le nom du fichier d'instructions pour ce mode."""
        if mode == "normal":
            return "instructions.txt"
        manifest = self._manifests.get(mode)
        if manifest is None:
            return None
        return manifest.get("instructions_file")

    def get_announce_message(self, mode: str) -> str:
        """Retourne le message d'annonce pour ce mode."""
        if mode == "normal":
            return _NORMAL_ANNOUNCE
        manifest = self._manifests.get(mode)
        if manifest is None:
            return ""
        return manifest.get("announce_message", "")

    def get_display_name(self, mode: str) -> str:
        """Retourne le nom d'affichage du mode."""
        if mode == "normal":
            return "Normal"
        manifest = self._manifests.get(mode)
        if manifest is None:
            return mode.capitalize()
        return manifest.get("display_name", mode.capitalize())

    def get_tools(self, mode: str) -> list[str]:
        """Retourne la liste des tools spécifiques à ce mode."""
        manifest = self._manifests.get(mode)
        if manifest is None:
            return []
        return manifest.get("tools", [])

    def get_manifest(self, mode: str) -> dict | None:
        """Retourne le manifest complet d'une activité."""
        return self._manifests.get(mode)

    def has_module(self, mode: str) -> bool:
        """Retourne True si l'activité déclare un module Python."""
        manifest = self._manifests.get(mode)
        if manifest is None:
            return False
        return bool(manifest.get("module"))

    def mode_suppresses(self, mode: str, feature: str) -> bool:
        """Raccourci : vérifie si un mode supprime une feature.

        Exemples :
            registry.mode_suppresses("echecs", "face_recognition")  → True
            registry.mode_suppresses("normal", "cry_detection")     → False
        """
        gates = self.get_gates(mode)
        key = f"suppress_{feature}"
        return bool(gates.get(key, False))

    def get_all_manifests(self) -> list[dict]:
        """Retourne tous les manifests chargés (pour le dashboard)."""
        return list(self._manifests.values())

    def rescan(self) -> None:
        """Recharge les manifests (hot-reload)."""
        self._scan()
