"""
audio_io.py — GStreamer audio I/O for conv_app_v2

Capture pipeline  : ALSA mic (XMOS) → appsink (F32LE stereo → S16LE mono via numpy)
AEC reference     : separate aplay process → plughw:CARD=Audio,DEV=0 (vol 0, XMOS chip input)

Playback is handled directly by the Reachy Mini SDK GStreamerAudio (ring buffer
50 ms in a native GLib thread outside the Python GIL). It is driven from
ConversationEngine.on_audio_delta via ``self._robot._mini.media.push_audio_sample``.
See ``conv_app_v2/conversation_engine.py`` and ``conv_app_v2/robot.py``.

Refactor  — the previous ``subprocess aplay`` BT playback + custom
GStreamer BT playback were removed after researcher confirmed Pollen vanilla
survives 1h30 no glitch using the SDK native pipeline. The ~17–53 ms microunderruns
observed during speech were caused by GIL contention between the Python worker
thread writing into aplay stdin and the other heavy threads (AEC ref, HeadWobbler,
GStreamer capture, main asyncio). The SDK path pushes buffers into a C-native
appsrc and never touches the GIL for playback.

The AEC reference aplay is kept as-is: it feeds the XMOS chip its own playback
signal so the hardware echo canceller can subtract it from the mic input (99.7%
cancellation measured ). It runs in a tight thread that does nothing
else, so GIL contention there is tolerable.
"""

import os
import subprocess
import threading
import logging
import queue
from typing import Callable

import numpy as np

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib

Gst.init(None)

log = logging.getLogger(__name__)


class AudioIO:
    def __init__(
        self,
        on_captured: Callable[[bytes], None],
        src_device: str = "conv_audio_in",
        aec_ref_device: str = "plughw:CARD=Audio,DEV=0",
    ):
        self._on_captured = on_captured
        self._src_device = src_device
        self._aec_ref_device = aec_ref_device

        self._capture_pipeline: Gst.Pipeline | None = None
        self._loop: GLib.MainLoop | None = None
        self._loop_thread: threading.Thread | None = None

        # AEC reference: separate aplay process writing to XMOS hw:0,0
        self._aec_queue: queue.Queue | None = None
        self._aec_thread: threading.Thread | None = None
        self._aec_proc: subprocess.Popen | None = None
        self._aec_running: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Build capture pipeline + AEC ref, start GLib main loop."""
        self._loop = GLib.MainLoop()
        self._loop_thread = threading.Thread(
            target=self._loop.run, daemon=True, name="gst-main-loop"
        )
        self._loop_thread.start()

        # Kill-switch RAM diag  — bissection des sous-pipelines GStreamer.
        if os.environ.get("CONV_DISABLE_CAPTURE_PIPELINE") == "1":
            log.warning("CONV_DISABLE_CAPTURE_PIPELINE=1 — capture pipeline skipped")
        else:
            self._build_capture_pipeline()
            ret = self._capture_pipeline.set_state(Gst.State.PLAYING)
            log.info("Capture pipeline → PLAYING: %s", ret)

        # AEC reference thread (writes SDK playback signal to XMOS for echo cancellation)
        # Kill-switch RAM diag : CONV_DISABLE_AEC_REF=1 skip le subprocess aplay.
        if self._aec_ref_device and os.environ.get("CONV_DISABLE_AEC_REF") != "1":
            self._start_aec_ref()

        log.info("AudioIO started (src=%s, aec_ref=%s)",
                 self._src_device, self._aec_ref_device)
        self._sample_count = 0

    def stop(self) -> None:
        """Stop capture pipeline, AEC ref and GLib loop."""
        self._stop_aec_ref()
        if self._capture_pipeline:
            self._capture_pipeline.set_state(Gst.State.NULL)
        if self._loop and self._loop.is_running():
            self._loop.quit()
        log.info("AudioIO stopped")

    # ------------------------------------------------------------------
    # Playback forwarding (AEC reference only)
    # ------------------------------------------------------------------

    def push_playback(self, pcm_bytes: bytes) -> None:
        """Push PCM to AEC reference queue (XMOS hw:0,0 ref pour echo cancellation).

        BT playback is now handled by RobotLayer via
        ``self._mini.media.push_audio_sample()`` directly in on_audio_delta
        (conv_app_v2/conversation_engine.py).
        """
        if self._aec_queue is not None:
            try:
                # Downsampling 24000 -> 16000 : plughw:0,0 native rate XMOS
                # np.interp = interpolation linéaire (meilleure qualité que nearest)
                samples_24k = np.frombuffer(pcm_bytes, dtype=np.int16)
                n_out = len(samples_24k) * 16000 // 24000
                if n_out > 0:
                    x_in = np.arange(len(samples_24k))
                    x_out = np.linspace(0, len(samples_24k) - 1, n_out)
                    samples_16k = np.interp(x_out, x_in, samples_24k.astype(np.float64))
                    pcm_16k = samples_16k.astype(np.int16).tobytes()
                    self._aec_queue.put_nowait(pcm_16k)
            except queue.Full:
                pass

    def clear_playback(self) -> None:
        """Flush the AEC reference queue on barge-in.

        The SDK GStreamer playback queue is flushed separately by
        ``RobotLayer.clear_playback()`` (conv_app_v2/robot.py).
        """
        if self._aec_queue is not None:
            cleared = 0
            while True:
                try:
                    self._aec_queue.get_nowait()
                    cleared += 1
                except queue.Empty:
                    break
            log.info("AEC ref queue cleared (%d chunks dropped)", cleared)

    # ------------------------------------------------------------------
    # AEC reference: separate aplay process for XMOS echo cancellation
    # ------------------------------------------------------------------

    # 200ms of silence at 16kHz S16LE mono (AEC ref aplay rate = 16kHz natif XMOS)
    # 16000 samples/s * 2 bytes/sample / 5 = 6400 bytes
    _SILENCE_200MS = b"\x00" * (16000 * 2 // 5)

    def _start_aec_ref(self) -> None:
        """Start a background aplay process that receives PCM via stdin.
        This feeds the XMOS chip the playback signal as AEC reference."""
        self._aec_queue = queue.Queue(maxsize=200)
        self._aec_running = True
        try:
            # Rate 16000 = rate natif XMOS plughw:0,0 (PI_KB §11: write_asoundrc_to_home rate 16000)
            # AVANT: -r 24000 → plug ALSA convertissait SW 24->16 → délai + distorsion de phase
            # Ref: AUDIT_V2_LIGNE_audio_io_echo.md §INCOMPREHENSION SÉRIEUSE
            self._aec_proc = subprocess.Popen(
                ["aplay", "-D", self._aec_ref_device,
                 "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "raw", "-q"],
                stdin=subprocess.PIPE,
            )
            self._aec_thread = threading.Thread(
                target=self._aec_ref_worker, daemon=True, name="aec-ref-writer"
            )
            self._aec_thread.start()
            log.info("AEC reference started → %s (aplay PID %d)",
                     self._aec_ref_device, self._aec_proc.pid)
        except Exception as exc:
            log.warning("AEC reference failed to start: %s", exc)
            self._aec_running = False

    def _aec_ref_worker(self) -> None:
        """Thread that reads PCM from queue and writes to aplay stdin."""
        while self._aec_running and self._aec_proc and self._aec_proc.poll() is None:
            try:
                pcm = self._aec_queue.get(timeout=0.2)
                self._aec_proc.stdin.write(pcm)
                self._aec_proc.stdin.flush()
            except queue.Empty:
                try:
                    self._aec_proc.stdin.write(self._SILENCE_200MS)
                    self._aec_proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    break
            except (BrokenPipeError, OSError):
                log.warning("AEC reference pipe broken — stopped")
                break
        log.info("AEC reference worker exited.")

    def _restart_aec_ref(self) -> None:
        """Restart the aplay process if it dies."""
        self._stop_aec_ref()
        self._start_aec_ref()

    def _stop_aec_ref(self) -> None:
        """Stop the AEC reference process and thread."""
        self._aec_running = False
        if self._aec_proc:
            try:
                self._aec_proc.stdin.close()
                self._aec_proc.terminate()
                self._aec_proc.wait(timeout=2)
            except Exception:
                pass
            self._aec_proc = None
        self._aec_queue = None

    # ------------------------------------------------------------------
    # Internal: capture pipeline construction + appsink callback
    # ------------------------------------------------------------------

    def _build_capture_pipeline(self) -> None:
        desc = (
            f'alsasrc device="{self._src_device}" '
            "! queue max-size-buffers=200 "
            "! audioconvert "
            "! audioresample "
            "! audio/x-raw,rate=16000,channels=2,format=F32LE "
            "! appsink name=sink emit-signals=true drop=true max-buffers=10"
        )
        pipeline = Gst.parse_launch(desc)
        appsink = pipeline.get_by_name("sink")
        appsink.connect("new-sample", self._on_new_sample)
        self._capture_pipeline = pipeline

    def _on_new_sample(self, appsink: Gst.Element) -> Gst.FlowReturn:
        """
        Called by GStreamer for each captured buffer.

        Steps:
        1. Pull the sample buffer.
        2. Convert F32LE stereo → S16LE mono via numpy.
        3. Call self._on_captured(pcm_bytes).
        """
        sample = appsink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR

        buf = sample.get_buffer()
        ok, map_info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.ERROR

        try:
            # Raw bytes → float32 stereo array
            raw = np.frombuffer(map_info.data, dtype=np.float32)
            # Reshape to (N, 2) and average channels to mono
            if raw.size % 2 != 0:
                raw = raw[: raw.size - (raw.size % 2)]
            mono_f32 = raw.reshape(-1, 2).mean(axis=1)
            # Scale to int16 range and clip
            mono_s16 = np.clip(mono_f32 * 32767.0, -32768, 32767).astype(np.int16)
            self._sample_count = getattr(self, "_sample_count", 0) + 1
            if self._sample_count <= 3 or self._sample_count % 500 == 0:
                log.info("Audio captured: sample #%d, %d bytes, rms=%.4f",
                         self._sample_count, len(mono_s16.tobytes()),
                         np.sqrt(np.mean(mono_f32 ** 2)))
            self._on_captured(mono_s16.tobytes())
        except Exception:
            log.exception("_on_new_sample: error processing buffer")
        finally:
            buf.unmap(map_info)

        return Gst.FlowReturn.OK
