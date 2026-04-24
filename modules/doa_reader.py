"""
doa_reader.py — Lecture du DOA (Direction of Arrival) du chip XMOS XVF3800.

Tourne dans un thread daemon. Toutes les 250 ms, lit l'angle d'arrivée de la voix
via HID USB. Si l'angle sort du cône frontal ±CONE_DEG, notifie conv_app_v2 via
IPC /doa_gate pour que l'audio ne soit plus envoyé à OpenAI.

Règle :
  - Voix dans ±CONE_DEG autour de l'axe corps → gate OUVERTE (audio → OpenAI)
  - Voix à 90° (TV au mur) ou 180° (conversation derrière) → gate FERMÉE
  - Override 10s après wake word : bypass (voir ipc_server.py /wake)

Le DOA XMOS est relatif au CORPS du robot (chip embarqué fixe), pas à la tête.
Une future itération pourra ajuster le cône au yaw tête courant.
"""
import logging
import math
import threading
import time

logger = logging.getLogger(__name__)

# Axe frontal XMOS = 0 rad. Cône accepté : [-30°, +30°] côté face.
# Remarque : certaines librairies XMOS rapportent 0..2π → on normalise en [-π, π].
CONE_DEG = 30.0
CONE_RAD = math.radians(CONE_DEG)
READ_INTERVAL_SEC = 0.25        # 4 Hz
SMOOTHING_WINDOW = 4            # moyenne glissante sur 1 s
EMERGENCY_LABEL = "emergency"   # bypass non géré ici (fait plus tard côté YAMNet)


def _normalize_to_pm_pi(angle_rad: float) -> float:
    """Ramener un angle radians dans ]-π, +π]."""
    a = angle_rad
    while a > math.pi:
        a -= 2 * math.pi
    while a <= -math.pi:
        a += 2 * math.pi
    return a


class DOAReader:
    """Thread daemon qui lit le DOA XMOS et pousse l'état de la gate via IPC."""

    def __init__(self, on_gate_change):
        """on_gate_change(in_cone: bool, angle_deg: float, energy: float)."""
        self._on_gate_change = on_gate_change
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._rsp = None
        self._last_angles: list[float] = []
        self._last_gate: bool | None = None
        # Dernière valeur lue (exposée pour wake word → rotation body)
        self.last_angle_rad: float = 0.0
        self.last_energy: float = 0.0

    def _connect(self) -> bool:
        """Ouvre la connexion USB au chip XMOS."""
        try:
            import sys
            sys.path.insert(0, "/venvs/mini_daemon/lib/python3.12/site-packages")
            from reachy_mini.media.audio_control_utils import ReSpeaker
            import usb.core
            from libusb_package import get_libusb1_backend
            dev = usb.core.find(idVendor=0x38fb, idProduct=0x1001, backend=get_libusb1_backend())
            if dev is None:
                logger.warning("DOAReader: XMOS device not found (idVendor=0x38fb)")
                return False
            self._rsp = ReSpeaker(dev)
            logger.info("DOAReader: connected to XMOS XVF3800 (cone=±%.0f°)", CONE_DEG)
            return True
        except Exception as exc:
            logger.warning("DOAReader: init failed: %s", exc)
            return False

    def start(self) -> None:
        if not self._connect():
            logger.warning("DOAReader: désactivé (pas de chip XMOS ou erreur init)")
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="doa-reader")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        last_observed_log = 0.0
        while not self._stop.is_set():
            try:
                # XMOS renvoie (angle_rad, energy) sur DOA_VALUE_RADIANS.
                # energy proche de 0 = pas de voix → on garde la gate ouverte (pas de source à filtrer).
                result = self._rsp.read("DOA_VALUE_RADIANS")
                angle_rad, energy = float(result[0]), float(result[1])
                angle_rad = _normalize_to_pm_pi(angle_rad)
                # Expose pour lecture externe (ex: main.py on wake word)
                self.last_angle_rad = angle_rad
                self.last_energy = energy

                # Log d'observation périodique (5s) pour calibration terrain
                now_log = time.monotonic()
                if now_log - last_observed_log >= 5.0:
                    logger.info("DOA observed: angle=%+6.1f° energy=%.3f",
                                math.degrees(angle_rad), energy)
                    last_observed_log = now_log

                if energy < 0.01:
                    # Pas de signal → on ne change rien (on garde la dernière décision).
                    time.sleep(READ_INTERVAL_SEC)
                    continue

                # Moyenne glissante pour stabilité
                self._last_angles.append(angle_rad)
                if len(self._last_angles) > SMOOTHING_WINDOW:
                    self._last_angles.pop(0)
                smoothed = sum(self._last_angles) / len(self._last_angles)
                in_cone = abs(smoothed) <= CONE_RAD

                if in_cone != self._last_gate:
                    logger.info(
                        "DOA gate %s (angle=%+.0f° energy=%.2f)",
                        "OPEN" if in_cone else "CLOSED",
                        math.degrees(smoothed), energy,
                    )
                    self._last_gate = in_cone
                    try:
                        self._on_gate_change(in_cone, math.degrees(smoothed), energy)
                    except Exception as exc:
                        logger.debug("on_gate_change failed: %s", exc)
            except Exception as exc:
                logger.debug("DOAReader loop error: %s", exc)
            time.sleep(READ_INTERVAL_SEC)
