"""
frame_queue.py — File d'attente thread-safe pour partager les frames caméra.

Utilisée pour passer les frames de la boucle principale (main.py)
au serveur dashboard (MJPEG stream) sans copie inutile.
"""

import threading

import numpy as np


class SharedFrameQueue:
    """Queue à un seul slot — le dashboard lit toujours la frame la plus récente."""

    def __init__(self) -> None:
        self._frame: np.ndarray | None = None
        self._lock = threading.Lock()
        self._event = threading.Event()

    def put(self, frame: np.ndarray) -> None:
        """Écrit une nouvelle frame (écrase la précédente)."""
        with self._lock:
            self._frame = frame
        self._event.set()

    def get(self, timeout: float = 1.0) -> np.ndarray | None:
        """Lit la frame la plus récente. Bloque jusqu'à timeout si aucune frame."""
        if self._event.wait(timeout=timeout):
            with self._lock:
                frame = self._frame
            self._event.clear()
            return frame
        return None

    @property
    def available(self) -> bool:
        return self._frame is not None
