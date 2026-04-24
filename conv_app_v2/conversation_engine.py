"""
conv_app_v2/conversation_engine.py — Central asyncio orchestrator.
"""
import asyncio
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
from scipy.signal import resample

logger = logging.getLogger(__name__)


class ConversationEngine:
    """Central orchestrator for the conv_app_v2 conversation pipeline."""

    def __init__(self):
        self._audio_muted: bool = False
        self._speaking: bool = False  # True while LLM is producing audio (gate)
        self._running: bool = False
        self._llm = None
        self._audio = None
        self._robot = None
        self._ipc = None
        self._tools: Dict[str, Any] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._echo_canceller = None  # kept for compat; unused since we switched to server_vad.
        self._audio_send_count: int = 0
        self._audio_delta_count: int = 0
        # AttenLabs gate: etats SILENT / TO_HUMAN / TO_COMPUTER
        # "TO_COMPUTER" = robot est en interaction avec le patient -> on traite l'audio
        # Tout autre etat -> on ignore (conversation entre humains, TV, etc.)
        # Conservative default SILENT: if main.py has not yet pushed an attention
        # state via IPC /attention after restart, the gate is closed. main.py
        # flips us to TO_COMPUTER as soon as an owner face is detected on camera.
        # The wake word "Hey Reachy" forces TO_COMPUTER via /wake (omnidirectional fallback).
        self._attention_state: str = "SILENT"
        self._attention_last_to_computer: float = time.monotonic()  # hysteresis gate
        self._ATTENTION_GATE_DELAY = 15.0  # seconds without TO_COMPUTER before closing the gate. 3s was too strict (Reachy cut off on every micro head-turn); 15s tolerates natural pauses and hesitations mid-conversation.
        # DOA gate: voices outside the ±30° frontal cone are blocked (TV, side
        # conversations). Default True so we don't break behaviour when the
        # DOAReader is unavailable. Bypassed for 15s after a /wake.
        self._doa_in_cone: bool = True
        self._doa_override_until: float = 0.0
        # Post-wake listening window: AttenLabs gate is bypassed for 15s after
        # a wake word, then control returns to main.py's attention state.
        self._wake_active_until: float = 0.0
        # Turn-taking state (kept for on_response_done reset)
        self._speech_detected: bool = False
        self._turn_response_sent: bool = False
        # Wake-word cancel flag: set True by /wake; while True we drop every
        # incoming audio_delta (OpenAI keeps streaming for ~30-60s after a
        # cancel_response). Reset to False in on_response_done so the next
        # response plays normally. Safety net: if the WS times out before the
        # cancelled response.done arrives, the flag would remain stuck, so we
        # stamp _cancel_received_at and auto-clear after 30s.
        self._cancel_received: bool = False
        self._cancel_received_at: float = 0.0
        # reading-context-restore: track the last reading tool invoked by the
        # LLM so we can restore context after a WS reconnect. Without this, the
        # fresh OpenAI session has no history and the LLM improvises a new
        # text (observed in the field: switching from one book to another
        # mid-reading). Generic: works for galileo_library, gutenberg, and any
        # future reading tool.
        self._last_reading_tool: str | None = None
        self._last_reading_args: dict | None = None
        # Silence keep-alive: avoids alsasink GStreamer underruns and the DSP
        # auto-mute observed on the USB speaker during inter-chunk gaps
        # (~1-2s between response.done and response.created). An asyncio task
        # pushes 20ms of zeros every 200ms when the gap exceeds 150ms and the
        # audio session is active.
        self._last_delta_at: float = 0.0
        self._keepalive_task = None
        # Audio duration tracking for the speaking gate (echo-loop fix).
        # The GStreamer queue level does not reflect the true output latency
        # on some external speakers (>5s observed), so we compute the total
        # audio duration received from the LLM and wait for it to drain, plus
        # a safety margin, before re-opening the gate.
        self._audio_delta_bytes_this_turn: int = 0
        self._speaking_started_at: float = 0.0
        self._last_response_done_at: float = 0.0
        # Lip movement detection (signal from main.py via IPC /user_speaking)
        self._user_speaking_visual: bool = False
        self._user_speaking_visual_at: float = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start all subsystems and wire them together."""
        self._loop = asyncio.get_event_loop()
        logger.info("ConversationEngine starting...")

        # bootstrap-order-fix — INVERTED ORDER: Audio before Robot.
        # Root cause: `mini.wake_up()` in the Pollen SDK opens a bluealsa A2DP
        # playback connection on the BT sink and never releases it. Because
        # bluealsa A2DP is single-client, our own aplay subprocess would then
        # fail with "Device or resource busy". A bisection confirmed the
        # sequence: ReachyMini / CameraWorker / MovementManager / HeadWobbler /
        # enable_motors stay HEALTHY; adding wake_up() breaks playback.
        # Solution: start aplay FIRST so it claims the sink. Consequence:
        # wake_up() cannot play its startup sound, but the motors still
        # lift the robot. Acceptable trade-off.

        # 1. Audio I/O (aplay subprocess first, so it claims the playback sink)
        if os.environ.get("CONV_DISABLE_AUDIO_IO") == "1":
            logger.warning("CONV_DISABLE_AUDIO_IO=1 — AudioIO skipped (mode diag)")
        else:
            try:
                from audio_io import AudioIO
                audio = AudioIO(on_captured=self.on_audio_captured)
                audio.start()
                self._audio = audio
            except Exception as exc:
                logger.error("AudioIO failed to start: %s", exc)
                self._audio = None

        # 2. Robot (movements) — started AFTER audio so that aplay has claimed
        # the playback sink before wake_up() attempts to open it.
        try:
            from robot import RobotLayer
            self._robot = RobotLayer()
            await self._robot.start()
        except Exception as exc:
            logger.error("RobotLayer failed to start: %s", exc)

        # 2b. Echo cancellation : handled by the XMOS hardware AEC of Reachy Mini,
        # plus OpenAI server_vad for barge-in.

        # 2c. Load .env for API keys. The Pollen conv_app stores OPENAI_API_KEY
        # in its own package directory, so we also look there as a convenience
        # when reachy_care is installed next to a stock Pollen conv_app tree.
        pollen_home = Path(os.environ.get(
            "POLLEN_CONV_APP_HOME",
            "/home/pollen/reachy_mini_conversation_app",
        ))
        for env_path in [
            pollen_home / "src" / "reachy_mini_conversation_app" / ".env",
            pollen_home / ".env",
            Path(__file__).resolve().parent.parent / ".env",
        ]:
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, val = line.partition("=")
                        val = val.strip().strip("'\"")
                        if key.strip() and val:
                            os.environ.setdefault(key.strip(), val)
                logger.info("Loaded env from %s", env_path)
                break

        # 3. Load system prompt from profile
        profile_dir = os.environ.get(
            "REACHY_MINI_EXTERNAL_PROFILES_DIRECTORY",
            str(Path(__file__).resolve().parent.parent / "external_profiles")
        )
        profile_name = os.environ.get("REACHY_MINI_CUSTOM_PROFILE", "reachy_care")
        instructions_path = Path(profile_dir) / profile_name / "instructions.txt"
        system_prompt = instructions_path.read_text() if instructions_path.exists() else ""
        # Interpoler {LOCATION}, {DATETIME}, {PRIMARY_PERSON}, {KNOWN_PEOPLE}
        # Source de vérité pour les personnes : known_faces/registry.json, lu par
        # config.py au démarrage (OWNER_NAME, SECONDARY_PERSONS). Édité par le
        # dashboard via /api/persons/<name>/primary. Pas de duplication ici.
        try:
            import config as _cfg
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(getattr(_cfg, "TIMEZONE", "Europe/Paris"))
            location = getattr(_cfg, "LOCATION", "non renseigné")
            owner = getattr(_cfg, "OWNER_NAME", "") or ""
            secondary = getattr(_cfg, "SECONDARY_PERSONS", []) or []
            primary = owner.capitalize() if owner else "non renseigné"
            known = ", ".join(p.capitalize() for p in secondary) if secondary else ""
            now_str = datetime.now(tz).strftime("%A %d %B %Y à %Hh%M")
            system_prompt = (
                system_prompt
                .replace("{LOCATION}", location)
                .replace("{DATETIME}", now_str)
                .replace("{PRIMARY_PERSON}", primary)
                .replace("{KNOWN_PEOPLE}", known)
            )
        except Exception as _interp_err:
            logger.warning("Interpolation instructions échouée: %s", _interp_err)

        # 4. Load tools from tools_for_conv_app/
        try:
            from tools.loader import load_tools
            care_dir = Path(__file__).resolve().parent.parent
            tools_txt = Path(profile_dir) / profile_name / "tools.txt"
            tool_names = []
            if tools_txt.exists():
                tool_names = [l.strip() for l in tools_txt.read_text().splitlines() if l.strip()]
            self._tools = load_tools(care_dir / "tools_for_conv_app", tool_names)
            logger.info("Loaded %d tools: %s", len(self._tools), list(self._tools.keys()))
        except Exception as exc:
            logger.error("Tool loading failed: %s", exc)

        tool_specs = self._build_tool_specs()

        # 5. LLM adapter — connect and wire callbacks
        # RAM-leak diagnostic kill-switch: CONV_DISABLE_LLM_CONNECT=1 skips the
        # entire OpenAI Realtime init (no WebSocket, no session, no callbacks).
        # Used to fully isolate the LLM chain from the other subsystems (audio
        # capture, GStreamer, robot SDK) during bisection.
        if os.environ.get("CONV_DISABLE_LLM_CONNECT") == "1":
            logger.warning("CONV_DISABLE_LLM_CONNECT=1 — LLM init skipped (mode diag)")
        else:
            try:
                from llm.openai_realtime import OpenAIRealtimeAdapter
                self._llm = OpenAIRealtimeAdapter()
                self._llm.on_audio_delta = self.on_audio_delta
                self._llm.on_tool_call = self.on_tool_call
                self._llm.on_speech_started = self.on_speech_started
                self._llm.on_speech_stopped = self.on_speech_stopped
                self._llm.on_response_done = self.on_response_done
                # reconnect-state-reset: callback invoked when the OpenAI WS
                # times out (close code 1011) and the client auto-reconnects
                # into a fresh session. Without this, stuck flags would block
                # the next turn entirely.
                self._llm.on_reconnect = self.on_reconnect
                await self._llm.connect(system_prompt, tool_specs)
                # Expose handler for tools (switch_mode, stop_speaking, set_reading_voice)
                # Tools use _get_handler() which scans sys.modules for _reachy_care_handler
                sys.modules[__name__]._reachy_care_handler = self._llm
                # Wire _clear_queue so stop_speaking can flush the audio player
                self._llm._clear_queue = self._clear_audio_queue
                logger.info("Handler exposed for tools (_reachy_care_handler + _clear_queue)")
            except Exception as exc:
                logger.error("OpenAIRealtimeAdapter failed to connect: %s", exc)

        # 6. IPC server — must start LAST (bridge expects all subsystems ready)
        try:
            from ipc_server import IPCServer
            self._ipc = IPCServer(self)
            self._ipc.start()
        except Exception as exc:
            logger.error("IPCServer failed to start: %s", exc)

        # 7. Silence keep-alive task — avoids the silent alsasink stall that
        # occurs between two chunks (the ~1-2s gap from response.done to
        # response.created can trigger a ring-buffer underrun or a DSP auto-mute
        # on the USB speaker).
        try:
            self._keepalive_task = asyncio.create_task(self._playback_keepalive())
            logger.info("Keep-alive silence task started (20ms zeros every 200ms on gap>150ms)")
        except Exception as exc:
            logger.warning("Keep-alive task failed to start: %s", exc)

        self._running = True
        logger.info("ConversationEngine started — all subsystems online.")

    async def _playback_keepalive(self) -> None:
        """Two combined keep-alives running on a 200 ms loop.

        1. GStreamer: push 20 ms of F32 zeros whenever the audio gap exceeds
           150 ms. Avoids ring-buffer underruns and the DSP auto-mute observed
           on the USB speaker when the signal goes silent.

        2. OpenAI WebSocket: push 200 ms of S16LE 16 kHz silence into
           input_audio_buffer roughly every 8 s while _speaking=True. The
           OpenAI edge then sees the WS as client-side active, which prevents
           the 1011 timeout we hit when Reachy receives a long audio burst.
           Community-validated pattern.
        """
        silence_f32_16k = np.zeros(320, dtype=np.float32)       # H: 20ms @16kHz
        silence_s16_16k_200ms = bytes(3200 * 2)                  # P: 200ms @16kHz S16LE mono
        iter_count = 0
        WS_KEEPALIVE_PERIOD = 40  # 40 × 200ms = 8 s
        while True:
            try:
                await asyncio.sleep(0.2)
                iter_count += 1
                if self._cancel_received:
                    continue  # cancel in progress, don't feed the pipeline
                now = time.monotonic()
                gap = now - self._last_delta_at if self._last_delta_at > 0 else 0.0

                # GStreamer keep-alive (avoids ring-buffer underrun)
                if (self._robot is not None and getattr(self._robot, "_mini", None) is not None
                        and 0.15 < gap < 5.0 and self._speaking_started_at > 0):
                    try:
                        self._robot._mini.media.push_audio_sample(silence_f32_16k)
                    except Exception:
                        pass

                # OpenAI WS keep-alive (avoids edge 1011 timeout)
                if (iter_count % WS_KEEPALIVE_PERIOD == 0
                        and self._speaking
                        and self._llm is not None):
                    try:
                        await self._llm.send_audio(silence_s16_16k_200ms)
                    except Exception as exc:
                        logger.debug("WS keep-alive send_audio failed: %s", exc)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.debug("keepalive loop error: %s", exc)

    def _build_tool_specs(self) -> list:
        """Build OpenAI tool specs from loaded tools.

        Note: OpenAI Realtime expects tools at session level, not nested under "function"
        like the Chat Completions API. The format below is correct for Realtime.
        """
        specs = []
        for name, tool in self._tools.items():
            specs.append({
                "type": "function",
                "name": name,
                "description": getattr(tool, "description", ""),
                "parameters": getattr(tool, "parameters_schema", {"type": "object", "properties": {}, "required": []}),
            })
        return specs

    async def stop(self) -> None:
        """Gracefully stop the engine."""
        if not self._running:
            return
        logger.info("ConversationEngine stopping...")
        self._running = False
        if self._ipc:
            self._ipc.stop()
        if self._llm:
            await self._llm.disconnect()
        if self._audio:
            self._audio.stop()
        if self._robot:
            self._robot.stop()
        logger.info("ConversationEngine stopped.")

    # ------------------------------------------------------------------
    # Audio path
    # ------------------------------------------------------------------

    def on_audio_captured(self, pcm_chunk: bytes) -> None:
        """Called from the audio capture thread. Forward audio to LLM (server_vad handles turn detection)."""
        if self._audio_muted:
            return
        if self._llm is None or self._loop is None:
            return

        # AttenLabs gate avec HYSTERESIS + CameraWorker bypass + Wake word fenêtre.
        # Bypass prioritaire : wake word a ouvert une fenêtre d'écoute 15s → gate ouverte.
        now_m = time.monotonic()
        _wake_active = now_m < self._wake_active_until
        _cam_face_recent = False
        if self._robot is not None and self._robot.camera_worker is not None:
            last_seen = getattr(self._robot.camera_worker, "last_face_detected_time", None)
            if last_seen is not None and (time.time() - last_seen) < 2.0:
                _cam_face_recent = True
        if (not _wake_active
                and self._attention_state != "TO_COMPUTER"
                and not _cam_face_recent):
            elapsed = now_m - self._attention_last_to_computer
            if elapsed > self._ATTENTION_GATE_DELAY:
                return  # vrai départ : gate fermée

        # DOA gate: voices outside the ±30° frontal cone are blocked, unless
        # the post-wake override is still active. Temporarily DISABLED via the
        # CONV_DISABLE_DOA_GATE=1 kill switch while the XMOS 0° axis calibration
        # and the energy-channel semantics are being verified in the field.
        # The DOA reader keeps running in observation mode.
        if os.environ.get("CONV_DISABLE_DOA_GATE") != "1":
            if not self._doa_in_cone and time.monotonic() > self._doa_override_until:
                return

        # RAM-leak diagnostic kill-switch: skip sending to the LLM when
        # CONV_DISABLE_LLM_SEND=1, used to bisect the C-native leak.
        if os.environ.get("CONV_DISABLE_LLM_SEND") == "1":
            return

        # Speaking gate (stable config): blocks the speaker echo from reaching
        # the VAD while Reachy is talking. interrupt_response=False prevents
        # OpenAI from reacting to that residual echo. The visual lip guard
        # stays available for a future build with proper AEC.
        if self._speaking:
            return
        self._audio_send_count += 1
        if self._audio_send_count <= 3 or self._audio_send_count % 100 == 0:
            logger.info("Sending audio to LLM: chunk #%d (%d bytes)", self._audio_send_count, len(pcm_chunk))
        asyncio.run_coroutine_threadsafe(
            self._llm.send_audio(pcm_chunk), self._loop
        )

    async def on_audio_delta(self, pcm_bytes: bytes, b64_delta: str = "") -> None:
        """Push audio delta to GStreamer (PCM) and wobbler (base64)."""
        # Instrumentation: log delay since response.done along with flag state.
        _gap_done = (time.monotonic() - self._last_response_done_at) if self._last_response_done_at else -1.0
        if self._audio_delta_count <= 3 or self._audio_delta_count == 50:
            logger.info("DELTA_IN count=%d speaking=%s started_at=%.2f gap_since_done=%.2f bytes=%d",
                        self._audio_delta_count, self._speaking,
                        self._speaking_started_at, _gap_done, len(pcm_bytes))
        # Drop deltas outright when a wake word cancelled the response in
        # flight. OpenAI keeps sending audio_delta for 30-60s after a
        # cancel_response, which we must completely ignore (no playback, no
        # wobbler). The flag is reset in on_response_done so the next response
        # plays. Safety: auto-clear after 30s so we don't stay stuck when
        # response.done never arrives (e.g. WS timeout mid-cancel).
        if self._cancel_received:
            if time.monotonic() - self._cancel_received_at > 30.0:
                logger.warning("_cancel_received stuck >30s — auto-clearing (WS timeout ?)")
                self._cancel_received = False
                self._cancel_received_at = 0.0
            else:
                return
        now = time.monotonic()
        if not self._speaking:
            # First delta of this response: start the duration counter.
            self._speaking_started_at = now
            self._audio_delta_bytes_this_turn = 0
            self._last_delta_at = now
        # Log the inter-delta gap (underrun diagnostic). When the gap exceeds
        # 200 ms, the 500 ms queue drains and alsasink underruns. When the gap
        # stays below 200 ms on average with occasional spikes, the
        # min-threshold-time buffer absorbs the jitter.
        gap_ms = (now - getattr(self, "_last_delta_at", now)) * 1000.0
        self._last_delta_at = now
        self._speaking = True  # Gate: Reachy is speaking
        self._audio_delta_bytes_this_turn += len(pcm_bytes)
        self._audio_delta_count += 1
        if self._audio_delta_count <= 5 or self._audio_delta_count % 50 == 0:
            logger.info("Audio delta #%d: %d bytes, gap=%.0f ms",
                        self._audio_delta_count, len(pcm_bytes), gap_ms)
        if self._audio_delta_count <= 3 or self._audio_delta_count % 200 == 0:
            logger.info("Audio delta #%d: %d bytes", self._audio_delta_count, len(pcm_bytes))
        # RAM-leak diagnostic kill-switch: skip GStreamer playback when
        # CONV_DISABLE_PLAYBACK=1.
        if os.environ.get("CONV_DISABLE_PLAYBACK") != "1":
            if self._audio is not None:
                self._audio.push_playback(pcm_bytes)
            # Native SDK GStreamer playback (ring buffer 50 ms, thread GLib hors GIL).
            # OpenAI Realtime émet du 24 kHz S16LE mono. Le SDK GStreamerAudio déclare
            # ses caps appsrc à 16 kHz F32LE (cf audio_gstreamer.py:182) → on resample
            # 24→16 kHz via scipy.signal.resample (band-limited, sans distorsion) et
            # on convertit int16 → float32 normalisé. Pattern exact Pollen console.py:480-497.
            if self._robot is not None and self._robot._mini is not None:
                try:
                    pcm_int16 = np.frombuffer(pcm_bytes, dtype=np.int16)
                    pcm_float32 = pcm_int16.astype(np.float32) / 32768.0
                    # 24 kHz -> 16 kHz: ratio  (len_out = len_in * 2 // 3)
                    n_out = len(pcm_float32) * 16000 // 24000
                    if n_out > 0:
                        pcm_16k = resample(pcm_float32, n_out).astype(np.float32)
                        self._robot._mini.media.push_audio_sample(pcm_16k)
                except Exception as exc:
                    logger.debug("push_audio_sample error: %s", exc)
            if self._robot is not None:
                self._robot.feed_wobbler(b64_delta)

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------

    async def on_tool_call(self, call_id: str, name: str, args: Dict[str, Any]) -> None:
        """Look up and execute a registered tool, then send the result back to the LLM."""
        tool = self._tools.get(name)
        if tool is None:
            logger.warning("Tool '%s' not found (call_id=%s)", name, call_id)
            result = {"error": f"unknown tool: {name}"}
        else:
            from tools.base import ToolDependencies
            deps = ToolDependencies(engine=self, robot=self._robot, llm=self._llm)
            try:
                result = await tool(deps, **args)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Tool '%s' raised: %s", name, exc)
                result = {"error": str(exc)}

            # reading-context-restore: track the most recent reading tool so
            # we can replay it after a WS reconnect.
            if name in ("galileo_library", "gutenberg"):
                self._last_reading_tool = name
                self._last_reading_args = dict(args)  # copy to avoid downstream mutation

        if self._llm is not None:
            await self._llm.send_tool_result(call_id, result)

    # ------------------------------------------------------------------
    # Speech events
    # ------------------------------------------------------------------

    async def on_speech_started(self) -> None:
        """User started speaking: clear playback (unless speaking/BT) and set robot to listening."""
        # Instrumentation: trace engine state when VAD fires. We suspect
        # residual acoustic noise between chunks can flush the appsrc buffer.
        _gap_done = (time.monotonic() - self._last_response_done_at) if self._last_response_done_at else -1.0
        logger.info("SPEECH_STARTED speaking=%s gap_since_done=%.2f delta_count=%d",
                    self._speaking, _gap_done, self._audio_delta_count)
        if self._speaking:
            logger.debug("speech_started ignored (speaking gate active)")
            return
        # response-done-guard: 3s temporal guard after response.done.
        # The SDK's clear_player() calls flush_stop(reset_time=True) on the
        # appsrc, which breaks the running-time timestamp, so buffers from the
        # next chunk are silently dropped by alsasink. Meanwhile OpenAI's VAD
        # can fire speech_started on acoustic residue right after Reachy
        # finishes speaking (room echo, wobbling antennas, ambient noise) —
        # this is NOT a real user barge-in. Solution: if we are within 3s of
        # the last response.done, we ignore this speech_started as a VAD
        # ghost. A real user barge-in typically arrives more than 3s later,
        # i.e. the time it takes for the user to start speaking.
        gap_since_done = time.monotonic() - self._last_response_done_at if self._last_response_done_at else 999.0
        if gap_since_done < 3.0:
            logger.info("speech_started IGNORED (VAD ghost, gap_since_done=%.2fs < 3.0s)", gap_since_done)
            return
        if self._audio is not None:
            self._audio.clear_playback()  # flush AEC reference queue
        if self._robot is not None:
            self._robot.clear_playback()  # flush GStreamer appsrc (SDK native)
            self._robot.set_listening(True)
            self._robot.reset_wobbler()

    async def on_reconnect(self) -> None:
        """Handle a WS reconnect after a 1011 timeout from OpenAI.

        A fresh session means the in-flight response is lost, so we must reset
        all turn-taking flags — otherwise they stay stuck (response.done will
        never arrive on the old session) and the next audio chunk cannot play.

        If we were in the middle of a reading, we also inject a system message
        into the new session so the LLM resumes the SAME text at the right
        place, instead of improvising or switching books.
        """
        logger.info("WS reconnect: resetting turn-taking state (was speaking=%s)", self._speaking)
        self._speaking = False
        self._speaking_started_at = 0.0
        self._turn_response_sent = False
        self._speech_detected = False
        self._cancel_received = False
        self._cancel_received_at = 0.0
        self._audio_delta_bytes_this_turn = 0
        if self._robot is not None:
            try:
                self._robot.reset_wobbler()
            except Exception:
                pass

        # Restore reading context if any (reading-context-restore)
        if self._last_reading_tool and self._llm is not None and self._llm._conn is not None:
            try:
                import json as _json
                args_str = _json.dumps(self._last_reading_args or {}, ensure_ascii=False)
                msg = (
                    f"[Reachy Care] Reconnexion réseau détectée. Tu étais en pleine lecture "
                    f"via le tool `{self._last_reading_tool}` avec les arguments {args_str}. "
                    f"Reprends IMMÉDIATEMENT cette lecture en appelant ce tool avec les mêmes "
                    f"arguments (ou action='resume' si disponible sur ce tool). "
                    f"NE CHANGE PAS de texte, d'auteur ou de chapitre. Ne demande pas confirmation. "
                    f"L'auditeur attend la suite de la même histoire."
                )
                await self._llm._conn.conversation.item.create(
                    item={
                        "type": "message",
                        "role": "system",
                        "content": [{"type": "input_text", "text": msg}],
                    }
                )
                await self._llm._conn.response.create()
                logger.info("Reconnect: reading context restored (%s, %s)",
                            self._last_reading_tool, args_str[:120])
            except Exception as exc:
                logger.warning("Reconnect reading-context restore failed: %s", exc)

    async def on_speech_stopped(self) -> None:
        """User stopped speaking."""
        if self._robot is not None:
            self._robot.set_listening(False)

    async def on_response_done(self) -> None:
        """LLM response turn complete. Wait for the real end of audio playback
        BEFORE re-opening the speaking gate — otherwise VAD would capture
        Reachy's own voice, causing an echo loop and a cascade of queued
        responses that eventually blew the process RAM (~1.8 GB of glibc
        arenas observed).

        The GStreamer queue level does not reflect the true output latency on
        some external speakers (>5s observed), so we use the audio duration
        received from the LLM as the ground truth:
            wait = max(0, audio_duration - elapsed_since_first_delta) + safety_margin
        """
        # Clear the cancel flag: the wake-word-cancelled response has now
        # finished on OpenAI's side, so the next response will play normally.
        _was_cancelled = self._cancel_received  # captured for the auto-continue skip below
        if self._cancel_received:
            logger.info("response.done — cancel_received flag cleared")
            self._cancel_received = False
            self._cancel_received_at = 0.0
        audio_duration_s = 0.0  # hoisted out of try so the post-finally auto-continue can read it
        try:
            # Total audio duration sent to the speaker for this response
            BYTES_PER_SECOND = 24000 * 2  # 24 kHz S16LE mono
            audio_duration_s = self._audio_delta_bytes_this_turn / BYTES_PER_SECOND
            elapsed_s = time.monotonic() - self._speaking_started_at if self._speaking_started_at > 0 else 0.0
            remaining_s = max(0.0, audio_duration_s - elapsed_s)
            # USB DAC latency margin. The previous BT A2DP sink needed 3s+ of
            # margin; the current USB audio path has <20ms of latency, so
            # 0.5s is plenty.
            USB_LATENCY_MARGIN_S = 0.5
            # Cap at 90s. Previously 15s with the BT speaker, which caused an
            # echo loop: when remaining=25.9s the gate would re-open 10.9s too
            # early and Reachy would hear its own voice still playing, then
            # respond to itself on repeat. Story mode can yield remaining>60s,
            # so a 90s cap is safe.
            MAX_WAIT_S = 90.0
            wait_s = min(remaining_s + USB_LATENCY_MARGIN_S, MAX_WAIT_S)
            logger.info(
                "Response done — audio=%.1fs elapsed=%.1fs remaining=%.1fs → wait %.1fs",
                audio_duration_s, elapsed_s, remaining_s, wait_s,
            )
            await asyncio.sleep(wait_s)
            # Keep _speaking=True for an extra 1.5s to block the
            # clear_playback -> pause/flush/play path triggered by
            # on_speech_started during the inter-chunk gap, where VAD may
            # catch stray noise (wobbling antennas, echo). Without this
            # guard, a flush on the appsrc followed by push_audio_sample
            # without an explicit timestamp leads to a silent alsasink stall
            # on the second chunk.
            await asyncio.sleep(1.5)
            # Previously we also polled playback_queue_level_ns() < 50 ms.
            # That was dropped when we switched to the native SDK GStreamer
            # pipeline — the SDK does not expose an equivalent metric and the
            # audio_duration-elapsed+margin calculation is ground truth on its
            # own. The 90s cap covers even long story-mode turns.
        finally:
            # Always reset turn-taking state, even on error or cancel,
            # otherwise _turn_response_sent remains True and the session
            # goes deaf to new user turns.
            self._speech_detected = False
            self._turn_response_sent = False
            self._speaking = False
            self._audio_delta_bytes_this_turn = 0
            self._speaking_started_at = 0.0
            self._last_response_done_at = time.monotonic()

        # Auto-continue for long readings. gpt-realtime is turn-based and
        # does NOT chain on to the next chunk automatically after
        # response.done (confirmed in the OpenAI docs). For long readings
        # (galileo_library / gutenberg) we inject an [URGENT] event that
        # bypasses inject_event's silencing, triggering the next tool call to
        # fetch and narrate the following chunk.
        # Conditions:
        #   - _last_reading_tool is set (see reading-context-restore above)
        #   - audio_duration > 5s (filters out tool-call-only responses whose audio=0s)
        #   - the turn was not cancelled by a wake word (the user wants to
        #     speak, not have us auto-continue)
        if self._last_reading_tool and audio_duration_s > 5.0 and not _was_cancelled:
            logger.info("Auto-continue: trigger %s pour chunk suivant (audio=%.1fs)",
                        self._last_reading_tool, audio_duration_s)
            try:
                await self.inject_event(
                    "[URGENT] [Reachy Care] Continue la lecture au chunk suivant.",
                    instructions=(
                        f"Appelle IMMÉDIATEMENT {self._last_reading_tool} en suivant "
                        "EXACTEMENT le `continuation_hint` retourné par ton précédent appel : "
                        "MÊME chapitre, mais offset = next_offset du précédent appel. "
                        "N'UTILISE JAMAIS le même offset qu'avant (tu relirais le même passage). "
                        "NE RÉPÈTE AUCUNE PHRASE déjà narrée. NE FAIS AUCUN résumé. "
                        "Narre uniquement et exactement le nouvel excerpt retourné, sans transition, "
                        "sans phrase d'attente type 'je charge' ou 'un instant'. "
                        "Si le tool retourne 'FIN DU CHAPITRE' ou équivalent, arrête-toi."
                    ),
                )
            except Exception as exc:
                logger.warning("Auto-continue inject_event failed: %s", exc)
            logger.info("Speaking gate opened — input listening.")

    # ------------------------------------------------------------------
    # LLM control helpers
    # ------------------------------------------------------------------

    async def inject_event(self, text: str, instructions: str) -> None:
        """Inject an external event (e.g. IPC message) into the LLM.

        Proactivity discipline: every event from main.py used to be forwarded
        straight to send_text_event -> response.create, which interrupted the
        user. Tightened rule: while the user is speaking OR within 15s of a
        Reachy response, every received event is downgraded to a silent
        session_update (context updated, no forced reply). The only exception
        is for events that must stay urgent (wake word, confirmed fall); these
        are tagged by main.py with [URGENT] in the text and go through
        unchanged.
        """
        if self._llm is None:
            return
        # Log for proactivity diagnostics
        logger.info("inject_event received: text=%r instructions=%r", text[:80], instructions[:80])
        # Urgent events (wake word, fall, etc.) always go through
        is_urgent = "[URGENT]" in text or "Wake word" in text or "chute" in text.lower()
        # Otherwise, if the conversation is active, silence the event (context only)
        if not is_urgent and (self._speaking or (time.monotonic() - self._last_response_done_at < 15.0)):
            logger.info("inject_event silencieux (conv active, non urgent) → session_update")
            await self._llm.update_instructions(
                f"[Contexte mis à jour] {text}"
            )
            return
        await self._llm.send_text_event(text, instructions)

    async def update_instructions(self, new_instructions: str) -> None:
        """Push updated system instructions to the LLM session."""
        if self._llm is not None:
            await self._llm.update_instructions(new_instructions)

    async def cancel_response(self) -> None:
        """Interrupt the current LLM response."""
        if self._llm is not None:
            await self._llm.cancel_response()

    async def handle_wake_event(
        self,
        doa_rad: Optional[float] = None,
        event_text: Optional[str] = None,
        event_instructions: Optional[str] = None,
    ) -> None:
        """Unified wake-word handler, shared by HTTP /wake and the SIGUSR1
        wake-priority path.

        Factors out the original /wake IPC logic so the signal handler can
        call it without duplication. Optional ``doa_rad`` rotates the body
        toward the sound source. Optional ``event_text`` / ``event_instructions``
        let the wake word inject an event, typically used in story mode.
        Everything stays async, callable through run_coroutine_threadsafe
        (HTTP thread) or asyncio.create_task (SIGUSR1 loop).
        """
        import threading as _t
        self.unmute()
        self._wake_active_until = time.monotonic() + 15.0
        self._doa_override_until = time.monotonic() + 15.0
        self._cancel_received = True
        self._cancel_received_at = time.monotonic()
        await self.cancel_response()
        # Save reading progress BEFORE clear_playback (estimates the number
        # of characters read based on the time since the chunk was sent).
        try:
            from tools_for_conv_app.galileo_library import save_interruption_point as _save
            saved = _save()
            if saved:
                logger.info("wake: galileo progress saved at %s offset=%d (elapsed=%.1fs)",
                            saved["chapter_file"], saved["offset"], saved["elapsed_seconds"])
        except Exception as exc:
            logger.warning("wake: save_interruption_point failed: %s", exc)
        try:
            if self._audio is not None:
                self._audio.clear_playback()
        except Exception as exc:
            logger.warning("wake: clear_playback failed: %s", exc)
        if self._robot is not None:
            try:
                self._robot.clear_playback()
                self._robot.reset_wobbler()
            except Exception as exc:
                logger.warning("wake: reset_wobbler/clear_playback failed: %s", exc)
            _t.Thread(target=self._robot.wake, daemon=True, name="wake-robot").start()
            _t.Thread(target=self._robot.acknowledge_gesture, daemon=True, name="wake-ack").start()
            if doa_rad is not None:
                try:
                    self._robot.turn_body(doa_rad, 0.8)
                except Exception as exc:
                    logger.warning("wake: turn_body failed: %s", exc)
        if event_text:
            try:
                await self.inject_event(event_text, event_instructions or "")
            except Exception as exc:
                logger.warning("wake: inject_event failed: %s", exc)

    def _clear_audio_queue(self) -> None:
        """Flush audio player — called by stop_speaking tool via handler._clear_queue."""
        if self._audio is not None:
            self._audio.clear_playback()
        self._speaking = False
        logger.info("Audio queue cleared (stop_speaking).")

    # ------------------------------------------------------------------
    # Mute control
    # ------------------------------------------------------------------

    def mute(self) -> None:
        """Mute microphone input and clear any ongoing playback."""
        self._audio_muted = True
        if self._audio is not None:
            self._audio.clear_playback()
        logger.info("ConversationEngine muted.")

    def unmute(self) -> None:
        """Resume microphone input."""
        self._audio_muted = False
        logger.info("ConversationEngine unmuted.")

    @property
    def is_muted(self) -> bool:
        return self._audio_muted
