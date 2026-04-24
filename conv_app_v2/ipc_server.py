"""
conv_app_v2/ipc_server.py — HTTP IPC server on localhost:8766.

API is identical to the existing Pollen patch so that conv_app_bridge.py
(in main.py) requires no modifications.
"""
import asyncio
import io
import json
import logging
import time
import math
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np
import requests

logger = logging.getLogger(__name__)

_PORT = 8766


def _make_handler(engine):
    """Return a handler class closed over the given ConversationEngine."""

    class _Handler(BaseHTTPRequestHandler):
        # ------------------------------------------------------------------
        # Routing
        # ------------------------------------------------------------------

        def do_POST(self):  # noqa: N802
            body = self._read_body()
            path = self.path

            try:
                if path == "/event":
                    text = body.get("text", "")
                    instructions = body.get("instructions", "")
                    self._schedule(engine.inject_event(text, instructions))

                elif path == "/session_update":
                    instructions = body.get("instructions", "")
                    self._schedule(engine.update_instructions(instructions))

                elif path == "/cancel":
                    self._schedule(engine.cancel_response())

                elif path == "/wake":
                    # The full wake logic lives in engine.handle_wake_event so
                    # that the HTTP /wake path and the SIGUSR1 handler share a
                    # single implementation. This HTTP path remains the
                    # fallback when the SIGUSR1 path fails on main.py's side
                    # (missing PID file, zombie process). Optional doa_rad
                    # payload is forwarded untouched.
                    doa_rad = body.get("doa_rad")
                    if doa_rad is not None:
                        try: doa_rad = float(doa_rad)
                        except (TypeError, ValueError): doa_rad = None
                    self._schedule(engine.handle_wake_event(
                        doa_rad=doa_rad,
                        event_text=body.get("event_text"),
                        event_instructions=body.get("event_instructions"),
                    ))

                elif path == "/doa_gate":
                    in_cone = bool(body.get("in_cone", True))
                    engine._doa_in_cone = in_cone
                    logger.info("IPC /doa_gate: in_cone=%s angle=%+.0f° energy=%.2f",
                                in_cone, body.get("angle_deg", 0.0), body.get("energy", 0.0))

                elif path == "/sleep":
                    # Sleep cascade while audio is actively playing. Muting and
                    # calling robot.sleep without first cancelling the LLM
                    # response and clearing the playback queue lets goto_sleep
                    # on the daemon run while aplay is still draining, which
                    # produces a broken IPC pipe and a zombie daemon. Fix:
                    # cancel + clear BEFORE mute, mirroring the pattern used
                    # in /wake.
                    self._schedule(engine.cancel_response())
                    try:
                        if getattr(engine, "_audio", None) is not None:
                            engine._audio.clear_playback()
                    except Exception as exc:
                        logger.warning("/sleep: clear_playback failed: %s", exc)
                    engine.mute()
                    if engine._robot is not None:
                        engine._robot.sleep()

                elif path == "/disable_vad":
                    # Chess pipeline: stop sending audio to LLM
                    engine.mute()

                elif path == "/enable_vad":
                    # Chess pipeline: resume audio to LLM
                    engine.unmute()

                elif path == "/set_head_pitch":
                    pitch_deg = float(body.get("pitch_deg", 0.0))
                    pitch_rad = math.radians(pitch_deg)
                    if engine._robot is not None:
                        engine._robot.set_head_pitch(pitch_rad)

                elif path == "/turn_body":
                    # Rotate the body toward a direction (typically a wake
                    # word pointing at the DOA of the sound). Queues a
                    # GotoQueueMove in movement_manager: non-blocking,
                    # compatible with an ongoing head tracking move.
                    angle_rad = float(body.get("angle_rad", 0.0))
                    duration = float(body.get("duration", 1.0))
                    if engine._robot is not None:
                        engine._robot.turn_body(angle_rad, duration)

                elif path == "/set_motors":
                    enabled = body.get("enabled", True)
                    mode = "enabled" if enabled else "disabled"
                    try:
                        resp = requests.post(
                            f"http://localhost:8000/api/motors/set_mode/{mode}",
                            timeout=5,
                        )
                        resp.raise_for_status()
                    except Exception as exc:
                        logger.warning("IPC /set_motors: daemon REST failed: %s", exc)

                elif path == "/attention":
                    state = body.get("state", "TO_COMPUTER")
                    # Ignore /attention while Reachy is speaking. When the
                    # head tracks during a sentence, the user bbox drifts off
                    # centre and AttenLabs flips to SILENT, which would cut
                    # the sentence short. Reachy must be allowed to finish.
                    # Note: an extra user_speaking_visual gate was tried and
                    # REVERTED because MediaPipe was firing lip-movement
                    # detections on Reachy's own reflected voice, which kept
                    # SILENT ignored permanently and produced an echo loop.
                    if engine._speaking:
                        logger.info("IPC /attention: state=%s IGNORED (Reachy speaking)", state)
                    else:
                        engine._attention_state = state
                        if state == "TO_COMPUTER":
                            engine._attention_last_to_computer = time.monotonic()
                        logger.info("IPC /attention: state=%s", state)

                elif path == "/user_speaking":
                    # Visual lip-movement signal (cross-checked with Silero VAD).
                    speaking = body.get("speaking", False)
                    engine._user_speaking_visual = speaking
                    if speaking:
                        engine._user_speaking_visual_at = time.monotonic()
                    logger.info("IPC /user_speaking: %s", speaking)

                else:
                    self._respond(404, {"error": f"unknown path: {path}"})
                    return

                self._respond(200, {"ok": True})

            except Exception as exc:  # noqa: BLE001
                logger.exception("IPC handler error on %s: %s", path, exc)
                self._respond(500, {"error": str(exc)})

        def do_GET(self):  # noqa: N802
            if self.path == "/get_frame":
                frame = None
                # Try CameraWorker first, then mini.media
                robot = engine._robot
                if robot is not None:
                    if robot.camera_worker is not None:
                        try:
                            # BUG FIX: vrai nom SDK Pollen est get_latest_frame() (camera_worker.py:61)
                            # get_frame() levait AttributeError -> fallback mini.media.get_frame()
                            # Ref: AUDIT_V2_LIGNE_robot_ipc_tools.md §BUG 1
                            frame = robot.camera_worker.get_latest_frame()
                        except Exception:
                            pass
                    if frame is None and robot.mini is not None:
                        try:
                            frame = robot.mini.media.get_frame()
                        except Exception:
                            pass
                if frame is None:
                    self._respond(503, {"error": "no frame available"})
                    return
                _, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                body = jpeg.tobytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._respond(404, {"error": f"unknown GET path: {self.path}"})

        # ------------------------------------------------------------------
        # Helpers
        # ------------------------------------------------------------------

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {}

        def _respond(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _schedule(self, coro) -> None:
            """Schedule a coroutine on the engine's asyncio loop (thread-safe)."""
            loop = engine._loop
            if loop is not None and loop.is_running():
                asyncio.run_coroutine_threadsafe(coro, loop)
            else:
                logger.warning("IPC: asyncio loop not available, dropping coroutine")

        # Silence HTTP access logs
        def log_message(self, fmt, *args):  # noqa: ARG002
            pass

    return _Handler


class _ReuseHTTPServer(ThreadingHTTPServer):
    # ThreadingHTTPServer is used because the previous single-threaded
    # HTTPServer was being saturated by main.py (/attention and
    # /user_speaking at 5 Hz plus a blocking /set_motors that could take 5s)
    # and /wake would exceed the client's 2s timeout, so Reachy never
    # acknowledged the wake word. To revert, switch back to HTTPServer.
    allow_reuse_address = True
    daemon_threads = True


class IPCServer:
    """HTTP IPC server that exposes the ConversationEngine to conv_app_bridge.py."""

    def __init__(self, engine):
        self._engine = engine
        self._server: _ReuseHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        handler = _make_handler(self._engine)
        self._server = _ReuseHTTPServer(("127.0.0.1", _PORT), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="ipc-server",
            daemon=True,
        )
        self._thread.start()
        logger.info("IPC server listening on http://127.0.0.1:%d", _PORT)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None
        logger.info("IPC server stopped.")
