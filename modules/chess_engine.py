"""
chess_engine.py — Interface Stockfish pour Reachy Care

Dépendances : python-chess
Stockfish   : installé via apt — /usr/games/stockfish
Cible       : Raspberry Pi 4, ARM aarch64
"""

import logging
import os

import chess
import chess.engine

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Chemins Stockfish à tester (dans l'ordre de priorité)
# ---------------------------------------------------------------------------
STOCKFISH_PATHS: list[str] = [
    "/usr/games/stockfish",
    "/usr/local/bin/stockfish",
    "/usr/bin/stockfish",
]


# ---------------------------------------------------------------------------
# Exception personnalisée
# ---------------------------------------------------------------------------

class StockfishNotFoundError(FileNotFoundError):
    """Levée quand aucun exécutable Stockfish n'est trouvé."""

    def __init__(self, paths_tested: list[str]) -> None:
        self.paths_tested = paths_tested
        paths_str = ", ".join(f"'{p}'" for p in paths_tested)
        super().__init__(
            f"Stockfish introuvable. Chemins testés : {paths_str}. "
            "Installer via : sudo apt install stockfish"
        )


# ---------------------------------------------------------------------------
# Classe principale
# ---------------------------------------------------------------------------

class ChessEngine:
    """
    Encapsule un processus Stockfish et expose des méthodes haut-niveau.

    Utilisation directe
    -------------------
    >>> engine = ChessEngine(think_time=1.5)
    >>> board  = chess.Board()           # géré par main.py
    >>> move   = engine.best_move_uci(board)   # "e2e4"
    >>> san    = engine.best_move_san(board)   # "e4"
    >>> score  = engine.evaluate(board)        # centipawns (+ = avantage blancs)
    >>> engine.close()

    Utilisation via context manager
    --------------------------------
    >>> with ChessEngine() as eng:
    ...     move = eng.best_move_uci(board)
    """

    def __init__(
        self,
        stockfish_path: str | None = None,
        think_time: float = 2.0,
    ) -> None:
        """
        stockfish_path : chemin explicite vers l'exécutable Stockfish.
                         None → auto-détection dans STOCKFISH_PATHS.
        think_time     : temps de réflexion par coup (secondes).
        """
        self.think_time = think_time
        self._skill_level = 3
        self._stockfish_path = self._find_stockfish(stockfish_path)
        self._engine: chess.engine.SimpleEngine | None = None
        self._start_engine()
        self.set_skill_level(3)

    # ------------------------------------------------------------------
    # Gestion du processus Stockfish
    # ------------------------------------------------------------------

    @staticmethod
    def _find_stockfish(explicit_path: str | None) -> str:
        """Retourne le chemin vers Stockfish, lève StockfishNotFoundError sinon."""
        candidates = ([explicit_path] + STOCKFISH_PATHS) if explicit_path else STOCKFISH_PATHS
        for path in candidates:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                logger.info("Stockfish trouvé : %s", path)
                return path
        raise StockfishNotFoundError(candidates)

    def _start_engine(self) -> None:
        """Lance le processus Stockfish."""
        logger.info("Démarrage de Stockfish (%s) …", self._stockfish_path)
        self._engine = chess.engine.SimpleEngine.popen_uci(self._stockfish_path)
        logger.info("Stockfish prêt.")

    def _restart_engine(self) -> bool:
        """
        Tente de redémarrer Stockfish après une erreur.
        Retourne True si le redémarrage a réussi, False sinon.
        """
        logger.warning("Tentative de redémarrage de Stockfish …")
        try:
            self._engine = None
            self._start_engine()
            logger.info("Stockfish redémarré avec succès.")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Redémarrage de Stockfish échoué : %s", exc)
            self._engine = None
            return False

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def set_skill_level(self, level: int) -> None:
        """Ajuste le niveau de Stockfish (0=débutant ~800 ELO, 20=expert ~3500 ELO)."""
        level = max(0, min(20, level))
        self._skill_level = level
        try:
            self._engine.configure({"Skill Level": level})
            logger.info("Stockfish Skill Level → %d", level)
        except Exception as exc:
            logger.warning("set_skill_level(%d) : %s", level, exc)

    def get_skill_label(self) -> str:
        """Retourne un label lisible pour le niveau actuel."""
        if self._skill_level <= 3:
            return "débutant"
        elif self._skill_level <= 7:
            return "intermédiaire"
        elif self._skill_level <= 12:
            return "avancé"
        else:
            return "expert"

    def best_move(self, board) -> object:
        """Retourne le meilleur coup pour la position actuelle (chess.Move | None)."""
        import chess as _chess
        try:
            result = self._engine.play(board, _chess.engine.Limit(time=self.think_time))
            return result.move
        except Exception as exc:
            logger.warning("best_move: %s", exc)
            return None

    def best_move_uci(self, board: chess.Board) -> str | None:
        """Retourne le meilleur coup en notation UCI (ex. "e2e4"), ou None."""
        move = self._get_best_move(board)
        return move.uci() if move is not None else None

    def best_move_san(self, board: chess.Board) -> str | None:
        """Retourne le meilleur coup en notation SAN (ex. "e4", "O-O"), ou None."""
        move = self._get_best_move(board)
        if move is None:
            return None
        try:
            return board.san(move)
        except Exception as exc:  # noqa: BLE001
            logger.error("Impossible de convertir le coup en SAN : %s", exc)
            return move.uci()

    def evaluate(self, board: chess.Board) -> int:
        """
        Évalue la position courante du point de vue des blancs.

        Retourne un score en centipions (centipawns) :
          - Positif  → avantage blancs
          - Négatif  → avantage noirs
          - ±30 000  → mat (convention interne)

        Retourne 0 en cas d'erreur.
        """
        if self._engine is None:
            logger.warning("evaluate : moteur non disponible.")
            return 0

        limit = chess.engine.Limit(time=self.think_time)
        try:
            info = self._engine.analyse(board, limit)  # type: ignore[union-attr]
        except chess.engine.EngineTerminatedError:
            logger.warning("evaluate : moteur terminé, tentative de redémarrage …")
            if not self._restart_engine():
                return 0
            try:
                info = self._engine.analyse(board, limit)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                logger.error("evaluate après redémarrage : %s", exc)
                return 0
        except Exception as exc:  # noqa: BLE001
            logger.error("evaluate : erreur d'analyse : %s", exc)
            return 0

        score = info.get("score")
        if score is None:
            return 0

        # Normaliser en centipions (point de vue blancs)
        pov_score = score.white()
        if pov_score.is_mate():
            # Mat : +30 000 si blancs gagnent, -30 000 si noirs gagnent
            mate_in = pov_score.mate()
            return 30_000 if mate_in > 0 else -30_000
        return int(pov_score.score())  # type: ignore[arg-type]

    def close(self) -> None:
        """Ferme proprement le processus Stockfish."""
        if self._engine is not None:
            try:
                self._engine.quit()
                logger.info("Stockfish arrêté proprement.")
            except Exception as exc:  # noqa: BLE001
                logger.debug("close() : exception lors de l'arrêt : %s", exc)
            finally:
                self._engine = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "ChessEngine":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Helpers internes
    # ------------------------------------------------------------------

    def evaluate_with_best_reply(self, board: chess.Board) -> dict:
        """
        Single Stockfish analysis: returns score AND best reply in one call.

        Returns a dict with keys:
            score_cp      (int | None)  — centipawn score from the side to move's
                                          perspective; None when mate is on the board.
            best_move_san (str | None)  — best reply in SAN notation.
            mate_in       (int | None)  — moves until forced mate (positive = winning
                                          for the side to move); None when no forced mate.
        """
        import chess as _chess
        try:
            info = self._engine.analyse(
                board,
                _chess.engine.Limit(time=self.think_time),
            )
            score = info.get("score")
            score_cp = None
            mate_in = None
            if score is not None:
                pov = score.white() if board.turn == _chess.WHITE else score.black()
                if pov.is_mate():
                    mate_in = pov.mate()
                else:
                    score_cp = pov.score()

            best_move_san = None
            pv = info.get("pv")
            if pv:
                try:
                    best_move_san = board.san(pv[0])
                except Exception:
                    pass

            return {"score_cp": score_cp, "best_move_san": best_move_san, "mate_in": mate_in}
        except Exception as exc:
            logger.warning("evaluate_with_best_reply: %s", exc)
            return {"score_cp": None, "best_move_san": None, "mate_in": None}

    def _get_best_move(self, board: chess.Board) -> chess.Move | None:
        """Requête interne avec gestion d'erreur et une tentative de redémarrage."""
        if self._engine is None:
            logger.warning("_get_best_move : moteur non disponible.")
            return None

        if board.is_game_over():
            logger.debug("_get_best_move : partie terminée, aucun coup à calculer.")
            return None

        limit = chess.engine.Limit(time=self.think_time)
        try:
            return self._engine.play(board, limit).move
        except chess.engine.EngineTerminatedError:
            logger.warning("_get_best_move : moteur terminé, tentative de redémarrage …")
            if not self._restart_engine():
                return None
            try:
                return self._engine.play(board, limit).move  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                logger.error("_get_best_move après redémarrage : %s", exc)
                return None
        except Exception as exc:  # noqa: BLE001
            logger.error("_get_best_move : erreur inattendue : %s", exc)
            return None
