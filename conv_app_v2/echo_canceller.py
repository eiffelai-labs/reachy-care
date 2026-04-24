"""
echo_canceller.py — Digital signal subtraction for BT echo cancellation.

Architecture (Rodin approach):
1. We keep a copy of every audio chunk sent to the BT speaker (reference buffer)
2. When the mic captures audio, we subtract the delayed reference from it
3. The residual contains only human voice + ambient noise (no echo)
4. VAD runs on the residual — only human speech triggers a response

This replaces the speaking gate + timer approach which could never handle
the variable BT latency (2-15 seconds measured).
"""

import collections
import logging
import threading
import time

import numpy as np

logger = logging.getLogger(__name__)

# Audio params
SAMPLE_RATE = 16000  # Mic capture rate (XMOS)
PLAYBACK_RATE = 24000  # LLM output rate
BYTES_PER_SAMPLE = 2  # S16LE

# Default BT delay — will be refined by calibration
DEFAULT_BT_DELAY_MS = 200
MAX_BT_DELAY_MS = 1000  # Max delay we support (1 second)

# Buffer size: enough to hold MAX_BT_DELAY_MS of audio at playback rate
BUFFER_DURATION_S = 2.0  # 2 seconds of reference audio


class EchoCanceller:
    """Digital echo cancellation via signal subtraction.

    Feed it the playback signal and the mic signal. It returns the residual
    (mic minus delayed playback) which should contain only human voice.
    """

    def __init__(self, bt_delay_ms: int = DEFAULT_BT_DELAY_MS):
        self._bt_delay_ms = bt_delay_ms
        self._bt_delay_samples = int(bt_delay_ms * SAMPLE_RATE / 1000)

        # Circular buffer of playback samples (resampled to mic rate)
        buf_size = int(BUFFER_DURATION_S * SAMPLE_RATE)
        self._ref_buffer = np.zeros(buf_size, dtype=np.float32)
        self._ref_write_pos = 0
        self._lock = threading.Lock()

        # Adaptive gain (alpha) — ratio of echo energy to reference energy
        self._alpha = 0.3  # Start conservative

        logger.info("EchoCanceller initialized (delay=%dms, buffer=%.1fs)",
                     bt_delay_ms, BUFFER_DURATION_S)

    @property
    def bt_delay_ms(self) -> int:
        return self._bt_delay_ms

    @bt_delay_ms.setter
    def bt_delay_ms(self, value: int):
        self._bt_delay_ms = min(value, int(MAX_BT_DELAY_MS))
        self._bt_delay_samples = int(self._bt_delay_ms * SAMPLE_RATE / 1000)
        logger.info("BT delay updated: %dms (%d samples)", self._bt_delay_ms, self._bt_delay_samples)

    @staticmethod
    def _resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
        """Resample float32 array via simple decimation (good enough for echo reference)."""
        if src_rate == dst_rate:
            return samples
        n_out = int(len(samples) * dst_rate / src_rate)
        indices = np.linspace(0, len(samples) - 1, n_out).astype(int)
        return samples[indices]

    def feed_playback(self, pcm_bytes: bytes) -> None:
        """Feed playback audio into the reference buffer.

        Input is S16LE mono at PLAYBACK_RATE (24kHz).
        We resample to SAMPLE_RATE (16kHz) to match the mic.
        """
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        samples = self._resample(samples, PLAYBACK_RATE, SAMPLE_RATE)

        # Write to circular buffer
        with self._lock:
            n = len(samples)
            buf_size = len(self._ref_buffer)
            pos = self._ref_write_pos % buf_size
            if pos + n <= buf_size:
                self._ref_buffer[pos:pos + n] = samples
            else:
                first = buf_size - pos
                self._ref_buffer[pos:] = samples[:first]
                self._ref_buffer[:n - first] = samples[first:]
            self._ref_write_pos += n

    def cancel_echo(self, mic_pcm_bytes: bytes) -> tuple[bytes, float]:
        """Subtract delayed reference from mic signal.

        Input: S16LE mono at SAMPLE_RATE (16kHz)
        Returns: (residual_pcm_bytes, residual_rms)
        """
        # Convert mic to float32
        mic = np.frombuffer(mic_pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        n = len(mic)

        # Get delayed reference from circular buffer + alpha snapshot
        # Lock unique : éviter TOCTOU entre lecture ref et snapshot alpha
        # (sinon feed_playback peut écraser ref entre les deux acquisitions).
        with self._lock:
            buf_size = len(self._ref_buffer)
            # Read position = write position - delay - current chunk length
            read_pos = (self._ref_write_pos - self._bt_delay_samples - n) % buf_size
            if read_pos + n <= buf_size:
                ref = self._ref_buffer[read_pos:read_pos + n].copy()
            else:
                first = buf_size - read_pos
                ref = np.concatenate([
                    self._ref_buffer[read_pos:],
                    self._ref_buffer[:n - first]
                ])
            alpha = self._alpha

        # Subtract: residual = mic - alpha * reference
        residual = mic - alpha * ref

        # Compute RMS of residual
        rms = float(np.sqrt(np.mean(residual ** 2)))

        # Adaptive alpha: when no human is speaking (low residual RMS),
        # adjust alpha to minimize residual energy.
        # Lock protects _alpha from concurrent read in feed_playback thread.
        if rms < 0.01 and np.sqrt(np.mean(ref ** 2)) > 0.01:
            mic_energy = np.mean(mic ** 2)
            ref_energy = np.mean(ref ** 2)
            if ref_energy > 1e-8:
                ideal_alpha = float(np.sqrt(mic_energy / ref_energy))
                with self._lock:
                    new_alpha = 0.95 * self._alpha + 0.05 * ideal_alpha
                    self._alpha = float(np.clip(new_alpha, 0.0, 2.0))

        # Convert residual back to S16LE
        residual_s16 = np.clip(residual * 32768.0, -32768, 32767).astype(np.int16)
        return residual_s16.tobytes(), rms

    def calibrate_delay(self, playback_func, capture_func, duration_ms: int = 200) -> int:
        """Calibrate BT delay by sending a chirp and measuring cross-correlation.

        playback_func: callable that plays PCM bytes to the BT speaker
        capture_func: callable that returns captured PCM bytes from the mic

        Returns estimated delay in milliseconds.
        """
        logger.info("Calibrating BT delay...")

        # Generate chirp (200-4000Hz sweep)
        t = np.linspace(0, duration_ms / 1000, int(PLAYBACK_RATE * duration_ms / 1000))
        chirp = np.sin(2 * np.pi * (200 + (4000 - 200) * t / t[-1]) * t).astype(np.float32)
        chirp_s16 = (chirp * 16000).astype(np.int16)
        chirp_bytes = chirp_s16.tobytes()

        playback_func(chirp_bytes)

        # Wait and capture (chirp + echo)
        time.sleep(0.5)  # Wait for BT to play
        captured = capture_func()
        if captured is None or len(captured) < 100:
            logger.warning("Calibration failed: no capture data")
            return self._bt_delay_ms

        # Cross-correlate
        cap_f32 = np.frombuffer(captured, dtype=np.int16).astype(np.float32)
        chirp_16k = self._resample(chirp_s16.astype(np.float32), PLAYBACK_RATE, SAMPLE_RATE)

        correlation = np.correlate(cap_f32, chirp_16k, mode='full')
        peak_idx = np.argmax(np.abs(correlation))
        delay_samples = peak_idx - len(chirp_16k) + 1
        delay_ms = int(delay_samples * 1000 / SAMPLE_RATE)

        if 50 <= delay_ms <= MAX_BT_DELAY_MS:
            self.bt_delay_ms = delay_ms
            logger.info("Calibration OK: BT delay = %dms", delay_ms)
        else:
            logger.warning("Calibration gave implausible delay %dms — keeping %dms",
                           delay_ms, self._bt_delay_ms)

        return self._bt_delay_ms
