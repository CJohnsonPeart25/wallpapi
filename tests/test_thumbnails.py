"""The **Thumbnail cache**, through `thumbnails` alone: verdict-aware eviction with a size cap behind it, and
the background downloader that fills it with every **Pool** member (ADR 0017).

A real in-memory database, a temporary directory handed to the constructor, the fake Wallhaven client and the
fake clock. **Wallpapers** are admitted, retired and decided as `pool` and `decisions` do. The downloader is
driven by hand, as the refill is: `wait` says how long the thread would wait and `step` does at most one
fetch. The rule ADR 0009 recorded is held here: a size cap never evicts a **Wallpaper** with an **Explicit
Verdict**.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Generator, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.conftest import FIXED_NOW, make_harness, raw_connection
from tests.fakes import (
    THUMBNAIL_BYTES,
    FakeClock,
    FakeSimilarities,
    FakeWallhavenClient,
    MemoryStore,
    ModelOnDisk,
    StubEmbed,
    WallhavenUnreachable,
    wallpaper,
)
from wallpapi import batches, decisions, pool, settings, storage
from wallpapi.batches import Batch, Batches
from wallpapi.model import Verdict, Wallpaper
from wallpapi.pool import RefillStatus, RefillStrategy
from wallpapi.rng import SeededRandom
from wallpapi.similarity import Embeddings
from wallpapi.thumbnails import (
    BACKOFF_SECONDS,
    BYTES_IN_A_MEGABYTE,
    GAP_SECONDS,
    IDLE_RECHECK_SECONDS,
    Thumbnails,
    gap_needed,
)
from wallpapi.wallhaven import RateLimited, ThumbnailUnavailable

BIG_THUMBNAIL = b"x" * (400 * 1024)
"""Three of these are over a one-megabyte cap and two are under it."""

ONE_MEGABYTE = 1 * BYTES_IN_A_MEGABYTE


@dataclass
class Rig:
    """`Thumbnails` over one in-memory database and one temporary directory, with the fakes behind it."""

    connection: sqlite3.Connection
    thumbnails: Thumbnails
    wallhaven: FakeWallhavenClient
    clock: FakeClock

    @property
    def directory(self) -> Path:
        return self.thumbnails.directory

    def admit(self, wallpapers: Sequence[Wallpaper]) -> None:
        with storage.write(self.connection) as write:
            pool.admit(
                write, wallpapers, settings.get(write), source=RefillStrategy.RANDOM, at=self.clock.now()
            )

    def retire(self, *wallpaper_ids: str) -> None:
        with storage.write(self.connection) as write:
            pool.retire(write, wallpaper_ids)

    def decide(self, verdict: Verdict, *wallpaper_ids: str) -> None:
        with storage.write(self.connection) as write:
            decisions.append(write, dict.fromkeys(wallpaper_ids, verdict), batch_id=None, at=self.clock.now())

    def configure(self, **fields: object) -> None:
        with storage.write(self.connection) as write:
            assert isinstance(settings.update(write, **fields), settings.Settings)

    def fill(self, *wallpaper_ids: str) -> None:
        """Fetch each thumbnail once, exactly as a page load does."""
        for wallpaper_id in wallpaper_ids:
            assert self.thumbnails.get(self.connection, wallpaper_id) is not None

    def cached(self) -> set[str]:
        return {path.stem for path in self.directory.iterdir()} if self.directory.is_dir() else set()

    def evict(self, cap_bytes: int = 500 * BYTES_IN_A_MEGABYTE) -> tuple[str, ...]:
        return self.thumbnails.evict(self.connection, cap_bytes).evicted

    def run(self, steps: int) -> None:
        """Step the downloader as its thread would, letting the fake clock pass for every wait it asks for."""
        for _ in range(steps):
            self.clock.advance(self.thumbnails.wait())
            self.thumbnails.step(self.connection)

    def fetches_of(self, wallpaper_id: str) -> int:
        return self.wallhaven.thumbnail_fetches.count(wallpaper(wallpaper_id).thumbnail_url)


@contextmanager
def rig_over(
    tmp_path: Path, wallpapers: Sequence[Wallpaper], *, thumbnail_bytes: bytes = THUMBNAIL_BYTES
) -> Generator[Rig]:
    """A Rig with `wallpapers` in the **Pool**, in the order given, and nothing cached yet."""
    wallhaven = FakeWallhavenClient(wallpapers, thumbnail_bytes=thumbnail_bytes)
    clock = FakeClock(FIXED_NOW)
    with closing(storage.connect(":memory:")) as connection:
        storage.migrate(connection)
        made = Rig(connection, Thumbnails(tmp_path / "cache", wallhaven, clock), wallhaven, clock)
        made.admit(wallpapers)
        yield made


# -- serving -----------------------------------------------------------------------------------------


def test_a_thumbnail_is_fetched_once_and_then_served_off_disk(tmp_path: Path) -> None:
    with rig_over(tmp_path, (wallpaper("one"),)) as rig:
        first = rig.thumbnails.get(rig.connection, "one")
        second = rig.thumbnails.get(rig.connection, "one")

        assert first == second == rig.directory / "one.jpg"
        assert rig.wallhaven.thumbnail_fetches == [wallpaper("one").thumbnail_url]


def test_a_wallpaper_this_database_has_never_seen_has_no_thumbnail(tmp_path: Path) -> None:
    with rig_over(tmp_path, ()) as rig:
        assert rig.thumbnails.get(rig.connection, "stranger") is None
        assert rig.wallhaven.thumbnail_fetches == []


# -- eviction ----------------------------------------------------------------------------------------


def test_an_undecided_wallpaper_that_left_the_pool_loses_its_thumbnail(tmp_path: Path) -> None:
    """Eviction is not "when it leaves the **Pool**": an **Explicit Verdict** keeps its thumbnail because
    **History** renders it. A retired **Ignore** loses its file (ADR 0016); a **Pool** member keeps its file
    until it leaves."""
    wallpapers = tuple(wallpaper(name) for name in ("banned", "liked", "ignored", "member"))
    with rig_over(tmp_path, wallpapers) as rig:
        rig.fill("banned", "liked", "ignored", "member")
        rig.decide(Verdict.BAN, "banned")
        rig.decide(Verdict.LIKE, "liked")
        rig.decide(Verdict.IGNORE, "ignored")
        rig.retire("banned", "liked", "ignored")

        assert rig.evict() == ("ignored",)
        assert rig.cached() == {"banned", "liked", "member"}

        rig.retire("member")

        assert rig.evict() == ("member",)
        assert rig.cached() == {"banned", "liked"}


QUIET = RefillStatus(
    pool_size=0,
    target_size=0,
    running=False,
    last_run_at=None,
    last_error=None,
    last_error_at=None,
    last_strategy=None,
)


def test_a_wallpaper_in_the_live_batch_keeps_its_thumbnail(tmp_path: Path) -> None:
    """A **Filter** change prunes the **Pool** and leaves the live **Batch** alone, so a tile on screen can
    have no **Verdict** and no place in the **Pool**."""
    with rig_over(tmp_path, (wallpaper("shown"), wallpaper("other"))) as rig:
        rig.configure(batch_size=1)
        embeddings = Embeddings(MemoryStore(), StubEmbed(), ModelOnDisk(), fallback=FakeSimilarities())
        live = Batches(embeddings, lambda: QUIET, rig.clock, SeededRandom(1)).next(rig.connection)
        assert isinstance(live, Batch)
        (shown,) = (w.id for w in live.wallpapers)
        rig.fill("shown", "other")
        rig.retire("shown", "other")
        assert batches.showing(rig.connection) == {shown}

        assert rig.evict() == tuple({"shown", "other"} - {shown})

        assert rig.cached() == {shown}


def test_a_file_belonging_to_no_wallpaper_is_swept_up(tmp_path: Path) -> None:
    """The file name is the whole mapping from a file to a **Wallpaper**, so one matching nothing goes."""
    with rig_over(tmp_path, (wallpaper("one"),)) as rig:
        rig.fill("one")
        (rig.directory / "abandoned.jpg").write_bytes(b"left over")

        assert rig.evict() == ("abandoned",)

        assert rig.cached() == {"one"}


def test_an_absent_cache_evicts_nothing(tmp_path: Path) -> None:
    with rig_over(tmp_path, (wallpaper("one"),)) as rig:
        evicted = rig.thumbnails.evict(rig.connection, 0)

        assert (evicted.evicted, evicted.remaining_bytes, evicted.over_cap) == ((), 0, False)


def test_the_size_cap_evicts_the_oldest_pool_thumbnails_first(tmp_path: Path) -> None:
    """Modification times are set by hand, or a real filesystem gives all three the same instant."""
    names = ("oldest", "middle", "newest")
    with rig_over(tmp_path, tuple(wallpaper(n) for n in names), thumbnail_bytes=BIG_THUMBNAIL) as rig:
        rig.fill(*names)
        for age, wallpaper_id in enumerate(names):
            os.utime(rig.directory / f"{wallpaper_id}.jpg", (1_000 + age, 1_000 + age))

        evicted = rig.thumbnails.evict(rig.connection, ONE_MEGABYTE)

        assert evicted.evicted == ("oldest",)
        assert evicted.remaining_bytes == 2 * len(BIG_THUMBNAIL)
        assert evicted.over_cap is False
        assert rig.cached() == {"middle", "newest"}


def test_the_size_cap_never_evicts_an_explicit_verdict_and_says_it_is_over(tmp_path: Path) -> None:
    """**History** renders every **Favourite**, so the cache stays over the cap and `over_cap` says so; the
    one still to be shown is what the cap takes."""
    names = ("one", "two", "three", "member")
    with rig_over(tmp_path, tuple(wallpaper(n) for n in names), thumbnail_bytes=BIG_THUMBNAIL) as rig:
        rig.fill(*names)
        rig.decide(Verdict.FAVOURITE, "one", "two")
        rig.decide(Verdict.BAN, "three")
        rig.retire("one", "two", "three")

        evicted = rig.thumbnails.evict(rig.connection, ONE_MEGABYTE)

        assert evicted.evicted == ("member",)
        assert evicted.remaining_bytes == 3 * len(BIG_THUMBNAIL)
        assert evicted.over_cap is True
        assert rig.cached() == {"one", "two", "three"}


def test_a_withdrawn_verdict_stops_protecting_a_thumbnail(tmp_path: Path) -> None:
    """An **Ignore** from **History** overturns the **Explicit Verdict** and its protection. Getting it
    wrong costs one request, never a broken page: the row fetches it again on demand."""
    with rig_over(tmp_path, (wallpaper("judged"),)) as rig:
        rig.fill("judged")
        rig.decide(Verdict.BAN, "judged")
        rig.retire("judged")
        assert rig.evict() == (), "a Ban protects the thumbnail"

        rig.decide(Verdict.IGNORE, "judged")

        assert rig.evict() == ("judged",)
        assert rig.cached() == set()
        assert rig.thumbnails.get(rig.connection, "judged") is not None, "and it is fetched again on demand"
        assert rig.fetches_of("judged") == 2


# -- the background downloader -----------------------------------------------------------------------

OUT_OF_ORDER = (wallpaper("cc0003"), wallpaper("aa0001"), wallpaper("bb0002"))
"""Admitted to the **Pool** in this order, and so not in `wallpaper_id` order."""

BY_ID = tuple(sorted(OUT_OF_ORDER, key=lambda w: w.id))

DEAD = wallpaper("aa0001")
"""First in `wallpaper_id` order: once a pass has fetched the other two, each later pass is one step."""


def _urls(wallpapers: Sequence[Wallpaper]) -> list[str]:
    return [w.thumbnail_url for w in wallpapers]


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    with rig_over(tmp_path, OUT_OF_ORDER) as made:
        yield made


def test_every_pool_member_is_fetched_in_wallpaper_id_order(rig: Rig) -> None:
    rig.run(3)

    assert rig.wallhaven.thumbnail_fetches == _urls(BY_ID)
    assert sorted(p.name for p in rig.directory.iterdir()) == [f"{w.id}.jpg" for w in BY_ID]


def test_consecutive_fetches_are_at_least_the_gap_apart(rig: Rig) -> None:
    rig.thumbnails.step(rig.connection)

    assert rig.thumbnails.wait() == GAP_SECONDS
    rig.clock.advance(0.1)
    assert rig.thumbnails.wait() == pytest.approx(GAP_SECONDS - 0.1)  # pyright: ignore[reportUnknownMemberType]


def test_a_thumbnail_already_in_the_cache_is_never_fetched_again(rig: Rig) -> None:
    rig.fill("aa0001")

    rig.run(3)

    assert rig.fetches_of("aa0001") == 1
    assert len(rig.wallhaven.thumbnail_fetches) == 3


def test_a_thumbnail_the_tile_route_fetched_mid_pass_is_not_fetched_again(rig: Rig) -> None:
    """The pass was listed before the tile rendered, so each fetch rechecks that the file is still
    missing."""
    rig.thumbnails.step(rig.connection)
    rig.fill("bb0002")

    rig.run(2)

    assert rig.fetches_of("bb0002") == 1


def test_a_wallpaper_that_left_the_pool_mid_pass_is_not_fetched(rig: Rig) -> None:
    """Otherwise it is fetched only for a submission's eviction to delete again."""
    rig.thumbnails.step(rig.connection)
    rig.retire("bb0002")

    rig.run(2)

    assert rig.wallhaven.thumbnail_fetches == _urls((BY_ID[0], BY_ID[2]))


def test_with_nothing_missing_the_downloader_looks_again_later(rig: Rig) -> None:
    """Not zero, which would spin on a directory listing."""
    rig.run(3)
    rig.clock.advance(GAP_SECONDS)

    rig.thumbnails.step(rig.connection)

    assert rig.thumbnails.wait() == IDLE_RECHECK_SECONDS


def test_a_newcomer_to_the_pool_is_picked_up_on_the_next_look(rig: Rig) -> None:
    rig.run(3)
    rig.admit((wallpaper("dd0004"),))

    rig.run(1)

    assert rig.wallhaven.thumbnail_fetches[-1] == wallpaper("dd0004").thumbnail_url


def test_a_refused_thumbnail_is_skipped_and_fetched_on_a_later_pass(rig: Rig) -> None:
    """A refusal is about one file, so the next member follows after the ordinary gap; the refused one is
    asked for once a pass, never four times a second."""
    refused = DEAD.thumbnail_url
    rig.wallhaven.failing_thumbnails[refused] = ThumbnailUnavailable(404)

    rig.thumbnails.step(rig.connection)
    assert rig.thumbnails.wait() == GAP_SECONDS
    rig.run(2)
    assert rig.wallhaven.thumbnail_fetches == _urls(BY_ID)
    assert rig.thumbnails.wait() == IDLE_RECHECK_SECONDS

    del rig.wallhaven.failing_thumbnails[refused]
    rig.run(1)

    assert rig.wallhaven.thumbnail_fetches[-1] == refused
    assert (rig.directory / "aa0001.jpg").exists()


@pytest.mark.parametrize(
    ("failure", "wait"),
    [
        pytest.param(RateLimited(None), BACKOFF_SECONDS, id="429"),
        pytest.param(RateLimited(5.0), BACKOFF_SECONDS, id="429 asking for less"),
        pytest.param(RateLimited(90.0), 90.0, id="429 asking for more"),
        pytest.param(WallhavenUnreachable("connection refused"), BACKOFF_SECONDS, id="connection error"),
    ],
)
def test_a_429_or_a_connection_error_backs_off(rig: Rig, failure: Exception, wait: float) -> None:
    """About the host, not the file, so the next fetch would fail the same way. A shorter `Retry-After`
    does not shorten the back-off: that is the downloader's own manners towards a host with no published
    limit."""
    rig.wallhaven.failing_thumbnails[DEAD.thumbnail_url] = failure

    rig.thumbnails.step(rig.connection)

    assert rig.thumbnails.wait() == wait


def test_a_cache_at_its_cap_fetches_nothing_until_it_is_under_it(rig: Rig) -> None:
    """Without it a cap below what the **Pool** needs would churn against the size-cap pass."""
    rig.configure(thumbnail_cache_max_mb=1)
    rig.directory.mkdir(parents=True)
    filler = rig.directory / "zz9999.jpg"
    filler.write_bytes(b"\0" * ONE_MEGABYTE)

    rig.run(3)

    assert rig.wallhaven.thumbnail_fetches == []
    assert rig.thumbnails.wait() == IDLE_RECHECK_SECONDS

    filler.unlink()
    rig.run(1)

    assert rig.wallhaven.thumbnail_fetches == [wallpaper("aa0001").thumbnail_url]


def test_a_thumbnail_that_cannot_be_written_does_not_stop_the_downloader(rig: Rig) -> None:
    """`step` never raises: a dead downloader would leave the **Pool** unembedded with nothing saying why."""
    rig.directory.write_bytes(b"a file where the cache directory should be")

    rig.run(3)

    assert len(rig.wallhaven.thumbnail_fetches) == 3


def _first_pass_refusing_the_dead_one(rig: Rig) -> None:
    rig.wallhaven.failing_thumbnails[DEAD.thumbnail_url] = ThumbnailUnavailable(404)
    rig.run(len(OUT_OF_ORDER))


def test_two_refusals_in_a_row_are_never_asked_for_again_in_this_process(rig: Rig) -> None:
    """One refusal is retried on the next pass; two in a row are given up on, or a dead file is asked for
    every thirty seconds for as long as wallpapi runs. In memory only: a restart asks once more."""
    _first_pass_refusing_the_dead_one(rig)
    assert rig.thumbnails.given_up() == frozenset()

    rig.run(1)
    assert rig.fetches_of(DEAD.id) == 2

    rig.run(10)

    assert rig.fetches_of(DEAD.id) == 2
    assert rig.thumbnails.given_up() == {DEAD.id}
    assert rig.thumbnails.wait() == IDLE_RECHECK_SECONDS


def test_a_429_between_two_refusals_does_not_count_as_one(rig: Rig) -> None:
    """A 429 says the host is busy, not that the file is gone: it neither counts towards giving up nor
    wipes the first refusal out."""
    _first_pass_refusing_the_dead_one(rig)
    rig.wallhaven.failing_thumbnails[DEAD.thumbnail_url] = RateLimited(None)
    rig.run(1)
    rig.wallhaven.failing_thumbnails[DEAD.thumbnail_url] = ThumbnailUnavailable(404)

    rig.run(10)

    assert rig.fetches_of(DEAD.id) == 3


def test_429s_and_failed_connections_never_add_up_to_giving_up(rig: Rig) -> None:
    rig.wallhaven.failing_thumbnails[DEAD.thumbnail_url] = RateLimited(None)
    rig.run(len(OUT_OF_ORDER))
    rig.wallhaven.failing_thumbnails[DEAD.thumbnail_url] = WallhavenUnreachable("connection refused")
    rig.run(1)
    rig.wallhaven.failing_thumbnails[DEAD.thumbnail_url] = ThumbnailUnavailable(404)
    rig.run(1)

    rig.run(1)

    assert rig.fetches_of(DEAD.id) == 4


@pytest.mark.parametrize(
    ("last", "now", "wait"),
    [
        pytest.param(None, 100.0, 0.0, id="nothing fetched yet"),
        pytest.param(10.0, 10.0, GAP_SECONDS, id="straight after a fetch the whole gap is owed"),
        pytest.param(10.0, 10.1, GAP_SECONDS - 0.1, id="part of the gap gone is not owed again"),
        pytest.param(10.0, 10.0 + GAP_SECONDS, 0.0, id="the gap has passed"),
        pytest.param(10.0, 99.0, 0.0, id="long after"),
    ],
)
def test_the_thumbnail_gap(last: float | None, now: float, wait: float) -> None:
    """One thumbnail at a time, a fixed gap apart."""
    assert gap_needed(last, now=now) == pytest.approx(wait)  # pyright: ignore[reportUnknownMemberType]


# -- the Core service: the tail of a submission, and the page's notice ---------------------------------


def test_a_submission_evicts_what_it_retired_without_a_verdict(db_path: Path) -> None:
    """Eviction runs at the tail of a submission, after the **Decision log** commits."""
    harness = make_harness(db_path, catalogue=(wallpaper("liked"), wallpaper("ignored")))
    harness.core.update_settings(batch_size=2)
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    for wallpaper_id in ("liked", "ignored"):
        harness.core.get_thumbnail(wallpaper_id)
    harness.core.set_draft_verdict(batch.id, "liked", Verdict.LIKE)

    harness.core.submit_batch(batch.id)

    assert {p.stem for p in harness.core.thumbnails.directory.iterdir()} == {"liked"}


def test_the_notice_leaves_out_what_the_downloader_gave_up_on(db_path: Path) -> None:
    """Coverage is of what can be embedded, or a dead thumbnail would hold the line on the page for ever."""
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)
    harness.wallhaven.failing_thumbnails[DEAD.thumbnail_url] = ThumbnailUnavailable(404)
    thumbnails = harness.core.thumbnails
    with raw_connection(db_path) as connection:
        for _ in range(len(OUT_OF_ORDER) + 1):
            harness.clock.advance(thumbnails.wait())
            thumbnails.step(connection)
    assert thumbnails.given_up() == {DEAD.id}

    harness.core.similarity_step(threading.Event())

    assert set(harness.embed.seen) == {"cc0003", "bb0002"}
    assert harness.core.similarity_notice() is None
