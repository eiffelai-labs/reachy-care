"""
robot.py — Robot layer for conv_app_v2.

Imports from the installed Pollen packages (reachy_mini + reachy_mini_conversation_app).
Replicates exactly what Pollen's main.py does: ReachyMini + MovementManager + HeadWobbler + CameraWorker.

On Mac (dev), all imports fail gracefully → headless mode.
"""

import logging
import sys

log = logging.getLogger(__name__)


class RobotLayer:
    """Wraps Pollen SDK for robot movements — identical to Pollen's own initialization."""

    def __init__(self):
        self._mini = None
        self._movement_manager = None
        self._camera_worker = None
        self._wobbler = None

    async def start(self) -> None:
        """Connect to robot and start all movement subsystems."""
        # Step 1: Import ReachyMini
        try:
            from reachy_mini import ReachyMini
        except ImportError as exc:
            log.warning("reachy_mini SDK not found (%s) — headless mode", exc)
            return

        # Step 2: Connect
        try:
            self._mini = ReachyMini()
            log.info("ReachyMini connected")
        except Exception as exc:
            log.error("ReachyMini connection failed: %s — headless mode", exc)
            return

        # Step 3: CameraWorker (face tracking)
        # Without a head_tracker instance, is_head_tracking_enabled=True has no
        # effect (the working_loop guards on `head_tracker is not None`). We pass
        # the MediaPipe HeadTracker so tracking actually runs.
        try:
            from reachy_mini_conversation_app.camera_worker import CameraWorker
            head_tracker = None
            # MediaPipe face mesh = léger (~15 MB), déjà installé avec reachy_mini_toolbox.
            # YOLO disponible aussi si yolo_vision extras, mais plus lourd (200+ MB).
            try:
                from reachy_mini_toolbox.vision import HeadTracker
                head_tracker = HeadTracker()
                log.info("MediaPipe HeadTracker loaded (face detection léger pour tracking)")
            except Exception as ht_exc:
                log.warning("HeadTracker MediaPipe KO, tentative YOLO : %s", ht_exc)
                try:
                    from reachy_mini_conversation_app.vision.yolo_head_tracker import HeadTracker as YHT
                    head_tracker = YHT()
                    log.info("YOLO HeadTracker loaded (fallback)")
                except Exception as yht_exc:
                    log.warning("Tous HeadTracker KO, tracking visage inactif : %s", yht_exc)
            self._camera_worker = CameraWorker(self._mini, head_tracker)
            log.info("CameraWorker created%s", " WITH head_tracker" if head_tracker else " WITHOUT head_tracker")
        except Exception as exc:
            log.warning("CameraWorker not available: %s", exc)

        # Step 4: MovementManager (takes robot + camera_worker)
        try:
            from reachy_mini_conversation_app.moves import MovementManager
            self._movement_manager = MovementManager(
                current_robot=self._mini,
                camera_worker=self._camera_worker,
            )
            log.info("MovementManager created")
        except Exception as exc:
            log.warning("MovementManager not available: %s", exc)

        # Step 5: HeadWobbler (takes set_speech_offsets callback from MovementManager)
        try:
            from reachy_mini_conversation_app.audio.head_wobbler import HeadWobbler
            if self._movement_manager is not None:
                self._wobbler = HeadWobbler(
                    set_speech_offsets=self._movement_manager.set_speech_offsets,
                )
                log.info("HeadWobbler created (linked to MovementManager)")
            else:
                log.warning("HeadWobbler skipped — no MovementManager")
        except Exception as exc:
            log.warning("HeadWobbler not available: %s", exc)

        # Step 5b: Patch BreathingMove — disable antenna sway (instance attr override)
        # A class-level patch does not stick because BreathingMove.__init__ sets
        # the attribute on each instance, so we wrap __init__ to override it.
        try:
            from reachy_mini_conversation_app.moves import BreathingMove as _BM
            _orig_bm_init = _BM.__init__
            def _patched_bm_init(self, *a, **kw):
                _orig_bm_init(self, *a, **kw)
                self.antenna_sway_amplitude = 0.0
            _BM.__init__ = _patched_bm_init
            log.info("BreathingMove: antenna_sway patched to 0.0")
        except Exception as exc:
            log.warning("BreathingMove patch failed: %s", exc)

        # Step 6: Start workers
        if self._movement_manager:
            try:
                self._movement_manager.start()
                log.info("MovementManager started (100Hz control loop)")
            except Exception as exc:
                log.error("MovementManager.start() failed: %s", exc)

        if self._wobbler:
            try:
                self._wobbler.start()
                log.info("HeadWobbler started")
            except Exception as exc:
                log.warning("HeadWobbler.start() failed: %s", exc)

        # CameraWorker.start() — démarre le thread de capture 30 Hz qui alimente
        # latest_frame via reachy_mini.media.get_frame(). Sans cet appel, le
        # worker est créé mais ne tourne pas → IPC /get_frame renvoie 404 →
        # main.py bridge.get_frame() reçoit rien → face reco et AttenLabs
        # aveugles. Bug latent du portage initial (start() oublié).
        if self._camera_worker:
            try:
                self._camera_worker.start()
                log.info("CameraWorker started (capture thread 30Hz)")
            except Exception as exc:
                log.warning("CameraWorker.start() failed: %s", exc)

            # Activation explicite du face tracking. Sans cet appel, il reste OFF
            # parce que set_head_pitch(HEAD_IDLE_PITCH_DEG=10) côté main.py
            # déclenche le fallback SDK set_head_tracking_enabled(False).
            try:
                self._camera_worker.set_head_tracking_enabled(True)
                log.info("CameraWorker: head tracking ENABLED (suit les visages)")
            except Exception as exc:
                log.warning("set_head_tracking_enabled failed: %s", exc)

            # Activation du body_yaw automatique. Sans ça, la tête clamp à ±58°
            # yaw (contrainte |body_yaw − head_yaw| ≤ 65°). Avec auto body, le
            # corps tourne pour étendre la plage de suivi : le robot peut suivre
            # un visage qui se déplace latéralement au-delà de ±58°.
            try:
                self._mini.set_automatic_body_yaw(True)
                log.info("ReachyMini: automatic body_yaw ENABLED (extension plage suivi)")
            except Exception as exc:
                log.warning("set_automatic_body_yaw failed: %s", exc)

        log.info("RobotLayer ready (mini=%s, mm=%s, cw=%s, wob=%s)",
                 self._mini is not None, self._movement_manager is not None,
                 self._camera_worker is not None, self._wobbler is not None)

        # Step 7: Start GStreamer audio playback AVANT wake_up (pattern Pollen
        # vanilla console.py:373-374). wake_up() joue un son d'animation qui
        # claim le sink → si start_playing() passe après, l'ouverture du
        # pipeline échoue "Device busy" (bootstrap-order-fix). Ring buffer
        # 50 ms, thread GLib hors GIL.
        try:
            self._mini.media.start_playing()
            log.info("Media playback started (GStreamer appsrc)")
        except Exception as exc:
            log.error("Media start_playing failed: %s", exc)

        # Step 8: Wake up the robot (enable motors + stand up)
        self.wake()

    def wake(self) -> None:
        """Wake the robot: turn on + wake_up (like Pollen's main.py)."""
        if self._mini is None:
            return
        try:
            self._mini.enable_motors()
            log.info("Robot motors enabled")
        except Exception as exc:
            log.warning("turn_on failed: %s", exc)
        try:
            self._mini.wake_up()
            log.info("Robot woke up (standing position)")
        except Exception as exc:
            log.warning("wake_up failed: %s", exc)

    def sleep(self) -> None:
        """Put the robot to sleep (Pollen pattern)."""
        if self._mini is None:
            return
        try:
            self._mini.goto_sleep()
            log.info("Robot sleeping")
        except Exception as exc:
            log.warning("sleep failed: %s", exc)

    def stop(self) -> None:
        """Stop all workers and disconnect."""
        if self._wobbler:
            try:
                self._wobbler.stop()
            except Exception:
                pass
        if self._movement_manager:
            try:
                self._movement_manager.stop()
            except Exception:
                pass
        if self._mini is not None:
            try:
                self._mini.media.stop_playing()
            except Exception:
                pass
        if self._mini:
            try:
                self._mini.disconnect()
            except Exception:
                pass
        log.info("RobotLayer stopped")

    def clear_playback(self) -> None:
        """Flush appsrc pour barge-in. Détection backend comme Pollen console.py:441-445."""
        if self._mini is None:
            return
        # Instrumentation : trace la stack d'appel pour identifier qui déclenche
        # clear_playback entre deux chunks. Le suspect est clear_player() qui
        # fait flush_stop(reset_time=True) sur l'appsrc, ce qui casse le
        # running-time du pipeline et fait dropper silencieusement les buffers
        # du chunk suivant par alsasink.
        import traceback as _tb
        caller = _tb.format_stack()[-3].strip().replace("\n", " | ")
        log.info("CLEAR_PLAYBACK CALLED | from: %s", caller[:240])
        try:
            from reachy_mini.media.media_manager import MediaBackend
            if self._mini.media.backend == MediaBackend.GSTREAMER:
                self._mini.media.audio.clear_player()
            else:
                self._mini.media.audio.clear_output_buffer()
        except Exception as exc:
            log.warning("clear_playback failed: %s", exc)

    def feed_wobbler(self, audio_delta_b64: str) -> None:
        """Feed base64-encoded audio delta to wobbler for sway animation.

        NOTE: Pollen's wobbler expects the raw base64 string from OpenAI
        (event.delta), NOT decoded PCM bytes.
        """
        if self._wobbler:
            try:
                self._wobbler.feed(audio_delta_b64)
            except Exception as exc:
                log.debug("wobbler.feed error: %s", exc)

    def set_listening(self, listening: bool) -> None:
        """Signal speech started/stopped to movement system."""
        if self._movement_manager:
            try:
                self._movement_manager.set_listening(listening)
            except Exception as exc:
                log.debug("set_listening error: %s", exc)

    def reset_wobbler(self) -> None:
        """Reset wobbler state (after tool calls, speech_started, etc.)."""
        if self._wobbler:
            try:
                self._wobbler.reset()
            except Exception:
                pass

    def set_head_pitch(self, pitch_rad: float) -> None:
        """Override head pitch (chess mode).

        Le SDK Pollen camera_worker n'expose pas set_head_pitch_override (juste
        set_head_tracking_enabled). Sans appel explicite à goto_target, on
        tombait dans le fallback qui désactivait juste le tracking, sans
        commander de pitch réel → la tête restait figée à sa position
        précédente (ChessActivityModule start_game signalait "tête baissée"
        en log mais rien ne bougeait physiquement). D'où l'appel direct
        self._mini.goto_target() en plus du toggle tracking.
        """
        if self._camera_worker:
            try:
                if hasattr(self._camera_worker, "set_head_pitch_override"):
                    self._camera_worker.set_head_pitch_override(pitch_rad)
                elif hasattr(self._camera_worker, "set_head_tracking_enabled"):
                    # Désactive le tracking si pitch ≠ 0 pour figer la tête à la
                    # nouvelle position (sinon tracking ramène la tête vers les visages).
                    self._camera_worker.set_head_tracking_enabled(pitch_rad == 0.0)
            except Exception as exc:
                log.debug("set_head_pitch tracking toggle error: %s", exc)
        # Commande physique la tête via SDK — le tracking seul ne bouge pas la tête
        # quand désactivé. En mode echecs pitch=15° (HEAD_CHESS_PITCH_DEG) pour
        # regarder le plateau, retour à pitch=0° quand on sort (appel set_head_pitch(0)).
        if self._mini is not None:
            try:
                from reachy_mini.utils import create_head_pose
                head = create_head_pose(pitch=pitch_rad)  # pitch déjà en radians
                self._mini.goto_target(head=head, duration=1.2)
                import math as _m
                log.info("set_head_pitch: goto_target pitch=%.3f rad (%.1f°)",
                         pitch_rad, _m.degrees(pitch_rad))
            except Exception as exc:
                log.warning("set_head_pitch goto_target failed: %s", exc)

    def acknowledge_gesture(self) -> None:
        """Accusé de présence gestuel (wake word) : antennes divergent à ±π/2
        puis reviennent neutre. Utilise le pattern éprouvé de tools/move_head.py,
        movement_manager.queue_move avec GotoQueueMove, qui respecte le
        control loop 100 Hz du movement_manager et évite les conflits entre
        goto_target directs (blocage silencieux des antennes observé sans cette
        sérialisation).
        """
        if self._mini is None or self._movement_manager is None:
            return
        try:
            import math as _m
            import time as _t
            from reachy_mini_conversation_app.dance_emotion_moves import GotoQueueMove

            current_head = self._mini.get_current_head_pose()
            _, current_joints = self._mini.get_current_joint_positions()
            current_body_yaw = float(current_joints[0])
            current_ant = (float(current_joints[1]), float(current_joints[2])) if len(current_joints) >= 3 else (0.0, 0.0)

            # Move 1 : aller à ±90° divergence extérieure.
            move_out = GotoQueueMove(
                target_head_pose=current_head,
                start_head_pose=current_head,
                target_antennas=(-_m.pi / 2, _m.pi / 2),
                start_antennas=current_ant,
                target_body_yaw=current_body_yaw,
                start_body_yaw=current_body_yaw,
                duration=0.2,
            )
            self._movement_manager.queue_move(move_out)
            self._movement_manager.set_moving_state(0.2)
            _t.sleep(0.25)

            # Move 2 : retour au neutre anti-shaking.
            move_back = GotoQueueMove(
                target_head_pose=current_head,
                start_head_pose=current_head,
                target_antennas=(-0.1745, 0.1745),
                start_antennas=(-_m.pi / 2, _m.pi / 2),
                target_body_yaw=current_body_yaw,
                start_body_yaw=current_body_yaw,
                duration=0.2,
            )
            self._movement_manager.queue_move(move_back)
            self._movement_manager.set_moving_state(0.2)
            log.info("acknowledge_gesture: antennes ±90°→neutre via queue_move (wake ack)")
        except Exception as exc:
            log.warning("acknowledge_gesture failed: %s", exc)

    def turn_body(self, angle_rad: float, duration: float = 1.0) -> None:
        """Tourne le corps vers un yaw cible (radians). Appelé au wake word
        pour pointer Reachy vers la source sonore (DOA XMOS)."""
        if self._mini is None or self._movement_manager is None:
            return
        try:
            from reachy_mini.utils import create_head_pose
            from reachy_mini_conversation_app.dance_emotion_moves import GotoQueueMove

            current_head_pose = self._mini.get_current_head_pose()
            _, current_antennas = self._mini.get_current_joint_positions()
            # Head neutre : on laisse camera_worker reprendre le tracking
            # une fois le corps réorienté.
            target_head = create_head_pose(0, 0, 0, 0, 0, 0, degrees=True)
            move = GotoQueueMove(
                target_head_pose=target_head,
                start_head_pose=current_head_pose,
                target_antennas=(0, 0),
                start_antennas=(current_antennas[0], current_antennas[1]),
                target_body_yaw=angle_rad,
                start_body_yaw=current_antennas[0],
                duration=duration,
            )
            self._movement_manager.queue_move(move)
            self._movement_manager.set_moving_state(duration)
            log.info("turn_body: queued yaw=%+.2f rad in %.1fs", angle_rad, duration)
        except Exception as exc:
            log.warning("turn_body failed: %s", exc)

    @property
    def mini(self):
        return self._mini

    @property
    def movement_manager(self):
        return self._movement_manager

    @property
    def camera_worker(self):
        return self._camera_worker

    @property
    def wobbler(self):
        return self._wobbler
