"""
fall_detector.py — Détection de chute pour Reachy Care via MediaPipe Pose Lite.

Optimisé pour Raspberry Pi 4 (ARM aarch64, pas de GPU). Latence cible < 100 ms/frame.

Deux algorithmes complémentaires :

A) Ratio vertical (original) :
   |mean(épaules_y) - mean(hanches_y)| < fall_ratio_threshold pendant sustained_seconds
   → détecte les chutes vers l'avant où le corps reste visible (caméra haute).

B) Disparition du squelette (workaround caméra horizontale) :
   La personne était visible, puis le squelette disparaît soudainement pendant
   >= ghost_trigger_seconds → proxy de chute (corps sorti du champ visuel).
   Si absent >= ghost_reset_seconds, on considère que la personne a quitté la pièce.

Coordonnées y normalisées MediaPipe : 0 = haut, 1 = bas.
"""

import time

import numpy as np

try:
    import mediapipe as mp
except ImportError as exc:
    raise ImportError(
        "MediaPipe n'est pas installé. Exécutez : pip install mediapipe"
    ) from exc


_LANDMARK_LEFT_SHOULDER  = 11
_LANDMARK_RIGHT_SHOULDER = 12
_LANDMARK_LEFT_HIP       = 23
_LANDMARK_RIGHT_HIP      = 24


class FallDetector:
    """
    Détecteur de chute temps-réel basé sur MediaPipe Pose Lite.

    Une chute est détectée lorsque le corps devient horizontal
    (|mean(épaules_y) - mean(hanches_y)| < fall_ratio_threshold)
    et que cette posture est maintenue pendant au moins `sustained_seconds`.

    Le détecteur ne se déclenche pas deux fois pour la même chute :
    appeler reset() pour relancer la surveillance après une alerte.

    Paramètres
    ----------
    model_complexity : int
        Complexité du modèle MediaPipe Pose. DOIT rester à 0 (Lite) sur Pi 4.
    detection_confidence : float
        Confiance minimale pour la détection de pose (0.0–1.0).
    fall_ratio_threshold : float
        Seuil de ratio vertical en coordonnées normalisées (0–1).
        En dessous de ce seuil, le corps est considéré horizontal.
    sustained_seconds : float
        Durée minimale (secondes) pendant laquelle la posture de chute
        doit être maintenue pour déclencher une alerte.
    """

    def __init__(
        self,
        model_complexity: int = 0,
        detection_confidence: float = 0.50,
        fall_ratio_threshold: float = 0.15,
        sustained_seconds: float = 3.0,
        ghost_trigger_seconds: float = 2.5,
        ghost_reset_seconds: float = 45.0,
    ):
        self.model_complexity = model_complexity
        self.detection_confidence = detection_confidence
        self.fall_ratio_threshold = fall_ratio_threshold
        self.sustained_seconds = sustained_seconds
        self.ghost_trigger_seconds = ghost_trigger_seconds
        self.ghost_reset_seconds = ghost_reset_seconds

        self._mp_pose = mp.solutions.pose
        self._pose = self._mp_pose.Pose(
            model_complexity=self.model_complexity,
            min_detection_confidence=self.detection_confidence,
            min_tracking_confidence=self.detection_confidence,
            enable_segmentation=False,
            smooth_landmarks=True,
        )

        # Algorithme A — ratio vertical
        self._fall_start_time: float | None = None
        self._alert_active: bool = False

        # Algorithme B — disparition squelette
        self._skeleton_seen: bool = False          # personne visible au moins une fois
        self._skeleton_absent_since: float | None = None  # début de l'absence

    def is_fallen(self, frame: np.ndarray) -> bool:
        """
        Analyse une frame et retourne True si une chute est confirmée (Algo B uniquement).

        Retourne True une seule fois par chute. Appeler reset() après avoir
        traité l'alerte pour relancer la surveillance.

        Paramètres
        ----------
        frame : np.ndarray
            Image BGR uint8 (typiquement 1280×720 depuis la caméra du robot).
        """
        if frame is None or self._alert_active:
            return False

        landmarks = self._get_landmarks_from_frame(frame)

        now = time.monotonic()

        if landmarks is None:
            # Algorithme B — squelette disparu
            if self._skeleton_seen:
                if self._skeleton_absent_since is None:
                    self._skeleton_absent_since = now
                else:
                    absent = now - self._skeleton_absent_since
                    if absent >= self.ghost_reset_seconds:
                        # Personne probablement sortie de la pièce — reset
                        self._skeleton_seen = False
                        self._skeleton_absent_since = None
                    elif absent >= self.ghost_trigger_seconds:
                        self._alert_active = True
                        return True
            return False

        # Squelette visible — mise à jour état
        self._skeleton_seen = True
        self._skeleton_absent_since = None
        self._fall_start_time = None

        # Algorithme A désactivé — caméra à hauteur d'yeux (~130cm),
        # les hanches sont toujours hors cadre → ratio épaules/hanches inutilisable.
        return False

    def reset(self):
        """Réinitialise l'état interne. À appeler après avoir traité une alerte."""
        self._fall_start_time = None
        self._alert_active = False
        self._skeleton_seen = False
        self._skeleton_absent_since = None

    def get_pose_landmarks(self, frame: np.ndarray) -> list | None:
        """
        Retourne les landmarks bruts MediaPipe pour la frame donnée.

        Utile pour le débogage, la visualisation ou la calibration du seuil.

        Retourne une liste de NormalizedLandmark, ou None si aucune personne
        n'est détectée ou si la frame est invalide.
        """
        if frame is None:
            return None
        landmarks = self._get_landmarks_from_frame(frame)
        return list(landmarks) if landmarks is not None else None

    def close(self):
        """Libère les ressources MediaPipe."""
        if self._pose is not None:
            self._pose.close()
            self._pose = None

    def _get_landmarks_from_frame(self, frame: np.ndarray):
        """Exécute l'inférence MediaPipe Pose sur la frame (BGR → RGB en interne)."""
        frame_rgb = frame[:, :, ::-1]
        frame_rgb.flags.writeable = False
        result = self._pose.process(frame_rgb)
        frame_rgb.flags.writeable = True

        if result.pose_landmarks is None:
            return None
        return result.pose_landmarks.landmark

    def _check_fall_criterion(self, landmarks) -> bool:
        """Retourne True si l'écart vertical épaules/hanches est sous le seuil."""
        mean_shoulders_y = (
            landmarks[_LANDMARK_LEFT_SHOULDER].y
            + landmarks[_LANDMARK_RIGHT_SHOULDER].y
        ) / 2.0
        mean_hips_y = (
            landmarks[_LANDMARK_LEFT_HIP].y
            + landmarks[_LANDMARK_RIGHT_HIP].y
        ) / 2.0
        return abs(mean_shoulders_y - mean_hips_y) < self.fall_ratio_threshold

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def __repr__(self) -> str:
        return (
            f"FallDetector("
            f"model_complexity={self.model_complexity}, "
            f"fall_ratio_threshold={self.fall_ratio_threshold}, "
            f"sustained_seconds={self.sustained_seconds}, "
            f"ghost_trigger={self.ghost_trigger_seconds}s, "
            f"ghost_reset={self.ghost_reset_seconds}s, "
            f"skeleton_seen={self._skeleton_seen}, "
            f"alert_active={self._alert_active})"
        )
