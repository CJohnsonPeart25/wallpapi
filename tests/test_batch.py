"""Building a Batch. Issue #2 acceptance criteria 1, 4 and 5."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from tests.conftest import FIXED_NOW, Harness, make_harness
from tests.fakes import catalogue_of, wallpaper
from wallpapi.core import Batch, BatchUnavailable


def test_batch_has_the_configured_size(harness: Harness) -> None:
    """batch_size is seeded at 8 by the first migration, so the walking skeleton shows 8.

    The setting exists from #2 even though the settings page is #4, so #4 adds a page rather than a code
    path. The fake's catalogue holds 24 — a Wallhaven search page — and the Batch takes 8 of them.
    """
    batch = harness.core.get_next_batch()

    assert isinstance(batch, Batch)
    assert len(batch.wallpapers) == 8
    assert batch.size == 8
    assert harness.core.get_setting("batch_size") == "8"


def test_batch_never_contains_the_same_wallpaper_twice(db_path: Path) -> None:
    """Acceptance criterion: a Batch never contains the same Wallpaper twice.

    The catalogue deliberately repeats each Wallhaven ID three times, which is the shape a careless
    implementation would trip on: sampling 8 rows rather than 8 distinct Wallpapers.
    """
    repeated = tuple(wallpaper(f"dup{n:02d}") for n in range(10) for _ in range(3))
    harness = make_harness(db_path, catalogue=repeated)

    batch = harness.core.get_next_batch()

    assert isinstance(batch, Batch)
    ids = [w.id for w in batch.wallpapers]
    assert len(ids) == 8
    assert len(set(ids)) == 8


def test_batch_carries_its_id_size_timestamp_and_wallpapers(harness: Harness) -> None:
    """Acceptance criterion: each Batch is stored with its ID, timestamp, size and the Wallpapers shown.

    Asserted on what the Core service returns rather than by reading the database, so the test survives a
    change of storage. Persistence itself is covered by test_decision_log_survives_a_restart.
    """
    batch = harness.core.get_next_batch()

    assert isinstance(batch, Batch)
    assert batch.id
    assert batch.size == len(batch.wallpapers)
    assert batch.created_at == FIXED_NOW
    assert all(w.thumbnail_url for w in batch.wallpapers)


def test_batch_timestamp_comes_from_the_clock_as_utc(harness: Harness) -> None:
    """Invariant: timestamps are UTC and come from the injected clock, never from datetime.now().

    A naive datetime, or one built from the machine clock, fails here. The isoformat round-trip is the
    property that matters downstream, because the Decision log stores ISO 8601 strings.
    """
    harness.clock.advance(3600)
    expected = FIXED_NOW + dt.timedelta(hours=1)

    batch = harness.core.get_next_batch()

    assert isinstance(batch, Batch)
    assert batch.created_at == expected
    assert batch.created_at.tzinfo is dt.UTC
    assert dt.datetime.fromisoformat(batch.created_at.isoformat()) == expected


def test_wallhaven_is_searched_with_random_sorting_and_sfw_purity(harness: Harness) -> None:
    """Acceptance criterion: the Batch comes from a Wallhaven random, SFW search.

    This asserts against the fake's recorded call rather than an outcome, which is normally an
    anti-pattern. It is justified here because the Wallhaven client is a pre-agreed injected seam rather
    than an internal collaborator, and "random, SFW" has no other observable at #2. Filters — atleast,
    ratios, minimum favourites — are #6 and must not appear yet.
    """
    harness.core.get_next_batch()

    assert harness.wallhaven.searches == [{"sorting": "random", "purity": "100", "page": 1}]


def test_batch_is_unavailable_when_wallhaven_returns_nothing(db_path: Path) -> None:
    """Returning a result beats raising, so the UI has one branch rather than an exception path.

    Unreachable at #2 in practice — a live random search always returns something — but the branch and its
    test exist from day one so that #6 adds a reason code instead of a new control path.
    """
    harness = make_harness(db_path, catalogue=())

    result = harness.core.get_next_batch()

    assert isinstance(result, BatchUnavailable)
    assert result.reason


def test_two_batches_in_a_row_each_come_from_their_own_search(harness: Harness) -> None:
    """At #2 there is no Pool, so every Batch is an independent live search.

    Consecutive Batches may therefore repeat Wallpapers. That is a property of #2, not a defect: the
    no-repeats-across-Batches behaviour arrives with the Pool at #6.
    """
    first = harness.core.get_next_batch()
    second = harness.core.get_next_batch()

    assert isinstance(first, Batch)
    assert isinstance(second, Batch)
    assert first.id != second.id
    assert len(harness.wallhaven.searches) == 2


def test_a_batch_of_a_different_size_is_honoured(db_path: Path) -> None:
    """Changing batch_size changes the next Batch. The settings page is #4; the setting itself is #2."""
    harness = make_harness(db_path, catalogue=catalogue_of(24))
    harness.core.set_setting("batch_size", "2")

    batch = harness.core.get_next_batch()

    assert isinstance(batch, Batch)
    assert len(batch.wallpapers) == 2
    assert batch.size == 2
