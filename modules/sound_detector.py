"""
sound_detector.py — Détection audio d'impact (chute) via YAMNet TFLite.

Tourne dans un thread daemon séparé. Appelle `on_impact(label, score)` quand un son
suspect est détecté (Thump/thud, Bang, Crash, Slam).

Dépendances :
    pip install tflite-runtime  # ou tensorflow-lite sur Pi
    pip install pyaudio         # déjà installé via openwakeword
"""

import collections
import contextlib
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from modules.silero_vad import SileroVAD

logger = logging.getLogger(__name__)

# Labels YAMNet qui indiquent une possible chute
_IMPACT_LABELS = {"Thump, thud", "Bang", "Crash", "Slam", "Knock"}

# Labels YAMNet liés à la musique — si présents simultanément avec un impact, on supprime
# (source : AudioSet ontology, yamnet_class_map.csv)
_MUSIC_SUPPRESS_LABELS = {"Music", "Musical instrument", "Singing"}
_MUSIC_SUPPRESS_THRESHOLD = 0.40

# Paramètres audio
_SAMPLE_RATE = 16000
_WINDOW_SAMPLES = 15600        # 975ms — taille fenêtre YAMNet (15600 échantillons @ 16kHz)
_HOP_SAMPLES = 8000            # 500ms entre fenêtres
_VOICE_BUFFER_SAMPLES = 48000  # 3s — buffer WeSpeaker speaker ID (calibré )
_SPEECH_RMS_MIN = 0.02         # seuil RMS pour considérer une frame comme "parole active"
_SPEECH_FRAMES_MAX = 6         # max frames de parole conservées (~3s à 500ms/frame)

# Détection de cri par RMS (indépendant de YAMNet — fonctionne pendant lecture Reachy)
# Calibré  : relevé 0.20 → 0.25 pour réduire faux positifs conversation animée
_CRY_RMS_THRESHOLD = 0.35

try:
    from config import VAD_SPEECH_THRESHOLD as _VAD_SPEECH_THRESHOLD
except ImportError:
    _VAD_SPEECH_THRESHOLD = 0.5

# Filtre passe-haut IIR 1er ordre — coupe le bruit moteur basse fréquence (<300Hz)
_HP_CUTOFF = 300  # Hz
_HP_RC = 1.0 / (2 * 3.141592653589793 * _HP_CUTOFF)
_HP_DT = 1.0 / _SAMPLE_RATE
_HP_ALPHA = _HP_RC / (_HP_RC + _HP_DT)


def _find_dsnoop_device(pa) -> Optional[int]:
    """Cherche le device audio pour détection cri / chute. Priorité : mic_alert_in
    (canal L ASR beamformé, split ), puis reachymini_audio_src (dsnoop stéréo
    legacy), puis tout device dsnoop.
    """
    preferred = ("mic_alert_in", "reachymini_audio_src", "dsnoop")
    for keyword in preferred:
        for i in range(pa.get_device_count()):
            try:
                info = pa.get_device_info_by_index(i)
                name = info.get("name", "").lower()
                if keyword in name and info.get("maxInputChannels", 0) > 0:
                    logger.info("SoundDetector: device '%s' → index %d (%s)", keyword, i, info["name"])
                    return i
            except Exception:
                continue
    logger.debug("SoundDetector: pas de device audio alerte trouvé, utilisation du défaut.")
    return None


class SoundDetector:
    """Détecteur audio d'impact via YAMNet TFLite.

    Usage :
        det = SoundDetector(
            model_path="/home/pollen/reachy_care/models/yamnet.tflite",
            on_impact=lambda label, score: print(f"Impact : {label} ({score:.2f})"),
            threshold=0.30,
        )
        det.start()
        # ... boucle principale ...
        det.stop()
    """

    def __init__(
        self,
        model_path: str,
        on_impact: Callable[[str, float], None],
        threshold: float = 0.30,
        device_index: Optional[int] = None,
        on_cry: Optional[Callable[[], None]] = None,
        vad_model_path: Optional[str] = None,
    ) -> None:
        self._model_path = Path(model_path)
        self._on_impact = on_impact
        self._threshold = threshold
        self._device_index = device_index
        self._on_cry = on_cry
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._interpreter = None
        self._class_names: list[str] = []
        self._available = False
        self._audio_buffer = None  # np.ndarray | None, buffer roulant complet (YAMNet)
        self._audio_lock = threading.Lock()
        # Buffer parole seule pour WeSpeaker — n'accumule que les frames avec RMS > _SPEECH_RMS_MIN
        self._speech_frames: collections.deque = collections.deque(maxlen=_SPEECH_FRAMES_MAX)
        self._speech_lock = threading.Lock()
        self._last_vad_speech: bool = False
        self._vad_monitor_last: float = 0.0  # monotonic — throttle VAD monitor logs
        self._vad_monitor_speech: int = 0
        self._vad_monitor_total: int = 0
        self._vad_monitor_max_prob: float = 0.0

        self._load_model()

        self._vad: SileroVAD | None = None
        if vad_model_path:
            self._vad = SileroVAD(vad_model_path)
            if self._vad.available:
                logger.info("SoundDetector: Silero VAD chargé.")
            else:
                self._vad = None

    def _load_model(self) -> None:
        """Charge le modèle YAMNet TFLite et la liste des classes."""
        if not self._model_path.exists():
            logger.warning(
                "SoundDetector: modèle introuvable : %s — détection audio désactivée.",
                self._model_path,
            )
            return
        try:
            import tflite_runtime.interpreter as tflite
        except ImportError:
            try:
                import ai_edge_litert.interpreter as tflite
            except ImportError:
                try:
                    from tensorflow.lite.python import interpreter as tflite
                except ImportError:
                    logger.warning(
                        "SoundDetector: tflite_runtime non disponible — détection audio désactivée."
                    )
                    return
        try:
            self._interpreter = tflite.Interpreter(model_path=str(self._model_path))
            self._interpreter.allocate_tensors()
            # Charger la liste des classes YAMNet (521 classes)
            self._class_names = self._load_class_names()
            self._available = True
            logger.info(
                "SoundDetector: modèle YAMNet chargé (%s), %d classes.",
                self._model_path.name,
                len(self._class_names),
            )
        except Exception as exc:
            logger.warning("SoundDetector: chargement modèle échoué : %s", exc)

    def _load_class_names(self) -> list[str]:
        """Retourne les noms de classes YAMNet (ordre fixe, 521 classes).

        Seuls les indices des sons d'impact sont renseignés — les 521 slots vides
        servent d'index direct sur les scores YAMNet (AudioSet ontology).
        Référence : yamnet_class_map.csv dans tensorflow/models.
        """
        names = [""] * 521
        names[398] = "Bang"
        names[399] = "Crash"
        names[460] = "Slam"
        names[461] = "Thump, thud"
        names[462] = "Thump, thud"
        names[463] = "Bang"
        names[464] = "Crash"
        names[465] = "Knock"
        names[466] = "Tap"
        # Classes musique — suppression si co-occurrence avec impact
        names[137] = "Music"
        names[138] = "Musical instrument"
        names[140] = "Singing"
        return names

    def _run(self) -> None:
        """Thread principal : capture audio et détecte les impacts."""
        try:
            import pyaudio
            import numpy as np
        except ImportError as e:
            logger.warning("SoundDetector: dépendance manquante : %s", e)
            return

        pa = pyaudio.PyAudio()
        stream = None
        buffer = np.zeros(_VOICE_BUFFER_SAMPLES, dtype=np.float32)
        # État persistant du filtre passe-haut IIR entre chunks
        hp_prev_raw = np.float32(0.0)
        hp_prev_out = np.float32(0.0)

        # Résolution du device : préférer le dsnoop partagé si disponible
        dev_idx = self._device_index
        if dev_idx is None:
            dev_idx = _find_dsnoop_device(pa)

        try:
            stream = pa.open(
                format=pyaudio.paFloat32,
                channels=1,
                rate=_SAMPLE_RATE,
                input=True,
                input_device_index=dev_idx,
                frames_per_buffer=_HOP_SAMPLES,
            )
            logger.info("SoundDetector: flux audio ouvert @ %d Hz", _SAMPLE_RATE)

            # Période de grâce : 60s sans détection de cri au démarrage
            # (son de réveil wake_up=true + TTS initial de la conv_app)
            _grace_until = time.monotonic() + 60.0

            while not self._stop_event.is_set():
                try:
                    raw = stream.read(_HOP_SAMPLES, exception_on_overflow=False)
                    chunk = np.frombuffer(raw, dtype=np.float32).copy()

                    # Filtre passe-haut 300Hz — supprime bruit moteur basse fréquence
                    # IIR 1er ordre vectorisé : y[i] = alpha * (y[i-1] + x[i] - x[i-1])
                    filtered = np.empty_like(chunk)
                    filtered[0] = _HP_ALPHA * (hp_prev_out + chunk[0] - hp_prev_raw)
                    for _i in range(1, len(chunk)):
                        filtered[_i] = _HP_ALPHA * (filtered[_i - 1] + chunk[_i] - chunk[_i - 1])
                    hp_prev_raw = chunk[-1]
                    hp_prev_out = filtered[-1]
                    chunk = filtered

                    # RMS calculé une seule fois par chunk (réutilisé par cri + speech filter)
                    chunk_rms = float(np.sqrt(np.mean(chunk ** 2)))

                    # Fenêtre glissante pour YAMNet — skip pendant période de grâce
                    buffer = np.roll(buffer, -len(chunk))
                    buffer[-len(chunk):] = chunk
                    with self._audio_lock:
                        self._audio_buffer = buffer.copy()

                    # Timestamp unique par chunk (réutilisé par VAD monitor, cri, YAMNet)
                    now_m = time.monotonic()

                    # Buffer parole seule pour WeSpeaker
                    # Si VAD disponible : utiliser Silero (bien plus précis que RMS)
                    # Sinon : fallback RMS (comportement actuel)
                    if self._vad is not None:
                        is_speech, vad_prob = self._vad.process_chunk(chunk, threshold=_VAD_SPEECH_THRESHOLD)
                        self._last_vad_speech = is_speech
                        if is_speech:
                            with self._speech_lock:
                                self._speech_frames.append(chunk.copy())
                        # VAD monitor logging (throttled 10s)
                        self._vad_monitor_total += 1
                        if is_speech:
                            self._vad_monitor_speech += 1
                        if vad_prob > self._vad_monitor_max_prob:
                            self._vad_monitor_max_prob = vad_prob
                        if now_m - self._vad_monitor_last >= 10.0:
                            logger.info(
                                "VAD monitor: speech=%d/%d (prob=%.2f, is_speech=%s, speech_frames=%d)",
                                self._vad_monitor_speech, self._vad_monitor_total,
                                self._vad_monitor_max_prob, is_speech, len(self._speech_frames),
                            )
                            self._vad_monitor_speech = 0
                            self._vad_monitor_total = 0
                            self._vad_monitor_max_prob = 0.0
                            self._vad_monitor_last = now_m
                    else:
                        if chunk_rms >= _SPEECH_RMS_MIN:
                            with self._speech_lock:
                                self._speech_frames.append(chunk.copy())

                    # Détection de cri par RMS — APRÈS le VAD pour disposer de is_speech à jour
                    # Un cri est forcément de la parole : si VAD dispo et speech=False, skip
                    # (élimine les faux positifs meuble, TV, porte)
                    if self._on_cry is not None:
                        if chunk_rms >= _CRY_RMS_THRESHOLD and now_m >= _grace_until:
                            if self._vad is None or self._last_vad_speech:
                                try:
                                    self._on_cry()
                                except Exception as cb_exc:
                                    logger.debug("SoundDetector: on_cry erreur : %s", cb_exc)
                    if now_m >= _grace_until:
                        self._infer(buffer)
                except Exception as exc:
                    logger.debug("SoundDetector: erreur lecture audio : %s", exc)
                    time.sleep(0.05)

        except Exception as exc:
            logger.warning("SoundDetector: erreur ouverture flux audio : %s", exc)
        finally:
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.stop_stream()
                    stream.close()
            pa.terminate()
            logger.info("SoundDetector: flux audio fermé.")

    def _infer(self, waveform) -> None:
        """Lance une inférence YAMNet sur la fenêtre audio."""
        if self._interpreter is None:
            return
        try:
            import numpy as np
            inp = self._interpreter.get_input_details()
            out = self._interpreter.get_output_details()
            self._interpreter.set_tensor(inp[0]["index"], waveform)
            self._interpreter.invoke()
            scores = self._interpreter.get_tensor(out[0]["index"])   # shape (N, 521)
            mean_scores = scores.mean(axis=0)
            # Vérifier si de la musique est présente (supprime les faux positifs percussifs)
            music_present = any(
                mean_scores[i] >= _MUSIC_SUPPRESS_THRESHOLD
                for i, name in enumerate(self._class_names)
                if name in _MUSIC_SUPPRESS_LABELS and i < len(mean_scores)
            )
            for idx, score in enumerate(mean_scores):
                if score >= self._threshold and idx < len(self._class_names):
                    label = self._class_names[idx]
                    if label in _IMPACT_LABELS:
                        if music_present:
                            logger.debug(
                                "SoundDetector: impact '%s' (%.2f) supprimé — musique présente", label, score
                            )
                            break
                        logger.warning(
                            "SoundDetector: impact détecté — %s (score=%.2f)", label, score
                        )
                        try:
                            self._on_impact(label, float(score))
                        except Exception as cb_exc:
                            logger.debug("SoundDetector: callback erreur : %s", cb_exc)
                        break   # un seul callback par fenêtre
        except Exception as exc:
            logger.debug("SoundDetector: erreur inférence : %s", exc)

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Démarre le thread de détection audio."""
        if not self._available:
            logger.info("SoundDetector: non disponible — thread non démarré.")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="sound-detector",
            daemon=True,
        )
        self._thread.start()
        logger.info("SoundDetector: thread démarré.")

    def stop(self) -> None:
        """Arrête le thread de détection audio."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        logger.info("SoundDetector: arrêté.")

    def get_recent_audio(self, duration_s: float = 3.0) -> "np.ndarray | None":
        """Retourne les dernières duration_s de captures audio (float32, 16kHz).
        Usage : YAMNet, détection chute. Contient parole + silence."""
        with self._audio_lock:
            if self._audio_buffer is None:
                return None
            n = int(duration_s * _SAMPLE_RATE)
            return self._audio_buffer[-n:].copy()

    def get_speech_audio(self, consume: bool = False) -> "np.ndarray | None":
        """Retourne uniquement les frames de parole active accumulées (VAD > seuil).

        Parameters
        ----------
        consume : bool
            Si True, vide le buffer après lecture (usage chess pipeline).
            Si False, conserve le buffer (usage WeSpeaker speaker ID).
        """
        import numpy as np
        with self._speech_lock:
            if not self._speech_frames:
                return None
            audio = np.concatenate(self._speech_frames)
            if consume:
                self._speech_frames.clear()
            return audio

    def is_speech(self) -> bool:
        """True si le dernier chunk audio contenait de la parole (VAD Silero)."""
        return self._last_vad_speech

    def reset_vad(self) -> None:
        if self._vad is not None:
            self._vad.reset_states()

    @property
    def available(self) -> bool:
        """True si le modèle a été chargé avec succès."""
        return self._available
