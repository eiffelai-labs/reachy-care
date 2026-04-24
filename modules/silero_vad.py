"""
Wrapper numpy pur pour Silero VAD ONNX (opset 15, 16 kHz).

Aucune dépendance PyTorch — utilise uniquement numpy + onnxruntime.
Le module peut être importé même si le modèle ONNX ou onnxruntime
ne sont pas disponibles : dans ce cas, ``available`` retourne False.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

try:
    from config import MODELS_DIR

    _DEFAULT_MODEL_PATH: Path = MODELS_DIR / "silero_vad.onnx"
except Exception:
    _DEFAULT_MODEL_PATH = Path("models/silero_vad.onnx")

logger = logging.getLogger(__name__)

# Constantes Silero VAD 16 kHz
_SAMPLE_RATE = 16000
_FRAME_SIZE = 512        # 32 ms à 16 kHz
_CONTEXT_SIZE = 64       # samples prépendus à chaque frame
_STATE_SHAPE = (2, 1, 128)
_SPEECH_RATIO = 0.05     # 5 % des frames > threshold → parole (micro Reachy signal faible — 1 frame speech suffit)


class SileroVAD:
    """Détecteur d'activité vocale Silero — inférence ONNX sans PyTorch.

    Paramètres
    ----------
    model_path : str | Path
        Chemin vers ``silero_vad_16k_op15.onnx``.
        Par défaut : ``config.MODELS_DIR / "silero_vad.onnx"``.
    """

    def __init__(self, model_path: str | Path | None = None) -> None:
        self._session = None
        self._state: np.ndarray = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._context: np.ndarray = np.zeros(_CONTEXT_SIZE, dtype=np.float32)
        self._sr = np.array(_SAMPLE_RATE, dtype=np.int64)
        # Buffer pré-alloué pour éviter np.concatenate à chaque frame (15×/chunk)
        self._frame_buf = np.zeros(_CONTEXT_SIZE + _FRAME_SIZE, dtype=np.float32)

        path = Path(model_path) if model_path else _DEFAULT_MODEL_PATH
        self._load_model(path)

    # ------------------------------------------------------------------
    # Chargement
    # ------------------------------------------------------------------

    def _load_model(self, path: Path) -> None:
        """Charge le modèle ONNX. En cas d'erreur, positionne available=False."""
        try:
            import onnxruntime as ort  # type: ignore[import-untyped]
        except ImportError:
            logger.warning(
                "silero_vad : onnxruntime non installé — VAD indisponible"
            )
            return

        if not path.is_file():
            logger.warning(
                "silero_vad : modèle introuvable (%s) — VAD indisponible", path
            )
            return

        try:
            opts = ort.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1
            self._session = ort.InferenceSession(
                str(path), sess_options=opts, providers=["CPUExecutionProvider"]
            )
            logger.info("silero_vad : modèle chargé (%s)", path)
        except Exception:
            logger.exception("silero_vad : erreur au chargement du modèle")
            self._session = None

    # ------------------------------------------------------------------
    # État interne
    # ------------------------------------------------------------------

    def reset_states(self) -> None:
        """Remet à zéro l'état RNN et le contexte."""
        self._state = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._context = np.zeros(_CONTEXT_SIZE, dtype=np.float32)
        self._frame_buf[:] = 0.0

    # ------------------------------------------------------------------
    # Inférence frame par frame
    # ------------------------------------------------------------------

    def __call__(self, audio_frame: np.ndarray) -> float:
        """Calcule la probabilité de parole sur une frame de 512 samples.

        Paramètres
        ----------
        audio_frame : np.ndarray
            float32, shape ``(512,)`` — 32 ms à 16 kHz.

        Retourne
        --------
        float
            Probabilité de parole entre 0.0 et 1.0.
        """
        if self._session is None:
            return 0.0

        # Prépend le contexte (64 samples) → shape (576,) — copie in-place
        self._frame_buf[:_CONTEXT_SIZE] = self._context
        self._frame_buf[_CONTEXT_SIZE:] = audio_frame
        # Batch dimension : (1, 576)
        frame = self._frame_buf[np.newaxis, :]

        ort_inputs = {
            "input": frame,
            "state": self._state,
            "sr": self._sr,
        }

        out, new_state = self._session.run(None, ort_inputs)

        # Mise à jour de l'état et du contexte
        self._state = new_state
        self._context = audio_frame[-_CONTEXT_SIZE:]

        # out shape : (1, 1) → scalaire
        return float(out.squeeze())

    # ------------------------------------------------------------------
    # Traitement d'un chunk arbitraire
    # ------------------------------------------------------------------

    def process_chunk(
        self, audio_chunk: np.ndarray, threshold: float = 0.5
    ) -> tuple[bool, float]:
        """Analyse un chunk audio de longueur quelconque.

        Le chunk est découpé en frames de 512 samples. Les échantillons
        restants (< 512) sont ignorés.

        Paramètres
        ----------
        audio_chunk : np.ndarray
            float32, shape ``(N,)`` — N quelconque (ex : 8000 = 500 ms).
        threshold : float
            Seuil de détection parole par frame (défaut 0.5).

        Retourne
        --------
        tuple[bool, float]
            ``(is_speech, max_probability)``
            ``is_speech`` est True si >= 30 % des frames dépassent le seuil.
        """
        if self._session is None:
            return False, 0.0

        audio = np.asarray(audio_chunk, dtype=np.float32)
        n_frames = len(audio) // _FRAME_SIZE

        if n_frames == 0:
            return False, 0.0

        max_prob = 0.0
        speech_count = 0

        for i in range(n_frames):
            frame = audio[i * _FRAME_SIZE : (i + 1) * _FRAME_SIZE]
            prob = self(frame)
            if prob > max_prob:
                max_prob = prob
            if prob > threshold:
                speech_count += 1

        # Une seule frame au-dessus du threshold suffit (micro Reachy signal faible)
        is_speech = speech_count > 0
        return is_speech, max_prob

    # ------------------------------------------------------------------
    # Propriétés
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """True si le modèle ONNX a été chargé avec succès."""
        return self._session is not None
