"""
galileo_library.py — Bibliothèque locale : Galileo, Le Lion Blanc (roman d'Alexandre Ferran).

Permet à Reachy de lire les chapitres originaux du roman en mode histoire.
Aucune dépendance externe — lecture filesystem pure.

Actions :
  - list_chapters : retourne la liste des chapitres disponibles (numéro + titre humain).
  - read_chapter   : retourne un extrait de chapitre (max 1 500 chars par appel, ~45 s
                     de lecture orale). La boucle est gérée par le LLM via
                     continuation_hint IMPORTANT / FIN DU CHAPITRE.
"""

import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies

logger = logging.getLogger(__name__)

# Bibliothèque narrative — racine = dossier du repo (via config.BASE_DIR).
try:
    _REPO_ROOT = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(_REPO_ROOT))
    import config as _cfg
    _REPO_ROOT = Path(_cfg.BASE_DIR)
except Exception:
    _REPO_ROOT = Path(__file__).resolve().parent.parent

_LIBRARY_DIR = _REPO_ROOT / "library" / "galileo"

# Taille cible d'un chunk de lecture, en caractères. ~120 s de TTS par chunk,
# cohérent avec le 4096 tokens/réponse max de gpt-realtime (≈4 min audio).
#  : revert 900 → 4000. La baisse à 900 visait les WS timeouts mais
# Fix + Fix + Fix résolvent ça mieux. 4000 = moins d'interruptions
# inter-chunks, moins de dépendance au mécanisme d'auto-continue.
_MAX_CHARS = 4000
# Fenêtre de recherche pour étendre un chunk jusqu'à la prochaine fin de
# phrase ou de paragraphe, afin de ne pas couper en milieu de mot.
_BOUNDARY_WINDOW = 500


def _find_sentence_boundary(text: str, from_idx: int, window: int = _BOUNDARY_WINDOW) -> int:
    """Étend l'offset from_idx jusqu'à la fin de la phrase/paragraphe suivante.
    Priorité : fin de paragraphe (\\n\\n) > fin de phrase (.!? + espace) > newline.
    Si rien dans la fenêtre, retourne from_idx (coupe brute, dernier recours)."""
    if from_idx >= len(text):
        return len(text)
    search_end = min(from_idx + window, len(text))
    chunk = text[from_idx:search_end]
    # 1. Paragraphe (\n\n)
    para = chunk.find("\n\n")
    if para != -1:
        return from_idx + para + 2
    # 2. Fin de phrase :. ! ? suivi d'un espace/newline
    m = re.search(r"[.!?][\"»]?\s", chunk)
    if m:
        return from_idx + m.end()
    # 3. Newline simple
    nl = chunk.find("\n")
    if nl != -1:
        return from_idx + nl + 1
    # 4. Fallback : fin de fenêtre (coupe brute très rare sur texte normal)
    return search_end

#  : progress persistant dans /home/pollen/reachy_care/data/ (plus /tmp/)
# et MULTI-MÉMOIRES : une entrée par chapitre touché, pour que l'utilisateur
# puisse alterner entre plusieurs lectures en cours (Socrate + Convalescence +
# autre chapitre) et reprendre chacune à son offset exact.
#
# Schéma JSON :
# {
#   "last_active": "01-Socrate, le dragon.md",   <- clé du dernier chapitre touché
#   "books": {
#     "01-Socrate, le dragon.md": {
#        "chapter_file": "01-Socrate, le dragon.md",
#        "chapter_title": "Socrate, le dragon",
#        "offset": 20000,                        <- adresse exacte en chars
#        "updated_at": "2026-04-24 17:31:02"
#     },
#     "03:2-Le Comte Émeric.md": { ... }
#   }
# }
#
# - action="resume" sans argument → charge l'entrée pointée par last_active
# - action="resume", chapter="<nom>" → charge l'entrée de ce chapitre si elle existe
# - Chaque read_chapter écrit/met à jour l'entrée correspondante.
_PROGRESS_DIR = Path("/home/pollen/reachy_care/data")
_PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
_PROGRESS_FILE = _PROGRESS_DIR / "galileo_reading_progress.json"


def _load_progress() -> dict:
    """Retourne le dict complet (ou vide si absent). Migre l'ancien schéma flat
    (last_chapter_file/last_offset en racine) vers le nouveau schéma books/{}."""
    try:
        data = json.loads(_PROGRESS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"last_active": None, "books": {}}

    # Migration ancien schéma → nouveau (rétro-compatibilité une seule fois)
    if "books" not in data and "last_chapter_file" in data:
        old_file = data.get("last_chapter_file", "")
        data = {
            "last_active": old_file,
            "books": {
                old_file: {
                    "chapter_file": old_file,
                    "chapter_title": data.get("last_chapter_title", ""),
                    "offset": data.get("last_offset", 0),
                    "updated_at": data.get("updated_at", ""),
                }
            } if old_file else {},
        }
    data.setdefault("last_active", None)
    data.setdefault("books", {})
    return data


_MAX_BOOKS_IN_PROGRESS = 5   # Cap LRU : max 5 lectures en cours. Si un 6ème
                              # chapitre est ajouté, la plus ancienne entrée
                              # (updated_at le plus vieux) est évincée.

# État du chunk en cours (mis à jour par _handle_read à chaque envoi au LLM).
# Permet à save_interruption_point() d'estimer la position réelle quand Reachy
# est coupé par un wake word avant la fin du chunk.
_CHARS_PER_SECOND = 15.0   # Vitesse de lecture TTS estimée (cedar voice français).
_current_reading: dict | None = None   # {chapter_file, chapter_title, start_offset, end_offset, sent_at}


def save_interruption_point() -> dict | None:
    """Appelée quand une interruption (wake word, stop_speaking, etc.) arrête
    Reachy en pleine lecture. Estime combien de chars ont été effectivement
    lus depuis l'envoi du chunk à OpenAI et met à jour progress.json à cette
    position. Retourne le dict sauvegardé ou None si aucun chunk actif.

    Estimation : chars_lus ≈ (now - sent_at) × _CHARS_PER_SECOND,
    cappée par end_offset du chunk (on ne peut pas avoir lu plus que envoyé).
    """
    import time as _t
    if _current_reading is None:
        return None
    elapsed = _t.monotonic() - _current_reading["sent_at"]
    chars_read = int(elapsed * _CHARS_PER_SECOND)
    estimated_offset = min(
        _current_reading["start_offset"] + chars_read,
        _current_reading["end_offset"],
    )
    _save_progress(
        _current_reading["chapter_file"],
        estimated_offset,
        _current_reading["chapter_title"],
    )
    logger.info(
        "galileo_library: interruption saved — %s offset=%d (elapsed=%.1fs, chars≈%d)",
        _current_reading["chapter_file"], estimated_offset, elapsed, chars_read,
    )
    return {
        "chapter_file": _current_reading["chapter_file"],
        "offset": estimated_offset,
        "elapsed_seconds": round(elapsed, 2),
    }


def _save_progress(chapter_file: str, offset: int, chapter_title: str) -> None:
    """Met à jour l'entrée du chapitre + pointe last_active dessus + applique LRU cap."""
    import time as _t
    data = _load_progress()
    data["books"][chapter_file] = {
        "chapter_file": chapter_file,
        "chapter_title": chapter_title,
        "offset": offset,
        "updated_at": _t.strftime("%Y-%m-%d %H:%M:%S"),
    }
    data["last_active"] = chapter_file
    # LRU eviction si > 5 lectures actives
    if len(data["books"]) > _MAX_BOOKS_IN_PROGRESS:
        sorted_by_age = sorted(
            data["books"].items(),
            key=lambda kv: kv[1].get("updated_at", ""),
        )
        data["books"] = dict(sorted_by_age[-_MAX_BOOKS_IN_PROGRESS:])
    _PROGRESS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_resume_entry(chapter: str | None = None) -> dict | None:
    """Retourne l'entrée de progress pour un chapitre donné, ou l'entrée last_active
    si chapter=None. None si rien à reprendre."""
    data = _load_progress()
    books = data.get("books", {})
    if chapter:
        # Match direct par nom de fichier ou fragment de titre
        if chapter in books:
            return books[chapter]
        fragment = chapter.lower()
        for key, entry in books.items():
            if fragment in key.lower() or fragment in entry.get("chapter_title", "").lower():
                return entry
        return None
    # Fallback : last_active
    la = data.get("last_active")
    if la and la in books:
        return books[la]
    return None

# Titres humains associés aux noms de fichiers (nouveau format )
_CHAPTER_TITLES = {
    "00-Prélude.md":             "Prélude",
    "01-Socrate, le dragon.md":  "Socrate, le dragon",
    "02-L’hydre.md":             "L’hydre",
    "03:1-La Convalescence.md":  "Chapitre 3, première partie : La Convalescence",
    "03:2-Le Comte Émeric.md":   "Chapitre 3, deuxième partie : Le Comte Émeric",
    "03:3-Le four.md":           "Chapitre 3, troisième partie : Le four",
}


def _human_title(filename: str) -> str:
    """Retourne le titre humain d'un fichier, ou le déduit du nom si inconnu."""
    if filename in _CHAPTER_TITLES:
        return _CHAPTER_TITLES[filename]
    # Déduction automatique : retirer numéro et extension, remplacer _ par espace
    name = re.sub(r"^\d+_", "", filename)
    name = re.sub(r"\.(txt|md)$", "", name)
    return name.replace("_", " ").title()


def _list_chapters() -> list[dict]:
    """Retourne la liste ordonnée des chapitres disponibles."""
    if not _LIBRARY_DIR.exists():
        return []
    files = sorted(_LIBRARY_DIR.glob("*.txt")) + sorted(_LIBRARY_DIR.glob("*.md"))
    # Trier par nom (l'ordre numérique est dans le nom)
    files = sorted(set(files), key=lambda p: p.name)
    chapters = []
    for idx, f in enumerate(files, start=1):
        chapters.append({
            "number": idx,
            "filename": f.name,
            "title": _human_title(f.name),
        })
    return chapters


def _find_chapter_file(chapter: str) -> Path | None:
    """
    Trouve le fichier correspondant à un identifiant chapitre.
    Accepte : numéro entier (1-7), nom de fichier exact, ou fragment de titre.
    """
    chapters = _list_chapters()
    if not chapters:
        return None

    # Essai par numéro
    try:
        n = int(chapter)
        for ch in chapters:
            if ch["number"] == n:
                return _LIBRARY_DIR / ch["filename"]
    except (ValueError, TypeError):
        pass

    # Essai par nom de fichier exact
    exact = _LIBRARY_DIR / chapter
    if exact.exists():
        return exact

    # Essai par fragment de titre (insensible à la casse)
    fragment = chapter.lower()
    for ch in chapters:
        if fragment in ch["filename"].lower() or fragment in ch["title"].lower():
            return _LIBRARY_DIR / ch["filename"]

    return None


class GalileoLibrary(Tool):
    """Bibliothèque locale du roman Galileo, Le Lion Blanc d'Alexandre Ferran."""

    name = "galileo_library"
    description = (
        "Bibliothèque locale du roman ORIGINAL d'Alexandre Ferran : 'Galileo, Le Lion Blanc'. "
        "Ce n'est pas un texte du domaine public — c'est l'œuvre de l'auteur lui-même. "
        "Action 'list_chapters' : liste les chapitres disponibles. "
        "Action 'read_chapter' : lit un chapitre par numéro (1-7), nom de fichier ou fragment de titre. "
        "Action 'resume' : reprend la lecture exactement là où on s'est arrêté la dernière fois. "
        "Utilise ce tool en mode histoire quand la personne demande Galileo, Le Lion Blanc, "
        "les Chroniques de Bucéphale, ou de reprendre la lecture. "
        "Quand la personne dit 'reprends la lecture' ou 'où on en était', utilise l'action 'resume'."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list_chapters", "read_chapter", "resume"],
                "description": (
                    "'list_chapters' : retourne la liste des chapitres. "
                    "'read_chapter' : lit le contenu d'un chapitre. "
                    "'resume' : reprend la lecture là où on s'est arrêté."
                ),
            },
            "chapter": {
                "type": "string",
                "description": (
                    "Identifiant du chapitre à lire. Accepte : numéro entier (ex: '1', '3'), "
                    "nom de fichier (ex: '02_socrate_le_dragon.txt'), "
                    "ou fragment de titre (ex: 'hydre', 'convalescence'). "
                    "Obligatoire pour l'action 'read_chapter'."
                ),
            },
            "offset": {
                "type": "integer",
                "description": (
                    "Position de départ dans le texte (en caractères). "
                    "Passe la valeur 'next_offset' renvoyée par l'appel précédent "
                    "pour lire la suite d'un chapitre long."
                ),
            },
        },
        "required": ["action"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        action: str = (kwargs.get("action") or "").strip()

        if action == "list_chapters":
            return self._handle_list()

        if action == "resume":
            # Multi-mémoires  : si chapter précisé, on reprend CE chapitre ;
            # sinon on reprend last_active (dernier chapitre touché).
            chapter_hint = (kwargs.get("chapter") or "").strip() or None
            entry = _get_resume_entry(chapter_hint)
            if not entry:
                data = _load_progress()
                n = len(data.get("books", {}))
                if n == 0:
                    return {"error": "Aucune lecture en cours. Utilise 'list_chapters' pour choisir un chapitre."}
                # Plusieurs mémoires mais aucune ne matche le chapter demandé
                titles = [e.get("chapter_title", e.get("chapter_file", "")) for e in data["books"].values()]
                return {"error": f"Aucune lecture en cours pour '{chapter_hint}'. Lectures actives : {titles}. Précise un chapitre ou utilise read_chapter."}
            logger.info("galileo_library: resume → %s offset=%d",
                        entry["chapter_file"], entry["offset"])
            return self._handle_read(entry["chapter_file"], entry["offset"])

        if action == "read_chapter":
            chapter = (kwargs.get("chapter") or "").strip()
            offset = int(kwargs.get("offset") or 0)
            return self._handle_read(chapter, offset)

        return {"error": f"Action inconnue : '{action}'. Utilise 'list_chapters' ou 'read_chapter'."}

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _handle_list(self) -> dict[str, Any]:
        chapters = _list_chapters()
        if not chapters:
            return {
                "error": (
                    "La bibliothèque Galileo est vide ou introuvable "
                    f"({_LIBRARY_DIR}). "
                    "Vérifier le déploiement."
                )
            }
        return {
            "book": "Galileo, Le Lion Blanc",
            "author": "Alexandre Ferran",
            "note": "Roman original — œuvre de l'auteur, pas du domaine public.",
            "chapters": chapters,
        }

    def _handle_read(self, chapter: str, offset: int) -> dict[str, Any]:
        if not chapter:
            return {"error": "Précise le numéro ou le titre du chapitre à lire."}

        path = _find_chapter_file(chapter)
        if path is None:
            return {
                "error": (
                    f"Chapitre '{chapter}' introuvable. "
                    "Appelle galileo_library(action='list_chapters') pour voir la liste."
                )
            }

        try:
            full_text = path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.error("galileo_library: lecture échouée pour %s : %s", path, exc)
            return {"error": f"Impossible de lire le fichier : {exc}"}

        start = max(offset, 0)
        #  : coupe respectueuse des phrases/paragraphes.
        # Au lieu de trancher brutalement à start+_MAX_CHARS (risque au milieu
        # d'un mot ou d'une phrase → Reachy perd le sens), on étend le chunk
        # jusqu'à la prochaine fin de paragraphe ou de phrase (fenêtre _BOUNDARY_WINDOW).
        target_end = start + _MAX_CHARS
        boundary = _find_sentence_boundary(full_text, target_end)
        excerpt = full_text[start:boundary]
        next_off = boundary
        truncated = next_off < len(full_text)

        title = _human_title(path.name)

        # Sauvegarder la progression pour pouvoir reprendre plus tard
        _save_progress(path.name, next_off if truncated else start, title)

        # Tracker le chunk en cours pour save_interruption_point() en cas de wake
        # word qui coupe la lecture avant la fin du chunk.
        import time as _t
        global _current_reading
        _current_reading = {
            "chapter_file": path.name,
            "chapter_title": title,
            "start_offset": start,
            "end_offset": next_off,
            "sent_at": _t.monotonic(),
        }

        if truncated:
            continuation_hint = (
                f"IMPORTANT : lis ce texte à voix haute IMMÉDIATEMENT, "
                f"puis appelle galileo_library(action='read_chapter', chapter='{path.name}', offset={next_off}) "
                f"pour la suite de '{title}'. "
                f"NE T'ARRÊTE PAS — c'est une boucle de lecture continue."
            )
        else:
            continuation_hint = (
                f"FIN DU CHAPITRE '{title}' — "
                "annonce la fin de ce chapitre en une phrase chaleureuse. "
                "Propose de continuer avec le chapitre suivant si la personne le souhaite."
            )

        return {
            "book": "Galileo, Le Lion Blanc",
            "author": "Alexandre Ferran",
            "chapter_file": path.name,
            "chapter_title": title,
            "excerpt": excerpt,
            "offset_used": start,
            "next_offset": next_off if truncated else len(full_text),
            "truncated": truncated,
            "total_chars": len(full_text),
            "source": "galileo_local",
            "continuation_hint": continuation_hint,
        }
