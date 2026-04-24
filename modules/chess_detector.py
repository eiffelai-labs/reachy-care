"""
chess_detector.py — Module 1B : détection de pièces d'échecs via YOLO11n (ONNX)
Projet Reachy Care — Backend 1B

Dépendances : onnxruntime, opencv-python, numpy
Modèle      : yamero999/chess-piece-detection-yolo11n (HuggingFace, best_mobile.onnx ~10.5 MB)
Cible       : Raspberry Pi 4, ARM aarch64, onnxruntime 1.24.2, pas de GPU
"""

import concurrent.futures
import logging
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

logger = logging.getLogger(__name__)

YOLO_TO_FEN: dict[str, str] = {
    "white-king":   "K",
    "white-queen":  "Q",
    "white-rook":   "R",
    "white-bishop": "B",
    "white-knight": "N",
    "white-pawn":   "P",
    "black-king":   "k",
    "black-queen":  "q",
    "black-rook":   "r",
    "black-bishop": "b",
    "black-knight": "n",
    "black-pawn":   "p",
}

HF_REPO_ID = "yamero999/chess-piece-detection-yolo11n"
HF_FILENAME = "best_mobile.onnx"

ONNX_INFERENCE_TIMEOUT = 5.0
NMS_IOU_THRESHOLD = 0.5


class ChessDetector:
    """
    Détecte les pièces d'échecs dans une frame caméra BGR 1280×720.

    Utilisation minimale
    --------------------
    >>> detector = ChessDetector()
    >>> pieces = detector.detect_pieces(frame)       # liste de dicts
    >>> grid   = detector.frame_to_grid(frame)       # dict (col, row) → fen_char
    >>> fen    = detector.grid_to_fen_pieces(grid)   # str partie-pièces du FEN
    """

    CLASS_NAMES = [
        "black-bishop", "black-king", "black-knight", "black-pawn",
        "black-queen", "black-rook", "white-bishop", "white-king",
        "white-knight", "white-pawn", "white-queen", "white-rook",
    ]

    def __init__(
        self,
        model_path: str = "/home/pollen/reachy_care/models/chess_yolo11n.onnx",
        conf_threshold: float = 0.40,
        imgsz: int = 640,
    ) -> None:
        """
        Paramètres
        ----------
        model_path      : chemin local vers le fichier .onnx.
                          Si absent, téléchargement automatique depuis HuggingFace.
        conf_threshold  : seuil de confiance minimum pour garder une détection.
        imgsz           : taille d'entrée YOLO (pixels, carré).
        """
        self.conf_threshold = conf_threshold
        self.imgsz = imgsz
        self._session: ort.InferenceSession | None = None

        self._model_path = self._ensure_model(model_path)
        logger.info("ChessDetector initialisé — modèle : %s", self._model_path)

    # ------------------------------------------------------------------
    # Gestion du modèle
    # ------------------------------------------------------------------

    def _ensure_model(self, model_path: str) -> str:
        """
        Vérifie que le fichier .onnx est présent localement.
        Si absent, le télécharge depuis HuggingFace et le renomme.
        """
        path = Path(model_path)
        if path.exists():
            logger.debug("Modèle trouvé localement : %s", path)
            return str(path)

        path.parent.mkdir(parents=True, exist_ok=True)
        logger.info(
            "Modèle ONNX introuvable à '%s'. Téléchargement depuis HuggingFace (%s)…",
            model_path,
            HF_REPO_ID,
        )
        try:
            from huggingface_hub import hf_hub_download  # import lazy
            downloaded = hf_hub_download(
                repo_id=HF_REPO_ID,
                filename=HF_FILENAME,
                local_dir=str(path.parent),
            )
            Path(downloaded).rename(path)
            logger.info("Modèle téléchargé et enregistré sous : %s", path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Échec du téléchargement du modèle : %s", exc)
        return str(path)

    def _load_session(self) -> None:
        """Charge la session ONNX Runtime une seule fois (lazy init)."""
        if self._session is not None:
            return

        if not Path(self._model_path).exists():
            raise FileNotFoundError(
                f"Fichier modèle ONNX introuvable : {self._model_path}"
            )

        logger.info("Chargement de la session ONNX depuis '%s' …", self._model_path)
        self._session = ort.InferenceSession(
            self._model_path, providers=["CPUExecutionProvider"]
        )
        logger.info("Session ONNX chargée.")

    # ------------------------------------------------------------------
    # Préprocessing
    # ------------------------------------------------------------------

    def _preprocess(self, frame: np.ndarray) -> np.ndarray:
        """
        Prépare la frame pour YOLO11n.

        frame  : BGR uint8, taille quelconque (ex. 1280×720)
        retour : float32 (1, 3, imgsz, imgsz) normalisé [0, 1], RGB
        """
        img = cv2.resize(frame, (self.imgsz, self.imgsz))
        img = img[:, :, ::-1]              # BGR → RGB
        img = img.astype(np.float32) / 255.0
        img = np.transpose(img, (2, 0, 1)) # HWC → CHW
        return np.expand_dims(img, 0)      # → (1, 3, H, W)

    # ------------------------------------------------------------------
    # Inférence et parsing
    # ------------------------------------------------------------------

    def _run_inference(self, frame: np.ndarray) -> list[dict]:
        """Lance l'inférence ONNX sur *frame* et retourne les détections parsées."""
        input_tensor = self._preprocess(frame)
        input_name = self._session.get_inputs()[0].name
        outputs = self._session.run(None, {input_name: input_tensor})
        # outputs[0] shape : (1, 16, 8400) pour YOLO11n (4 bbox + 12 classes)
        return self._parse_outputs(outputs[0], frame.shape)

    def _parse_outputs(self, output: np.ndarray, orig_shape: tuple) -> list[dict]:
        """
        Parse la sortie brute YOLO11n.

        output     : (1, 16, 8400) — 4 coords bbox + 12 scores de classes
        orig_shape : (H, W, C) de la frame originale

        Retourne une liste de dicts après NMS.
        """
        # (1, 16, 8400) → (8400, 16)
        preds = output[0].T

        h_orig, w_orig = orig_shape[:2]
        detections: list[dict] = []

        for pred in preds:
            class_scores = pred[4:]
            class_id = int(np.argmax(class_scores))
            conf = float(class_scores[class_id])
            if conf < self.conf_threshold:
                continue

            cx, cy, w, h = pred[:4]
            cx *= w_orig
            cy *= h_orig
            w  *= w_orig
            h  *= h_orig
            x1 = cx - w / 2
            y1 = cy - h / 2
            x2 = cx + w / 2
            y2 = cy + h / 2

            class_name = (
                self.CLASS_NAMES[class_id]
                if class_id < len(self.CLASS_NAMES)
                else "unknown"
            )
            detections.append({
                "class_name": class_name,
                "fen_char":   YOLO_TO_FEN.get(class_name, "?"),
                "bbox_xyxy":  [x1, y1, x2, y2],
                "conf":       conf,
            })

        return self._nms(detections)

    # ------------------------------------------------------------------
    # NMS (Non-Maximum Suppression)
    # ------------------------------------------------------------------

    @staticmethod
    def _iou(box_a: list[float], box_b: list[float]) -> float:
        """Calcule l'Intersection over Union de deux boîtes [x1, y1, x2, y2]."""
        xa1, ya1, xa2, ya2 = box_a
        xb1, yb1, xb2, yb2 = box_b

        inter_x1 = max(xa1, xb1)
        inter_y1 = max(ya1, yb1)
        inter_x2 = min(xa2, xb2)
        inter_y2 = min(ya2, yb2)

        inter_area = max(0.0, inter_x2 - inter_x1) * max(0.0, inter_y2 - inter_y1)

        area_a = max(0.0, xa2 - xa1) * max(0.0, ya2 - ya1)
        area_b = max(0.0, xb2 - xb1) * max(0.0, yb2 - yb1)
        union_area = area_a + area_b - inter_area

        if union_area < 1e-6:
            return 0.0
        return inter_area / union_area

    def _nms(self, detections: list[dict]) -> list[dict]:
        """
        Applique une NMS greedy par score décroissant.
        Supprime les boîtes dont l'IoU avec une boîte déjà retenue > NMS_IOU_THRESHOLD.
        """
        if not detections:
            return []

        detections = sorted(detections, key=lambda d: d["conf"], reverse=True)

        kept: list[dict] = []
        suppressed = [False] * len(detections)

        for i, det_i in enumerate(detections):
            if suppressed[i]:
                continue
            kept.append(det_i)
            for j in range(i + 1, len(detections)):
                if not suppressed[j]:
                    if self._iou(det_i["bbox_xyxy"], detections[j]["bbox_xyxy"]) > NMS_IOU_THRESHOLD:
                        suppressed[j] = True

        return kept

    # ------------------------------------------------------------------
    # Interface publique — détection brute
    # ------------------------------------------------------------------

    def detect_pieces(self, frame: np.ndarray) -> list[dict]:
        """
        Détecte toutes les pièces dans *frame* (BGR, 1280×720).

        Retourne
        --------
        list[dict] — chaque élément :
            {
                "class_name": str,          # ex. "white-pawn"
                "fen_char":   str,          # ex. "P"
                "bbox_xyxy":  list[float],  # [x1, y1, x2, y2] en pixels
                "conf":       float,        # confiance [0, 1]
            }
        Retourne [] sans lever d'exception si aucune détection ou erreur.
        """
        if frame is None or frame.size == 0:
            logger.warning("detect_pieces : frame vide reçue.")
            return []

        try:
            self._load_session()
        except Exception as exc:  # noqa: BLE001
            logger.error("Impossible de charger la session ONNX : %s", exc)
            return []

        try:
            detections = self._run_with_timeout(frame)
        except Exception as exc:  # noqa: BLE001
            logger.error("Erreur lors de l'inférence ONNX : %s", exc)
            return []

        logger.debug("detect_pieces → %d détection(s)", len(detections))
        return detections

    def _run_with_timeout(self, frame: np.ndarray) -> list[dict]:
        """
        Lance l'inférence ONNX avec un timeout logiciel de ONNX_INFERENCE_TIMEOUT s.

        Implémenté via ThreadPoolExecutor pour rester compatible avec les threads
        non-principaux (signal.alarm non disponible hors thread principal).
        """
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self._run_inference, frame)
            try:
                return future.result(timeout=ONNX_INFERENCE_TIMEOUT)
            except concurrent.futures.TimeoutError:
                logger.warning(
                    "Inférence ONNX dépassement de délai (> %.1f s) — retour []",
                    ONNX_INFERENCE_TIMEOUT,
                )
                return []

    # ------------------------------------------------------------------
    # Mapping sur la grille 8×8
    # ------------------------------------------------------------------

    def frame_to_grid(self, frame: np.ndarray, flip: bool = False) -> "dict[tuple, str] | None":
        """
        Mappe les détections sur une grille 8×8 en se basant uniquement
        sur les positions relatives des pièces détectées (pas de détection
        de coins d'échiquier).

        Algorithme
        ----------
        1. Détecter les pièces avec detect_pieces().
        2. Calculer les centres (cx, cy) de chaque bbox.
        3. Quantifier les positions X en 8 colonnes et Y en 8 rangées
           en utilisant les bornes min/max observées + clustering uniforme.
        4. Attribuer (col, row) ∈ [0,7]² à chaque pièce.

        Paramètres
        ----------
        flip : si True, retourne la grille à 180° (new_row = 7 - row,
               new_col = 7 - col), utile quand la caméra voit l'échiquier
               du côté des noirs.

        Retourne
        --------
        dict[(col, row): fen_char]  — col=0 est la colonne la plus à gauche,
                                      row=0 est la rangée la plus haute (côté noir).
        None si moins de 2 pièces détectées (grille non reconstructible).
        """
        detections = self.detect_pieces(frame)

        if len(detections) < 2:
            logger.debug("frame_to_grid : pas assez de pièces détectées (%d).", len(detections))
            return None

        centers = []
        for det in detections:
            x1, y1, x2, y2 = det["bbox_xyxy"]
            cx = (x1 + x2) / 2.0
            cy = (y1 + y2) / 2.0
            centers.append((cx, cy, det["fen_char"]))

        xs = np.array([c[0] for c in centers])
        ys = np.array([c[1] for c in centers])

        cols = self._quantize_to_grid(xs, n=8)
        rows = self._quantize_to_grid(ys, n=8)

        grid: dict[tuple, str] = {}
        for i, (cx, cy, fen_char) in enumerate(centers):
            key = (cols[i], rows[i])
            # En cas de conflit, garder la première détection
            # (déjà triée par confiance décroissante via NMS).
            if key not in grid:
                grid[key] = fen_char

        if flip:
            grid = {(7 - col, 7 - row): fen_char for (col, row), fen_char in grid.items()}

        logger.debug("frame_to_grid → %d case(s) occupée(s)", len(grid))
        return grid

    @staticmethod
    def _quantize_to_grid(values: np.ndarray, n: int = 8) -> np.ndarray:
        """
        Quantifie un tableau de coordonnées 1-D en indices [0, n-1].

        Méthode : on divise l'intervalle [min, max] en n cases égales
        et on affecte chaque valeur à sa case.  Robustesse : si toutes
        les valeurs sont identiques (1 seule rangée détectée), on retourne
        tout à 0.
        """
        v_min = values.min()
        v_max = values.max()

        if v_max - v_min < 1e-6:
            return np.zeros(len(values), dtype=int)

        normalized = (values - v_min) / (v_max - v_min)  # [0, 1]
        indices = np.floor(normalized * n).astype(int)
        return np.clip(indices, 0, n - 1)

    # ------------------------------------------------------------------
    # Construction du FEN (partie pièces)
    # ------------------------------------------------------------------

    def grid_to_fen_pieces(self, grid: "dict[tuple, str]") -> str:
        """
        Construit la partie pièces d'un FEN à partir d'une grille 8×8.

        La grille est indexée (col, row) avec col ∈ [0,7] (a=0 … h=7)
        et row ∈ [0,7] (row=0 = rangée 8 côté noir dans la vue FEN standard).

        Retourne une chaîne du type :
            "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR"
        """
        rows_fen: list[str] = []

        for row in range(8):
            empty = 0
            row_str = ""
            for col in range(8):
                piece = grid.get((col, row))
                if piece and piece != "?":
                    if empty > 0:
                        row_str += str(empty)
                        empty = 0
                    row_str += piece
                else:
                    empty += 1
            if empty > 0:
                row_str += str(empty)
            rows_fen.append(row_str)

        return "/".join(rows_fen)

    # ------------------------------------------------------------------
    # Détection de coup (comparaison de positions)
    # ------------------------------------------------------------------

    def detect_move(
        self,
        old_fen_pieces: str,
        new_fen_pieces: str,
        board,  # chess.Board
    ):  # chess.Move | None
        """
        Compare two FEN piece-placement strings and find which legal move
        explains the change.

        Both old_fen_pieces and new_fen_pieces must be in the same format
        as grid_to_fen_pieces() and chess.Board.board_fen() — i.e. only
        the piece placement part, e.g.
        "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR".

        Returns the chess.Move if exactly one legal move produces a board
        whose board_fen() matches new_fen_pieces, else None.
        """
        import chess as _chess
        if old_fen_pieces == new_fen_pieces:
            return None
        for move in board.legal_moves:
            test_board = board.copy()
            test_board.push(move)
            # board_fen() returns only the piece placement part — same
            # format as grid_to_fen_pieces(), so the comparison is valid.
            test_fen_pieces = test_board.board_fen()
            if test_fen_pieces == new_fen_pieces:
                return move
        return None
