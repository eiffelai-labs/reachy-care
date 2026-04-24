"""
face_recognizer.py — Module 1A Reachy Care
Reconnaissance faciale via insightface buffalo_s (det_500m + w600k_mbf).
Optimisé pour Raspberry Pi 4 ARM aarch64, sans GPU.
"""

import os
import logging
import numpy as np

logger = logging.getLogger(__name__)


class FaceRecognizer:
    """
    Identifie les visages dans une frame BGR en comparant leurs embeddings
    aux embeddings connus chargés depuis known_faces_dir (.npy).
    """

    def __init__(
        self,
        known_faces_dir: str,
        models_root: str,
        model_name: str = "buffalo_s",
        det_size: tuple = (320, 320),
        threshold: float = 0.40,
        det_score_min: float = 0.70,
    ):
        self.known_faces_dir = known_faces_dir
        self.models_root = models_root
        self.model_name = model_name
        self.det_size = det_size
        self.threshold = threshold
        self.det_score_min = det_score_min

        self._check_models_exist()
        self._app = self._load_app()
        self._known: dict[str, np.ndarray] = {}
        self.reload_known_faces()

    def identify(self, frame: np.ndarray) -> tuple:
        """
        Identifie le visage principal dans la frame.
        Retourne (nom, score) si un visage connu est détecté, (None, score) sinon.
        """
        if frame is None:
            return (None, 0.0)

        faces = self._detect_faces(frame)
        if not faces:
            return (None, 0.0)

        best_face = max(faces, key=lambda f: f.det_score)

        if best_face.det_score < self.det_score_min:
            return (None, 0.0)

        embedding = self._get_normalized_embedding(best_face)
        return self._match(embedding)

    def get_all_faces(self, frame: np.ndarray) -> list:
        """
        Retourne tous les visages détectés avec leur identification.
        Chaque élément : {"name": str|None, "score": float, "bbox": list, "det_score": float}
        """
        if frame is None:
            return []

        faces = self._detect_faces(frame)
        results = []

        for face in faces:
            if face.det_score < self.det_score_min:
                continue

            embedding = self._get_normalized_embedding(face)
            name, score = self._match(embedding)
            bbox = [int(v) for v in face.bbox.tolist()]

            results.append(
                {
                    "name": name,
                    "score": round(float(score), 4),
                    "bbox": bbox,
                    "det_score": round(float(face.det_score), 4),
                }
            )

        return results

    def is_known(self, frame: np.ndarray) -> bool:
        """Retourne True si un visage connu est présent dans la frame."""
        name, _ = self.identify(frame)
        return name is not None

    def reload_known_faces(self):
        """Recharge tous les fichiers .npy depuis known_faces_dir."""
        self._known = {}

        if not os.path.isdir(self.known_faces_dir):
            logger.warning(
                "known_faces_dir introuvable : %s — mode inconnu seulement.",
                self.known_faces_dir,
            )
            return

        npy_files = [f for f in os.listdir(self.known_faces_dir) if f.endswith(".npy") and not f.endswith("_voice.npy")]

        if not npy_files:
            logger.warning(
                "Aucun fichier .npy dans %s — mode inconnu seulement.",
                self.known_faces_dir,
            )
            return

        for fname in npy_files:
            name = os.path.splitext(fname)[0]
            path = os.path.join(self.known_faces_dir, fname)
            try:
                embedding = np.load(path)
                if embedding.ndim == 2:
                    embedding = embedding.mean(axis=0)
                norm = np.linalg.norm(embedding)
                if norm > 0:
                    embedding = embedding / norm
                self._known[name] = embedding.astype(np.float32)
                logger.debug("Embedding chargé : %s (dim=%d)", name, embedding.shape[0])
            except Exception as exc:
                logger.error("Impossible de charger %s : %s", path, exc)

        logger.info(
            "%d personne(s) chargée(s) depuis %s : %s",
            len(self._known),
            self.known_faces_dir,
            list(self._known.keys()),
        )

    def _check_models_exist(self):
        """
        Vérifie que les fichiers ONNX du modèle sont présents sur disque.
        Lève FileNotFoundError si un fichier est manquant, sans déclencher de téléchargement.
        """
        model_dir = os.path.join(self.models_root, self.model_name)

        if not os.path.isdir(model_dir):
            raise FileNotFoundError(
                f"Dossier modèle introuvable : {model_dir}\n"
                f"Téléchargez buffalo_s manuellement et placez-le dans {self.models_root}."
            )

        required_files = ["det_500m.onnx", "w600k_mbf.onnx"]
        for fname in required_files:
            fpath = os.path.join(model_dir, fname)
            if not os.path.isfile(fpath):
                raise FileNotFoundError(
                    f"Fichier modèle manquant : {fpath}\n"
                    f"Assurez-vous que {fname} est présent dans {model_dir}."
                )

        logger.info("Modèles vérifiés dans %s", model_dir)

    def _load_app(self):
        """Instancie FaceAnalysis avec les providers CPU uniquement (Pi 4)."""
        try:
            from insightface.app import FaceAnalysis
        except ImportError as exc:
            raise ImportError(
                "insightface n'est pas installé. "
                "Activez le venv : source /venvs/apps_venv/bin/activate"
            ) from exc

        app = FaceAnalysis(
            name=self.model_name,
            root=self.models_root,
            providers=["CPUExecutionProvider"],
        )
        app.prepare(ctx_id=0, det_size=self.det_size)

        logger.info("FaceAnalysis prêt — modèle=%s, det_size=%s", self.model_name, self.det_size)
        return app

    def _detect_faces(self, frame: np.ndarray) -> list:
        """Lance la détection sur la frame BGR, retourne les objets Face insightface."""
        try:
            return self._app.get(frame) or []
        except Exception as exc:
            logger.error("Erreur lors de la détection : %s", exc)
            return []

    @staticmethod
    def _get_normalized_embedding(face) -> np.ndarray:
        """Retourne l'embedding L2-normalisé d'un objet Face insightface."""
        emb = face.normed_embedding
        if emb is None:
            emb = face.embedding
            norm = np.linalg.norm(emb)
            if norm > 0:
                emb = emb / norm
        return emb.astype(np.float32)

    def identify_nbest(self, frame: np.ndarray, min_score: float = 0.35) -> list[tuple[str, float]]:
        """Retourne les N meilleurs candidats (score >= min_score), triés par score décroissant."""
        if frame is None:
            return []
        faces = self._detect_faces(frame)
        if not faces:
            return []
        best_face = max(faces, key=lambda f: f.det_score)
        if best_face.det_score < self.det_score_min:
            return []
        embedding = self._get_normalized_embedding(best_face)
        return self._match_nbest(embedding, min_score)

    def _match_nbest(self, embedding: np.ndarray, min_score: float) -> list[tuple[str, float]]:
        """Retourne tous les candidats connus au-dessus de min_score, triés par score décroissant."""
        if not self._known:
            return []
        candidates = [
            (name, float(np.dot(embedding, known_emb)))
            for name, known_emb in self._known.items()
        ]
        return sorted(
            [(n, s) for n, s in candidates if s >= min_score],
            key=lambda x: x[1],
            reverse=True,
        )

    def _match(self, embedding: np.ndarray) -> tuple:
        """
        Cherche le meilleur match parmi les embeddings connus par similarité cosinus.
        Retourne (nom, score) si score >= threshold, sinon (None, best_score).
        """
        if not self._known:
            return (None, 0.0)

        best_name = None
        best_score = -1.0

        for name, known_emb in self._known.items():
            score = float(np.dot(embedding, known_emb))
            if score > best_score:
                best_score = score
                best_name = name

        if best_score >= self.threshold:
            return (best_name, best_score)
        return (None, best_score)
