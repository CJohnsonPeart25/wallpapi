"""The **Similarity provider**'s upkeep thread: it starts with the app and it stops when told (#14).

The second test in the suite that starts a thread, and for `test_refill_thread.py`'s reason: everything
the upkeep *decides* is behind the seam and driven by hand in `test_similarity_upkeep.py`. What is left
here is what a fake cannot stand in for — a loop on another thread, and a shutdown that has to end it.

Still no network and still no model: the provider is the fake, which records the calls and hands back the
waits a test arranged.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import make_harness
from tests.fakes import catalogue_of
from wallpapi.similarity_thread import JOIN_TIMEOUT, SimilarityThread
from wallpapi.web.app import create_app


def test_the_lifespan_starts_the_upkeep_and_stops_it_cleanly(db_path: Path) -> None:
    """Acceptance criterion: the model is fetched off the request path, and shutdown is clean.

    The fake settles into `NOTHING_TO_CATCH_UP` after one call, so the loop's second act is a long
    cancellable wait. What is checked is that the thread starts, that it asks the provider to catch up
    without anything having requested a page, and that the join returns.

    `TestClient` as a context manager is what runs the lifespan at all; leaving it out would skip both
    halves.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(24))
    harness.core.update_settings(pool_target_size=1)
    app = create_app(harness.core, refill=True)

    with TestClient(app):
        assert harness.similarity.catch_up_started.wait(JOIN_TIMEOUT), "the thread should have caught up"

    assert harness.similarity.catch_up_calls, "the upkeep should have run without a request"


def test_the_upkeep_is_handed_the_thumbnail_cache(db_path: Path) -> None:
    """The one fact the Core service has that the provider does not.

    The images wallpapi already holds are the ones in the **Thumbnail cache** (invariant 8), and a
    **Wallpaper** whose thumbnail has not been fetched is simply not in there — which is the whole of what
    happens to it: nothing embeds it, so it falls back to the baseline until its tile has rendered.
    """
    harness = make_harness(db_path)

    harness.core.similarity_step(SimilarityThread(harness.core)._stop)  # pyright: ignore[reportPrivateUsage]

    assert harness.similarity.catch_up_calls == [harness.core.thumbnail_dir]


def test_a_provider_with_more_to_do_is_called_again_without_waiting(db_path: Path) -> None:
    """A 0.0 means "come straight back", which is how a **Pool**-sized backlog is worked through one batch
    at a time without the loop sleeping between batches."""
    harness = make_harness(db_path, catalogue=catalogue_of(24), catch_up_waits=[0.0, 0.0])
    app = create_app(harness.core, refill=True)

    with TestClient(app):
        assert harness.similarity.catch_up_started.wait(JOIN_TIMEOUT)

    assert len(harness.similarity.catch_up_calls) >= 3, "the two 0.0s should have been followed straight up"


def test_starting_and_stopping_twice_is_harmless(db_path: Path) -> None:
    """A stop without a start, and a second stop, must not raise — shutdown paths get run twice."""
    harness = make_harness(db_path, fill_pool=0)
    thread = SimilarityThread(harness.core)

    thread.stop()
    thread.start()
    thread.start()
    thread.stop()
    thread.stop()
