"""
chess_parser.py — Parser de coups d'échecs en français parlé.

Convertit une transcription vocale française (Groq Whisper cloud) en
notation UCI ou SAN exploitable par python-chess / Stockfish.

Exemples d'entrées → sorties :
    "e4"              → "e4"   (SAN pion)
    "pion e4"         → "e4"
    "cavalier e5"     → "Ne5"
    "cheval en f3"    → "Nf3"
    "fou prend c4"    → "Bxc4"
    "dame d4"         → "Qd4"
    "tour a1"         → "Ra1"
    "petit roque"     → "O-O"
    "grand roque"     → "O-O-O"
    "e2 e4"           → "e2e4" (UCI direct)
    "prend en d5"     → "xd5"  (pion capture, désambiguïsé par le board)

Si le texte ne correspond à rien → retourne None (jamais de coup null).
"""

import logging
import re
from typing import NamedTuple, Optional

import chess

logger = logging.getLogger(__name__)


class ParseResult(NamedTuple):
    """Résultat du parsing vocal.

    move: le coup le plus probable (ou None si ambiguïté non résolue)
    alternatives: coups alternatifs (vide avec Groq Whisper — conservé pour compatibilité d'interface)
    """
    move: Optional[chess.Move]
    alternatives: list[chess.Move]

# ── Mapping pièces français → code SAN ──────────────────────────────────────

_PIECE_MAP: dict[str, str] = {
    "roi": "K",
    "rois": "K",
    "dame": "Q",
    "reine": "Q",
    "reines": "Q",
    "tour": "R",
    "tours": "R",
    "roc": "R",
    "rocs": "R",
    "fou": "B",
    "fous": "B",
    "cavalier": "N",
    "cavaliers": "N",
    "cheval": "N",
    "chevaux": "N",
    "pion": "",
    "pions": "",
}

# ── Mots parasites à supprimer ──────────────────────────────────────────────

_NOISE_WORDS = frozenset({
    "je", "joue", "fais", "mon", "ma", "mes", "le", "la", "les", "un",
    "du", "en", "au", "sur", "vers", "à", "dit",
    "case", "cases", "va", "aller", "met", "mets", "place", "deplace",
    "bouge", "avance", "recule", "pose",
    "echec", "check", "mat",
    "alors", "donc", "euh", "bon", "ben", "bah", "voila",
    "c'est", "s'il", "te", "plait",
})

# Regex : deux chiffres isolés ("2 4", "2, 4") — pion sans lettre de colonne
_BARE_NUMBERS_RE = re.compile(r"\b([1-8])\s+([1-8])\b")

# ── Regex patterns ──────────────────────────────────────────────────────────

# Case d'échiquier : lettre a-h + chiffre 1-8
_SQUARE_RE = re.compile(r"\b([a-h])\s*([1-8])\b")

# UCI direct : e2e4, g1f3, etc.
_UCI_RE = re.compile(r"\b([a-h][1-8])\s*([a-h][1-8])\b")

# Promotion : "promotion dame", "promeut en tour", etc.
_PROMO_MAP = {"dame": "q", "reine": "q", "tour": "r", "fou": "b", "cavalier": "n", "cheval": "n"}

# Chiffres en toutes lettres → numériques (le STT peut renvoyer "e quatre"
# au lieu de "e4" si l'utilisateur articule lentement).
_NUMBER_MAP: dict[str, str] = {
    "un": "1", "une": "1",
    "deux": "2", "trois": "3", "quatre": "4",
    "cinq": "5", "six": "6", "sept": "7", "huit": "8",
}

# ── Corrections STT → colonnes d'échiquier ───────────────────────────────────
# Groq Whisper (language=fr + prompt biais échecs) transcrit correctement
# la grande majorité des lettres de colonne. Ce mapping ne conserve que les
# homophones universels du français susceptibles d'apparaître avec n'importe
# quel STT cloud ("et 2 et 4" à la place de "e2 e4", etc.).
_STT_COLUMN_FIX: dict[str, str] = {
    # Colonne A — /a/
    "ah": "a", "ha": "a", "as": "a",
    # Colonne B — /be/
    "be": "b", "bé": "b", "bais": "b", "bay": "b", "baie": "b", "bey": "b",
    # Colonne C — /se/
    "ce": "c", "cé": "c", "se": "c", "ses": "c", "ces": "c",
    "sais": "c", "sait": "c", "say": "c",
    # Colonne D — /de/
    "de": "d", "dé": "d", "des": "d", "dès": "d", "day": "d",
    # Colonne E — /ə/
    "et": "e", "est": "e", "eh": "e", "ai": "e", "hé": "e", "hey": "e",
    # Colonne F — /ɛf/
    "ef": "f", "eph": "f", "eff": "f",
    # Colonne G — /ʒe/
    "ge": "g", "gé": "g", "geai": "g", "jet": "g", "jay": "g",
    # Colonne H — /aʃ/
    "ache": "h", "hache": "h", "ash": "h", "asch": "h",
}


def _normalize_french_text(text: str) -> str:
    """Normalise le texte français : minuscules, accents, ponctuation, chiffres en lettres, homophones STT."""
    raw = text.strip().lower()
    raw = raw.replace("é", "e").replace("è", "e").replace("ê", "e")
    raw = raw.replace("î", "i").replace("ô", "o")
    raw = raw.replace(",", " ").replace(".", " ").replace("!", " ").replace("?", " ")
    # Pré-traitement formes composées AVANT split (apostrophes)
    raw = raw.replace("j'ai", "g").replace("j\u2019ai", "g")
    raw = raw.replace("c'est", " ").replace("c\u2019est", " ")
    raw = raw.replace("l'", " ").replace("l\u2019", " ")
    raw = raw.replace("d'", " ").replace("d\u2019", " ")
    raw = raw.replace("'", " ").replace("\u2019", " ")
    words = raw.split()
    # Ordre critique : convertir chiffres/homophones AVANT le filtrage noise,
    # sinon "de 4" est filtré comme bruit au lieu d'être converti en "d 4" → "d4".
    normalized = []
    for w in words:
        original = w
        w = _NUMBER_MAP.get(w, w)
        w = _STT_COLUMN_FIX.get(w, w)
        # Si le mot a été transformé par _NUMBER_MAP ou _STT_COLUMN_FIX,
        # il porte une information d'échecs — ne PAS le filtrer comme noise.
        if w == original and w in _NOISE_WORDS:
            continue
        normalized.append(w)
    return " ".join(normalized)


def parse_french_move(text: str, board: chess.Board) -> Optional[chess.Move]:
    """Parse un texte français et retourne un chess.Move légal, ou None.

    Tente plusieurs stratégies dans l'ordre :
    1. Roque
    2. UCI direct (e2e4)
    3. SAN avec pièce française
    4. Fuzzy : pièce + case cible → désambiguïsation via legal_moves
    """
    if not text or not text.strip():
        return None

    raw = _normalize_french_text(text)

    logger.debug("chess_parser: raw='%s'", raw)

    # ── 0. Chiffres seuls ("2 4", "2, 4") → pion, deviner la colonne ──────
    move = _try_bare_numbers(raw, board)
    if move is not None:
        return move

    # ── 1. Roque ────────────────────────────────────────────────────────────
    move = _try_castling(raw, board)
    if move is not None:
        return move

    # ── 2. UCI direct (e2e4, g1f3) ─────────────────────────────────────────
    move = _try_uci(raw, board)
    if move is not None:
        return move

    # ── 3. SAN avec pièce française ─────────────────────────────────────────
    move = _try_french_san(raw, board)
    if move is not None:
        return move

    # ── 4. Fuzzy : pièce + case cible ──────────────────────────────────────
    move = _try_fuzzy(raw, board)
    if move is not None:
        return move

    logger.info("chess_parser: aucune interprétation pour '%s'", text.strip())
    return None


def parse_with_alternatives(text: str, board: chess.Board) -> ParseResult:
    """Parse un texte français et retourne le coup + alternatives.

    Interface publique utilisée par ChessActivityModule. Retourne un ParseResult
    avec .move (coup trouvé ou None) et .alternatives (toujours vide — la
    correction phonétique Vosk n'est plus nécessaire avec Groq Whisper cloud).
    """
    move = parse_french_move(text, board)
    return ParseResult(move=move, alternatives=[])


# ── Stratégies de parsing ───────────────────────────────────────────────────


def _try_bare_numbers(raw: str, board: chess.Board) -> Optional[chess.Move]:
    """Gère "2 4" / "2, 4" → pion e2e4.

    Fallback défensif rare avec Groq Whisper (cloud transcrit généralement
    les lettres de colonne correctement), mais conservé au cas où l'utilisateur
    articule uniquement les chiffres ("deux quatre" → rangées sans colonne).
    Essaie toutes les colonnes a-h pour trouver un coup de pion légal
    de la rangée source vers la rangée cible.
    """
    m = _BARE_NUMBERS_RE.search(raw)
    if m is None:
        return None
    r1, r2 = m.group(1), m.group(2)
    for col in "abcdefgh":
        uci = f"{col}{r1}{col}{r2}"
        try:
            move = chess.Move.from_uci(uci)
            if move in board.legal_moves:
                logger.info("chess_parser: bare numbers '%s %s' → %s (pion)", r1, r2, uci)
                return move
        except ValueError:
            continue
    # Essayer aussi les captures diagonales (±1 colonne)
    cols = "abcdefgh"
    for i, col in enumerate(cols):
        for di in (-1, 1):
            j = i + di
            if 0 <= j < 8:
                uci = f"{col}{r1}{cols[j]}{r2}"
                try:
                    move = chess.Move.from_uci(uci)
                    if move in board.legal_moves:
                        logger.info("chess_parser: bare numbers '%s %s' → %s (capture)", r1, r2, uci)
                        return move
                except ValueError:
                    continue
    return None


def _try_castling(raw: str, board: chess.Board) -> Optional[chess.Move]:
    """Détecte petit/grand roque."""
    castling_keywords = {
        "petit roque": "O-O",
        "roque cote roi": "O-O",
        "roque court": "O-O",
        "grand roque": "O-O-O",
        "roque cote dame": "O-O-O",
        "roque long": "O-O-O",
    }
    for phrase, san in castling_keywords.items():
        if phrase in raw:
            try:
                move = board.parse_san(san)
                if move in board.legal_moves:
                    return move
            except Exception:
                pass
    return None


def _try_uci(raw: str, board: chess.Board) -> Optional[chess.Move]:
    """Détecte une notation UCI directe (e2e4, g1f3)."""
    match = _UCI_RE.search(raw)
    if match:
        uci_str = match.group(1) + match.group(2)
        # Vérifier promotion
        promo = _extract_promotion(raw)
        if promo:
            uci_str += promo
        try:
            move = board.parse_uci(uci_str)
            if move in board.legal_moves:
                return move
        except Exception:
            pass
    return None


def _try_french_san(raw: str, board: chess.Board) -> Optional[chess.Move]:
    """Convertit 'cavalier e5', 'fou prend c4' en SAN et valide."""
    words = raw.split()

    # Identifier la pièce
    piece_code = ""
    is_capture = False
    target_sq = None

    for word in words:
        if word in _PIECE_MAP:
            piece_code = _PIECE_MAP[word]
        elif word in ("prend", "prends", "capture", "captures", "mange", "manges",
                      "bouffe", "bouffes", "tape", "tapes", "reprend", "reprends"):
            is_capture = True
        elif word in _NOISE_WORDS:
            continue

    # Trouver la case cible (dernière case mentionnée)
    squares = _SQUARE_RE.findall(raw)
    if not squares:
        return None
    target_sq = squares[-1][0] + squares[-1][1]

    # Construire le SAN
    x = "x" if is_capture else ""
    san_str = f"{piece_code}{x}{target_sq}"

    # Essayer les variantes SAN : tel quel, avec/sans capture
    san_variants = [san_str]
    if not is_capture and piece_code:
        san_variants.append(f"{piece_code}x{target_sq}")
    if is_capture:
        san_variants.append(f"{piece_code}{target_sq}")

    for variant in san_variants:
        try:
            move = board.parse_san(variant)
            if move in board.legal_moves:
                return move
        except Exception:
            pass

    return None


def _try_fuzzy(raw: str, board: chess.Board) -> Optional[chess.Move]:
    """Dernier recours : trouve une pièce et une case cible parmi les coups légaux.

    Gère aussi le cas "pièce + rangée" sans colonne (ex: "roi 1" → Kd1 s'il
    n'y a qu'un seul coup légal du roi vers la rangée 1).
    """
    words = raw.split()

    # Identifier la pièce
    piece_type = None
    for word in words:
        if word in _PIECE_MAP:
            code = _PIECE_MAP[word]
            if code == "":
                piece_type = chess.PAWN
            else:
                piece_type = chess.Piece.from_symbol(code).piece_type
            break

    # Trouver la case cible
    squares = _SQUARE_RE.findall(raw)

    if squares:
        # Cas normal : colonne + rangée trouvées
        target_col = squares[-1][0]
        target_row = squares[-1][1]
        target_sq = chess.parse_square(target_col + target_row)

        candidates = []
        for legal_move in board.legal_moves:
            if legal_move.to_square == target_sq:
                if piece_type is None:
                    candidates.append(legal_move)
                else:
                    piece = board.piece_at(legal_move.from_square)
                    if piece and piece.piece_type == piece_type:
                        candidates.append(legal_move)

        if len(candidates) == 1:
            return candidates[0]

        # Ambiguïté : si deux cases de départ, chercher dans le texte un indice
        if len(candidates) > 1 and len(squares) >= 2:
            from_col = squares[0][0]
            from_row = squares[0][1]
            from_sq = chess.parse_square(from_col + from_row)
            for c in candidates:
                if c.from_square == from_sq:
                    return c

        # Ambiguïté non résolue
        if len(candidates) > 1:
            logger.info(
                "chess_parser: ambiguïté — %d coups possibles vers %s pour '%s'",
                len(candidates), target_col + target_row, raw,
            )
            return None

    # ── Fallback : pièce NOMMÉE + rangée sans colonne ──────────────────
    # Cas où l'utilisateur dit "roi un" sans préciser la colonne — pas de
    # colonne détectée mais on a la pièce et un chiffre isolé.
    # CONSERVATIF : exige que le mot-pièce soit le PREMIER mot significatif.
    if piece_type is not None and piece_type != chess.PAWN:
        # Vérifier que le mot-pièce est en position saillante (premier mot non-bruit)
        first_meaningful = None
        for w in words:
            if w not in _NOISE_WORDS and w not in _NUMBER_MAP:
                first_meaningful = w
                break
        piece_is_salient = first_meaningful is not None and first_meaningful in _PIECE_MAP

        if piece_is_salient:
            rank_match = re.search(r"\b([1-8])\b", raw)
            if rank_match:
                target_rank = int(rank_match.group(1)) - 1  # 0-indexed

                candidates = []
                for legal_move in board.legal_moves:
                    if chess.square_rank(legal_move.to_square) == target_rank:
                        piece = board.piece_at(legal_move.from_square)
                        if piece and piece.piece_type == piece_type:
                            candidates.append(legal_move)

                if len(candidates) == 1:
                    to_name = chess.square_name(candidates[0].to_square)
                    logger.info(
                        "chess_parser: fuzzy rangée — %s vers rangée %d → %s",
                        chess.piece_name(piece_type), target_rank + 1, to_name,
                    )
                    return candidates[0]

                if len(candidates) > 1:
                    logger.info(
                        "chess_parser: fuzzy rangée — %d coups de %s vers rangée %d pour '%s'",
                        len(candidates), chess.piece_name(piece_type),
                        target_rank + 1, raw,
                    )

    return None


def _extract_promotion(raw: str) -> Optional[str]:
    """Extrait la pièce de promotion si mentionnée."""
    has_promo_keyword = "promot" in raw or "promeut" in raw or "promu" in raw
    for keyword, code in _PROMO_MAP.items():
        if keyword in raw and (has_promo_keyword or f"en {keyword}" in raw):
            return code
    return None


# ---------------------------------------------------------------------------
# SAN → français lisible (partagé par ChessActivityModule et chess_move.py)
# ---------------------------------------------------------------------------

_PIECE_NAMES_FR = {
    "K": "roi", "Q": "dame", "R": "tour", "B": "fou", "N": "cavalier",
}


def san_to_french(san: str) -> str:
    """Convertit un coup SAN en français lisible. Ex: Nf3 → 'cavalier en f3'."""
    if san in ("O-O", "0-0"):
        return "petit roque"
    if san in ("O-O-O", "0-0-0"):
        return "grand roque"
    clean = san.rstrip("+#!?")
    if not clean:
        return san
    if clean[0] in _PIECE_NAMES_FR:
        piece = _PIECE_NAMES_FR[clean[0]]
        rest = clean[1:]
        if "x" in rest:
            target = rest.split("x")[-1]
            return f"{piece} prend en {target}"
        return f"{piece} en {rest}"
    if "x" in clean:
        target = clean.split("x")[-1]
        return f"pion prend en {target}"
    return f"pion en {clean}"
