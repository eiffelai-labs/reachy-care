"""
wikisource_reader.py — Module Reachy Care
Lecture de textes littéraires depuis fr.wikisource.org via l'API MediaWiki.

Usage:
    from modules.wikisource_reader import WikisourceReader
    reader = WikisourceReader()
    chapters = reader.list_chapters("Les Trois Mousquetaires")
    text = reader.get_chapter_text(chapters[0])
    nav = reader.get_navigation(chapters[0])
"""

import re
import html as html_module
import requests
import logging

log = logging.getLogger(__name__)

BASE_URL = "https://fr.wikisource.org/w/api.php"
USER_AGENT = "ReachyCare/1.0 (robot-assistant-senior)"

# Subpage name fragments to exclude from chapter lists
_EXCLUDE = frozenset(["Texte entier", "Table des matières", "homonymie", "Éditions"])


def _natural_sort_key(s: str) -> list:
    """Sort key for natural ordering: 'Chapitre 9' < 'Chapitre 10'."""
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r"(\d+)", s)]


def _clean_html_to_text(raw_html: str) -> str:
    """
    Convert Wikisource rendered HTML to clean plain text for TTS (espeak-ng / OpenAI TTS).

    Pipeline:
      1. Remove <style> / <script> blocks
      2. Seek first <p> tag — skips the Wikisource header (author / title / edition /
         navigation arrows) which always precedes the actual prose
      3. Convert </p> and <br> to newlines for paragraph preservation
      4. Strip all remaining HTML tags WITHOUT adding spaces between them
         (critical: Wikisource drop-caps split "Le" into <span>L</span><span>e</span>;
          replacing tags with spaces produces "L e" instead of "Le")
      5. Decode HTML entities (&amp; &#160; etc.)
      6. Collapse whitespace and normalize newlines

    Returns clean prose suitable for direct TTS submission.
    """
    # 1. Remove CSS/JS
    raw_html = re.sub(r"<style[^>]*>.*?</style>", "", raw_html, flags=re.DOTALL)
    raw_html = re.sub(r"<script[^>]*>.*?</script>", "", raw_html, flags=re.DOTALL)

    # 2. Skip header — start at first <p> tag
    first_p = raw_html.find("<p>")
    if first_p == -1:
        first_p = 0
    raw_html = raw_html[first_p:]

    # 3. Block-level tags → newlines (preserves paragraph boundaries)
    raw_html = re.sub(r"</p>|</h[1-6]>|<br\s*/?>", "\n", raw_html, flags=re.IGNORECASE)

    # 4. Strip all remaining tags — no space replacement
    text = re.sub(r"<[^>]+>", "", raw_html)

    # 5. Decode HTML entities
    text = html_module.unescape(text)

    # 6. Normalize whitespace (including non-breaking space \xa0)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


class WikisourceReader:
    """
    Client for fr.wikisource.org MediaWiki API.

    All methods are synchronous and use a shared requests.Session.
    The session is NOT thread-safe — consistent with the rest of Reachy Care's
    sequential single-threaded architecture.
    """

    def __init__(self, timeout: int = 15):
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})
        self._timeout = timeout

    def _get(self, **params) -> dict:
        params["format"] = "json"
        resp = self._session.get(BASE_URL, params=params, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    # ── 1. RECHERCHE ─────────────────────────────────────────────────────────
    def search(self, query: str, limit: int = 10) -> list[dict]:
        """
        Fulltext search on fr.wikisource.org.

        Uses the MediaWiki search API (action=query&list=search), which supports
        fulltext search across all page content (not just titles).

        Args:
            query: Free-text query, e.g. "Dumas Trois Mousquetaires" or
                   "Victor Hugo Notre-Dame"
            limit: Max results to return (1–50)

        Returns:
            List of {"title": str, "pageid": int, "snippet": str}

        Example:
            results = reader.search("Dumas Trois Mousquetaires", limit=5)
            # → [{"title": "Les Trois Mousquetaires", "pageid": 10787,
            #     "snippet": "Dumas Les Trois Mousquetaires MM. Dufour..."}, ...]
        """
        data = self._get(
            action="query",
            list="search",
            srsearch=query,
            srnamespace=0,
            srlimit=limit,
        )
        return [
            {
                "title": r["title"],
                "pageid": r["pageid"],
                "snippet": re.sub(r"<[^>]+>", "", r.get("snippet", "")),
            }
            for r in data["query"]["search"]
        ]

    # ── 2. LISTE DES CHAPITRES ────────────────────────────────────────────────
    def list_chapters(self, book_title: str) -> list[str]:
        """
        Returns a naturally-ordered list of chapter/section page titles for a work.

        Strategy (two-pass):
          Pass 1 — links from the book's main page: fast, uses the editors'
            intended reading order. Works for flat structures like
            "Les Trois Mousquetaires" (Chapitre 1 … Chapitre 67).
          Pass 2 — allpages prefix search (paginated): exhaustive fallback for
            orphan chapters not linked from the TOC, or when the main page
            doesn't exist at the given title level.

        Both passes exclude utility pages (Texte entier, Table des matières, etc.)
        and apply natural sorting so Chapitre 9 precedes Chapitre 10.

        Nested structures (e.g. Les Misérables) must be called per level:
            reader.list_chapters("Les Misérables/Tome 1/Livre 1")
            # → ["Les Misérables/Tome 1/Livre ", ...]

        Args:
            book_title: Exact Wikisource page title, e.g. "Les Trois Mousquetaires"
                        Note: some titles use Unicode apostrophe U+2019 ('), not ASCII (').
                        Use list_chapters() result titles as-is for get_chapter_text().

        Returns:
            Sorted list of subpage titles (strings).
        """
        prefix = book_title + "/"

        # Pass 1: links from main page (fastest, editor-ordered)
        try:
            data = self._get(action="parse", page=book_title, prop="links")
            if "error" not in data:
                links = data["parse"]["links"]
                subpages = [
                    link["*"]
                    for link in links
                    if link.get("*", "").startswith(prefix)
                    and link.get("ns", -1) == 0
                    and not any(x in link["*"] for x in _EXCLUDE)
                ]
                if subpages:
                    return sorted(subpages, key=_natural_sort_key)
        except Exception as exc:
            log.warning("list_chapters pass1 failed for %r: %s", book_title, exc)

        # Pass 2: allpages prefix search (paginated, exhaustive)
        chapters: list[str] = []
        ap_prefix = book_title.replace(" ", "_")
        params: dict = dict(
            action="query",
            list="allpages",
            apprefix=ap_prefix,
            apnamespace=0,
            aplimit=100,
        )
        while True:
            data = self._get(**params)
            for page in data["query"]["allpages"]:
                t = page["title"]
                if t != book_title and "/" in t and not any(x in t for x in _EXCLUDE):
                    chapters.append(t)
            if "continue" in data:
                params["apcontinue"] = data["continue"]["apcontinue"]
            else:
                break

        return sorted(chapters, key=_natural_sort_key)

    # ── 3. TEXTE D'UN CHAPITRE ────────────────────────────────────────────────
    def get_chapter_text(self, page_title: str) -> str:
        """
        Fetch and return clean plain text of a Wikisource page for TTS.

        Uses action=parse (not action=query&prop=revisions) because Wikisource
        stores chapter text via <pages index="...pdf" from=N to=M /> transclusion
        from scanned PDFs — the raw wikitext is just a one-liner that triggers
        server-side rendering. action=parse performs that rendering and returns
        the full HTML including the actual prose text.

        Args:
            page_title: Exact Wikisource page title.
                        Use titles returned by list_chapters() directly.

        Returns:
            Clean plain text string, ~30 000 chars for a typical Dumas chapter.

        Raises:
            ValueError: if the page does not exist on Wikisource.

        Example:
            text = reader.get_chapter_text("Les Trois Mousquetaires/Chapitre 1")
            # → "Le premier lundi du mois d'avril 1626, le bourg de Meung..."
        """
        data = self._get(action="parse", page=page_title, prop="text")
        if "error" in data:
            raise ValueError(
                f"Page introuvable: {page_title!r} — {data['error']['info']}"
            )
        raw_html = data["parse"]["text"]["*"]
        return _clean_html_to_text(raw_html)

    # ── 4. NAVIGATION CHAPITRE SUIVANT / PRÉCÉDENT ───────────────────────────
    def get_navigation(self, page_title: str) -> dict:
        """
        Returns prev/next/parent navigation for a chapter page.

        Wikisource's standard header template embeds navigation links (◄ prev / next ►)
        as wikilinks on every chapter page. These appear in the page's link list as the
        only same-book sibling pages. The current chapter itself is NOT in its own link
        list, so we determine position by comparing natural sort order.

        Returns:
            {
                "prev": str | None,   # title of previous chapter
                "next": str | None,   # title of next chapter
                "parent": str | None  # title of the book's root page
            }

        Example:
            nav = reader.get_navigation("Les Trois Mousquetaires/Chapitre 10")
            # → {"prev": "Les Trois Mousquetaires/Chapitre 9",
            #    "next": "Les Trois Mousquetaires/Chapitre 11",
            #    "parent": "Les Trois Mousquetaires"}
        """
        data = self._get(action="parse", page=page_title, prop="links")
        if "error" in data:
            return {"prev": None, "next": None, "parent": None}

        book_root = page_title.split("/")[0]
        links_raw = data["parse"]["links"]

        same_book = [
            link["*"]
            for link in links_raw
            if link.get("*", "").startswith(book_root) and link.get("ns", -1) == 0
        ]
        parent = book_root if book_root in same_book else None
        siblings = sorted(
            [s for s in same_book if "/" in s and not any(x in s for x in _EXCLUDE)],
            key=_natural_sort_key,
        )

        # A page doesn't link to itself — find neighbours by sort position
        current_key = _natural_sort_key(page_title)
        prev_title: str | None = None
        next_title: str | None = None

        for sib in siblings:
            sib_key = _natural_sort_key(sib)
            if sib_key < current_key:
                prev_title = sib          # keep updating → will end with closest prev
            elif sib_key > current_key and next_title is None:
                next_title = sib          # first one larger = immediate next

        return {"prev": prev_title, "next": next_title, "parent": parent}

    # ── 5. CATALOGUE D'UN AUTEUR ─────────────────────────────────────────────
    def list_author_works(self, author_name: str) -> list[str]:
        """
        Returns sorted list of work titles from the author's Auteur: namespace page.

        Only returns top-level titles (no "/" in title) to avoid listing sub-chapters
        mixed with full works.

        Args:
            author_name: Author name as it appears on Wikisource, e.g.
                         "Victor Hugo", "Alexandre Dumas", "Molière"

        Returns:
            Sorted list of work title strings (may include some meta-articles).
            Returns [] if the Auteur: page doesn't exist.

        Example:
            reader.list_author_works("Victor Hugo")
            # → ["Angelo, tyran de Padoue", "Bug-Jargal (éditions)",
            #    "Claude Gueux", "Cromwell", "Les Misérables",
            #    "Notre-Dame de Paris", "Quatrevingt-Treize", ...]
        """
        auteur_page = f"Auteur:{author_name}"
        data = self._get(
            action="query",
            titles=auteur_page,
            prop="links",
            pllimit=500,
            plnamespace=0,
        )
        # MediaWiki returns a single-entry dict when querying one title.
        pages = data["query"]["pages"]
        pid, page = next(iter(pages.items()))
        if pid == "-1":
            log.warning("Page auteur introuvable: %s", auteur_page)
            return []
        links = page.get("links", [])
        return sorted(
            link["title"] for link in links if "/" not in link.get("title", "")
        )
