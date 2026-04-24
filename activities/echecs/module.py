"""
activities/echecs/module.py — Module d'activité échecs pour Reachy Care.

Gère :
- L'état complet de la partie (board, game_state, scores, etc.)
- Les commandes IPC chess_human_move / chess_reset (mode LLM tools)
- La persistance FEN sur disque
"""

import chess
import contextlib
import json
import logging
import os
import tempfile
import time

import config
from modules.activity_base import ActivityModule
from modules.chess_parser import san_to_french

try:
    from reachy_mini.utils import create_head_pose
except ImportError:
    create_head_pose = None  # type: ignore[misc, assignment]

logger = logging.getLogger(__name__)

# ── Fichiers temporaires ─────────────────────────────────────────────
_CHESS_STATE_FILE = "/tmp/reachy_chess_state.json"
_CHESS_RESPONSE_FILE = "/tmp/reachy_chess_response.json"


class ChessActivityModule(ActivityModule):
    """Module d'activité échecs — gère board, Stockfish, commandes IPC."""

    name = "echecs"

    def __init__(
        self,
        chess_engine,
        sound_detector=None,
        tts=None,
        bridge=None,
        mini=None,
        mode_manager=None,
        journal=None,
    ):
        self._chess_engine = chess_engine
        self._sound_detector = sound_detector
        self._tts = tts
        self._bridge = bridge
        self._mini = mini
        self._mode_manager = mode_manager
        self._journal = journal

        # ── État de la partie ────────────────────────────────────────
        self.board = chess.Board()
        self.game_state = "idle"  # "idle" | "human_turn" | "reachy_turn"
        self.reachy_color = None  # chess.BLACK or chess.WHITE
        self.move_count = 0
        self.last_stable_fen = None
        self.last_move_san = None
        self.last_move_time = 0.0
        self.wins = 0
        self.losses = 0

        # ── Personne courante (pour journal) ─────────────────────────
        self._current_person = None

    # ------------------------------------------------------------------
    # API publique : set_person (appelé par main.py)
    # ------------------------------------------------------------------

    def set_person(self, name: str | None) -> None:
        """Met à jour le nom de la personne courante (pour le journal)."""
        self._current_person = name

    # ------------------------------------------------------------------
    # ActivityModule interface
    # ------------------------------------------------------------------

    def on_enter(self, context: str = "") -> None:
        """Démarre ou restaure une partie d'échecs."""
        self.start_game()

    def on_exit(self) -> None:
        """Quitte le mode échecs : reset l'état, relève la tête."""
        self._reset_state(raise_head=True)

    def tick(self) -> None:
        """Appelé à chaque frame par main.py — sans-op (LLM tools mode)."""

    def handle_command(self, cmd: dict) -> bool:
        """Traite les commandes IPC chess_human_move et chess_reset.

        Retourne True si la commande a été traitée.
        """
        command = cmd.get("cmd", "")

        if command == "chess_human_move":
            self._handle_chess_human_move(cmd)
            return True

        if command == "chess_reset":
            self._handle_chess_reset()
            return True

        return False

    # ------------------------------------------------------------------
    # Démarrage de partie
    # ------------------------------------------------------------------

    def start_game(self) -> None:
        """Initialise une nouvelle partie (ou restaure une sauvegardée)."""
        if self._chess_engine is None:
            logger.warning("start_game: module chess inactif — partie non lancée.")
            if self._bridge:
                self._bridge.send_event(
                    "[Reachy Care] Erreur : impossible de lancer la partie (Stockfish introuvable).",
                    instructions="Explique que le module d'échecs n'est pas disponible sur ce robot. 1 phrase.",
                )
            return

        # Tenter de restaurer une partie en cours
        saved = None
        try:
            with open(_CHESS_STATE_FILE) as f:
                saved = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

        self._reset_state(raise_head=False)

        if saved and saved.get("fen"):
            try:
                self.board = chess.Board(saved["fen"])
                self.move_count = saved.get("move_count", 0)
                # Reconstruire le move_stack depuis l'historique sauvegardé
                history = saved.get("history", [])
                if history:
                    replay_board = chess.Board()
                    for entry in history:
                        try:
                            m = replay_board.parse_uci(entry["uci"])
                            replay_board.push(m)
                        except Exception:
                            break
                    if replay_board.fen() == saved["fen"]:
                        self.board = replay_board
                        logger.info(
                            "Chess: partie restaurée avec %d coups (FEN=%s)",
                            len(history), saved["fen"],
                        )
                    else:
                        logger.warning("Chess: historique incohérent avec FEN — restauration FEN seule")
                else:
                    logger.info("Chess: partie restaurée (FEN=%s, sans historique)", saved["fen"])
            except ValueError as exc:
                logger.warning("Chess: FEN invalide au chargement : %s", exc)

        self.reachy_color = chess.BLACK
        self.game_state = "human_turn"

        # Baisser la tête pour voir l'échiquier
        if self._bridge:
            self._bridge.set_head_pitch(float(config.HEAD_CHESS_PITCH_DEG))
        if self._mini and create_head_pose is not None:
            try:
                self._mini.goto_target(
                    head=create_head_pose(pitch=config.HEAD_CHESS_PITCH_DEG, degrees=True),
                    duration=1.5,
                )
                logger.info("Tête position échecs (pitch=%d°).", config.HEAD_CHESS_PITCH_DEG)
            except Exception as exc:
                logger.warning("goto_target chess pitch échoué : %s", exc)

        # Informer le LLM de l'état de la partie (nouvelle ou restaurée)
        if self._bridge and self.board.move_stack:
            pgn = self._build_history_pgn()
            turn = "au joueur (Blancs)" if self.board.turn == chess.WHITE else "à Reachy (Noirs)"
            self._bridge.send_event(
                f"[Reachy Care] Partie d'échecs restaurée — {len(self.board.move_stack)} coups joués. "
                f"Historique : {pgn}. C'est {turn} de jouer.",
                instructions="Annonce que tu reprends la partie en cours. Rappelle le dernier coup joué et dis à qui c'est le tour. 1-2 phrases.",
            )
        elif self._bridge:
            self._bridge.send_event(
                "[Reachy Care] Nouvelle partie d'échecs. Board vierge.",
                instructions="Annonce le début d'une nouvelle partie. Demande au joueur son premier coup. 1 phrase.",
            )

        # Niveau Stockfish
        level = config.CHESS_SKILL_LEVEL_INIT
        if self._chess_engine:
            self._chess_engine.set_skill_level(level)

        if self._bridge:
            self._bridge.announce_chess_game_start(
                reachy_color="Noirs",
                skill_label=self._chess_engine.get_skill_label() if self._chess_engine else "débutant",
            )

    # ------------------------------------------------------------------
    # Gestion des commandes IPC
    # ------------------------------------------------------------------

    def _handle_chess_human_move(self, cmd: dict) -> None:
        """Traite la commande chess_human_move (coup soumis par le tool LLM)."""
        move_str = cmd.get("move", "").strip()
        now_mono = time.monotonic()

        # Anti-spam : ignorer si même coup dans les 5 dernières secondes
        if (
            move_str == self.last_move_san
            and now_mono - self.last_move_time < 5.0
        ):
            logger.info("chess_human_move: doublon %s < 5s — ignoré", move_str)
            return

        # Supprimer toute réponse précédente
        with contextlib.suppress(FileNotFoundError):
            os.unlink(_CHESS_RESPONSE_FILE)

        if self._bridge and self._bridge.is_muted():
            self._bridge.unmute()
            logger.info("chess_human_move: bridge démuté automatiquement.")

        if not move_str:
            logger.warning("chess_human_move: champ 'move' manquant.")
            self.write_chess_response({"status": "error", "error": "Coup manquant."})
            return

        if self._chess_engine is None:
            logger.warning("chess_human_move: module chess inactif.")
            self.write_chess_response({"status": "error", "error": "Module échecs inactif (Stockfish introuvable)."})
            return

        if self.game_state == "idle":
            logger.warning("chess_human_move: aucune partie en cours.")
            self.write_chess_response({"status": "error", "error": "Aucune partie en cours. Lance une partie d'abord."})
            return

        if self.board.turn != chess.WHITE:
            logger.warning("chess_human_move: c'est le tour de Reachy — coup ignoré.")
            self.write_chess_response({"status": "error", "error": "C'est le tour de Reachy, pas du joueur."})
            return

        try:
            try:
                move = self.board.parse_uci(move_str)
            except Exception:
                move = self.board.parse_san(move_str)

            if move not in self.board.legal_moves:
                logger.warning("chess_human_move: coup illégal %r.", move_str)
                piece = self.board.piece_at(move.from_square)
                piece_name = piece.symbol().upper() if piece else "?"
                is_pinned = self.board.is_pinned(chess.WHITE, move.from_square)
                reason = (
                    "la pièce est clouée (bouger exposerait ton roi)"
                    if is_pinned
                    else "chemin bloqué ou case illégale pour cette pièce"
                )
                self.write_chess_response({
                    "status": "illegal",
                    "move": move_str,
                    "piece": piece_name,
                    "reason": reason,
                    "fen": self.board.fen(),
                })
            else:
                move_san = self.board.san(move)
                self.board.push(move)
                self.last_stable_fen = self.board.board_fen()
                self.move_count += 1
                self.last_move_san = move_san
                self.last_move_time = time.monotonic()
                self.save_state()
                logger.info("Chess (IPC): joueur → %s", move_san)

                if self.check_game_over():
                    pass  # Fin de partie gérée par check_game_over
                else:
                    self.game_state = "reachy_turn"
                    self.play_reachy_move_sync(move_san)

        except Exception as exc:
            exc_msg = str(exc).lower()
            # python-chess lève "illegal san" quand le coup est parsé mais illegal (ex: clouage)
            if "illegal" in exc_msg:
                logger.warning("chess_human_move: coup illégal %r : %s", move_str, exc)
                reason = "ce coup exposerait ton roi. Coups possibles avec cette pièce : vois sur l'échiquier"
                self.write_chess_response({
                    "status": "illegal",
                    "move": move_str,
                    "reason": reason,
                    "fen": self.board.fen(),
                })
            else:
                logger.warning("chess_human_move: coup invalide %r : %s", move_str, exc)
                self.write_chess_response({
                    "status": "invalid",
                    "move": move_str,
                    "error": str(exc),
                    "fen": self.board.fen(),
                })

    def _handle_chess_reset(self) -> None:
        """Traite la commande chess_reset."""
        if self._chess_engine is None:
            logger.warning("chess_reset ignoré — module chess inactif.")
            return

        self._reset_state()
        self.start_game()

        if self._bridge:
            self._bridge.send_event(
                "[Reachy Care] Partie d'échecs réinitialisée.",
                instructions="Annonce que la partie est remise à zéro et propose de rejouer. 1 phrase.",
            )
        logger.info("Chess: reset manuel demandé — état remis à zéro.")

    # ------------------------------------------------------------------
    # Helpers internes
    # ------------------------------------------------------------------

    def _build_history_pgn(self) -> str:
        """Retourne l'historique de la partie courante en notation PGN compacte."""
        parts = []
        temp = chess.Board()
        for m in self.board.move_stack:
            san = temp.san(m)
            num = len(parts) // 2 + 1
            parts.append(f"{num}. {san}" if temp.turn == chess.WHITE else f"{num}... {san}")
            temp.push(m)
        return " ".join(parts)

    # ------------------------------------------------------------------
    # Moteur de jeu
    # ------------------------------------------------------------------

    def play_reachy_move_sync(self, human_move_san: str) -> None:
        """Calcule le coup de Reachy et écrit la réponse JSON (pour le tool chess_move)."""
        move = self._chess_engine.best_move(self.board)
        if move is None:
            self.game_state = "human_turn"
            self.write_chess_response({
                "status": "ok",
                "human_move": human_move_san,
                "reachy_move": None,
                "fen": self.board.fen(),
                "move_number": self.move_count,
                "error": "Stockfish n'a pas trouvé de coup.",
            })
            return

        from_sq = chess.square_name(move.from_square)
        to_sq = chess.square_name(move.to_square)
        try:
            move_san = self.board.san(move)
        except Exception:
            move_san = move.uci()

        self.board.push(move)
        self.move_count += 1
        self.save_state()
        logger.info("Chess: Reachy → %s (%s → %s)", move_san, from_sq, to_sq)

        self.game_state = "human_turn"

        # Vérifier fin de partie après le coup de Reachy
        game_over = None
        if self.board.is_checkmate():
            game_over = "échec et mat — Reachy gagne"
        elif self.board.is_stalemate():
            game_over = "pat — partie nulle"
        elif self.board.is_insufficient_material():
            game_over = "matériel insuffisant — partie nulle"

        self.write_chess_response({
            "status": "ok",
            "human_move": human_move_san,
            "reachy_move": move_san,
            "reachy_from": from_sq,
            "reachy_to": to_sq,
            "fen": self.board.fen(),
            "move_number": self.move_count,
            "game_over": game_over,
            "is_check": self.board.is_check(),
            "history_pgn": self._build_history_pgn(),
        })

    def check_game_over(self) -> bool:
        """Vérifie fin de partie. Retourne True si terminée."""
        board = self.board
        if board.is_checkmate():
            winner = "Reachy" if board.turn != self.reachy_color else "le joueur"
            if winner == "Reachy":
                self.wins += 1
                new_level = (
                    min(20, getattr(self._chess_engine, '_skill_level', config.CHESS_SKILL_LEVEL_INIT) + 1)
                    if self._chess_engine else config.CHESS_SKILL_LEVEL_INIT
                )
            else:
                self.losses += 1
                new_level = (
                    max(0, getattr(self._chess_engine, '_skill_level', config.CHESS_SKILL_LEVEL_INIT) - 1)
                    if self._chess_engine else config.CHESS_SKILL_LEVEL_INIT
                )
            if self._chess_engine:
                self._chess_engine.set_skill_level(new_level)
            if self._bridge:
                self._bridge.announce_chess_game_over(
                    winner=winner,
                    reason="échec et mat",
                    new_skill_label=self._chess_engine.get_skill_label() if self._chess_engine else "",
                )
            if self._journal and self._current_person:
                result = "victoire" if winner != "Reachy" else "défaite"
                self._journal.log(self._current_person, "activite", f"Partie d'échecs terminée ({result}, échec et mat)")
            self._reset_state()
            if self._mode_manager:
                self._mode_manager.switch_mode("normal")
            return True

        if board.is_stalemate() or board.is_insufficient_material() or board.is_seventyfive_moves():
            if self._bridge:
                self._bridge.announce_chess_game_over(winner="personne", reason="nulle", new_skill_label="")
            if self._journal and self._current_person:
                self._journal.log(self._current_person, "activite", "Partie d'échecs terminée (nulle)")
            self._reset_state()
            if self._mode_manager:
                self._mode_manager.switch_mode("normal")
            return True

        return False

    # ------------------------------------------------------------------
    # Persistance
    # ------------------------------------------------------------------

    def write_chess_response(self, data: dict) -> None:
        """Écrit la réponse chess dans le fichier JSON (atomique)."""
        try:
            with tempfile.NamedTemporaryFile("w", dir="/tmp", delete=False, suffix=".tmp") as f:
                json.dump(data, f)
                tmp_path = f.name
            os.replace(tmp_path, _CHESS_RESPONSE_FILE)
            logger.info("Chess response écrite : %s", _CHESS_RESPONSE_FILE)
        except Exception as exc:
            logger.error("Chess response écriture échouée : %s", exc)

    def save_state(self) -> None:
        """Sauvegarde le FEN + historique complet des coups pour survivre à un restart."""
        try:
            history = []
            temp_board = chess.Board()
            for move in self.board.move_stack:
                san = temp_board.san(move)
                player = "humain" if temp_board.turn == chess.WHITE else "reachy"
                history.append({"player": player, "san": san, "uci": move.uci()})
                temp_board.push(move)
            state = {
                "fen": self.board.fen(),
                "move_count": self.move_count,
                "history": history,
                "history_pgn": self._build_history_pgn(),
                "reachy_color": "black" if self.reachy_color == chess.BLACK else "white",
            }
            with open(_CHESS_STATE_FILE, "w") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning("save_state échoué : %s", exc)

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def _reset_state(self, raise_head: bool = True) -> None:
        """Remet à zéro l'état complet du module pour une nouvelle partie."""
        self.reachy_color = None
        self.game_state = "idle"

        if raise_head:
            if self._bridge:
                self._bridge.set_head_pitch(0.0)
            if self._mini and create_head_pose is not None:
                try:
                    self._mini.goto_target(
                        head=create_head_pose(pitch=config.HEAD_IDLE_PITCH_DEG, degrees=True),
                        duration=1.5,
                    )
                except Exception:
                    pass

        self.board = chess.Board()
        self.last_stable_fen = None
        self.move_count = 0
        self.last_move_san = None
        self.last_move_time = 0.0

        with contextlib.suppress(FileNotFoundError):
            os.unlink(_CHESS_STATE_FILE)
        logger.info("Chess: état remis à zéro.")
