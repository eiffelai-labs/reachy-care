"""
set_reading_voice.py — Ajuste le débit TTS selon le type de personnage.

Cedar reste la voix unique. Seul le débit change pour accompagner
les marqueurs inline [voix grave, souveraine] que cedar interprète nativement.

Mapping personnage → vitesse :
    narrateur  → 1.0  (débit neutre)
    grave      → 0.82 (lent, pesant : Thésée, roi, père)
    doux       → 0.94 (posé, confidentiel : Œnone, confidente)
    feminin    → 1.06 (léger : Phèdre, Aricie)
    jeune      → 1.13 (vif : Hippolyte, jeune héros)
    neutre     → 1.0  (intermédiaire : confident, soldat)
"""

import logging
import sys
from typing import Any

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies

logger = logging.getLogger(__name__)

_SPEED_MAP = {
    "narrateur": 1.0,
    "grave":     0.82,
    "doux":      0.94,
    "feminin":   1.06,
    "jeune":     1.13,
    "neutre":    1.0,
}


def _get_handler():
    for mod in sys.modules.values():
        if hasattr(mod, "_reachy_care_handler") and getattr(mod, "_reachy_care_handler") is not None:
            return mod._reachy_care_handler
    return None


class SetReadingVoice(Tool):
    """Ajuste le débit de cedar avant une réplique en mode histoires."""

    name = "set_reading_voice"
    description = (
        "Ajuste le débit de la voix cedar avant une réplique en mode histoires. "
        "Cedar reste la voix unique — les intonations viennent des marqueurs inline dans le texte. "
        "Valeurs : 'narrateur' (débit neutre), 'grave' (lent : Thésée, roi), "
        "'doux' (posé : Œnone, confidente), 'feminin' (léger : Phèdre, Aricie), "
        "'jeune' (vif : Hippolyte), 'neutre' (intermédiaire)."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "voice": {
                "type": "string",
                "enum": ["narrateur", "grave", "doux", "feminin", "jeune", "neutre"],
                "description": "Type de personnage — détermine le débit de cedar.",
            },
        },
        "required": ["voice"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        style = (kwargs.get("voice") or "narrateur").strip()
        speed = _SPEED_MAP.get(style, 1.0)

        handler = _get_handler()
        if handler is None:
            logger.warning("set_reading_voice: handler non disponible.")
            return {"status": "error", "reason": "handler indisponible"}

        connection = getattr(handler, "connection", None)
        if connection is None:
            logger.warning("set_reading_voice: pas de connexion active.")
            return {"status": "error", "reason": "pas de connexion"}

        try:
            await connection.session.update(session={
                "type": "realtime",
                "audio": {"output": {"speed": speed}},
            })
            logger.info("set_reading_voice: %s → speed=%.2f", style, speed)
            return {"status": "ok", "style": style, "speed": speed}
        except Exception as exc:
            logger.warning("set_reading_voice: session.update échoué : %s", exc)
            return {"status": "error", "reason": str(exc)}
