"""Harness for driving the Core service the way the UI does.

Every test enters through the Core service. Nothing here touches the network, and nothing reaches past the
seam into storage to check a result — if a behaviour isn't observable through the Core service, it isn't
asserted.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from tests.fakes import (
    THUMBNAIL_BYTES,
    FakeClock,
    FakeLibraryWriter,
    FakeSimilarityProvider,
    FakeWallhavenClient,
    catalogue_of,
)
from wallpapi.core import Batch, CoreService
from wallpapi.model import Verdict, Wallpaper
from wallpapi.rng import SeededRandom

FIXED_NOW = dt.datetime(2026, 9, 24, 11, 30, 0, tzinfo=dt.UTC)

BATCH_ID = re.compile(r'name="batch_id" value="([0-9a-f]+)"')


def batch_id_of(body: str) -> str:
    match = BATCH_ID.search(body)
    assert match is not None, "the page must carry the Batch ID it will submit"
    return match.group(1)


@dataclass
class Harness:
    """A Core service plus the fakes behind it, so tests can assert on both sides of the seam."""

    core: CoreService
    wallhaven: FakeWallhavenClient
    library: FakeLibraryWriter
    similarity: FakeSimilarityProvider
    clock: FakeClock

    def fill_pool(self, steps: int = 1) -> None:
        """Run the refill by hand, `steps` **API calls** worth.

        Never the thread. A test that started one would be a test with a race in it and a real second of
        waiting somewhere; `refill_step` is a Core service method precisely so the refill can be driven one
        call at a time. The one test that does start a thread is `test_refill_thread.py`, and what it
        checks is the thread, not the refilling.
        """
        for _ in range(steps):
            self.core.refill_step()


def make_harness(
    db_path: Path,
    *,
    catalogue: Sequence[Wallpaper] | None = None,
    now: dt.datetime = FIXED_NOW,
    seed: int = 1,
    search_seed: str | None = None,
    page_size: int = 24,
    thumbnail_bytes: bytes = THUMBNAIL_BYTES,
    fail_from_call: int | None = None,
    rate_limited_calls: int = 0,
    retry_after: float | None = None,
    like_results: Mapping[str, Sequence[Wallpaper]] | None = None,
    fill_pool: int = 1,
    similarities: dict[tuple[str, str], float] | None = None,
    similarity_notice: str | None = None,
    catch_up_waits: Sequence[float] = (),
    vectors: Mapping[str, NDArray[np.float32]] | None = None,
) -> Harness:
    """Build a Core service over `db_path`, with the **Pool** already primed.

    Called twice with the same path to prove the Decision log survives a restart, so it must run migrations
    idempotently rather than assuming an empty database.

    `fill_pool` is how many refill steps to run before handing the harness over. One by default, because a
    **Batch** is drawn from the **Pool** and a test that only wants "a **Batch** exists" should not have to
    say so; one step is a whole page, which is 24 **Wallpapers** at the default page size. Pass `0` in the
    tests that care what an empty **Pool** does.

    `similarities` goes straight to the fake **Similarity provider**: `{(pool id, decided id): value}`, with
    a **Wallpaper** against itself 1.0 and everything unnamed 0.0.

    `similarity_notice` is what that provider says about itself on the **Batch** page — `None`, a provider
    working at full strength, unless a test is about the notice. `catch_up_waits` is what its upkeep asks
    the background thread to wait between steps.

    `vectors` is each **Wallpaper**'s position for the varied **Unknown** draw (#45), keyed by ID. `None`,
    a provider with no positions, unless a test is about that draw.
    """
    wallhaven = FakeWallhavenClient(
        catalogue_of(24) if catalogue is None else catalogue,
        seed=search_seed,
        page_size=page_size,
        thumbnail_bytes=thumbnail_bytes,
        fail_from_call=fail_from_call,
        rate_limited_calls=rate_limited_calls,
        retry_after=retry_after,
        like_results=like_results,
    )
    library = FakeLibraryWriter()
    similarity = FakeSimilarityProvider(
        similarities, notice=similarity_notice, catch_up_waits=catch_up_waits, vectors=vectors
    )
    clock = FakeClock(now)
    core = CoreService(
        db_path=db_path,
        wallhaven=wallhaven,
        library=library,
        similarity=similarity,
        random_source=SeededRandom(seed),
        clock=clock,
    )
    harness = Harness(core=core, wallhaven=wallhaven, library=library, similarity=similarity, clock=clock)
    harness.fill_pool(fill_pool)
    return harness


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "wallpapi.db"


@pytest.fixture
def harness(db_path: Path) -> Harness:
    return make_harness(db_path)


def favourite(harness: Harness, *wallpaper_ids: str) -> None:
    """Record a **Favourite** the way the UI does: mint a **Batch**, mark it, submit it.

    Through the seam and never by writing a row. The **Wallpapers** have to be *in* the **Batch** to be
    judged, so every test here keeps the **Pool** small enough that one **Batch** shows all of it.
    """
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    shown = {w.id for w in batch.wallpapers}
    assert set(wallpaper_ids) <= shown, f"{set(wallpaper_ids) - shown} is not in the batch to judge"
    for wallpaper_id in wallpaper_ids:
        harness.core.set_draft_verdict(batch.id, wallpaper_id, Verdict.FAVOURITE)
    harness.core.submit_batch(batch.id)


def favourite_the_whole_batch(harness: Harness, verdict: Verdict = Verdict.FAVOURITE) -> Batch:
    """Mark every **Wallpaper** on the live **Batch** and submit it, returning the **Batch** submitted."""
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    harness.core.set_all_draft_verdicts(batch.id, verdict)
    harness.core.submit_batch(batch.id)
    return batch
