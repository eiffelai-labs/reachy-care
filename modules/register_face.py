"""
register_face.py — Module 1A Reachy Care
Enrôlement de nouveaux visages : calcul de l'embedding moyen depuis une liste de frames,
sauvegarde du .npy normalisé et mise à jour de registry.json.

Le déclenchement vocal est géré par main.py — ce module est purement logique.
"""

import os
import json
import logging
from datetime import datetime

import numpy as np

logger = logging.getLogger(__name__)


class FaceEnroller:
    """Gère l'enrôlement, la liste et la suppression de personnes connues."""

    MAX_PERSONS = 5

    def __init__(self, face_app, known_faces_dir: str, registry_path: str):
        self.face_app = face_app
        self.known_faces_dir = known_faces_dir
        self.registry_path = registry_path

        os.makedirs(self.known_faces_dir, exist_ok=True)

        if not os.path.isfile(self.registry_path):
            self._write_registry({})
            logger.info("registry.json initialisé : %s", self.registry_path)

    def enroll(self, name: str, frames: list, min_valid: int = 5) -> dict:
        """
        Enrôle une personne à partir d'une liste de frames BGR.
        Retourne un dict avec les clés : success, n_valid, name, message.
        """
        name = name.strip().lower()

        if not name:
            return {
                "success": False,
                "n_valid": 0,
                "name": name,
                "message": "Le nom fourni est vide.",
            }

        registry = self._read_registry()
        is_replacement = name in registry

        if not is_replacement and len(registry) >= self.MAX_PERSONS:
            return {
                "success": False,
                "n_valid": 0,
                "name": name,
                "message": (
                    f"Quota atteint : {self.MAX_PERSONS} personnes maximum. "
                    f"Supprimez une personne avant d'en enrôler une nouvelle."
                ),
            }

        valid_embeddings = []

        for i, frame in enumerate(frames):
            if frame is None:
                logger.debug("Frame %d ignorée (None).", i)
                continue

            try:
                faces = self.face_app.get(frame)
            except Exception as exc:
                logger.warning("Erreur détection frame %d : %s", i, exc)
                continue

            if not faces:
                logger.debug("Frame %d : aucun visage détecté.", i)
                continue

            best = max(faces, key=lambda f: f.det_score)
            emb = best.normed_embedding if best.normed_embedding is not None else best.embedding

            norm = np.linalg.norm(emb)
            if norm == 0:
                logger.debug("Frame %d : embedding nul, ignoré.", i)
                continue

            valid_embeddings.append((emb / norm).astype(np.float32))
            logger.debug("Frame %d OK — det_score=%.3f", i, float(best.det_score))

        n_valid = len(valid_embeddings)

        if n_valid < min_valid:
            return {
                "success": False,
                "n_valid": n_valid,
                "name": name,
                "message": (
                    f"Enrôlement échoué : seulement {n_valid} frame(s) valide(s) "
                    f"sur {len(frames)} ({min_valid} minimum requis). "
                    f"Assurez-vous que le visage est bien visible et bien éclairé."
                ),
            }

        mean_emb = np.mean(valid_embeddings, axis=0)
        norm = np.linalg.norm(mean_emb)
        if norm > 0:
            mean_emb = mean_emb / norm

        npy_path = self._npy_path(name)
        np.save(npy_path, mean_emb)
        logger.info("Embedding sauvegardé : %s (%d frames valides)", npy_path, n_valid)

        display_name = name.capitalize()
        registry[name] = {
            "enrolled_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "n_photos": n_valid,
            "display_name": display_name,
        }
        self._write_registry(registry)

        action = "mis à jour" if is_replacement else "enrôlé"
        return {
            "success": True,
            "n_valid": n_valid,
            "name": name,
            "message": f"Enrôlement réussi : {display_name} {action} ({n_valid} photos valides sur {len(frames)}).",
        }

    def list_known(self) -> list:
        """Retourne la liste des personnes enrôlées depuis registry.json."""
        registry = self._read_registry()
        return [
            {
                "name": name,
                "display_name": info.get("display_name", name.capitalize()),
                "enrolled_at": info.get("enrolled_at", ""),
                "n_photos": info.get("n_photos", 0),
            }
            for name, info in registry.items()
        ]

    def remove(self, name: str) -> bool:
        """
        Supprime une personne enrôlée : retire son .npy et son entrée du registry.
        Retourne True si la suppression a réussi, False si la personne n'existe pas.
        """
        name = name.strip().lower()
        registry = self._read_registry()

        if name not in registry:
            logger.warning("remove() : '%s' introuvable dans le registry.", name)
            return False

        npy_path = self._npy_path(name)
        if os.path.isfile(npy_path):
            os.remove(npy_path)
            logger.info("Fichier supprimé : %s", npy_path)
        else:
            logger.warning("Fichier .npy introuvable pour '%s' — registry nettoyé quand même.", name)

        del registry[name]
        self._write_registry(registry)

        logger.info("Personne supprimée : %s", name)
        return True

    def _npy_path(self, name: str) -> str:
        """Retourne le chemin absolu du fichier .npy pour une personne."""
        return os.path.join(self.known_faces_dir, f"{name}.npy")

    def _read_registry(self) -> dict:
        """Charge registry.json. Retourne un dict vide en cas d'erreur."""
        try:
            with open(self.registry_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.warning(
                "Impossible de lire registry.json (%s) — registry vide utilisé.", exc
            )
            return {}

    def _write_registry(self, data: dict):
        """Sauvegarde registry.json avec indentation lisible."""
        try:
            with open(self.registry_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.error("Impossible d'écrire registry.json : %s", exc)
            raise
