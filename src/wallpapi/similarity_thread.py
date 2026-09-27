"""The background thread that keeps the **Similarity provider**'s own cache up to date.

The same shape as `refill.py`, and for the same reason: every decision — what to fetch, what to embed, how
long to wait before the next step — belongs to the provider behind the Core service's seam, where a test
reaches it with no thread at all (invariant 1). What is left here is the one thing a test cannot have, a
loop on another thread and a way to stop it.

**A thread of its own rather than a few lines inside `refill_loop`.** The two do not want the same thing.
The refill's job on a first boot is to fill an empty **Pool** while somebody watches an empty page, and
putting an 85MiB model download in front of its first search would hold that page empty for the length of
the download — for a **Score** that cannot matter until there are **Wallpapers** to score and **Verdicts**
to score them from. They also idle differently: the refill waits on a rate limit measured in seconds, and
this waits on there being new thumbnails at all. Two loops, two waits, one `stop_event` each.

Costs nothing when it is not needed: the baseline and tag providers answer `NOTHING_TO_CATCH_UP` and do
nothing, so selecting either leaves this thread asleep for an hour at a time.
"""

from __future__ import annotations

import threading

from wallpapi.core import CoreService
from wallpapi.similarity_embedding import DOWNLOAD_TIMEOUT

JOIN_TIMEOUT = DOWNLOAD_TIMEOUT + 5.0
"""How long shutdown waits for this thread, in seconds.

Greater than the model download's read timeout, the way `refill.JOIN_TIMEOUT` is greater than the
Wallhaven client's (invariant 12). The worst case is a stop arriving the instant a chunk was requested: the
read cannot outlast its own timeout, the loop gets back to the `stop_event` the download checks between
chunks, and the join does not have to give up.
"""


def similarity_loop(core: CoreService, stop_event: threading.Event) -> None:
    """Take one step, wait as long as the provider asked, repeat until stopped.

    The other way round from `refill_loop`, which waits first: the refill has a rate limit to respect
    before its first call, and this has nothing to respect before its first step — on a fresh install the
    first step is the one that starts the download, and waiting an hour to begin would be an hour of
    **Scores** from the baseline for no reason.

    `similarity_step` never raises, so nothing here catches anything: a failed download is recorded by the
    provider and shows up on the page as a line. The loop is not allowed to die, for `refill_loop`'s
    reason — a provider stuck on the baseline with nothing saying so is worse than one that is obviously
    broken.
    """
    while not stop_event.is_set():
        wait = core.similarity_step(stop_event)
        if wait > 0 and stop_event.wait(wait):
            return


class SimilarityThread:
    """Start and stop `similarity_loop`. Owned by the FastAPI lifespan and nothing else."""

    def __init__(self, core: CoreService) -> None:
        self._core = core
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        # Not a daemon, like the refill thread: shutdown joins it deliberately, so a batch of embeddings
        # half way into its cache finishes rather than being abandoned by an interpreter on its way out.
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
