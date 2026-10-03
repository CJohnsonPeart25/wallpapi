"""The background thread that fetches a thumbnail for every **Pool** member (ADR 0017).

The same shape as `refill.py`. A thread of its own so neither the refill's rate limit nor the model download
holds it up.
"""

from __future__ import annotations

import threading

from wallpapi.core import CoreService
from wallpapi.wallhaven import REQUEST_TIMEOUT

THREAD_NAME = "wallpapi-thumbnails"

JOIN_TIMEOUT = REQUEST_TIMEOUT + 5.0
"""Seconds shutdown waits for the downloader: greater than the client's request timeout (invariant 12)."""


def thumbnail_loop(core: CoreService, stop_event: threading.Event) -> None:
    """Wait as long as the Core service says, take one step, repeat until stopped.

    `thumbnail_step` never raises, so the thread cannot die.
    """
    while not stop_event.is_set():
        wait = core.thumbnail_wait()
        if wait > 0 and stop_event.wait(wait):
            return
        if stop_event.is_set():
            return
        core.thumbnail_step()


class ThumbnailThread:
    """Start and stop `thumbnail_loop`. Owned by the FastAPI lifespan."""

    def __init__(self, core: CoreService) -> None:
        self._core = core
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        # Not a daemon: shutdown joins it, so a thumbnail half way into the cache is finished.
        self._thread = threading.Thread(
            target=thumbnail_loop, args=(self._core, self._stop), name=THREAD_NAME, daemon=False
        )
        self._thread.start()

    def stop(self, timeout: float = JOIN_TIMEOUT) -> None:
        """Ask the loop to stop and wait for it. Safe to call without a start, and twice."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)
