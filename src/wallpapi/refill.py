"""The background **Pool** refill thread: a loop on another thread and a way to stop it.

Every decision belongs to the Core service, where a test reaches it without a thread. Every wait is
`stop_event.wait(n)`, never `time.sleep(n)` (invariant 12).
"""

from __future__ import annotations

import threading

from wallpapi.core import CoreService
from wallpapi.wallhaven import REQUEST_TIMEOUT

JOIN_TIMEOUT = REQUEST_TIMEOUT + 5.0
"""Seconds shutdown waits for the thread: greater than the Wallhaven client's request timeout (invariant
12).
"""


def refill_loop(core: CoreService, stop_event: threading.Event) -> None:
    """Wait as long as the Core service says, take one step, repeat until stopped.

    `refill_step` never raises, so the thread cannot die.
    """
    with core.refill_running():
        while not stop_event.is_set():
            wait = core.refill_wait()
            if wait > 0 and stop_event.wait(wait):
                return
            if stop_event.is_set():
                return
            core.refill_step()


class RefillThread:
    """Start and stop `refill_loop`. Owned by the FastAPI lifespan.

    One thread, which is why the app runs single-worker.
    """

    def __init__(self, core: CoreService) -> None:
        self._core = core
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        # Not a daemon: shutdown joins it, so a step half way through a write finishes.
        self._thread = threading.Thread(
            target=refill_loop, args=(self._core, self._stop), name="wallpapi-refill", daemon=False
        )
        self._thread.start()

    def stop(self, timeout: float = JOIN_TIMEOUT) -> None:
        """Ask the loop to stop and wait for it. Safe to call without a start, and twice."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)
