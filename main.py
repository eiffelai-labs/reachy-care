"""
main.py — Orchestrateur principal de Reachy Care
Gère la boucle principale, les modules de perception et les commandes vocales.

Démarrage :
    python main.py [--debug] [--no-chess] [--no-face]
"""

import argparse
import contextlib
import glob
import json
import logging
import logging.handlers
import math
import os
import signal
import smtplib
import subprocess
import sys
import threading
import time
import unicodedata
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import requests

import config
from modules.face_recognizer import FaceRecognizer
from modules.register_face import FaceEnroller
from modules.chess_engine import ChessEngine
from modules.fall_detector import FallDetector
from modules.memory_manager import MemoryManager
from modules.sound_detector import SoundDetector
from modules.mode_manager import ModeManager, MODE_ECHECS, MODE_HISTOIRE, MODE_PRO, MODE_NORMAL
from modules.tts import TTSEngine
from modules.wake_word import WakeWordDetector
from modules.speaker_id import SpeakerIdentifier
from modules.journal import Journal
from modules.notifier import Notifier
from modules.daily_scheduler import DailyScheduler
from modules.activity_registry import ActivityRegistry
from modules.frame_queue import SharedFrameQueue
from conv_app_bridge import bridge

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

CMD_DIR = "/tmp/reachy_care_cmds"
_VOICE_ENROLL_NEEDED = 3   # segments audio requis pour l'enrôlement vocal
_FALL_CHECKIN_FILE = "/tmp/reachy_care_fall_checkin.json"

logger = logging.getLogger(__name__)


def _normalize_name(name: str) -> str:
    """Minuscules, suppression accents, espaces→underscores."""
    name = name.strip().lower()
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    name = name.replace(" ", "_")
    return name


def _mark_fall_checkin_active() -> None:
    """Écrit un fichier marqueur indiquant qu'un check-in chute est actif.

    Permet à report_wellbeing.py de vérifier qu'une suspicion de chute
    existe réellement avant d'accepter un appel du LLM.
    """
    try:
        with open(_FALL_CHECKIN_FILE, "w", encoding="utf-8") as f:
            json.dump({"timestamp": time.time()}, f)
    except Exception as exc:
        logging.getLogger(__name__).debug("_mark_fall_checkin_active: %s", exc)


def _clear_fall_checkin() -> None:
    """Supprime le fichier marqueur de check-in chute."""
    with contextlib.suppress(FileNotFoundError):
        os.remove(_FALL_CHECKIN_FILE)


# ---------------------------------------------------------------------------
# Setup du logging
# ---------------------------------------------------------------------------

def setup_logging(debug: bool = False) -> None:
    """Configure les handlers fichier (rotation) et console."""
    level = logging.DEBUG if debug else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    os.makedirs(config.LOGS_DIR, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        config.LOG_FILE,
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setLevel(level)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(fh)
    root.addHandler(ch)


# ---------------------------------------------------------------------------
# Classe principale
# ---------------------------------------------------------------------------

class ReachyCare:
    """Orchestrateur principal de Reachy Care."""

    def __init__(
        self,
        enable_chess: bool = True,
        enable_face: bool = True,
    ) -> None:
        self._stop = False
        self._enable_chess = enable_chess
        self._enable_face = enable_face

        self.mini = None
        self._last_greeted: str | None = None
        self._face_miss_count: int = 0  # frames consécutives sans visage reconnu
        self._last_face_seen_time: float = 0.0  # timestamp dernière reconnaissance faciale réussie

        # Multi-person presence tracking
        self._present_people: dict[str, float] = {}   # name → last_seen (monotonic)
        self._last_greet: dict[str, float] = {}        # name → last greet (monotonic)
        _PRESENCE_TIMEOUT = 60.0   # secondes sans détection → considéré parti
        _GREET_COOLDOWN = 900.0    # 15 min entre deux salutations de la même personne
        self._PRESENCE_TIMEOUT = _PRESENCE_TIMEOUT
        self._GREET_COOLDOWN = _GREET_COOLDOWN

        # Pre-initialize module attributes so shutdown() is always safe
        self.tts = None
        self.recognizer = None
        self.enroller = None
        self.chess_eng = None
        self.fall_det = None
        self.memory = None
        self.activity_registry: ActivityRegistry | None = None
        self.mode_manager: ModeManager | None = None
        self.wake_word: WakeWordDetector | None = None
        self.doa_reader = None   # DOAReader (lazy import)
        self.sound_det: SoundDetector | None = None
        self.speaker_id: SpeakerIdentifier | None = None
        self.journal: Journal | None = None
        self.notifier: Notifier | None = None
        self.scheduler: DailyScheduler | None = None
        self.dashboard = None
        self.frame_queue: SharedFrameQueue | None = None
        self._chess_activity = None  # ChessActivityModule (Phase 2c)
        self._voice_collect_buffers: dict[str, list] = {}
        self._voice_collect_last: dict[str, float] = {}
        self._sleeping: bool = False  # True = mode veille logiciel (loops suspendues, bridge muté)

        # Cache locuteur — évite de recalculer WeSpeaker si identifié récemment
        self._speaker_cache_name: str | None = None
        self._speaker_cache_score: float = 0.0
        self._speaker_cache_until: float = 0.0  # time.monotonic()

        # Session tracking pour la génération de résumé en fin de session
        self._session_events: list[str] = []
        self._seen_persons: dict[str, dict] = {}  # name → memory dict

        # AttenLabs attention gate
        # Grâce 15s + face désactivée → TO_COMPUTER (sinon robot muet au démarrage)
        self._attention_start_time: float = time.monotonic()
        self._attention_state: str = "TO_COMPUTER" if not enable_face else "SILENT"
        self._attention_history: list[str] = []
        # Bypass wake word : 10 s après détection wake, on force TO_COMPUTER
        # même si la caméra perd le visage (tourne la tête, contre-jour, profil).
        # Toggle dashboard : désactive complètement AttenLabs → toujours TO_COMPUTER.
        # TODO DOA : calibration axe 0° XMOS ≠ face robot (+90°) à faire session dédiée.
        self._wake_bypass_until: float = 0.0
        self._attenlabs_enabled: bool = True
        # Runtime state poll : dashboard écrit dans /tmp/reachy_runtime_state.json,
        # main.py lit à 1 Hz pour toggle AttenLabs live sans restart.
        self._runtime_state_path: Path = Path("/tmp/reachy_runtime_state.json")

        # Check-in chute — état du check-in en cours
        self._fall_checkin_active: bool = False
        self._fall_checkin_time: float = 0.0
        self._pending_impact_time: float | None = None  # timestamp impact sonore en attente de fusion
        self._last_cry_time: float = 0.0               # cooldown anti-spam détection cri
        self._last_bridge_speech_time: float = 0.0    # gate "Reachy parle" pour on_cry
        self._user_interruption_start: float = 0.0  # monotonic start quand l'utilisateur parle pendant Reachy

        # MODE VISITEUR — visage non enrôlé détecté
        self._visitor_present: bool = False
        self._visitor_miss_count: int = 0
        self._visitor_frames_count: int = 0  # frames consécutives avec visiteur (anti-flash)

        # Ambiguïté identité — question en attente de confirmation
        self._ambiguity_pending: bool = False
        self._ambiguity_candidates: list[str] = []
        self._ambiguity_last_asked: float = 0.0

        # Timestamp de démarrage — pour la période de grâce RMS (60s)
        self._start_time: float = time.monotonic()

        # Suivi session OpenAI Realtime (expire à 60min — reconnexion proactive à 55min)
        self._conv_app_start_time: float = time.monotonic()

        # Keepalive bridge — timestamp de la dernière activité envoyée au bridge
        self._last_bridge_activity = time.monotonic()

        # Mémoire de session — ré-injection périodique dans le contexte LLM
        self._last_memory_inject = time.monotonic()

        self._check_daemon()

        # Protection double instance — vérifier si un autre main.py tourne déjà
        if config.PID_FILE.exists():
            try:
                existing_pid = int(config.PID_FILE.read_text().strip())
                os.kill(existing_pid, 0)  # signal 0 = vérif existence seulement
                logger.error(
                    "main.py déjà en cours (PID %d) — arrêt immédiat pour éviter les conflits.",
                    existing_pid,
                )
                sys.exit(1)
            except (ProcessLookupError, ValueError, PermissionError):
                # PID mort, invalide, ou appartenant à un autre user — fichier obsolète
                logger.warning("PID_FILE obsolète — nettoyage.")
                config.PID_FILE.unlink(missing_ok=True)

        with open(config.PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        logger.info("PID %d écrit dans %s", os.getpid(), config.PID_FILE)

        self._init_modules()

        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _check_daemon(self) -> None:
        """Démarre le daemon HTTP Reachy si nécessaire et vérifie son accessibilité.

        Si le daemon est déjà actif (control_mode == enabled — cas normal via start_all.sh),
        on ne rappelle PAS daemon/start pour éviter le deadlock "move is currently running".
        Si le daemon est absent ou en erreur, on active les moteurs puis joue wake_up.
        """
        state_url    = f"{config.REACHY_DAEMON_URL}/api/state/full"
        set_mode_url = f"{config.REACHY_DAEMON_URL}/api/motors/set_mode/enabled"
        wake_url     = f"{config.REACHY_DAEMON_URL}/api/move/play/wake_up"

        # Étape 1 — vérifier si le daemon est déjà actif
        already_ok = False
        try:
            resp = requests.get(state_url, timeout=config.REACHY_DAEMON_TIMEOUT)
            resp.raise_for_status()
            state = resp.json()
            already_ok = state.get("control_mode") == "enabled"
        except Exception:
            pass

        if already_ok:
            logger.info("Daemon déjà actif (control_mode=enabled) — wake_up forcé pour activer tous les joints.")
            # Fix : le daemon actif ne garantit pas que TOUS les joints sont sous tension.
            # Sans ce wake_up, la moitié des moteurs restent mous au boot.
            try:
                resp = requests.post(wake_url, timeout=config.REACHY_DAEMON_TIMEOUT)
                resp.raise_for_status()
                logger.info("Wake_up forcé OK : %s (HTTP %d)", wake_url, resp.status_code)
            except Exception as exc:
                logger.warning("Wake_up forcé échoué : %s — %s", wake_url, exc)
        else:
            # Séquence correcte : activer les moteurs puis jouer l'animation wake_up
            try:
                resp = requests.post(set_mode_url, timeout=config.REACHY_DAEMON_TIMEOUT)
                resp.raise_for_status()
                logger.info("Moteurs activés : %s (HTTP %d)", set_mode_url, resp.status_code)
            except Exception as exc:
                logger.warning("Impossible d'activer les moteurs : %s — %s", set_mode_url, exc)
            time.sleep(1)
            try:
                resp = requests.post(wake_url, timeout=config.REACHY_DAEMON_TIMEOUT)
                resp.raise_for_status()
                logger.info("Animation wake_up lancée : %s (HTTP %d)", wake_url, resp.status_code)
            except Exception as exc:
                logger.warning("Impossible de jouer wake_up : %s — %s", wake_url, exc)
                logger.warning("Poursuite du démarrage — le daemon est peut-être déjà actif.")

        # Note : pas de second GET /state — la vérification initiale (étape 1) suffit.

    def _write_status_file(self) -> None:
        """Ecrit l'etat courant dans /tmp/reachy_care_status.json pour le controller."""
        # Expose attention_state + reachy_speaking en live pour les indicateurs
        # du header dashboard. reachy_speaking est recalculé inline depuis
        # _last_bridge_speech_time (même pattern que ligne 1000 de la main loop).
        last_speech = getattr(self, "_last_bridge_speech_time", 0.0)
        status = {
            "running": True,
            "mode": self.mode_manager.get_current_mode() if self.mode_manager else "normal",
            "person": self._last_greeted,
            "sleeping": self._sleeping,
            "uptime": round(time.monotonic() - self._start_time, 1),
            "attention_state": self._attention_state,
            "reachy_speaking": (time.monotonic() - last_speech) < 2.0,
            "modules": {
                "face": self._enable_face and self.recognizer is not None,
                "chess": self._enable_chess and self.chess_eng is not None,
                "wake_word": self.wake_word is not None,
                "sound": self.sound_det is not None and getattr(self.sound_det, 'available', False),
                "fall": self.fall_det is not None,
                "conversation": self._check_conv_app_alive(),
            },
            "persons": list(self._present_people.keys()),
        }
        try:
            tmp = "/tmp/reachy_care_status.tmp"
            with open(tmp, "w") as f:
                json.dump(status, f)
            os.replace(tmp, "/tmp/reachy_care_status.json")
        except Exception:
            pass

    def _check_conv_app_alive(self) -> bool:
        """Verifie si conv_app tourne (PID file)."""
        try:
            pid = int(Path("/tmp/conv_app.pid").read_text().strip())
            os.kill(pid, 0)
            return True
        except Exception:
            return False

    def _init_modules(self) -> None:
        """Instancie tous les modules de perception et d'action."""

        self.tts = TTSEngine(
            voice=config.TTS_VOICE,
            speed=config.TTS_SPEED,
            amplitude=config.TTS_AMPLITUDE,
            backend=config.TTS_BACKEND,
        )
        logger.info("TTSEngine initialisé.")

        self.memory = MemoryManager(str(config.KNOWN_FACES_DIR))
        logger.info("MemoryManager initialisé.")

        self.activity_registry = ActivityRegistry()
        logger.info("ActivityRegistry initialisé (%d activités).", len(self.activity_registry.get_valid_modes()) - 1)

        self.mode_manager = ModeManager(
            profiles_dir=str(config.BASE_DIR / "external_profiles" / "reachy_care"),
            bridge=bridge,
            registry=self.activity_registry,
        )
        logger.info("ModeManager initialisé.")

        if self._enable_face:
            try:
                self.recognizer = FaceRecognizer(
                    known_faces_dir=str(config.KNOWN_FACES_DIR),
                    models_root=str(config.MODELS_DIR),
                    model_name=config.FACE_MODEL_NAME,
                    det_size=config.FACE_DET_SIZE,
                    threshold=config.FACE_COSINE_THRESHOLD,
                    det_score_min=config.FACE_DET_SCORE_MIN,
                )
                self.enroller = FaceEnroller(
                    face_app=self.recognizer._app,
                    known_faces_dir=str(config.KNOWN_FACES_DIR),
                    registry_path=str(config.KNOWN_FACES_DIR / "registry.json"),
                )
                logger.info("FaceRecognizer et FaceEnroller initialisés.")
            except Exception as exc:
                logger.warning("Module face désactivé : %s", exc)
                self._enable_face = False

        if self._enable_chess:
            try:
                stockfish_path = next(
                    (p for p in config.CHESS_STOCKFISH_PATHS if os.path.isfile(p) and os.access(p, os.X_OK)),
                    None,
                )
                if stockfish_path is None:
                    raise FileNotFoundError(
                        f"Stockfish introuvable dans : {config.CHESS_STOCKFISH_PATHS}"
                    )
                self.chess_eng = ChessEngine(
                    stockfish_path=stockfish_path,
                    think_time=config.CHESS_THINK_TIME,
                )
                logger.info("ChessEngine initialisé (stockfish=%s).", stockfish_path)
            except Exception as exc:
                logger.warning("Module chess désactivé : %s", exc)
                self._enable_chess = False

        try:
            self.fall_det = FallDetector(
                model_complexity=config.FALL_MODEL_COMPLEXITY,
                detection_confidence=config.FALL_DETECTION_CONF,
                fall_ratio_threshold=config.FALL_RATIO_THRESHOLD,
                sustained_seconds=config.FALL_SUSTAINED_SEC,
                ghost_trigger_seconds=config.FALL_GHOST_TRIGGER_SEC,
                ghost_reset_seconds=config.FALL_GHOST_RESET_SEC,
            )
            logger.info("FallDetector initialisé.")
        except Exception as exc:
            logger.warning("FallDetector désactivé : %s", exc)

        if config.SOUND_DETECTION_ENABLED:
            try:
                self.sound_det = SoundDetector(
                    model_path=str(config.SOUND_MODEL_PATH),
                    on_impact=self._handle_sound_impact,
                    threshold=config.SOUND_IMPACT_THRESHOLD,
                    on_cry=self._handle_cry,
                    vad_model_path=str(config.VAD_MODEL_PATH),
                )
                logger.info("SoundDetector initialisé (disponible=%s).", self.sound_det.available)
            except Exception as exc:
                logger.warning("SoundDetector désactivé : %s", exc)

        if self._enable_face:
            try:
                self.speaker_id = SpeakerIdentifier(str(config.KNOWN_FACES_DIR))
                logger.info("SpeakerIdentifier initialisé (disponible=%s).", self.speaker_id.available)
            except Exception as exc:
                logger.warning("SpeakerIdentifier désactivé : %s", exc)

        if config.WAKE_WORD_ENABLED:
            try:
                self.wake_word = WakeWordDetector(
                    model_path=config.WAKE_WORD_MODEL_PATH,
                    tflite_path=config.WAKE_WORD_TFLITE_PATH,
                    on_wake=self._on_wake_word,
                    threshold=config.WAKE_WORD_THRESHOLD,
                    input_device_index=config.WAKE_WORD_DEVICE_INDEX,
                    fallback_model=config.WAKE_WORD_FALLBACK,
                )
                logger.info("WakeWordDetector initialisé.")
            except Exception as exc:
                logger.warning("WakeWordDetector désactivé : %s", exc)

        # DOA reader — lit l'angle d'arrivée de la voix du chip XMOS et pousse
        # l'état de la gate directionnelle à conv_app_v2 via IPC /doa_gate.
        # Voix hors cône frontal ±30° → audio ignoré côté conv (TV, conv latérale).
        try:
            from modules.doa_reader import DOAReader
            self.doa_reader = DOAReader(on_gate_change=lambda ok, a, e: bridge.set_doa_gate(ok, a, e))
            logger.info("DOAReader créé.")
        except Exception as exc:
            self.doa_reader = None
            logger.warning("DOAReader désactivé : %s", exc)

        # Module d'activité échecs pluggable (Phase 2c — fallback inline si échec)
        if self._enable_chess and self.chess_eng is not None:
            try:
                from activities.echecs.module import ChessActivityModule
                self._chess_activity = ChessActivityModule(
                    chess_engine=self.chess_eng,
                    sound_detector=self.sound_det,
                    tts=self.tts,
                    bridge=bridge,
                    mini=None,  # Option B : main.py n'a plus le SDK, chess fonctionne sans mouvements
                    mode_manager=self.mode_manager,
                    journal=None,  # sera mis à jour après init journal
                )
                logger.info("ChessActivityModule chargé (module pluggable).")
            except Exception as exc:
                logger.info("ChessActivityModule non chargé — fallback inline : %s", exc)
                self._chess_activity = None

        # Journal quotidien + notifications + scheduler
        self.journal = Journal(str(config.KNOWN_FACES_DIR))
        self.notifier = Notifier()
        self.scheduler = DailyScheduler(self.journal, self.notifier)
        logger.info("Journal + Notifier + DailyScheduler initialisés.")

        # Mettre à jour la référence journal dans le module chess (init différée)
        if self._chess_activity is not None:
            self._chess_activity._journal = self.journal

        # Dashboard web (optionnel — désactivé par défaut)
        if getattr(config, "DASHBOARD_ENABLED", False):
            try:
                from modules.dashboard import Dashboard
                self.frame_queue = SharedFrameQueue()
                self.dashboard = Dashboard(self.frame_queue, self.journal, self.activity_registry)
                logger.info("Dashboard initialisé (port %d).", getattr(config, "DASHBOARD_PORT", 8080))
            except Exception as exc:
                logger.warning("Dashboard désactivé : %s", exc)

    # ------------------------------------------------------------------
    # Boucle principale
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Démarre la boucle principale (sans SDK ReachyMini — Option B coexistence).

        Le daemon robot est piloté exclusivement par conv_app_v2.
        main.py communique via IPC :8766 (bridge) et REST daemon pour les moteurs.
        La caméra est obtenue via IPC /get_frame depuis conv_app_v2.
        """
        try:
            # on NE force PLUS un pitch idle au boot. Le SDK fallback
            # set_head_tracking_enabled(False) est déclenché par tout pitch != 0.
            # Résultat sur 6 semaines : tracking visage OFF en permanence.
            # Désormais conv_app_v2/robot.py active explicitement le tracking au start.

            # Salutation initiale
            self.tts.say("Bonjour, je suis Reachy. Je suis là.", blocking=True)

            # Démarrer la détection sonore et le wake word
            if self.sound_det:
                self.sound_det.start()
            if self.wake_word:
                self.wake_word.start()
            if self.doa_reader:
                self.doa_reader.start()
            if self.dashboard:
                self.dashboard.start()

            # Boucle principale avec throttling
            last_face = last_fall = 0.0
            _frame_count = 0

            while not self._stop:
                # Commandes vocales et santé conv_app — indépendants de la caméra
                # Fix : ces checks étaient après le "continue" sur frame=None,
                # donc jamais exécutés quand la caméra ne retourne pas de frames.
                t_cmd = time.monotonic()
                if t_cmd - getattr(self, '_last_cmd_check', 0) >= 1.0:
                    self._check_voice_commands()
                    self._poll_runtime_state()
                    self._last_cmd_check = t_cmd
                self._check_fall_checkin_timeout()
                self._check_conv_app_health()
                self._check_user_interruption()
                if self.scheduler:
                    self.scheduler.tick()
                if self.dashboard:
                    self.dashboard.update_status(
                        mode=self.mode_manager.get_current_mode() if self.mode_manager else "normal",
                        person=self._last_greeted,
                        sleeping=self._sleeping,
                    )

                # Écrit le status pour le controller web — cadence 1 s, pour
                # que l'indicateur attention/speaking du dashboard soit
                # semi-temps-réel. Coût CPU négligeable (dict + os.replace).
                if t_cmd - getattr(self, '_last_status_write', 0) >= 1.0:
                    self._write_status_file()
                    self._last_status_write = t_cmd

                # Motor watchdog : toutes les 30s, si daemon actif mais
                # motor_control_mode=disabled (ex: perte de courant, reflash), on
                # re-enable les moteurs + wake_up REST. main.py le faisait au boot
                # mais jamais ensuite.
                if t_cmd - getattr(self, '_last_motor_check', 0) >= 30.0:
                    self._last_motor_check = t_cmd
                    try:
                        r = requests.get("http://localhost:8000/api/daemon/status", timeout=2)
                        bs = r.json().get("backend_status", {})
                        if bs.get("motor_control_mode") == "disabled":
                            logger.warning("Moteurs disabled détectés — re-enable + wake_up")
                            bridge.set_motors(enabled=True)
                            requests.post("http://localhost:8000/api/move/play/wake_up", timeout=10)
                    except Exception as exc:
                        logger.debug("motor watchdog failed: %s", exc)

                # Obtenir la frame via IPC depuis conv_app_v2 (qui possède la caméra)
                frame = bridge.get_frame()
                if frame is None:
                    time.sleep(0.1)
                    continue

                # Pousser la frame au dashboard pour le flux MJPEG
                if self.frame_queue is not None:
                    self.frame_queue.put(frame)

                # AttenLabs — classifieur attention (SILENT/TO_HUMAN/TO_COMPUTER)
                # Throttle 2 Hz : _compute_attention → InsightFace _app.get(frame)
                # qui fait détection SCRFD + extraction embedding → très coûteux.
                # Avant throttle : main.py ~150-245% CPU au repos. Cible ~70-100%.
                if self._enable_face:
                    _now_att = time.monotonic()
                    if _now_att - getattr(self, '_last_attention_at', 0.0) >= 0.5:
                        self._last_attention_at = _now_att
                        # Grâce 15s au démarrage : caméra/InsightFace pas encore prêts
                        in_grace = _now_att - self._attention_start_time < 15.0
                        #  : retiré le bypass "not _is_owner → TO_COMPUTER".
                        # toggle dashboard + bypass wake word 10 s (prioritaire
                        # sur la bbox, hystérésis 3-frames conservée en aval).
                        if not self._attenlabs_enabled:
                            new_state = "TO_COMPUTER"
                        elif _now_att < self._wake_bypass_until:
                            new_state = "TO_COMPUTER"
                        else:
                            new_state = "TO_COMPUTER" if in_grace else self._compute_attention(frame)
                        self._attention_history.append(new_state)
                        if len(self._attention_history) > 3:
                            self._attention_history.pop(0)
                        # Transition confirmée : 3 frames identiques consécutives
                        if (len(self._attention_history) >= 3
                                and len(set(self._attention_history[-3:])) == 1
                                and new_state != self._attention_state):
                            prev_state = self._attention_state
                            self._attention_state = new_state
                            bridge.set_attention(new_state)
                            logger.info(
                                "AttenLabs: %s → %s%s",
                                prev_state, new_state,
                                " (grâce démarrage)" if in_grace else " (bbox)",
                            )
                            # Force un refresh de status.json à chaque transition
                            # d'attention, sinon les transitions fugaces (< 1 s
                            # entre deux writes réguliers) sont invisibles au
                            # dashboard même avec poll 1 s. Coût : un write JSON
                            # atomique.
                            self._write_status_file()
                            self._last_status_write = t_cmd
                        # Sync initial : main.py envoie son état une fois
                        # au démarrage, même si pas de transition. Évite la désync
                        # avec conv_app_v2 quand le wake word a forcé TO_COMPUTER
                        # côté conv_app mais main.py reste en SILENT par défaut.
                        elif (len(self._attention_history) >= 3
                                and len(set(self._attention_history[-3:])) == 1
                                and not getattr(self, '_initial_att_sent', False)):
                            self._initial_att_sent = True
                            bridge.set_attention(self._attention_state)
                            logger.info("AttenLabs initial sync: %s", self._attention_state)

                t = time.monotonic()
                _frame_count += 1

                try:
                    if self.fall_det and t - last_fall >= config.FALL_INTERVAL_SEC:
                        self._handle_fall(frame)
                        last_fall = t

                    if (
                        self._enable_face
                        and t - last_face >= config.FACE_INTERVAL_SEC
                        and _frame_count % config.FACE_FRAME_SKIP == 0
                    ):
                        self._handle_face(frame)
                        last_face = t

                    # Pipeline chess — module pluggable
                    if self._chess_activity is not None:
                        self._chess_activity.tick()

                    # _check_voice_commands, _check_fall_checkin_timeout et
                    # _check_conv_app_health sont appelés en début de boucle
                    # (avant get_frame) pour fonctionner même sans caméra.
                except Exception as _loop_exc:
                    logger.exception("Erreur boucle principale : %s", _loop_exc)
                    self._notify_loop_error(_loop_exc)

                time.sleep(config.FRAME_INTERVAL_SEC)

        finally:
            self.shutdown()

    # ------------------------------------------------------------------
    # Gates dynamiques (depuis ActivityRegistry)
    # ------------------------------------------------------------------

    def _mode_suppresses(self, feature: str) -> bool:
        """Vérifie si le mode courant supprime une feature via le registry.

        Exemples : _mode_suppresses("face_recognition"), _mode_suppresses("cry_detection")
        Fallback sur False si pas de registry ou pas de mode_manager.
        """
        if not self.mode_manager:
            return False
        mode = self.mode_manager.get_current_mode()
        if self.activity_registry:
            return self.activity_registry.mode_suppresses(mode, feature)
        return False

    def _mode_gate(self, gate_key: str, default=None):
        """Retourne la valeur d'un gate pour le mode courant.

        Exemples : _mode_gate("head_pitch_deg"), _mode_gate("silent_face_recognition")
        """
        if not self.mode_manager:
            return default
        mode = self.mode_manager.get_current_mode()
        if self.activity_registry:
            gates = self.activity_registry.get_gates(mode)
            return gates.get(gate_key, default)
        return default

    # ------------------------------------------------------------------
    # Handlers d'événements
    # ------------------------------------------------------------------

    def _write_current_person(self, name: str) -> None:
        """Écrit current_person dans /tmp/reachy_session_memory.json.

        Permet à gutenberg.py de savoir à qui rattacher la progression de
        lecture après un redémarrage.
        """
        session_file = "/tmp/reachy_session_memory.json"
        try:
            if os.path.exists(session_file):
                with open(session_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            else:
                data = {}
        except Exception:
            data = {}
        data["current_person"] = name
        try:
            with open(session_file, "w", encoding="utf-8") as f:
                json.dump(data, ensure_ascii=False, indent=2, fp=f)
            logger.debug("current_person écrit dans session_memory: %s", name)
        except Exception as exc:
            logger.warning("Impossible d'écrire current_person: %s", exc)

    def _clear_visitor_if_present(self) -> None:
        """Réinitialise l'état visiteur si un visiteur non enrôlé était présent."""
        self._visitor_miss_count = 0
        self._visitor_frames_count = 0
        if self._visitor_present:
            self._visitor_present = False
            bridge.set_visitor_mode(False)

    def _decide_identity(
        self,
        candidates: list[tuple[str, float]],
        voice_name: str | None,
        voice_score: float,
        voice_available: bool,
    ) -> tuple[str | None, float]:
        """Décide de l'identité à partir des N-best candidats visuels + confirmation vocale.

        Cas 1 : top_score >= 0.50 ET (candidat unique OU écart >= 0.15) → ID directe.
        Cas 2 : top_score >= 0.50 ET écart < 0.15 → ambiguïté → voix lève ou question.
        Cas 3 : top_score < 0.50 → face insuffisante ; seule la voix peut confirmer.
        """
        if not candidates:
            return None, 0.0

        top_name, top_score = candidates[0]
        second_score = candidates[1][1] if len(candidates) >= 2 else 0.0
        ecart = top_score - second_score

        # Cas 3 : score face trop faible — la voix peut encore confirmer
        if top_score < config.FACE_COSINE_THRESHOLD:
            if voice_available and voice_name == top_name and voice_score >= config.VOICE_ONLY_THRESHOLD:
                logger.debug(
                    "Identité voix (face %.2f < seuil) — %s voix=%.2f",
                    top_score, voice_name, voice_score,
                )
                return voice_name, voice_score
            return None, top_score

        # top_score >= 0.50

        # Cas 1 : identification directe (écart suffisant ou candidat unique)
        if ecart >= config.FACE_AMBIGUITY_ECART or len(candidates) < 2:
            if voice_available:
                if voice_name == top_name and voice_score >= config.VOICE_ONLY_THRESHOLD:
                    # Voix confirme → score fusionné renforcé
                    return top_name, 0.6 * top_score + 0.4 * voice_score
                if voice_name is not None and voice_name != top_name and voice_score >= config.VOICE_ONLY_THRESHOLD:
                    # Désaccord face/voix → rejeté (sécurité)
                    logger.debug(
                        "Désaccord face=%s(%.2f) voix=%s(%.2f) → rejeté",
                        top_name, top_score, voice_name, voice_score,
                    )
                    return None, max(top_score, voice_score)
                # Voix active mais score insuffisant → face seule acceptée avec confiance réduite
                if top_score >= 0.80:
                    return top_name, top_score
                return top_name, top_score * 0.85
            # Voix non disponible → face seule acceptée
            if top_score >= 0.80:
                return top_name, top_score
            return top_name, top_score * 0.85  # confiance légèrement réduite (zone 0.50–0.79)

        # Cas 2 : ambiguïté (écart < FACE_AMBIGUITY_ECART)
        if voice_available and voice_score >= config.VOICE_ONLY_THRESHOLD:
            # La voix lève l'ambiguïté
            for cname, cscore in candidates:
                if cname == voice_name:
                    logger.debug(
                        "Ambiguïté résolue par voix — %s voix=%.2f face=%.2f",
                        cname, voice_score, cscore,
                    )
                    return cname, 0.6 * cscore + 0.4 * voice_score
            # La voix ne confirme aucun candidat → rejeté
            return None, top_score

        # Ambiguïté persistante → poser la question (avec cooldown)
        self._fire_ambiguity_question(candidates[:2])
        return None, top_score

    def _fire_ambiguity_question(self, candidates: list[tuple[str, float]]) -> None:
        """Demande à Douze de confirmer l'identité d'une personne ambiguë."""
        now = time.monotonic()
        if self._ambiguity_pending:
            return
        if now - self._ambiguity_last_asked < config.FACE_AMBIGUITY_ASK_COOLDOWN:
            return

        names = [name for name, _ in candidates]
        self._ambiguity_pending = True
        self._ambiguity_candidates = names
        self._ambiguity_last_asked = now

        # Contexte mémoire des deux candidats pour Douze
        snippets = []
        for name in names:
            mem = self.memory.load(name) if self.memory else {}
            last_seen = mem.get("last_seen", "?")[:10]  # date seule
            sessions = mem.get("sessions_count", 0)
            snippets.append(f"{name} ({sessions} session(s), vu en dernier : {last_seen})")
        context = " — ".join(snippets)

        bridge.send_event(
            f"[Reachy Care] Ambiguïté identité : {' ou '.join(names)}",
            instructions=(
                f"Tu vois quelqu'un mais tu hésites entre {names[0]} et {names[1]}. "
                f"Contexte mémoire : {context}. "
                f"Pose une seule question naturelle pour confirmer qui c'est. "
                f"Quand la personne répond, appelle confirm_identity avec le prénom confirmé."
            ),
        )
        logger.info("Ambiguïté identité déclenchée : %s", names)

    def _maybe_collect_voice_segment(self, name: str, audio: np.ndarray) -> None:
        """Collecte progressivement des segments audio pour l'enrôlement vocal automatique.

        Déclenche l'enrôlement après _VOICE_ENROLL_NEEDED segments valides (parole détectée).
        Anti-overlap : ignore les segments collectés dans les 4s précédentes.
        """
        if self.speaker_id is None:
            return
        rms = float(np.sqrt(np.mean(audio ** 2)))
        if rms < 0.02:
            return  # silence — pas de parole détectable
        now = time.monotonic()
        if now - self._voice_collect_last.get(name, 0.0) < 4.0:
            return  # anti-overlap
        self._voice_collect_buffers.setdefault(name, []).append(audio.copy())
        self._voice_collect_last[name] = now
        collected = len(self._voice_collect_buffers[name])
        logger.info("Enrôlement vocal %s : segment %d/%d.", name, collected, _VOICE_ENROLL_NEEDED)
        if collected >= _VOICE_ENROLL_NEEDED:
            success = self.speaker_id.enroll(name, self._voice_collect_buffers.pop(name))
            if success:
                logger.info("Enrôlement vocal %s terminé — fusion face+voix activée.", name)
                bridge.send_event(
                    f"[Reachy Care] Enrôlement vocal terminé pour {name}.",
                    instructions=(
                        f"Tu viens d'apprendre à reconnaître la voix de {name}. "
                        f"Dis-le lui naturellement en une phrase."
                    ),
                )

    # ------------------------------------------------------------------
    # Cache locuteur — helpers
    # ------------------------------------------------------------------

    def _speaker_cache_get(self) -> tuple[str, float] | None:
        """Retourne (name, score) si le cache est valide, None sinon."""
        if self._speaker_cache_name is not None and time.monotonic() < self._speaker_cache_until:
            return self._speaker_cache_name, self._speaker_cache_score
        return None

    def _speaker_cache_set(self, name: str, score: float) -> None:
        """Met en cache le locuteur identifié pour SPEAKER_CACHE_TTL secondes."""
        self._speaker_cache_name = name
        self._speaker_cache_score = score
        self._speaker_cache_until = time.monotonic() + config.SPEAKER_CACHE_TTL
        logger.info("Speaker cache set: %s (score=%.2f, TTL=%.1fs)", name, score, config.SPEAKER_CACHE_TTL)

    def _speaker_cache_invalidate(self) -> None:
        """Invalide le cache locuteur (changement de personne, wake word)."""
        self._speaker_cache_name = None
        self._speaker_cache_score = 0.0
        self._speaker_cache_until = 0.0

    def _compute_attention(self, frame: np.ndarray) -> str:
        """Classifie l'état d'attention : SILENT / TO_HUMAN / TO_COMPUTER.

        - Visage centré + proche → TO_COMPUTER (seul état qui ouvre la gate audio)
        - Visage détecté mais décalé → TO_HUMAN (silence)
        - Pas de visage → SILENT
        - Bbox identique > 60s (cadre photo, poster, objet) → SILENT forcé

        Seuils : ATTENTION_HEADING_MAX (0.40) + ATTENTION_SIZE_MIN (0.12) dans config.
        Note  : le fallback mouvement scène a été retiré car il
        ouvrait la gate dès qu'une zone bougeait sans preuve qu'un humain regarde
        le robot (Reachy parlait pendant conversations de la maison).
        """
        if self.recognizer is None or frame is None:
            return "SILENT"

        try:
            faces = self.recognizer._app.get(frame)
        except Exception:
            return "SILENT"

        if not faces:
            self._lip_speaking = False
            return "SILENT"

        best = max(faces, key=lambda f: f.det_score)
        bbox = best.bbox  # [x1, y1, x2, y2]
        frame_w = max(int(frame.shape[1]), 1)
        center_x = (float(bbox[0]) + float(bbox[2])) / 2.0
        bbox_w = float(bbox[2]) - float(bbox[0])
        heading = abs(center_x / frame_w - 0.5)   # 0 = centré = regarde robot
        size_ratio = bbox_w / frame_w              # gros = proche

        # --- Anti-bbox-statique : photo / poster / objet facial figé ---
        # Signature bbox arrondie à 2 décimales (≈ 1% frame) : 2 photos consécutives
        # d'un humain varient toujours d'au moins 1% (respiration, micro-mouvements).
        now = time.monotonic()
        bbox_sig = (round(center_x / frame_w, 2), round(size_ratio, 2))
        if bbox_sig != getattr(self, "_attn_bbox_sig", None):
            self._attn_bbox_move_time = now
        self._attn_bbox_sig = bbox_sig
        if now - getattr(self, "_attn_bbox_move_time", now) > 60.0:
            return "SILENT"

        # Lip movement detection DÉSACTIVÉ : surcharge CPU Pi 4.
        # self._update_lip_movement(best.kps)

        heading_max = getattr(config, "ATTENTION_HEADING_MAX", 0.25)
        size_min = getattr(config, "ATTENTION_SIZE_MIN", 0.10)
        if heading < heading_max and size_ratio > size_min:
            return "TO_COMPUTER"
        return "TO_HUMAN"

    def _update_lip_movement(self, kps) -> None:
        """Détecte le mouvement des lèvres via le mouth stretch ratio (kps 5 points).

        Mouth stretch = distance coins bouche / distance yeux.
        Quand ce ratio varie ET Silero détecte de la parole → user is speaking.
        Envoie le signal via bridge pour empêcher le commit audio côté conv_app_v2.
        """
        if kps is None or len(kps) < 5:
            return
        eye_dist = float(np.linalg.norm(kps[0] - kps[1]))
        if eye_dist < 1.0:
            return
        mouth_dist = float(np.linalg.norm(kps[3] - kps[4]))
        stretch = mouth_dist / eye_dist

        if not hasattr(self, "_lip_stretch_history"):
            self._lip_stretch_history = []
            self._lip_speaking = False
            self._lip_last_sent = 0.0

        self._lip_stretch_history.append(stretch)
        if len(self._lip_stretch_history) > 10:
            self._lip_stretch_history.pop(0)
        if len(self._lip_stretch_history) < 5:
            return

        variance = float(np.var(self._lip_stretch_history))
        audio_speech = self.sound_det.is_speech() if self.sound_det else False
        is_speaking = variance > 0.001 and audio_speech

        now = time.monotonic()
        if is_speaking != self._lip_speaking and (now - self._lip_last_sent > 1.0):
            self._lip_speaking = is_speaking
            self._lip_last_sent = now
            try:
                bridge.set_user_speaking(is_speaking)
            except Exception:
                pass

    def _handle_face(self, frame: np.ndarray) -> None:
        """Identifie le visage et met à jour le contexte conversationnel."""
        if self.recognizer is None:
            return
        if self._sleeping:
            return
        # Gate dynamique : ignorer complètement si le mode supprime la face recognition
        if self._mode_suppresses("face_recognition"):
            return
        # Gate dynamique : reconnaître silencieusement (pas de salutation, mais garder le contexte)
        silent_mode = bool(self._mode_gate("silent_face_recognition"))
        try:
            # N-best candidats visuels (scores >= FACE_CANDIDATE_MIN)
            candidates = self.recognizer.identify_nbest(frame, min_score=config.FACE_CANDIDATE_MIN)
            face_name = candidates[0][0] if candidates else None  # meilleur candidat visuel brut

            # Audio récent — fusion + enrôlement progressif
            audio = None
            if candidates and self.speaker_id is not None and self.sound_det is not None:
                audio = self.sound_det.get_recent_audio(3.0)

            # Fusion multimodale N-best + voix
            # Gate : ne pas appeler WeSpeaker pendant que Reachy parle (AGC XVF3800 réduit le gain
            # global → embeddings moins énergétiques → scores dégradés vs enrôlement en silence)
            _reachy_speaking = (time.monotonic() - self._last_bridge_speech_time) < 2.0
            voice_name: str | None = None
            voice_score: float = 0.0
            voice_available = False
            # Cache speech audio pour éviter double lock + concatenation
            speech_audio = self.sound_det.get_speech_audio() if self.sound_det and not _reachy_speaking else None
            if candidates and self.speaker_id is not None and self.speaker_id.available and not _reachy_speaking:
                cached = self._speaker_cache_get()
                if cached is not None:
                    voice_name, voice_score = cached
                    voice_available = True
                elif speech_audio is not None:
                    voice_name, voice_score = self.speaker_id.identify_from_array(speech_audio)
                    logger.info("WeSpeaker result (face+voice): name=%s score=%.2f", voice_name, voice_score)
                    # Exclure la voix propre du robot (cedar) — ne pas confondre Reachy avec une personne
                    if voice_name == config.ROBOT_VOICE_NAME:
                        voice_name, voice_score = None, 0.0
                    voice_available = True
                    if voice_name is not None and voice_score >= config.VOICE_ONLY_THRESHOLD:
                        self._speaker_cache_set(voice_name, voice_score)
            name, score = self._decide_identity(candidates, voice_name, voice_score, voice_available)

            # Enrôlement vocal progressif (pour top candidat visuel, même si fusion rejetée)
            # Gate : pas d'enrôlement pendant que Reachy parle (signal AGC-compressé)
            # Utilise speech_audio (VAD-filtré) pour cohérence avec la reconnaissance
            enroll_audio = speech_audio if speech_audio is not None else audio
            if face_name is not None and self.speaker_id is not None and enroll_audio is not None and not _reachy_speaking and not self.speaker_id.is_enrolled(face_name):
                self._maybe_collect_voice_segment(face_name, enroll_audio)

            # Reconnaissance vocale seule — voix ≥ VOICE_ONLY_THRESHOLD = reconnu, même si visage présent
            # Règle : voix ≥ seuil prend toujours le dessus (face insuffisante ou absente)
            # Note : _last_greeted NON requis — permet re-identification en cours de session
            _face_absent_too_long = (time.monotonic() - self._last_face_seen_time) > 30.0
            if (name is None
                    and not _reachy_speaking
                    and not _face_absent_too_long
                    and self.speaker_id is not None
                    and self.speaker_id.available
                    and self.sound_det is not None):
                # Réutiliser le résultat WeSpeaker déjà calculé si speech_audio a été utilisé
                if voice_available and voice_name is not None and voice_score >= config.VOICE_ONLY_THRESHOLD:
                    voice_name_vo, voice_score_vo = voice_name, voice_score
                    rms_vo = 1.0  # déjà filtré par speech frames RMS > seuil
                else:
                    audio_vo = speech_audio if speech_audio is not None else (audio if audio is not None else self.sound_det.get_recent_audio(3.0))
                    voice_name_vo, voice_score_vo = None, 0.0
                    rms_vo = 0.0
                    if audio_vo is not None:
                        rms_vo = float(np.sqrt(np.mean(audio_vo ** 2)))
                        logger.info("voix-seule: name=%s score=%.2f rms=%.2f speech_frames=%s candidates=%s",
                                    name, score, rms_vo, speech_audio is not None, [c[0] for c in candidates])
                        cached = self._speaker_cache_get()
                        if cached is not None:
                            voice_name_vo, voice_score_vo = cached
                        elif rms_vo >= 0.05:
                            voice_name_vo, voice_score_vo = self.speaker_id.identify_from_array(audio_vo)
                            logger.info("WeSpeaker result (voice-only): name=%s score=%.2f rms=%.2f", voice_name_vo, voice_score_vo, rms_vo)
                            if voice_name_vo is not None and voice_score_vo >= config.VOICE_ONLY_THRESHOLD:
                                self._speaker_cache_set(voice_name_vo, voice_score_vo)
                if (voice_name_vo is not None
                        and voice_score_vo >= config.VOICE_ONLY_THRESHOLD
                        and voice_name_vo != config.ROBOT_VOICE_NAME):
                    name = voice_name_vo
                    score = voice_score_vo
                    logger.info("Reconnaissance vocale seule (face=%.2f) : %s (score=%.2f)",
                                candidates[0][1] if candidates else 0.0, name, score)

            if name:
                self._face_miss_count = 0
                self._last_face_seen_time = time.monotonic()
                self._clear_visitor_if_present()

                # --- Multi-person presence tracking ---
                now_p = time.monotonic()
                was_absent = (
                    name not in self._present_people
                    or (now_p - self._present_people.get(name, 0) > self._PRESENCE_TIMEOUT)
                )
                self._present_people[name] = now_p

                # Nettoyer les personnes parties (timeout)
                self._present_people = {
                    n: t for n, t in self._present_people.items()
                    if now_p - t < self._PRESENCE_TIMEOUT
                }

                # Vérifier si on doit saluer (nouvelle personne + cooldown 15 min)
                should_greet = (
                    was_absent
                    and (now_p - self._last_greet.get(name, 0) > self._GREET_COOLDOWN)
                )

                if should_greet:
                    self._ambiguity_pending = False  # identité confirmée — reset ambiguïté
                    self._speaker_cache_invalidate()
                    self._last_greet[name] = now_p
                    logger.info("Personne reconnue: %s (score=%.2f)", name, score)

                    # Mémoire persistante
                    memory_summary = None
                    mem = {}
                    if self.memory:
                        mem = self.memory.on_seen(name)
                        memory_summary = mem.get("conversation_summary") or None
                        if name not in self._seen_persons:
                            self._seen_persons[name] = mem
                            self._session_events.append(
                                f"Personne reconnue : {name} (session #{mem['sessions_count']})"
                            )
                            logger.info("Personne reconnue : %s (session #%d)", name, mem["sessions_count"])

                    profile = mem.get("profile") or None

                    # Contexte mémoire enrichi : 3 dernières sessions + 15 faits récents
                    sessions = mem.get("sessions", [])[-3:]
                    facts = mem.get("facts", [])[-15:]
                    if sessions or facts:
                        sessions_txt = "\n".join(
                            f"- {s.get('date', '?')} : {s.get('summary', '')}" for s in sessions
                        ) or "Première rencontre."
                        facts_txt = "\n".join(
                            f"- [{f.get('category', '?')}] {f.get('fact', '')}" for f in facts
                        ) or "Aucun fait enregistré."
                        meds = ", ".join(profile.get("medications", [])) if profile else ""
                        contact = profile.get("emergency_contact", "") if profile else ""
                        memory_summary = (
                            f"HISTORIQUE RÉCENT ({name}) :\n{sessions_txt}\n\n"
                            f"FAITS CONNUS :\n{facts_txt}\n\n"
                            f"PROFIL :\nMédicaments : {meds or 'non renseigné'}\n"
                            f"Contact urgence : {contact or 'non renseigné'}"
                        )

                    if not silent_mode:
                        present_names = list(self._present_people.keys())
                        others = [n for n in present_names if n != name]
                        if others:
                            # Multi-person: envoyer un event groupé
                            bridge.send_event(
                                f"[Reachy Care] Personnes présentes : {', '.join(present_names)}",
                                instructions=(
                                    f"Salue {name} qui vient d'arriver. "
                                    f"Tu parlais déjà avec {', '.join(others)}."
                                ),
                            )
                            self._last_bridge_speech_time = now_p
                            self._set_antennas(config.ANTENNA_HAPPY, duration=0.5)
                            self._last_greeted = name
                        else:
                            # Single person: comportement existant via set_context
                            injected = bridge.set_context(person=name, memory_summary=memory_summary, profile=profile)
                            if injected:
                                self._last_bridge_speech_time = now_p
                                self._set_antennas(config.ANTENNA_HAPPY, duration=0.5)
                                self._last_greeted = name
                            # Si injection échouée (bridge pas encore prêt) : ne pas setter _last_greeted
                            # → la prochaine frame retentrera l'injection automatiquement
                    else:
                        self._last_greeted = name
                    self._write_current_person(name)
                    if self.journal:
                        self.journal.log(name, "note", f"Présence détectée ({name})")
                # else: person already greeted and within cooldown — no action needed
            else:
                # Aucun visage reconnu
                self._face_miss_count += 1
                if self._face_miss_count >= config.FACE_MISS_RESET_COUNT:
                    if self._last_greeted:
                        bridge.person_departed(self._last_greeted)
                    self._last_greeted = None
                    # Nettoyer la liste de présence aussi
                    self._present_people.clear()
                    self._face_miss_count = 0

                # MODE SILENCE SOCIAL AUTO-DÉCLENCHEMENT DÉSACTIVÉ
                # Le silence est déclenché uniquement par commande vocale explicite
                # (stop_speaking tool : "tais-toi", "chut", "laisse-nous"…)
        except Exception as exc:
            logger.debug("_handle_face: %s", exc)

    def _handle_fall(self, frame: np.ndarray) -> None:
        """Détecte une chute et déclenche un check-in vocal avant d'alerter."""
        if self.fall_det is None or self._fall_checkin_active:
            return
        if self._sleeping:
            return
        try:
            if self.fall_det.is_fallen(frame):
                logger.warning("Suspicion de chute — check-in LLM déclenché")
                self._session_events.append("Suspicion de chute — check-in en cours")
                self._fall_checkin_active = True
                self._fall_checkin_time = time.monotonic()
                _mark_fall_checkin_active()
                self._set_antennas(config.ANTENNA_ALERT, duration=0.3)
                bridge.trigger_check_in(self._last_greeted)
        except Exception as exc:
            logger.debug("_handle_fall: %s", exc)

    def _handle_sound_impact(self, label: str, score: float) -> None:
        """Appelé par SoundDetector quand un son d'impact (chute possible) est détecté."""
        logger.warning("Impact sonore détecté : %s (score=%.2f)", label, score)
        self._session_events.append(f"Impact sonore : {label}")

        # Fusion audio + vidéo obligatoire : squelette absent depuis > 2s → check-in
        # Un son seul ne suffit pas (chien, objet qui tombe, bruit ambiant)
        # Sécurité : personne vue par caméra il y a < 2min → fausse alarme probable
        face_elapsed = time.monotonic() - self._last_face_seen_time
        if face_elapsed < 120 and score < 0.65:
            logger.debug("Impact sonore ignoré — personne vue il y a %.0fs (< 120s)", face_elapsed)
            return
        if (
            self.fall_det
            and self.fall_det._skeleton_absent_since is not None
            and time.monotonic() - self.fall_det._skeleton_absent_since > 2.0
            and not self._fall_checkin_active
        ):
            logger.warning("Fusion audio+vidéo : impact sonore + squelette absent → check-in")
            self._fall_checkin_active = True
            self._fall_checkin_time = time.monotonic()
            _mark_fall_checkin_active()
            self._set_antennas(config.ANTENNA_ALERT, duration=0.3)
            bridge.trigger_check_in(self._last_greeted)
        else:
            # Mémoriser l'impact : si le squelette disparaît dans les 5s → check-in différé
            self._pending_impact_time = time.monotonic()
            logger.info("Impact sonore mémorisé — surveillance squelette pendant 5s (fusion différée)")

    def _handle_cry(self) -> None:
        """Détection de cri par RMS — indépendant de la VAD conv_app (half-duplex).

        Appelé par SoundDetector quand RMS > seuil sur 500ms.
        Interrompt la réponse de Reachy et déclenche un check-in immédiat.
        Cooldown 60s pour éviter le spam (calibré en test réel ).

        Note : intentionnellement pas de gate sur _visitor_present — la sécurité prime
        sur le mode silence social. Un cri déclenche toujours un check-in.
        """
        now = time.monotonic()
        # Période de grâce de 60s au démarrage : wake_up=true joue un son + TTS initial
        if now - self._start_time < 60.0:
            logger.debug("on_cry ignoré — période de grâce démarrage (60s)")
            return
        if now - self._last_cry_time < 180.0:
            return
        if self._fall_checkin_active:
            return
        # Gate : si Reachy a parlé dans les 45 dernières secondes → probablement le TTS
        if now - self._last_bridge_speech_time < 45.0:
            logger.debug("on_cry ignoré — Reachy parle probablement encore (gate 45s)")
            return
        # Gate dynamique : personne forcément présente et consciente en mode activité
        if self._mode_suppresses("cry_detection"):
            logger.debug("on_cry ignoré — mode %s supprime cry_detection",
                         self.mode_manager.get_current_mode() if self.mode_manager else "?")
            return
        # Sécurité : personne vue par la caméra il y a < 120s → elle est là, pas de chute
        # (aligné sur la gate d'escalade — calibré  : conversation animée = fausse alarme à 30s)
        if now - self._last_face_seen_time < 120.0:
            logger.debug("on_cry ignoré — personne vue il y a %.0fs (< 120s)", now - self._last_face_seen_time)
            return
        if bridge.is_muted():
            logger.debug("Cri RMS détecté mais bridge muté — check-in ignoré.")
            return
        self._last_cry_time = now
        logger.warning("Cri détecté (RMS) — interruption conv_app + check-in")
        self._session_events.append("Cri ou son fort détecté (RMS)")
        _mark_fall_checkin_active()
        bridge.cancel()
        bridge.trigger_check_in(self._last_greeted)
        self._last_bridge_speech_time = now

    def _check_user_interruption(self) -> None:
        """Détecte si l'utilisateur parle pendant que Reachy parle → /cancel.

        Heuristique : si le VAD détecte de la parole humaine pendant >1s
        alors que Reachy est en train de parler, on envoie /cancel pour
        couper Reachy et laisser l'utilisateur s'exprimer.
        """
        if self.sound_det is None or not self.sound_det.available:
            return
        now = time.monotonic()
        reachy_speaking = (now - self._last_bridge_speech_time) < 2.0
        user_speaking = self.sound_det.is_speech()

        if reachy_speaking and user_speaking:
            if self._user_interruption_start == 0.0:
                self._user_interruption_start = now
            elif now - self._user_interruption_start >= 1.0:
                logger.info("Interruption locale : utilisateur parle pendant Reachy (%.1fs) → /cancel",
                            now - self._user_interruption_start)
                bridge.cancel()
                self._user_interruption_start = 0.0
        else:
            self._user_interruption_start = 0.0

    def _escalate_fall_alert(self) -> None:
        """Escalade l'alerte chute après un check-in négatif ou sans réponse."""
        self._fall_checkin_active = False
        _clear_fall_checkin()
        # Sécurité : si la personne a été vue par la caméra dans les 2 dernières minutes,
        # elle est présente et consciente — ne pas envoyer d'alerte (fausse alarme LLM)
        if time.monotonic() - self._last_face_seen_time < 120:
            logger.warning(
                "Escalade annulée — personne reconnue par caméra il y a %.0fs (< 120s) : fausse alarme.",
                time.monotonic() - self._last_face_seen_time,
            )
            bridge.trigger_alert("fausse_alarme_annulee")
            return
        self._session_events.append("Alerte chute confirmée")
        logger.warning("CHUTE CONFIRMÉE — alerte escaladée")
        bridge.trigger_alert("chute confirmée")
        if self.notifier:
            self.notifier.send_fall_alert(self._last_greeted)
        else:
            self._send_fall_telegram(self._last_greeted)
            self._send_fall_email(self._last_greeted)
        if self.journal and self._last_greeted:
            self.journal.log(self._last_greeted, "sante", "Alerte chute confirmée — proches prévenus")
        if self.fall_det:
            self.fall_det.reset()

    def _check_fall_checkin_timeout(self) -> None:
        """Escalade si le LLM n'a pas rappelé dans les 45 secondes."""
        # Vérification fusion différée : impact récent + squelette vient de disparaître
        if (
            self._pending_impact_time is not None
            and time.monotonic() - self._pending_impact_time < 5.0
            and self.fall_det
            and self.fall_det._skeleton_absent_since is not None
            and not self._fall_checkin_active
        ):
            logger.warning("Fusion différée : squelette absent après impact sonore → check-in")
            self._pending_impact_time = None
            self._fall_checkin_active = True
            self._fall_checkin_time = time.monotonic()
            _mark_fall_checkin_active()
            self._set_antennas(config.ANTENNA_ALERT, duration=0.3)
            bridge.trigger_check_in(self._last_greeted)
            return
        # Annuler si impact trop vieux (>5s)
        if self._pending_impact_time is not None and time.monotonic() - self._pending_impact_time >= 5.0:
            self._pending_impact_time = None

        if not self._fall_checkin_active:
            return
        if time.monotonic() - self._fall_checkin_time > 45:
            logger.warning("Check-in chute : timeout 45s — escalade")
            self._escalate_fall_alert()

    def _send_telegram(self, text: str, parse_mode: str = "Markdown") -> None:
        """Envoie un message Telegram en arrière-plan. No-op si non configuré."""
        if not config.TELEGRAM_ENABLED:
            return
        if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
            return

        def _send():
            try:
                resp = requests.post(
                    f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": parse_mode},
                    timeout=10,
                )
                if not resp.ok:
                    logger.error("Telegram: %s %s", resp.status_code, resp.text)
            except Exception as exc:
                logger.error("Échec envoi Telegram : %s", exc)

        threading.Thread(target=_send, name="telegram", daemon=True).start()

    def _send_fall_telegram(self, person_name: str | None) -> None:
        """Envoie une alerte Telegram en cas de chute détectée."""
        if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
            logger.warning("Alerte Telegram : BOT_TOKEN ou CHAT_ID non configurés.")
            return
        who = person_name.capitalize() if person_name else "une personne inconnue"
        now = datetime.now().strftime("%d/%m/%Y à %Hh%M")
        self._send_telegram(
            f"⚠️ *Reachy Care — Chute détectée*\n\n"
            f"🕐 {now}\n"
            f"👤 Personne : {who}\n\n"
            f"Reachy a réagi vocalement. Vérifiez la situation."
        )

    def _notify_loop_error(self, exc: Exception) -> None:
        """Envoie un Telegram d'avertissement si la boucle principale lève une exception."""
        msg = f"⚠️ Reachy Care — erreur boucle principale : {type(exc).__name__}: {exc}"
        if self.notifier:
            self.notifier.send_telegram(msg, parse_mode="")
        else:
            self._send_telegram(msg, parse_mode="")

    def _send_fall_email(self, person_name: str | None) -> None:
        """Envoie une alerte email en cas de chute détectée."""
        if not config.ALERT_EMAIL_ENABLED:
            return
        if not config.ALERT_EMAIL_FROM or not config.ALERT_EMAIL_PASSWORD:
            logger.warning("Alerte email : identifiants non configurés (ALERT_EMAIL_FROM / ALERT_EMAIL_PASSWORD).")
            return

        who = person_name.capitalize() if person_name else "une personne inconnue"
        now = datetime.now().strftime("%d/%m/%Y à %Hh%M")

        msg = EmailMessage()
        msg["Subject"] = f"⚠️ Reachy Care — Chute détectée ({who})"
        msg["From"]    = config.ALERT_EMAIL_FROM
        msg["To"]      = config.ALERT_EMAIL_TO
        msg.set_content(
            f"Bonjour,\n\n"
            f"Le robot Reachy a détecté une chute le {now}.\n\n"
            f"Personne concernée : {who}\n\n"
            f"Reachy a immédiatement réagi vocalement. Veuillez vérifier la situation.\n\n"
            f"— Reachy Care"
        )

        def _send():
            try:
                with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as smtp:
                    smtp.login(config.ALERT_EMAIL_FROM, config.ALERT_EMAIL_PASSWORD)
                    smtp.send_message(msg)
                logger.info("Alerte email chute envoyée à %s.", config.ALERT_EMAIL_TO)
            except Exception as exc:
                logger.error("Échec envoi email alerte : %s", exc)

        # Envoyer dans un thread pour ne pas bloquer la boucle principale
        threading.Thread(target=_send, name="fall-email", daemon=True).start()

    # ------------------------------------------------------------------
    # Enrôlement vocal
    # ------------------------------------------------------------------

    def _capture_enrollment_frames(self, name: str, max_valid: int, timeout: float) -> list:
        """Capture des frames en s'arrêtant dès max_valid visages détectés ou timeout."""
        frames = []
        valid_count = 0
        announced_halfway = False
        t_start = time.monotonic()

        while valid_count < max_valid and (time.monotonic() - t_start) < timeout:
            frame = bridge.get_frame()
            if frame is not None:
                frames.append(frame)
                try:
                    faces = self.recognizer._app.get(frame)
                    if faces:
                        valid_count += 1
                        if not announced_halfway and valid_count >= max_valid // 2:
                            announced_halfway = True
                            self.tts.say("Bien, continuez.", blocking=False)
                except Exception:
                    pass
            time.sleep(0.4)

        logger.info("Capture enrôlement : %d frames, %d visages valides.", len(frames), valid_count)
        return frames

    def _enroll_mode(self, name: str) -> None:
        """Capture des frames et enrôle une nouvelle personne, avec feedback vocal."""
        if self.enroller is None or self.recognizer is None:
            self.tts.say("Le module de reconnaissance faciale n'est pas disponible.", blocking=True)
            return

        MIN_VALID = 5
        MAX_VALID = 12
        TIMEOUT   = 12.0  # secondes max par tentative

        self.tts.say(f"Je vais mémoriser {name}. Regardez-moi bien, ne bougez pas.", blocking=True)
        time.sleep(0.5)

        frames = self._capture_enrollment_frames(name, MAX_VALID, TIMEOUT)
        result = self.enroller.enroll(name, frames, min_valid=MIN_VALID)

        if not result["success"] and result["n_valid"] < MIN_VALID:
            # Une tentative supplémentaire
            self.tts.say(
                f"Je n'ai pas bien vu votre visage. Approchez-vous et regardez-moi encore.",
                blocking=True,
            )
            time.sleep(0.5)
            frames2 = self._capture_enrollment_frames(name, MAX_VALID, TIMEOUT)
            result = self.enroller.enroll(name, frames + frames2, min_valid=MIN_VALID)

        if result["success"]:
            self.recognizer.reload_known_faces()
        self.tts.say(result["message"], blocking=True)
        bridge.enroll_complete(name, result["success"])

    # ------------------------------------------------------------------
    # Helpers moteurs
    # ------------------------------------------------------------------

    def _do_wake_motors(self) -> None:
        """Wake_up via REST daemon (sans SDK — Option B coexistence).

        Appelle le daemon REST pour réactiver les moteurs et jouer wake_up.
        """
        try:
            requests.post(
                f"{config.REACHY_DAEMON_URL}/api/motors/set_mode/enabled",
                timeout=config.REACHY_DAEMON_TIMEOUT,
            )
        except Exception:
            pass
        try:
            requests.post(
                f"{config.REACHY_DAEMON_URL}/api/move/play/wake_up",
                timeout=config.REACHY_DAEMON_TIMEOUT,
            )
        except Exception as _rest_e:
            logger.warning("wake_up REST échoué : %s", _rest_e)
        time.sleep(1)
        bridge.set_motors(enabled=True)
        # plus de set_head_pitch idle — laisser le tracking visage Pollen gérer.

    def _set_antennas(self, antennas: list, duration: float = 0.5) -> None:
        """Contrôle les antennes via REST daemon (sans SDK).

        Passe par l'API REST /api/move/play/ du daemon Reachy.
        Les valeurs d'antennes sont envoyées comme paramètres.
        """
        try:
            # Le daemon REST expose goto_target via un endpoint dédié
            requests.post(
                f"{config.REACHY_DAEMON_URL}/api/move/goto_target",
                json={"antennas": antennas, "duration": duration},
                timeout=config.REACHY_DAEMON_TIMEOUT,
            )
        except Exception as exc:
            logger.debug("_set_antennas REST: %s", exc)

    # ------------------------------------------------------------------
    # Runtime state (polling fichier écrit par dashboard)
    # ------------------------------------------------------------------

    def _poll_runtime_state(self) -> None:
        """Lit /tmp/reachy_runtime_state.json (écrit par dashboard :8080) et met à jour
        les toggles live sans nécessiter de restart. Appelé à 1 Hz depuis run().

        Clés supportées :
        - attenlabs_enabled (bool, défaut True) : active/désactive AttenLabs
        """
        try:
            data = json.loads(self._runtime_state_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            return
        except Exception as exc:
            logger.debug("Runtime state poll: %s", exc)
            return

        new_att = bool(data.get("attenlabs_enabled", True))
        if new_att != self._attenlabs_enabled:
            self._attenlabs_enabled = new_att
            logger.info("Runtime state (dashboard): attenlabs_enabled = %s", new_att)

    # ------------------------------------------------------------------
    # Commandes vocales (polling fichier)
    # ------------------------------------------------------------------

    def _check_voice_commands(self) -> None:
        """Interroge la queue de commandes vocales (répertoire) et les exécute dans l'ordre."""
        try:
            files = sorted(Path(CMD_DIR).glob("*.json"))
        except Exception:
            return
        for fpath in files:
            try:
                raw = fpath.read_bytes()
                fpath.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                logger.warning("_check_voice_commands: lecture %s échouée : %s", fpath.name, exc)
                continue

            try:
                cmd = json.loads(raw)
            except json.JSONDecodeError as exc:
                logger.warning("_check_voice_commands: JSON invalide (%s) : %s", fpath.name, exc)
                continue

            command = cmd.get("cmd")

            try:
                if command == "enroll":
                    name = _normalize_name(cmd.get("name", ""))
                    if name:
                        self._enroll_mode(name)
                    else:
                        logger.warning("Commande enroll sans nom.")

                elif command == "list_persons":
                    if self.enroller:
                        persons = self.enroller.list_known()
                        names = ", ".join(p["name"] for p in persons) if persons else "personne"
                        self.tts.say(f"Je connais : {names}")
                    else:
                        self.tts.say("Le module de reconnaissance n'est pas actif.")

                elif command in ("enroll_voice", "reenroll_voice"):
                    name = _normalize_name(cmd.get("name", ""))
                    if name:
                        # Supprimer l'embedding existant pour forcer le re-collect
                        if command == "reenroll_voice" and self.speaker_id is not None:
                            voice_path = os.path.join(str(config.KNOWN_FACES_DIR), f"{name}_voice.npy")
                            if os.path.exists(voice_path):
                                os.remove(voice_path)
                                logger.info("Ancien embedding vocal supprimé : %s", voice_path)
                            self.speaker_id.reload_known_voices()
                        self._voice_collect_buffers.pop(name, None)
                        self._voice_collect_last.pop(name, None)
                        logger.info("Enrôlement vocal %s demandé pour %s.", "re-" if command == "reenroll_voice" else "", name)
                        bridge.send_event(
                            f"[Reachy Care] Enrôlement vocal demandé pour {name}.",
                            instructions=(
                                f"Demande à {name} de te parler naturellement pendant quelques secondes. "
                                "Tu vas apprendre à reconnaître sa voix. Explique en 1-2 phrases."
                            ),
                        )
                    else:
                        logger.warning("Commande %s sans nom.", command)

                elif command == "wake_motors" or command == "reveille" or command == "wake":
                    # Réveil complet : même séquence que le wake word handler (ligne ~1655)
                    self._sleeping = False
                    # Reset état face/visiteur (évite silence social résiduel)
                    self._face_miss_count = 0
                    self._last_greeted = None
                    self._present_people.clear()
                    self._last_greet.clear()
                    self._visitor_present = False
                    self._visitor_frames_count = 0
                    self._visitor_miss_count = 0
                    self._last_face_seen_time = time.monotonic()
                    bridge.set_head_pitch(0.0)
                    if bridge.is_muted():
                        bridge.unmute()
                    bridge.wake()
                    # Cycle daemon stop→start wake_up=true (reflash servos)
                    try:
                        requests.post(
                            f"{config.REACHY_DAEMON_URL}/api/daemon/stop?goto_sleep=false",
                            timeout=config.REACHY_DAEMON_TIMEOUT,
                        )
                        time.sleep(3)
                        requests.post(
                            f"{config.REACHY_DAEMON_URL}/api/daemon/start?wake_up=true",
                            timeout=config.REACHY_DAEMON_TIMEOUT,
                        )
                        time.sleep(5)
                        logger.info("Commande %s — réveil complet OK (daemon + face reset + bridge).", command)
                    except Exception as _e:
                        logger.warning("Commande %s daemon cycle échoué : %s — moteurs peut-être inactifs", command, _e)
                        try:
                            self._do_wake_motors()
                        except Exception as _e2:
                            logger.warning("Fallback _do_wake_motors échoué : %s", _e2)

                elif command == "sleep_mode":
                    #  : séquence propre pour éviter broken pipe IPC + daemon zombie.
                    # bridge.mute() déclenche conv_app /sleep qui fait maintenant :
                    # cancel_response → clear_playback → mute → robot.sleep.
                    # On attend 1.5 s que la queue aplay BT soit complètement drainée
                    # AVANT d'envoyer goto_sleep au daemon. 0.6 s était insuffisant
                    # (queue pouvait contenir 30+ s d'audio en mode histoire monologue).
                    bridge.mute()
                    bridge.sleep()  # VAD 0.99 — privacy conv_app
                    self._sleeping = True  # avant animation : garantit suspend même si REST timeout
                    # Baisser les antennes avant de descendre dans l'habitacle
                    self._set_antennas(config.ANTENNA_ALERT, duration=0.5)
                    time.sleep(1.5)
                    try:
                        requests.post(
                            f"{config.REACHY_DAEMON_URL}/api/move/play/goto_sleep",
                            timeout=config.REACHY_DAEMON_TIMEOUT,
                        )
                    except Exception as _re:
                        logger.warning("goto_sleep REST échoué : %s", _re)
                    # Couper les moteurs pour éviter la chauffe dans l'habitacle
                    bridge.set_motors(enabled=False)
                    logger.info("Commande sleep_mode — loops suspendues, bridge muté, moteurs coupés.")

                elif command == "forget":
                    name = _normalize_name(cmd.get("name", ""))
                    if name and self.enroller:
                        ok = self.enroller.remove(name)
                        self.tts.say(
                            f"J'ai oublié {name}" if ok else f"Je ne connais pas {name}"
                        )
                    elif not name:
                        logger.warning("Commande forget sans nom.")
                    else:
                        self.tts.say("Le module de reconnaissance n'est pas actif.")

                elif command == "wellbeing_response":
                    if not self._fall_checkin_active:
                        logger.debug("wellbeing_response ignoré — aucun check-in en cours (réponse orpheline).")
                    else:
                        status = cmd.get("status", "no_response")
                        if status == "ok":
                            logger.info("Check-in : personne OK — reset alerte chute")
                            self._fall_checkin_active = False
                            _clear_fall_checkin()
                            if self.fall_det:
                                self.fall_det.reset()
                        else:
                            logger.warning("Check-in : status=%r — escalade alerte chute", status)
                            self._escalate_fall_alert()

                elif command == "chess_reset":
                    if self._chess_activity is not None:
                        self._chess_activity.handle_command(cmd)
                    else:
                        logger.warning("chess_reset ignoré — module chess inactif.")

                elif command == "confirm_identity":
                    name_confirmed = cmd.get("name", "").strip().lower()
                    if name_confirmed and name_confirmed in self._ambiguity_candidates:
                        self._ambiguity_pending = False
                        self._last_greeted = None  # force re-salutation avec le bon prénom
                        self._present_people.clear()
                        self._last_greet.clear()
                        logger.info("Identité confirmée par Douze : %s", name_confirmed)
                    else:
                        self._ambiguity_pending = False
                        logger.warning("confirm_identity : prénom inconnu ou hors candidats — ignoré (%r)", name_confirmed)

                elif command == "wake":
                    self._on_wake_word()
                    logger.info("Réveil manuel déclenché.")

                elif command == "wake_word_test":
                    # Déclenché depuis le dashboard (bouton ▶ dans Contrôles rapides).
                    # Simule une détection wake word complète : acknowledge_gesture
                    # antennes + unmute + ré-ouverture session. Le modèle optionnel
                    # (cmd["model"]) est loggué pour trace, pas utilisé côté robot.
                    model = cmd.get("model", "")
                    logger.info("Wake word test demandé depuis dashboard (model=%s)", model)
                    self._on_wake_word()

                elif command == "mute":
                    bridge.mute()
                    logger.info("Commande mute reçue — bridge silencieux.")

                elif command == "unmute":
                    bridge.unmute()
                    logger.info("Commande unmute reçue — bridge réactivé.")

                elif command == "switch_mode":
                    mode = cmd.get("mode", "").strip()
                    topic = cmd.get("topic", "").strip()
                    if mode and self.mode_manager:
                        # Un switch_mode = l'utilisateur parle activement → démuter le bridge
                        # Fix : le bridge muté (stop_speaking) bloquait les events chess
                        if bridge.is_muted():
                            bridge.unmute()
                            logger.info("switch_mode: bridge démuté automatiquement.")
                        if mode == MODE_ECHECS and self.mode_manager.get_current_mode() != MODE_ECHECS:
                            if self._chess_activity is not None:
                                self._chess_activity.set_person(self._last_greeted)
                                self._chess_activity.on_enter(context=topic)
                            else:
                                logger.warning("switch_mode échecs : module chess inactif.")
                            # La branche entry mode échecs doit notifier le
                            # mode_manager — sans cela, _current_mode reste
                            # obsolète, status.json n'est pas mis à jour, et
                            # le dashboard affiche l'ancien mode.
                            self.mode_manager.switch_mode(MODE_ECHECS, context=topic)
                        else:
                            # Si on quitte le mode échecs, déléguer au module
                            if self.mode_manager.get_current_mode() == MODE_ECHECS:
                                if self._chess_activity is not None:
                                    self._chess_activity.on_exit()
                            switched = self.mode_manager.switch_mode(mode, context=topic)
                            if not switched:
                                logger.debug("switch_mode ignoré (déjà actif ou throttle).")
                        # Journal : noter le changement de mode
                        if self.journal and self._last_greeted:
                            label = {"echecs": "Partie d'échecs", "histoire": "Histoires", "musique": "Écoute musicale", "pro": f"Exposé ({topic})" if topic else "Exposé"}.get(mode, f"Mode {mode}")
                            self.journal.log(self._last_greeted, "activite", label)
                        # Refresh status.json immédiatement (sans attendre la
                        # boucle 3 s) pour que le dashboard voie le nouveau
                        # mode sans lag.
                        self._write_status_file()
                    else:
                        logger.warning("Commande switch_mode sans mode ou mode_manager absent.")

                elif command == "mark_awake":
                    # Commande émise par le bouton "wake_smart" du dashboard
                    # après un POST /api/move/play/wake_up réussi, pour
                    # synchroniser l'état sleeping côté main.py sans toucher
                    # les moteurs ni déclencher de cascade.
                    self._sleeping = False
                    self._write_status_file()
                    logger.info("mark_awake: sleeping → False (cmd dashboard).")

                elif command == "chess_human_move":
                    if self._chess_activity is not None:
                        self._chess_activity.handle_command(cmd)
                    else:
                        logger.warning("chess_human_move ignoré — module chess inactif.")

                elif command == "toggle_module":
                    module_name = cmd.get("module", "")
                    enabled = cmd.get("enabled", True)
                    if module_name == "face":
                        self._enable_face = enabled
                        logger.info("Module face %s", "active" if enabled else "desactive")
                    elif module_name == "chess":
                        self._enable_chess = enabled
                        logger.info("Module chess %s", "active" if enabled else "desactive")
                    elif module_name == "wake_word":
                        if not enabled and self.wake_word:
                            self.wake_word.stop()
                            logger.info("Module wake_word desactive")
                        elif enabled and self.wake_word:
                            self.wake_word.start()
                            logger.info("Module wake_word active")
                    elif module_name == "sound":
                        if not enabled and self.sound_det:
                            self.sound_det.stop()
                            logger.info("Module sound desactive")
                        elif enabled and self.sound_det:
                            self.sound_det.start()
                            logger.info("Module sound active")
                    elif module_name == "fall":
                        if self.fall_det:
                            self.fall_det._enabled = enabled
                            logger.info("Module fall %s", "active" if enabled else "desactive")
                    else:
                        logger.warning("toggle_module: module inconnu '%s'", module_name)

                else:
                    logger.warning("Commande vocale inconnue : %s", command)

            except Exception as exc:
                logger.warning("Erreur commande vocale: %s", exc)

    # ------------------------------------------------------------------
    # Wake word callback
    # ------------------------------------------------------------------

    def _on_wake_word(self) -> None:
        """Appelé par WakeWordDetector lors d'une détection."""
        logger.info("Wake word détecté — réactivation de la session.")
        # Bypass AttenLabs 10 s : l'utilisateur peut tourner la tête, la caméra
        # peut perdre le visage, l'audio LLM doit rester ouvert (bug terrain ).
        self._wake_bypass_until = time.monotonic() + 10.0
        self._speaker_cache_invalidate()
        # Rotation body vers la source sonore (spatialisation XMOS DOA).
        # Calcule l'angle pour le transmettre dans le payload SIGUSR1 (mode histoire)
        # ou l'envoyer via HTTP /turn_body (mode normal).
        angle_body = None
        if self.doa_reader is not None:
            doa_energy = self.doa_reader.last_energy
            if doa_energy > 0.01:
                angle_body = max(-math.pi / 2, min(math.pi / 2, self.doa_reader.last_angle_rad))
                logger.info(
                    "Wake word DOA: body turn to %+.0f° (energy=%.2f)",
                    math.degrees(angle_body), doa_energy,
                )
        # Gate dynamique : interrompre la lecture en cours et écouter
        if self._mode_gate("wake_word_interrupts_reading"):
            # wake-priority — SIGUSR1 (<5 ms observé ) remplace les 4 IPC HTTP
            # séquentiels (turn_body + wake + send_event + wake) qui saturaient
            # le canal :8766 pendant les bursts audio_delta OpenAI. Le signal
            # POSIX préempte le epoll_wait du loop asyncio conv_app_v2 →
            # handler exécuté immédiatement quel que soit le niveau de charge
            # WS / GStreamer / keepalive.
            # Côté conv, engine.handle_wake_event fait tout en une passe :
            # cancel_response + unmute + save galileo progress + clear_playback +
            # reset_wobbler + wake motors + ack gesture + turn_body + inject_event.
            event_instructions = (
                "STOP lecture. L'utilisateur vient de dire 'Hey Reachy' pour t'interrompre. "
                "Écoute sa demande. Réponds brièvement. "
                "Quand il a fini, propose de reprendre la lecture où tu en étais. "
                "NE reprends PAS la lecture automatiquement — attends qu'il te le demande."
            )
            event_text = (
                "[Reachy Care] Wake word détecté pendant la lecture. "
                "La progression est sauvegardée. Écoute ce que la personne veut dire."
            )
            if bridge.wake_interrupt(
                doa_rad=angle_body,
                event_text=event_text,
                event_instructions=event_instructions,
            ):
                self._last_bridge_activity = time.monotonic()
                logger.info("Wake word MODE_HISTOIRE via SIGUSR1 (unifié, 1 signal).")
                return
            # Fallback HTTP si PID absent / process KO — ancien chemin 4 IPC
            logger.warning("Wake word MODE_HISTOIRE fallback HTTP (signal failed).")
            if angle_body is not None:
                try: bridge.turn_body(angle_body, duration=0.8)
                except Exception as _e: logger.debug("turn_body fallback failed: %s", _e)
            bridge.wake()
            bridge.send_event(event_text, instructions=event_instructions)
            bridge.wake()
            self._last_bridge_activity = time.monotonic()
            logger.info("Wake word en MODE_HISTOIRE : lecture interrompue, écoute active (fallback HTTP).")
            return
        # Mode normal — turn_body HTTP (pas saturé hors lecture)
        if angle_body is not None:
            try: bridge.turn_body(angle_body, duration=0.8)
            except Exception as _e: logger.debug("turn_body failed: %s", _e)
        # Réveil du mode sommeil logiciel — lever la tête et reprendre les loops
        if self._sleeping:
            self._sleeping = False
            # Reset complet de l'état face/visiteur — évite le MODE SILENCE SOCIAL au réveil
            self._face_miss_count = 0
            self._last_greeted = None
            self._present_people.clear()
            self._last_greet.clear()
            self._visitor_present = False
            self._visitor_frames_count = 0
            self._visitor_miss_count = 0
            self._last_face_seen_time = time.monotonic()
            logger.info("Réveil sleep_mode — état face/visiteur réinitialisé.")
            # Reset pitch override (mode échecs = 30°) → position neutre
            bridge.set_head_pitch(0.0)
            # Cycle daemon stop→start wake_up=true pour reflash les servos
            # (BUG_MOTEURS_DAEMON_DESYNC.md : sans ce cycle, les servos restent désync après goto_sleep)
            try:
                requests.post(
                    f"{config.REACHY_DAEMON_URL}/api/daemon/stop?goto_sleep=false",
                    timeout=config.REACHY_DAEMON_TIMEOUT,
                )
                time.sleep(3)
                requests.post(
                    f"{config.REACHY_DAEMON_URL}/api/daemon/start?wake_up=true",
                    timeout=config.REACHY_DAEMON_TIMEOUT,
                )
                time.sleep(5)
                logger.info("Réveil sleep_mode — daemon stop→start wake_up=true OK.")
            except Exception as _wake_e:
                logger.warning("Réveil daemon cycle échoué : %s — fallback _do_wake_motors", _wake_e)
                try:
                    self._do_wake_motors()
                except Exception as _e2:
                    logger.warning("Fallback _do_wake_motors échoué : %s", _e2)
        # Wake word explicite : prime sur le silence social
        if self._visitor_present:
            logger.info("Wake word — sortie temporaire du mode visiteur.")
            self._visitor_present = False
            self._visitor_miss_count = 0
            self._visitor_frames_count = 0
            bridge.set_visitor_mode(False)
        # Si le bridge était muté (stop_speaking), on le démute
        if bridge.is_muted():
            bridge.unmute()
            # Ré-injecter le contexte personne immédiatement si on connaît quelqu'un
            if self._last_greeted and self.memory:
                mem = self.memory.on_seen(self._last_greeted)
                bridge.set_context(
                    person=self._last_greeted,
                    memory_summary=mem.get("conversation_summary"),
                    profile=mem.get("profile"),
                )
                self._last_greeted = None  # force re-détection fraîche à la prochaine frame
                self._present_people.clear()
                self._last_greet.clear()
            bridge.send_event(
                "[Reachy Care] Wake word détecté — reprends la conversation normalement.",
                instructions="L'utilisateur vient de dire 'Hey Reachy'. Reprends ta personnalité de Douze normalement. Réponds brièvement.",
            )
        bridge.wake()
        self._last_bridge_activity = time.monotonic()

    # ------------------------------------------------------------------
    # Keepalive bridge
    # ------------------------------------------------------------------

    def _restart_conv_app(self) -> None:
        """Redémarre la conv_app en arrière-plan pour ne pas bloquer la boucle principale."""
        def _do_restart() -> None:
            pid_file = "/tmp/conv_app.pid"
            log_path = "/home/pollen/reachy_care/logs/conv_app.log"
            try:
                if os.path.exists(pid_file):
                    with open(pid_file) as f:
                        old_pid = int(f.read().strip())
                    os.kill(old_pid, signal.SIGTERM)
                    time.sleep(3)
            except Exception as exc:
                logger.warning("Arrêt conv_app échoué : %s", exc)
            try:
                with open(log_path, "a") as log_f:
                    care_dir = "/home/pollen/reachy_care"
                    env = {
                        **os.environ,
                        "REACHY_MINI_CUSTOM_PROFILE": "reachy_care",
                        "REACHY_MINI_EXTERNAL_PROFILES_DIRECTORY": f"{care_dir}/external_profiles",
                        "REACHY_MINI_EXTERNAL_TOOLS_DIRECTORY": f"{care_dir}/tools_for_conv_app",
                        "AUTOLOAD_EXTERNAL_TOOLS": "true",
                    }
                    proc = subprocess.Popen(
                        ["/venvs/apps_venv/bin/reachy-mini-conversation-app"],
                        stdout=log_f, stderr=log_f,
                        env=env,
                    )
                with open(pid_file, "w") as f:
                    f.write(str(proc.pid))
                logger.info("Conv_app redémarrée (PID %d) — session OpenAI Realtime réinitialisée.", proc.pid)
                self._last_greeted = None  # nouvelle session — force re-injection contexte facial
                self._present_people.clear()
                self._last_greet.clear()
            except Exception as exc:
                logger.error("Redémarrage conv_app échoué : %s", exc)

        threading.Thread(target=_do_restart, daemon=True).start()

    def _check_conv_app_health(self) -> None:
        """Reconnexion proactive à 55 min avant l'expiration de la session
        OpenAI Realtime (60 min).

        Politique "no-proactive" : les anciens comportements "keepalive silence
        prolongé" et "inject_memory 3 min" qui faisaient parler Reachy
        spontanément ont été supprimés. Reachy reste silencieux par défaut et
        ne répond que quand on le sollicite directement (voix, wake word,
        événement Reachy Care — face, chute, wake, etc.).
        La reconnexion 55 min reste active car elle est purement technique
        (évite l'expiration silencieuse de la session OpenAI Realtime).
        """
        t = time.monotonic()

        if t - self._conv_app_start_time > 3300:  # 55 minutes
            logger.warning("Session OpenAI Realtime proche de l'expiration (55min) — redémarrage conv_app")
            self._restart_conv_app()
            self._conv_app_start_time = t

    # ------------------------------------------------------------------
    # Signal et arrêt propre
    # ------------------------------------------------------------------

    def _signal_handler(self, signum, frame) -> None:
        """Gestionnaire de signal SIGTERM / SIGINT."""
        logger.info("Signal %d reçu — arrêt demandé.", signum)
        self._stop = True

    def _summarize_session(self) -> None:
        """Génère et sauvegarde résumé + faits structurés pour chaque personne vue."""
        if not self._seen_persons or self.memory is None:
            return

        api_key = os.getenv("OPENAI_API_KEY") or self._read_openai_key_from_env_file()
        if not api_key:
            logger.warning("Résumé session ignoré : OPENAI_API_KEY introuvable.")
            return

        today = date.today().isoformat()
        events_text = "\n".join(self._session_events) if self._session_events else "Aucun événement notable."

        for name, mem in self._seen_persons.items():
            # --- Appel 1 : résumé narratif ---
            existing = mem.get("conversation_summary", "")
            prompt_summary = (
                f"Tu gères la mémoire d'un robot compagnon pour personnes âgées.\n"
                f"Personne : {name}\n"
                f"Résumé existant : {existing or 'Aucun'}\n"
                f"Événements de cette session :\n{events_text}\n\n"
                "Génère un résumé concis (3 phrases max) de cette session. "
                "Mentionne les activités faites, l'ambiance générale, rien de plus. "
                "Réponds uniquement avec le résumé, sans introduction."
            )
            summary = ""
            try:
                resp = requests.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": "gpt-4o-mini",
                        "messages": [{"role": "user", "content": prompt_summary}],
                        "max_tokens": 150,
                        "temperature": 0.3,
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                summary = resp.json()["choices"][0]["message"]["content"].strip()
                logger.info("Résumé session généré pour %s.", name)
            except Exception as exc:
                logger.warning("Résumé session échoué pour %s : %s", name, exc)
                summary = events_text[:200] if events_text else ""

            # Sauvegarde dans l'historique roulant
            self.memory.add_session(name, {
                "date": today,
                "summary": summary,
                "activities": [e for e in self._session_events if "echecs" in e.lower() or "histoire" in e.lower()],
            })

            # --- Appel 2 : extraction de faits structurés ---
            if events_text and events_text != "Aucun événement notable.":
                prompt_facts = (
                    f"Événements d'une session avec {name} :\n{events_text}\n\n"
                    "Extrait les faits importants sous forme de liste JSON. "
                    "Chaque fait est un objet avec les champs 'fact' (string) et 'category' "
                    "(une parmi : santé, famille, préférences, habitudes, activités). "
                    "Exemple : [{\"fact\": \"A mal au genou droit\", \"category\": \"santé\"}]. "
                    "Si aucun fait notable, réponds avec []. "
                    "Réponds UNIQUEMENT avec le JSON valide, sans explication."
                )
                try:
                    resp2 = requests.post(
                        "https://api.openai.com/v1/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={
                            "model": "gpt-4o-mini",
                            "messages": [{"role": "user", "content": prompt_facts}],
                            "max_tokens": 300,
                            "temperature": 0.1,
                        },
                        timeout=15,
                    )
                    resp2.raise_for_status()
                    raw = resp2.json()["choices"][0]["message"]["content"].strip()
                    facts = json.loads(raw)
                    if isinstance(facts, list) and facts:
                        # Ajoute la date à chaque fait
                        for f in facts:
                            if isinstance(f, dict):
                                f.setdefault("date", today)
                                f.setdefault("source", "session")
                        self.memory.add_facts(name, facts)
                        logger.info("Faits extraits pour %s : %d faits.", name, len(facts))
                except Exception as exc:
                    logger.warning("Extraction faits échouée pour %s : %s", name, exc)

    def _read_openai_key_from_env_file(self) -> str | None:
        """Lit OPENAI_API_KEY depuis le .env de reachy_mini_conversation_app."""
        matches = glob.glob(
            "/venvs/apps_venv/lib/python*/site-packages/reachy_mini_conversation_app/.env"
        )
        if not matches:
            logger.debug("_read_openai_key: fichier .env non trouvé via glob")
            return None
        env_path = matches[0]
        try:
            with open(env_path, encoding="utf-8") as f:
                for line in f:
                    if line.startswith("OPENAI_API_KEY="):
                        return line.split("=", 1)[1].strip()
        except FileNotFoundError:
            logger.debug("_read_openai_key: fichier .env non trouvé : %s", env_path)
        except Exception as exc:
            logger.warning("_read_openai_key: erreur lecture .env : %s", exc)
        return None

    def shutdown(self) -> None:
        """Libère les ressources et nettoie le fichier PID."""
        logger.info("Arrêt de Reachy Care …")

        self._summarize_session()

        try:
            if self.dashboard is not None:
                self.dashboard.stop()
                logger.info("Dashboard arrêté.")
        except Exception as exc:
            logger.debug("dashboard.stop(): %s", exc)

        try:
            if self.sound_det is not None:
                self.sound_det.stop()
                logger.info("SoundDetector arrêté.")
        except Exception as exc:
            logger.debug("sound_det.stop(): %s", exc)

        try:
            if self.wake_word is not None:
                self.wake_word.stop()
                logger.info("WakeWordDetector arrêté.")
        except Exception as exc:
            logger.debug("wake_word.stop(): %s", exc)

        try:
            if self.chess_eng is not None:
                self.chess_eng.close()
                logger.info("ChessEngine fermé.")
        except Exception as exc:
            logger.debug("chess_eng.close(): %s", exc)

        try:
            if self.fall_det is not None:
                self.fall_det.close()
                logger.info("FallDetector fermé.")
        except Exception as exc:
            logger.debug("fall_det.close(): %s", exc)

        try:
            if config.PID_FILE.exists():
                config.PID_FILE.unlink()
                logger.info("PID_FILE supprimé : %s", config.PID_FILE)
        except Exception as exc:
            logger.debug("Suppression PID_FILE: %s", exc)

        with contextlib.suppress(FileNotFoundError):
            os.remove("/tmp/reachy_care_status.json")

        logger.info("Reachy Care arrêté proprement")


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reachy Care — orchestrateur principal")
    parser.add_argument("--debug", action="store_true", help="Active le mode DEBUG")
    parser.add_argument("--no-chess", action="store_true", help="Désactive le module chess")
    parser.add_argument("--no-face", action="store_true", help="Désactive la reconnaissance faciale")
    args = parser.parse_args()

    setup_logging(debug=args.debug)

    app = ReachyCare(
        enable_chess=not args.no_chess,
        enable_face=not args.no_face,
    )
    app.run()
