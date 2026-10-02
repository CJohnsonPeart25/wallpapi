"""The background thread that fetches a thumbnail for every **Pool** member (#44, ADR 0017).

The same shape as `refill.py`: every decision — what to fetch, in what order, how long to wait, what to do
with a failure — belongs to the Core service, where a test reaches it through the seam with a fake clock
and no thread at all (invariant 1). What is left here is a loop on another thread and a way to stop it.

**A thread of its own, and not a third duty for either of the other two.** Not inside `refill_loop`, whose
waits are the 45-per-minute budget's and whose host is a different one. Not on the similarity thread,
because on a fresh install that thread spends its first minutes on an 85MiB model download, and the
thumbnails it will embed afterwards should already be waiting when it finishes. The downloader and the
embedder are connected only by the **Thumbnail cache**: this writes files into it, and `catch_up` embeds
whatever it finds there.

Runs whichever **Similarity provider** is selected. `metadata` and `tags` read no thumbnails, but every
tile render is then a cache hit, and the seam stays free of a "wants thumbnails" member.
"""

from __future__ import annotations

import threading

from wallpapi.core import CoreService
from wallpapi.wallhaven import REQUEST_TIMEOUT

THREAD_NAME = "wallpapi-thumbnails"

JOIN_TIMEOUT = REQUEST_TIMEOUT + 5.0
"""How long shutdown waits for the downloader, in seconds.

Greater than the client's request timeout (invariant 12), for `refill.JOIN_TIMEOUT`'s reason: a stop that
arrives the instant a fetch was sent has to outlast that fetch, which cannot outlast its own timeout.
"""


def thumbnail_loop(core: CoreService, stop_event: threading.Event) -> None:
    """Wait as long as the Core service says, take one step, repeat until stopped.

    Waits first, like `refill_loop`: the gap after a fetch is owed before the next one, and the first wait
    of all is zero. `thumbnail_step` never raises, so nothing here catches anything.
    """
    while not stop_event.is_set():
        wait = core.thumbnail_wait()
        if wait > 0 and stop_event.wait(wait):
            return
        if stop_event.is_set():
            return
        core.thumbnail_step()


class ThumbnailThread:
    """Start and stop `thumbnail_loop`. Owned by the FastAPI lifespan and nothing else."""

    def __init__(self, core: CoreService) -> None:
        self._core = core
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        # Not a daemon, like the other two: shutdown joins it, so a thumbnail half way into the cache is
        # finished by `os.replace` rather than abandoned as a `.part` file.
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
