"""The background **Pool** refill thread.

Deliberately almost nothing. Every decision — whether to call Wallhaven, how long to wait first, what to do
with a failure — belongs to the Core service, where a test reaches it through the seam with a fake clock and
no thread at all (invariant 1). What is left here is the one thing a test cannot have: a loop on another
thread, and a way to stop it.

A `threading.Thread` rather than asyncio, per ADR 0001. Every wait is `stop_event.wait(n)` and never
`time.sleep(n)`, so shutdown does not have to outlast an idle period (invariant 12).
"""

from __future__ import annotations

import threading

from wallpapi.core import CoreService
from wallpapi.wallhaven import REQUEST_TIMEOUT

JOIN_TIMEOUT = REQUEST_TIMEOUT + 5.0
"""How long shutdown waits for the refill thread, in seconds.

Greater than the Wallhaven client's request timeout (invariant 12). The worst case is a stop arriving the
instant a search was sent: the request cannot outlast its own timeout, so the loop gets back to the
`stop_event` inside it and the join does not have to give up.
"""


def refill_loop(core: CoreService, stop_event: threading.Event) -> None:
    """Wait as long as the Core service says, take one step, repeat until stopped.

    The whole thread. `refill_step` never raises, so nothing here has to catch anything: a failed **API
    call** is recorded as the refill's last error and turned into a back-off that the next `refill_wait`
    hands back. The loop is not allowed to die, because the **Batch** page's only answer to an empty
    **Pool** is what this thread last did (#15).
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
    """Start and stop `refill_loop`. Owned by the FastAPI lifespan and nothing else.

    One thread, which is why the app runs single-worker: `uvicorn --workers N` would be N refill threads
    spending N times the 45-per-minute budget against one SQLite file.
    """

    def __init__(self, core: CoreService) -> None:
        self._core = core
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        # Not a daemon: shutdown joins it deliberately, so that a step half way through a write finishes
        # rather than being abandoned by an interpreter on its way out.
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
