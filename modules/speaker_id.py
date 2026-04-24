"""
speaker_id.py — Identification vocale Reachy Care
Utilise WeSpeaker ResNet34 via wespeakerruntime (ONNX, pas de PyTorch).
Compatible Pi 4 aarch64 — utilise onnxruntime déjà installé.
"""
import logging
import os
import tempfile
import wave

import numpy as np

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 16000
_VOICE_THRESHOLD = 0.65      # seuil cosine identification vocale (baissé scores VAD-filtrés atteignent 0.69)
_VOICE_RMS_MIN = 0.02        # RMS min pour considérer qu'il y a de la parole


class SpeakerIdentifier:
    """
    Identifie un locuteur par similarité cosinus d'embeddings WeSpeaker ResNet34.
    Stocke les embeddings dans known_faces/<nom>_voice.npy.
    """

    def __init__(self, voice_embed_dir: str, threshold: float = _VOICE_THRESHOLD):
        self.voice_embed_dir = voice_embed_dir
        self.threshold = threshold
        self._known: dict[str, np.ndarray] = {}
        self._speaker = None
        self._load_model()
        self.reload_known_voices()

    def _load_model(self) -> None:
        try:
            import wespeakerruntime as wespeaker
            self._speaker = wespeaker.Speaker(lang="en")
            logger.info("WeSpeaker ResNet34 ONNX chargé.")
        except ImportError:
            logger.warning(
                "wespeakerruntime non installé — speaker ID désactivé. "
                "Installe avec : pip install wespeakerruntime"
            )

    @property
    def available(self) -> bool:
        return self._speaker is not None and bool(self._known)

    def identify_from_array(self, audio: np.ndarray) -> tuple[str | None, float]:
        """Identifie le locuteur depuis un array numpy float32 PCM 16kHz.
        Retourne (nom, score) ou (None, best_score).
        """
        if self._speaker is None or not self._known:
            return None, 0.0
        if float(np.sqrt(np.mean(audio ** 2))) < _VOICE_RMS_MIN:
            return None, 0.0  # silence — pas de parole détectable
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name
            self._write_wav(tmp_path, audio)
            emb = self._speaker.extract_embedding(tmp_path)
            emb = np.array(emb, dtype=np.float32).reshape(-1)
            norm = np.linalg.norm(emb)
            if norm > 0:
                emb /= norm
            return self._match(emb)
        except Exception as exc:
            logger.warning("Speaker ID erreur : %s", exc)
            return None, 0.0
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def enroll(self, name: str, audio_arrays: list[np.ndarray]) -> bool:
        """Enrôle un locuteur depuis une liste d'arrays audio numpy float32 16kHz."""
        if self._speaker is None:
            return False
        embeddings = []
        for audio in audio_arrays:
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    tmp_path = tmp.name
                self._write_wav(tmp_path, audio)
                emb = self._speaker.extract_embedding(tmp_path)
                emb = np.array(emb, dtype=np.float32).reshape(-1)
                norm = np.linalg.norm(emb)
                if norm > 0:
                    emb /= norm
                embeddings.append(emb)
            except Exception as exc:
                logger.warning("Enrôlement segment échoué : %s", exc)
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)

        if not embeddings:
            logger.error("Aucun embedding valide pour %s", name)
            return False

        mean_emb = np.mean(embeddings, axis=0).astype(np.float32)
        norm = np.linalg.norm(mean_emb)
        if norm > 0:
            mean_emb /= norm
        out_path = os.path.join(self.voice_embed_dir, f"{name}_voice.npy")
        np.save(out_path, mean_emb)
        self._known[name] = mean_emb
        logger.info("Voix enrôlée : %s (%d segments) → %s", name, len(embeddings), out_path)
        return True

    def is_enrolled(self, name: str) -> bool:
        """Retourne True si un embedding vocal existe pour cette personne."""
        return name in self._known

    def reload_known_voices(self) -> None:
        """Recharge tous les *_voice.npy depuis voice_embed_dir."""
        self._known = {}
        if not os.path.isdir(self.voice_embed_dir):
            return
        for fname in os.listdir(self.voice_embed_dir):
            if not fname.endswith("_voice.npy"):
                continue
            name = fname[: -len("_voice.npy")]
            path = os.path.join(self.voice_embed_dir, fname)
            try:
                emb = np.load(path).astype(np.float32).reshape(-1)
                norm = np.linalg.norm(emb)
                if norm > 0:
                    emb /= norm
                self._known[name] = emb
            except Exception as exc:
                logger.error("Chargement voix %s échoué : %s", path, exc)
        logger.info("%d voix connue(s) : %s", len(self._known), list(self._known.keys()))

    def _match(self, embedding: np.ndarray) -> tuple[str | None, float]:
        best_name, best_score = None, -1.0
        for name, known_emb in self._known.items():
            score = float(np.dot(embedding, known_emb))
            if score > best_score:
                best_score, best_name = score, name
        return best_name, best_score

    @staticmethod
    def _write_wav(path: str, audio: np.ndarray) -> None:
        """Sauvegarde un array float32 PCM 16kHz en WAV 16-bit mono."""
        audio_int16 = (audio * 32767).clip(-32768, 32767).astype(np.int16)
        with wave.open(path, "w") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(_SAMPLE_RATE)
            wf.writeframes(audio_int16.tobytes())
