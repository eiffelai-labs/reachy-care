"""
gutenberg.py — Récupération de textes du domaine public pour le mode histoires.

Chaîne de sources (par ordre de qualité) :
  1. Wikisource FR   — textes transcrits à la main, qualité maximale.
                       Utilise action=parse (seule façon d'obtenir le texte réel
                       car les chapitres sont stockés via transclusion PDF).
                       Romans multi-chapitres + pièces de théâtre page unique.
  2. Project Gutenberg — catalogue immense, filtre langue FR pour les auteurs français.

Note : Gallica BnF est inaccessible sans navigateur réel (Altcha anti-bot, 403).

Aucune clé API requise.
"""

import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

import requests

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies

logger = logging.getLogger(__name__)

# Regex : noms de personnages en MAJUSCULES au début de ligne, suivis de ". —" ou "." ou " :"
# Ex: "PHÈDRE. —", "ŒNONE.", "THÉSÉE :", "HIPPOLYTE. —"
_THEATRE_NAME_RE = re.compile(
    r"^\s*[A-ZÀÂÉÊÈËÏÎÔÙÛÜÇ][A-ZÀÂÉÊÈËÏÎÔÙÛÜÇ\s\-']{1,30}\s*[\.\:]\s*[—–-]?\s*",
    re.MULTILINE,
)

_GUTENDEX_URL       = "https://gutendex.com/books/"
_GUTENBERG_TEXT_URL = "https://www.gutenberg.org/files/{id}/{id}-0.txt"
_DEFAULT_MAX_CHARS  = 500   # chunk-size-tuning : 4000 was too large, the LLM would narrate the whole excerpt as a single 160s TTS response that the user could not interrupt. 500 chars is ~15-25s of speech, the auto-continue tool call happens quickly, the wake word cuts in within 5s, and the reading still sounds continuous to the ear.
_HEADER_SKIP        = 500   # skip boilerplate Gutenberg (~500 chars)

# Auteurs français classiques → recherche Gutenberg avec filtre langue FR
_GUTENBERG_FR_AUTHORS = {
    "molière", "moliere", "racine", "corneille", "hugo", "zola", "flaubert",
    "baudelaire", "maupassant", "balzac", "stendhal", "dumas", "musset",
    "rostand", "la fontaine", "perrault", "voltaire", "rousseau", "sand",
    "verne", "verlaine", "rimbaud", "daudet", "mérimée", "nerval",
}

# Fichier de session — contient current_person (écrit par main.py)
_SESSION_FILE = Path("/tmp/reachy_session_memory.json")

# Dossier des mémoires persistantes
try:
    sys.path.insert(0, "/home/pollen/reachy_care")
    import config as _cfg
    _KNOWN_FACES_DIR = _cfg.KNOWN_FACES_DIR
except Exception:
    _KNOWN_FACES_DIR = Path("/home/pollen/reachy_care/known_faces")


class GutenbergFetch(Tool):
    """Récupère un extrait d'un livre du domaine public (Wikisource FR / Project Gutenberg)."""

    name = "gutenberg"
    description = (
        "Récupère un extrait d'un livre du domaine public pour le mode histoires. "
        "Cherche sur Wikisource FR en premier (romans par chapitres, pièces de théâtre), "
        "puis sur Project Gutenberg (filtre français pour les auteurs FR). "
        "Cherche par auteur ou titre. "
        "Exemples : 'Molière Tartuffe', 'Dumas Les Trois Mousquetaires', 'Verne 20000 lieues'."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Auteur ou titre à chercher. Exemple : 'Molière Tartuffe', 'Hugo Les Misérables', 'La Fontaine fables'.",
            },
            "book_id": {
                "type": "integer",
                "description": "ID Gutenberg si déjà connu (évite la recherche).",
            },
            "chapter_page": {
                "type": "string",
                "description": (
                    "Titre exact de la page Wikisource du chapitre à lire "
                    "(ex: 'Les Trois Mousquetaires/Chapitre 3'). "
                    "Passe la valeur 'next_chapter' renvoyée par l'appel précédent pour avancer au chapitre suivant."
                ),
            },
            "offset": {
                "type": "integer",
                "description": (
                    "Position de départ dans le texte (en caractères). "
                    "Passe la valeur 'next_offset' renvoyée par l'appel précédent pour continuer dans le même chapitre."
                ),
            },
            "resume": {
                "type": "boolean",
                "description": (
                    "Si True, reprend la lecture là où la personne s'était arrêtée "
                    "(sauvegardé automatiquement à chaque appel). "
                    "Utilise quand quelqu'un dit 'continue', 'reprends', 'où en étais-je'."
                ),
            },
        },
    }

    # ------------------------------------------------------------------
    # Helpers progression lecture
    # ------------------------------------------------------------------

    def _current_person(self) -> str | None:
        """Lit la personne courante depuis le fichier de session."""
        try:
            data = json.loads(_SESSION_FILE.read_text(encoding="utf-8"))
            return data.get("current_person") or data.get("person")
        except Exception:
            return None

    def _load_progress(self, person: str) -> dict | None:
        try:
            from modules.memory_manager import MemoryManager
            mm = MemoryManager(str(_KNOWN_FACES_DIR))
            return mm.get_reading_progress(person)
        except Exception as exc:
            logger.debug("gutenberg: chargement progression échoué : %s", exc)
            return None

    def _save_progress(self, person: str, result: dict) -> None:
        try:
            from modules.memory_manager import MemoryManager
            mm = MemoryManager(str(_KNOWN_FACES_DIR))
            # chapter_hint : prochain chapitre pour les romans Wikisource,
            # chapitre courant pour les autres (l'offset suffit à reprendre).
            chapter_hint = result.get("next_chapter") or result.get("chapter") or result.get("chapter_hint", "")
            # Reculer de 500 caractères (~30s de lecture TTS) pour que la reprise
            # chevauche un peu le dernier passage lu. Si le wake word interrompt
            # la lecture en plein milieu de l'excerpt, on ne perd rien.
            next_off = result.get("next_offset", 0)
            overlap = 500
            safe_offset = max(next_off - overlap, result.get("offset_used", 0))
            mm.save_reading_progress(
                name=person,
                book_id=result.get("book_id"),
                title=result.get("title", ""),
                authors=result.get("authors", ""),
                offset=safe_offset,
                source=result.get("source", ""),
                chapter_hint=chapter_hint,
            )
        except Exception as exc:
            logger.debug("gutenberg: sauvegarde progression échouée : %s", exc)

    # ------------------------------------------------------------------

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        query        = (kwargs.get("query") or "").strip()
        book_id: int | None = kwargs.get("book_id")
        chapter_page: str | None = kwargs.get("chapter_page")
        resume       = bool(kwargs.get("resume", False))
        max_chars    = int(kwargs.get("max_chars") or _DEFAULT_MAX_CHARS)

        # Identifier la personne courante pour la progression
        person = self._current_person()

        # Si resume=True → charger la progression sauvegardée
        saved_offset  = 0
        saved_chapter: str | None = None
        if resume and person:
            progress = self._load_progress(person)
            if progress:
                book_id       = book_id       or progress.get("book_id")
                query         = query         or progress.get("title", "")
                saved_offset  = progress.get("offset", 0)
                saved_chapter = progress.get("chapter_hint") or None
                logger.info(
                    "gutenberg: reprise lecture pour %s — '%s' chapter=%r offset=%d",
                    person, progress.get("title", "?"), saved_chapter, saved_offset,
                )
            else:
                return {"error": f"Aucune lecture en cours pour {person}. Dis-moi quel livre tu veux lire."}

        raw_offset   = kwargs.get("offset")
        offset       = int(raw_offset) if raw_offset is not None else saved_offset
        chapter_page = chapter_page or saved_chapter

        # ------------------------------------------------------------------
        # Source 1 — Wikisource FR via WikisourceReader (action=parse)
        # ------------------------------------------------------------------

        def _fetch_wikisource_reader(search_query: str, chapter: str | None) -> dict[str, Any] | None:
            try:
                from modules.wikisource_reader import WikisourceReader
            except ImportError as exc:
                logger.warning("wikisource_reader non disponible : %s", exc)
                return None

            reader = WikisourceReader()

            # Résoudre le titre du livre
            book_title: str | None = None
            if chapter:
                book_title = chapter.split("/")[0]
            else:
                try:
                    results = reader.search(search_query, limit=5)
                    for r in results:
                        if "/" not in r["title"]:
                            book_title = r["title"]
                            break
                    if not book_title and results:
                        book_title = results[0]["title"].split("/")[0]
                except Exception as exc:
                    logger.debug("wikisource search échoué : %s", exc)
                    return None

            if not book_title:
                return None

            try:
                chapters = reader.list_chapters(book_title)
            except Exception as exc:
                logger.debug("wikisource list_chapters échoué pour %r : %s", book_title, exc)
                chapters = []

            if chapters:
                # Livre à chapitres (roman, recueil...)
                if chapter and chapter in chapters:
                    start_idx = chapters.index(chapter)
                elif chapter:
                    # Chapitre mémorisé introuvable (titre changé ?) → reprendre depuis le début
                    logger.warning("wikisource: chapitre %r introuvable, reprise depuis le début", chapter)
                    start_idx = 0
                else:
                    start_idx = 0

                # Cherche le premier chapitre avec du contenu réel (skip TOC/pages vides)
                current_chapter = None
                text = ""
                for idx in range(start_idx, min(start_idx + 5, len(chapters))):
                    candidate = chapters[idx]
                    try:
                        t = reader.get_chapter_text(candidate)
                    except Exception as exc:
                        logger.debug("wikisource: chapitre %r inaccessible : %s", candidate, exc)
                        continue
                    if len(t) >= 200:
                        current_chapter = candidate
                        text = t
                        if idx > start_idx:
                            logger.info("wikisource: chapitres %d-%d vides/TOC, repris à index %d (%r)",
                                        start_idx, idx - 1, idx, candidate)
                        break

                if not current_chapter:
                    logger.debug("wikisource: aucun chapitre lisible trouvé à partir de index %d", start_idx)
                    return None

                start   = max(offset, 0)
                excerpt = _THEATRE_NAME_RE.sub("", text[start: start + max_chars])
                next_off = start + max_chars
                end_of_chapter = next_off >= len(text)

                chapter_short = current_chapter.replace(book_title + "/", "")

                if end_of_chapter:
                    try:
                        nav = reader.get_navigation(current_chapter)
                        next_chapter = nav.get("next")
                    except Exception:
                        next_chapter = None
                    end_of_book = next_chapter is None
                    if end_of_book:
                        cont_hint = "FIN DU LIVRE — annonce la fin chaleureusement."
                    else:
                        nc_short = next_chapter.replace(book_title + "/", "") if next_chapter else ""
                        cont_hint = (
                            f"IMPORTANT : lis ce texte à voix haute IMMÉDIATEMENT, "
                            f"puis appelle gutenberg(chapter_page='{next_chapter}') pour continuer avec '{nc_short}'. "
                            f"NE T'ARRÊTE PAS — c'est une boucle de lecture continue."
                        )
                    return {
                        "title": book_title,
                        "authors": "",
                        "chapter": current_chapter,
                        "chapter_short": chapter_short,
                        "excerpt": excerpt,
                        "offset_used": start,
                        "next_offset": 0,
                        "next_chapter": next_chapter,
                        "end_of_book": end_of_book,
                        "source": "wikisource_fr",
                        "continuation_hint": cont_hint,
                    }
                else:
                    cont_hint = (
                        f"IMPORTANT : lis ce texte à voix haute IMMÉDIATEMENT, "
                        f"puis appelle gutenberg(chapter_page='{current_chapter}', offset={next_off}) pour la suite de '{chapter_short}'. "
                        f"NE T'ARRÊTE PAS — c'est une boucle de lecture continue."
                    )
                    return {
                        "title": book_title,
                        "authors": "",
                        "chapter": current_chapter,
                        "chapter_short": chapter_short,
                        "excerpt": excerpt,
                        "offset_used": start,
                        "next_offset": next_off,
                        "next_chapter": current_chapter,
                        "end_of_book": False,
                        "source": "wikisource_fr",
                        "continuation_hint": cont_hint,
                    }

            else:
                # Œuvre page unique (pièce de théâtre, poème, etc.)
                try:
                    text = reader.get_chapter_text(book_title)
                except Exception as exc:
                    logger.debug("wikisource get_chapter_text (page unique) échoué pour %r : %s", book_title, exc)
                    return None

                if len(text) < 200:
                    return None

                start   = max(offset, 0)
                excerpt = _THEATRE_NAME_RE.sub("", text[start: start + max_chars])
                next_off = start + max_chars
                end = next_off >= len(text)
                return {
                    "title": book_title,
                    "authors": "",
                    "excerpt": excerpt,
                    "offset_used": start,
                    "next_offset": next_off,
                    "end_of_book": end,
                    "source": "wikisource_fr",
                    "continuation_hint": (
                        "FIN DU LIVRE — annonce la fin chaleureusement." if end else
                        f"IMPORTANT : lis ce texte à voix haute IMMÉDIATEMENT, "
                        f"puis appelle gutenberg(query='{search_query}', offset={next_off}) pour la suite. "
                        f"NE T'ARRÊTE PAS — c'est une boucle de lecture continue."
                    ),
                }

        # ------------------------------------------------------------------
        # Source 2 — Project Gutenberg (Gutendex + texte brut)
        # ------------------------------------------------------------------

        def _fetch_gutenberg(search_query: str, lang_filter: str | None = None) -> dict[str, Any] | None:
            nonlocal book_id
            # Préserver le titre sauvegardé pour éviter de l'écraser avec "" lors d'un resume
            title = query or ""
            authors = ""
            # Recherche Gutendex seulement si book_id inconnu — évite d'écraser un book_id connu
            if not book_id:
                try:
                    params: dict = {"search": search_query}
                    if lang_filter:
                        params["languages"] = lang_filter
                    resp = requests.get(_GUTENDEX_URL, params=params, timeout=10)
                    resp.raise_for_status()
                    results = resp.json().get("results", [])
                    if not results:
                        return None
                    book    = results[0]
                    book_id = book["id"]
                    title   = book.get("title", "")
                    authors = ", ".join(a.get("name", "") for a in book.get("authors", []))
                except Exception as exc:
                    logger.debug("gutendex: recherche échouée : %s", exc)
                    return None
            if not book_id:
                return None

            raw = None
            for url in [
                _GUTENBERG_TEXT_URL.format(id=book_id),
                f"https://www.gutenberg.org/cache/epub/{book_id}/pg{book_id}.txt",
                f"https://www.gutenberg.org/ebooks/{book_id}.txt.utf-8",
            ]:
                try:
                    r = requests.get(url, timeout=15)
                    r.raise_for_status()
                    raw = r.text
                    break
                except Exception:
                    continue
            if raw is None:
                logger.debug("gutenberg: texte introuvable pour id=%s (3 URLs testées)", book_id)
                return None

            start = max(offset, _HEADER_SKIP)
            excerpt = _THEATRE_NAME_RE.sub("", raw[start: start + max_chars])
            end = (start + max_chars) >= len(raw)
            next_off = start + max_chars
            return {
                "book_id": book_id,
                "title": title,
                "authors": authors,
                "excerpt": excerpt,
                "offset_used": start,
                "next_offset": next_off,
                "end_of_book": end,
                "source": "gutenberg",
                "continuation_hint": (
                    "FIN DU LIVRE — annonce la fin chaleureusement." if end else
                    f"IMPORTANT : lis ce texte à voix haute IMMÉDIATEMENT, "
                    f"puis appelle gutenberg(book_id={book_id}, offset={next_off}) pour la suite. "
                    f"NE T'ARRÊTE PAS — c'est une boucle de lecture continue."
                ),
            }

        # ------------------------------------------------------------------
        # Orchestration
        # ------------------------------------------------------------------

        def _fetch() -> dict[str, Any]:
            # Cas direct : book_id Gutenberg fourni
            if book_id:
                result = _fetch_gutenberg(query or "")
                return result or {"error": f"Livre Gutenberg id={book_id} introuvable."}

            if not query and not chapter_page:
                return {"error": "Précise un auteur ou un titre à chercher."}

            # 1. Wikisource FR — romans à chapitres ET pièces de théâtre
            result = _fetch_wikisource_reader(query, chapter_page)
            if result:
                return result

            if not query:
                return {"error": "Aucun texte trouvé. Précise un auteur ou un titre."}

            # 2. Gutenberg avec filtre langue FR pour les auteurs français classiques
            is_fr_author = any(a in query.lower() for a in _GUTENBERG_FR_AUTHORS)
            result = _fetch_gutenberg(query, lang_filter="fr" if is_fr_author else None)
            if result:
                return result

            # 3. Gutenberg sans filtre (dernier recours)
            if is_fr_author:
                result = _fetch_gutenberg(query)
                if result:
                    return result

            return {"error": f"Aucun texte trouvé pour : {query}"}

        try:
            result = await asyncio.get_running_loop().run_in_executor(None, _fetch)
            if "error" not in result:
                logger.info(
                    "gutenberg tool: '%s' récupéré via %s (%d chars).",
                    result.get("title", query),
                    result.get("source", "?"),
                    len(result.get("excerpt", "")),
                )
                if person:
                    if result.get("end_of_book"):
                        try:
                            from modules.memory_manager import MemoryManager
                            MemoryManager(str(_KNOWN_FACES_DIR)).clear_reading_progress(person)
                        except Exception as exc:
                            logger.debug("gutenberg: effacement progression échoué : %s", exc)
                    else:
                        self._save_progress(person, result)
            return result
        except requests.Timeout:
            return {"error": "La récupération du livre a pris trop de temps."}
        except requests.HTTPError as exc:
            return {"error": f"Erreur HTTP {exc.response.status_code} pour le livre."}
        except Exception as exc:
            logger.error("gutenberg tool: erreur : %s", exc)
            return {"error": "Impossible de récupérer le livre."}
