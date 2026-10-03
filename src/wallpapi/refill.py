"""The background **Pool** refill thread. Every decision belongs to the Core service; every wait is
`stop_event.wait(n)`, never `time.sleep(n)`.
"""

from __future__ import annotations

import threading

from wallpapi.core import CoreService
from wallpapi.pool import JOIN_TIMEOUT, refill_loop


class RefillThread:
    """Start and stop `refill_loop`. Owned by the FastAPI lifespan; one thread, hence single-worker."""

    def __init__(self, core: CoreService) -> None:
        self._core = core
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        # Not a daemon: shutdown joins it, so a step half way through a write finishes.
        self._thread = threading.Thread(
            target=refill_loop, args=(self._core.refill, self._stop), name="wallpapi-refill", daemon=False
        )
        self._thread.start()

    def stop(self, timeout: float = JOIN_TIMEOUT) -> None:
        """Ask the loop to stop and wait for it. Safe to call without a start, and twice."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)
