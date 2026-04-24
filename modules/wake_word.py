"""
wake_word.py — Détection du wake word "Hey Reachy" pour Reachy Care.

Tourne dans un thread daemon séparé. Sur détection, appelle on_wake()
(typiquement bridge.keepalive() pour réactiver la conversation).

Prérequis :
    pip install "openwakeword>=0.6.0" pyaudio
    Modèle ONNX custom : models/hey_reachy.onnx
    (fallback automatique sur "hey_jarvis" si absent)
"""

import logging
import threading
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

_SAMPLE_RATE   = 16_000   # Hz — seul taux supporté par openwakeword
_CHUNK_SAMPLES = 1_280    # 80 ms par chunk
_CHANNELS      = 1
_COOLDOWN_SEC  = 3.0


class WakeWordDetector:
    """Détecteur de wake word ONNX dans un thread daemon."""

    def __init__(
        self,
        model_path: str | Path | None = None,
        on_wake: Callable[[], None] | None = None,
        threshold: float = 0.5,
        input_device_index: int | None = None,
        fallback_model: str = "hey_jarvis",
        tflite_path: str | Path | None = None,
    ) -> None:
        self._model_path = Path(model_path) if model_path else None
        self._tflite_path = Path(tflite_path) if tflite_path else None
        self._on_wake = on_wake
        self._threshold = threshold
        self._input_device_index = input_device_index
        self._fallback_model = fallback_model
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_detection: float = 0.0

        try:
            import openwakeword
            self._oww = openwakeword
        except ImportError as exc:
            raise ImportError("pip install 'openwakeword>=0.6.0'") from exc

        try:
            import pyaudio
            self._pyaudio = pyaudio
        except ImportError as exc:
            raise ImportError("pip install pyaudio") from exc

        logger.info(
            "WakeWordDetector initialisé (model=%s, threshold=%.2f).",
            self._model_path or f"{self._fallback_model} [built-in]",
            self._threshold,
        )

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="wake-word", daemon=True)
        self._thread.start()
        logger.info("WakeWordDetector : thread démarré.")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3.0)
        self._thread = None
        logger.info("WakeWordDetector : arrêté.")

    def close(self) -> None:
        self.stop()

    def _build_model(self):
        # 1. ONNX custom (hey_Reatchy.onnx)
        if self._model_path and self._model_path.exists():
            try:
                model = self._oww.Model(
                    wakeword_models=[str(self._model_path)],
                    inference_framework="onnx",
                )
                logger.info("WakeWordDetector : ONNX chargé : %s", self._model_path.name)
                return model
            except Exception as exc:
                logger.warning("WakeWordDetector : ONNX échoué (%s) — essai tflite.", exc)

        # 2. TFLite custom (hey_Reatchy.tflite)
        if self._tflite_path and self._tflite_path.exists():
            try:
                model = self._oww.Model(
                    wakeword_models=[str(self._tflite_path)],
                    inference_framework="tflite",
                )
                logger.info("WakeWordDetector : TFLite chargé : %s", self._tflite_path.name)
                return model
            except Exception as exc:
                logger.warning("WakeWordDetector : TFLite échoué (%s) — fallback built-in.", exc)

        # 3. Modèle built-in (hey_jarvis, alexa, etc.)
        if self._model_path:
            logger.warning(
                "WakeWordDetector : %s introuvable — fallback %s.",
                self._model_path.name, self._fallback_model,
            )
        model = self._oww.Model(wakeword_models=[self._fallback_model], inference_framework="onnx")
        logger.info("WakeWordDetector : modèle built-in '%s' chargé.", self._fallback_model)
        return model

    @staticmethod
    def _find_dsnoop_device(pa) -> "int | None":
        """Cherche le device audio pour wake word. Priorité : mic_alert_in (canal L
        ASR beamformé, split ), puis reachymini_audio_src (dsnoop stéréo legacy),
        puis tout device dsnoop."""
        preferred = ("mic_alert_in", "reachymini_audio_src", "dsnoop")
        for keyword in preferred:
            for i in range(pa.get_device_count()):
                try:
                    info = pa.get_device_info_by_index(i)
                    name = info.get("name", "").lower()
                    if keyword in name and info.get("maxInputChannels", 0) > 0:
                        logger.info("WakeWordDetector : device '%s' → index %d (%s)", keyword, i, info["name"])
                        return i
                except Exception:
                    continue
        return None

    def _run(self) -> None:
        pa = self._pyaudio.PyAudio()
        stream = None
        try:
            model = self._build_model()
            dev_idx = self._input_device_index
            if dev_idx is None:
                dev_idx = self._find_dsnoop_device(pa)
            stream = pa.open(
                rate=_SAMPLE_RATE,
                channels=_CHANNELS,
                format=self._pyaudio.paInt16,
                input=True,
                frames_per_buffer=_CHUNK_SAMPLES,
                input_device_index=dev_idx,
            )
            logger.info("WakeWordDetector : stream audio ouvert.")

            import numpy as np
            _last_score_log = 0.0
            _max_score_since_log = 0.0
            while not self._stop_event.is_set():
                raw = stream.read(_CHUNK_SAMPLES, exception_on_overflow=False)
                audio = np.frombuffer(raw, dtype=np.int16)
                for name, score in model.predict(audio).items():
                    if score >= self._threshold:
                        now = time.monotonic()
                        if now - self._last_detection >= _COOLDOWN_SEC:
                            self._last_detection = now
                            logger.info("Wake word détecté : '%s' (score=%.3f)", name, score)
                            self._trigger()
                    elif score > 0.02:
                        # Logger les scores proches pour calibration (throttled 5s)
                        if score > _max_score_since_log:
                            _max_score_since_log = score
                        _now_m = time.monotonic()
                        if _now_m - _last_score_log >= 5.0 and _max_score_since_log > 0.05:
                            logger.info("Wake word rejeté (calibration) : '%s' max_score=%.3f (seuil=%.2f)", name, _max_score_since_log, self._threshold)
                            _last_score_log = _now_m
                            _max_score_since_log = 0.0

        except OSError as exc:
            logger.error("WakeWordDetector : micro inaccessible : %s", exc)
        except Exception as exc:
            logger.error("WakeWordDetector : erreur : %s", exc, exc_info=True)
        finally:
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            try:
                pa.terminate()
            except Exception:
                pass

    def _trigger(self) -> None:
        if self._on_wake:
            try:
                self._on_wake()
            except Exception as exc:
                logger.error("WakeWordDetector : erreur callback : %s", exc)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.close()
