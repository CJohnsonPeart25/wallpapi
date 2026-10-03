"""The background thread that keeps the **Similarity provider**'s cache up to date, apart from the refill so a
model download never delays its first search.
"""

from __future__ import annotations

import threading

from wallpapi.core import CoreService
from wallpapi.similarity_embedding import DOWNLOAD_TIMEOUT

JOIN_TIMEOUT = DOWNLOAD_TIMEOUT + 5.0
"""Seconds shutdown waits for the thread: greater than the model download's read timeout (invariant 12)."""


def similarity_loop(core: CoreService, stop_event: threading.Event) -> None:
    """Take one step, wait as the provider asked, repeat; `similarity_step` never raises."""
    while not stop_event.is_set():
        wait = core.similarity_step(stop_event)
        if wait > 0 and stop_event.wait(wait):
            return


class SimilarityThread:
    """Start and stop `similarity_loop`. Owned by the FastAPI lifespan."""

    def __init__(self, core: CoreService) -> None:
        self._core = core
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        # Not a daemon: shutdown joins it, so a batch of embeddings half way into its cache finishes.
        self._thread = threading.Thread(
            target=similarity_loop, args=(self._core, self._stop), name="wallpapi-similarity", daemon=False
        )
        self._thread.start()

    def stop(self, timeout: float = JOIN_TIMEOUT) -> None:
        """Ask the loop to stop and wait for it. Safe to call without a start, and twice."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)
