"""
search.py — Brave Search tool pour reachy_mini_conversation_app.

Convention : classe héritant de Tool, async __call__ avec deps + kwargs.
Clé BRAVE_API_KEY lue depuis le .env ou l'environnement.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict

import requests

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies

logger = logging.getLogger(__name__)


def _load_brave_api_key() -> str | None:
    key = os.environ.get("BRAVE_API_KEY")
    if key:
        return key
    candidates = [
        Path(__file__).parent.parent / ".env",
        Path("/venvs/apps_venv/lib/python3.12/site-packages/reachy_mini_conversation_app/.env"),
    ]
    for path in candidates:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("BRAVE_API_KEY="):
                    return line.split("=", 1)[1].strip()
        except FileNotFoundError:
            continue
        except Exception as exc:
            logger.warning("search: lecture .env échouée (%s): %s", path, exc)
    return None


_BRAVE_API_KEY: str | None = _load_brave_api_key()


class Search(Tool):
    """Recherche des informations sur internet via Brave Search."""

    name = "search"
    description = (
        "Recherche des informations actuelles sur internet via Brave Search. "
        "Utilise cet outil pour répondre à des questions sur la météo, l'actualité, "
        "les horaires, les recettes ou tout fait récent que tu ne connais pas avec certitude."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Requête courte et précise (3 à 7 mots). "
                    "Exemples : 'météo Paris demain', 'recette tarte aux pommes'."
                ),
            }
        },
        "required": ["query"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> Dict[str, Any]:
        query = (kwargs.get("query") or "").strip()
        if not query:
            return {"error": "query must be a non-empty string"}

        logger.info("Tool call: search query=%s", query[:120])

        if not _BRAVE_API_KEY:
            logger.error("search: BRAVE_API_KEY introuvable.")
            return {"error": "Clé API Brave Search non configurée."}

        def _fetch() -> Dict[str, Any]:
            resp = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={"Accept": "application/json", "X-Subscription-Token": _BRAVE_API_KEY},
                params={"q": query, "count": 5, "search_lang": "fr", "country": "FR", "text_decorations": "false"},
                timeout=8,
            )
            resp.raise_for_status()
            return resp.json()

        try:
            data = await asyncio.get_event_loop().run_in_executor(None, _fetch)
        except requests.Timeout:
            return {"error": "La recherche a pris trop de temps."}
        except requests.HTTPError as exc:
            return {"error": f"Erreur HTTP {exc.response.status_code}."}
        except Exception as exc:
            logger.error("search: erreur pour '%s': %s", query, exc)
            return {"error": "Recherche impossible."}

        results = data.get("web", {}).get("results", [])
        if not results:
            return {"results": f"Aucun résultat pour : {query}."}

        lines = [f"Résultats pour « {query} » :"]
        for i, r in enumerate(results[:5], 1):
            title = r.get("title", "").strip()
            snippet = r.get("description", "").strip()
            if snippet:
                lines.append(f"{i}. {title} — {snippet}")
            else:
                lines.append(f"{i}. {title}")

        return {"results": "\n".join(lines)}
