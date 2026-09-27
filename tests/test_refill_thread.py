"""The refill thread itself: it starts with the app and it stops when told. Issue #6.

The only test in the suite that starts a thread, and the only one that can be: everything the refill
*decides* is a Core service method that `test_refill.py` drives by hand with a fake clock. What is left
here is what a fake cannot stand in for — a loop on another thread, and a shutdown that has to end it.

Still no network: the Wallhaven client is the fake, so the thread fetches from an in-memory catalogue.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import make_harness
from tests.fakes import catalogue_of
from wallpapi.refill import JOIN_TIMEOUT, RefillThread
from wallpapi.wallhaven import REQUEST_TIMEOUT
from wallpapi.web.app import create_app


def test_the_lifespan_starts_the_refill_and_stops_it_cleanly(db_path: Path) -> None:
    """Acceptance criterion: the refill runs in the background, and shutdown is clean.

    The **Pool** is at its target before the app starts, so the loop's first act is a long cancellable
    wait rather than 45 **API calls**. That is what makes this test fast and deterministic: what is being
    checked is that the thread starts, that the wait is cancellable, and that the join returns — not how
    much the refill managed to fetch in the meantime.

    `TestClient` as a context manager is what runs the lifespan at all; leaving it out would skip both
    halves.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(24), fill_pool=1)
    harness.core.update_settings(pool_target_size=1)
    app = create_app(harness.core, refill=True)

    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert harness.core.refill_status().running, "the lifespan should have started the thread"
        assert "Refill idle" in response.text

    assert not harness.core.refill_status().running, "shutdown should have joined the thread"


def test_the_thread_fills_the_pool_from_an_empty_start(db_path: Path) -> None:
    """The loop really does call `refill_step`, and not merely exist.

    The catalogue is one page and the target is one **Wallpaper**, so a single call takes the **Pool**
    past its target and the loop settles into its idle wait.

    The wait is on the fake's own event rather than on a clock: no sleep, and no guess about how long
    another thread needs. It returns the instant the search happens and fails the test rather than hanging
    if it never does.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(24), fill_pool=0)
    harness.core.update_settings(pool_target_size=1)
    app = create_app(harness.core, refill=True)

    with TestClient(app) as client:
        client.get("/")
        assert harness.wallhaven.searched.wait(JOIN_TIMEOUT), "the thread should have searched"

    assert harness.core.refill_status().pool_size == 24


def test_starting_and_stopping_twice_is_harmless(db_path: Path) -> None:
    """A stop without a start, and a second stop, must not raise — shutdown paths get run twice."""
    harness = make_harness(db_path, fill_pool=0)
    harness.core.update_settings(pool_target_size=1)
    thread = RefillThread(harness.core)

    thread.stop()
    thread.start()
    thread.start()
    thread.stop()
    thread.stop()

    assert not harness.core.refill_status().running


def test_the_join_timeout_outlasts_a_request_that_is_still_in_flight() -> None:
    """Invariant 12: the shutdown join must be longer than the client's own request timeout.

    Otherwise a stop arriving the instant a search was sent gives up on a thread that was always going to
    come back, and the process is left with a thread it stopped waiting for.
    """
    assert JOIN_TIMEOUT > REQUEST_TIMEOUT
