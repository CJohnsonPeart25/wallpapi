"""The thumbnail downloader's thread: it starts with the app, runs alone, and stops when told (#44).

The third test file in the suite that starts a thread, for `test_refill_thread.py`'s reason: everything the
downloader *decides* is behind the seam and driven by hand in `test_thumbnail_prefetch.py`. What is left
here is what a fake cannot stand in for — a loop on another thread, which thread a fetch happens on, and a
shutdown that has to end it.

Still no network: the thumbnail host is the fake, and every wait on it is on an event, never a sleep.
"""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import make_harness
from tests.fakes import catalogue_of
from wallpapi.thumbnail_thread import JOIN_TIMEOUT, THREAD_NAME, ThumbnailThread
from wallpapi.wallhaven import REQUEST_TIMEOUT
from wallpapi.web.app import create_app


def _running(name: str) -> bool:
    return any(thread.name == name for thread in threading.enumerate())


def test_the_lifespan_starts_the_downloader_and_it_fetches_on_its_own_thread(db_path: Path) -> None:
    """Acceptance criteria: thumbnail fetching for the **Pool** never runs on a request thread, never inside
    `refill_loop` and never on the similarity thread — and it starts through the `refill` flag.

    All three background threads are running, nothing asks for a page, and every fetch the fake saw was
    made from the downloader's own thread.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(3))
    harness.core.update_settings(pool_target_size=1)
    app = create_app(harness.core, refill=True)

    with TestClient(app):
        assert harness.wallhaven.thumbnail_fetched.wait(JOIN_TIMEOUT), "the downloader should have fetched"
        assert _running(THREAD_NAME)

    assert harness.wallhaven.thumbnail_threads
    assert set(harness.wallhaven.thumbnail_threads) == {"wallpapi-thumbnails"}
    assert not _running(THREAD_NAME), "shutdown should have joined the downloader"


def test_without_the_refill_flag_no_downloader_starts(db_path: Path) -> None:
    """Acceptance criterion: off by default, so no test starts it by accident."""
    harness = make_harness(db_path, catalogue=catalogue_of(3))
    app = create_app(harness.core)

    with TestClient(app) as client:
        client.get("/batch")
        assert not _running(THREAD_NAME)

    assert harness.wallhaven.thumbnail_fetches == []


def test_shutdown_with_a_fetch_in_flight_completes_within_the_join_timeout(db_path: Path) -> None:
    """Acceptance criterion. The stop arrives while a fetch is outstanding; the fetch then returns — as a
    real one must, inside `REQUEST_TIMEOUT` — and the loop goes back to its `stop_event` rather than on to
    the next **Pool** member. The join returns and nothing else is fetched."""
    harness = make_harness(db_path, catalogue=catalogue_of(3))
    release = threading.Event()
    harness.wallhaven.hold_thumbnails = release
    downloader = ThumbnailThread(harness.core)
    downloader.start()
    assert harness.wallhaven.thumbnail_fetched.wait(JOIN_TIMEOUT), "a fetch should be in flight"

    stopper = threading.Thread(target=downloader.stop)
    stopper.start()
    # Released only once the stop has landed, so the fetch returning is the first thing the loop sees.
    assert downloader._stop.wait(JOIN_TIMEOUT)  # pyright: ignore[reportPrivateUsage]
    release.set()
    stopper.join(JOIN_TIMEOUT)

    assert not stopper.is_alive(), "stop should have returned inside the join timeout"
    assert not _running(THREAD_NAME)
    assert len(harness.wallhaven.thumbnail_fetches) == 1


def test_starting_and_stopping_twice_is_harmless(db_path: Path) -> None:
    """A stop without a start, and a second stop, must not raise — shutdown paths get run twice."""
    harness = make_harness(db_path, fill_pool=0)
    downloader = ThumbnailThread(harness.core)

    downloader.stop()
    downloader.start()
    downloader.start()
    downloader.stop()
    downloader.stop()

    assert not _running(THREAD_NAME)


def test_the_join_timeout_outlasts_a_fetch_that_is_still_in_flight() -> None:
    """Invariant 12: the shutdown join must be longer than the client's own request timeout."""
    assert JOIN_TIMEOUT > REQUEST_TIMEOUT
