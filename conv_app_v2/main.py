#!/usr/bin/env python3
"""
conv_app_v2/main.py — Entry point for Reachy Care custom conversation app.
"""
import asyncio
import json
import logging
import os
import resource
import signal
import sys
import tracemalloc
from pathlib import Path

import requests

from conversation_engine import ConversationEngine

_PID_FILE = Path("/tmp/conv_app_v2.pid")
_WAKE_SHM = Path("/dev/shm/reachy_wake.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [conv_app_v2] %(levelname)s  %(message)s",
)
logger = logging.getLogger(__name__)


async def _mem_monitor():
    """Log RSS + tracemalloc top allocators every 30 s.

    Also logs the number of live threads and the two largest growth stacks,
    to help locate threads that accumulate at the rhythm of the 100 Hz loop
    (the Reachy Mini SDK gRPC client is a known suspect).
    """
    import threading as _t
    tracemalloc.start(25)
    baseline = tracemalloc.take_snapshot()
    while True:
        await asyncio.sleep(30)
        rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        n_threads = _t.active_count()
        snap = tracemalloc.take_snapshot()
        top = snap.compare_to(baseline, "lineno")[:5]
        logger.info("[MEM] RSS=%.0f MB, threads=%d, top growth vs baseline:",
                    rss_mb, n_threads)
        for i, stat in enumerate(top):
            logger.info("[MEM]   #%d +%.1f KB %s",
                        i + 1, stat.size_diff / 1024, stat.traceback[0])
        # Full stack of the two largest growths (helps locate the SDK call site)
        for i, stat in enumerate(top[:2]):
            logger.info("[MEM] === full traceback #%d (+%.1f KB) ===",
                        i + 1, stat.size_diff / 1024)
            for frame in stat.traceback.format():
                logger.info("[MEM]   %s", frame)


_DAEMON_MEDIA_URL = "http://localhost:8000/api/media"


def _daemon_media_release() -> None:
    """Ask the Pollen daemon to stop GstMediaServer so conv_app can open the mic.

    The daemon keeps reachymini_audio_src (dsnoop ipc_key=4242) in PLAYING via
    GstMediaServer. Our GStreamer pipeline may request incompatible caps on the
    same dsnoop slave → EINVAL. POST /api/media/release puts the pipeline in NULL,
    freeing the dsnoop for exclusive use by conv_app.

    Non-blocking: if the daemon is unreachable, we log a warning and continue.
    The dsnoop sharing may still work if formats happen to match.
    """
    try:
        r = requests.post(f"{_DAEMON_MEDIA_URL}/release", timeout=5)
        r.raise_for_status()
        logger.info("[startup] daemon media released: %s", r.json())
    except Exception as exc:
        logger.warning(
            "[startup] POST /api/media/release failed (non-blocking, dsnoop sharing may fail): %s",
            exc,
        )


def _daemon_media_acquire() -> None:
    """Ask the Pollen daemon to restart GstMediaServer after conv_app is done.

    Called at shutdown so the daemon resumes normal WebRTC / DOA / beamforming
    operation once the conv_app releases the mic.

    Non-blocking: if the daemon is unreachable, it will recover on next restart.
    """
    try:
        r = requests.post(f"{_DAEMON_MEDIA_URL}/acquire", timeout=5)
        r.raise_for_status()
        logger.info("[shutdown] daemon media acquired: %s", r.json())
    except Exception as exc:
        logger.warning(
            "[shutdown] POST /api/media/acquire failed (non-blocking): %s",
            exc,
        )


def _on_sigusr1(engine):
    """Wake-word interrupt handler (wake-priority path).

    The vision process writes the wake payload to /dev/shm and then sends
    SIGUSR1. This handler reads the payload and schedules handle_wake_event
    on the asyncio loop. Measured latency stays below 5 ms even under a
    saturated asyncio loop (benched around 1.8 ms on Pi 4 with a tight
    sleep(0.001) loop running in parallel).
    """
    def _handler():
        doa_rad = None
        event_text = None
        event_instructions = None
        try:
            payload = json.loads(_WAKE_SHM.read_text())
            doa_rad = payload.get("doa_rad")
            event_text = payload.get("event_text")
            event_instructions = payload.get("event_instructions")
            logger.info("SIGUSR1 wake: doa_rad=%s event=%s",
                        doa_rad, (event_text or "")[:60])
        except Exception as exc:
            logger.warning("SIGUSR1 payload read failed: %s (continuing without payload)", exc)
        asyncio.create_task(engine.handle_wake_event(
            doa_rad=doa_rad,
            event_text=event_text,
            event_instructions=event_instructions,
        ))
    return _handler


async def main():
    engine = ConversationEngine()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(engine.stop()))
    # SIGUSR1 = wake-word interrupt; preempts a saturated asyncio loop.
    loop.add_signal_handler(signal.SIGUSR1, _on_sigusr1(engine))
    _PID_FILE.write_text(str(os.getpid()))
    logger.info("conv_app_v2 PID=%d written to %s, SIGUSR1 wake handler installed",
                os.getpid(), _PID_FILE)

    # Release daemon media BEFORE opening the audio pipeline.
    # This stops GstMediaServer (daemon) so it no longer holds the dsnoop slave
    # on reachymini_audio_src — our GStreamer capture pipeline can then open it
    # without EINVAL (incompatible caps on the shared dsnoop).
    _daemon_media_release()

    try:
        await engine.start()
        if os.environ.get("CONV_APP_V2_MEM_DIAG") == "1":
            asyncio.create_task(_mem_monitor())
        while engine._running:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        await engine.stop()
        # Re-acquire daemon media so GstMediaServer resumes after conv_app exits.
        _daemon_media_acquire()
        try:
            _PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
    logger.info("conv_app_v2 exited.")


if __name__ == "__main__":
    asyncio.run(main())
