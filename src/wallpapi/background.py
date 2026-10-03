"""One background loop on its own thread: the shell the refill, the **Similarity provider**'s upkeep and the
thumbnail downloader share. What each loop does, and every wait in it, belongs to its module; this starts it
and stops it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable


class BackgroundLoop:
    """Run `loop(stop_event)` on a non-daemon thread called `name`, owned by the FastAPI lifespan.

    `loop` returns once `stop_event` is set; its every wait is `stop_event.wait(n)` (invariant 12).
    `join_timeout` must exceed the longest request the loop can be inside, or shutdown hangs on it.
    """

    def __init__(self, loop: Callable[[threading.Event], None], *, name: str, join_timeout: float) -> None:
        self.name = name
        self._loop = loop
        self._join_timeout = join_timeout
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the thread. A second start is ignored."""
        if self._thread is not None:
            return
        # Not a daemon: shutdown joins it, so a step half way through a write finishes.
        self._thread = threading.Thread(target=self._loop, args=(self._stop,), name=self.name, daemon=False)
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        """Ask the loop to stop and wait up to `timeout` (default `join_timeout`) for it. Safe to call without
        a start, and twice.
        """
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(self._join_timeout if timeout is None else timeout)
