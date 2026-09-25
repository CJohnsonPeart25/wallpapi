"""**Banned Wallpapers** never come back, and what **Batch** building does when that leaves it short.

The walk across pages is temporary. Once the **Pool** lands at #6, **Batch** building draws from locally
stored **Wallpapers** a background thread has already topped up and no **API call** happens on the page load
path at all — at which point these tests go with it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import make_harness
from tests.fakes import WallhavenUnreachable, catalogue_of
from wallpapi.core import Batch, BatchUnavailable
from wallpapi.model import Verdict


def test_a_banned_wallpaper_never_appears_in_a_later_batch_including_after_a_restart(
    db_path: Path,
) -> None:
    """The acceptance criterion, and the reason **Ban** is the strongest negative **Verdict**.

    The restart half matters because the exclusion has to come from the **Decision log** rather than from
    anything the process was holding: a second Core service over the same file must exclude it too.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(24))
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    banned = first.wallpapers[0].id
    harness.core.set_draft_verdict(first.id, banned, Verdict.BAN)

    following = harness.core.submit_batch(first.id)

    assert isinstance(following, Batch)
    assert banned not in {w.id for w in following.wallpapers}

    restarted = make_harness(db_path, catalogue=catalogue_of(24))
    live = restarted.core.get_next_batch()
    assert isinstance(live, Batch)
    after_restart = restarted.core.submit_batch(live.id)
    assert isinstance(after_restart, Batch)
    assert banned not in {w.id for w in after_restart.wallpapers}


def test_batch_building_walks_to_the_next_page_when_bans_leave_it_short(db_path: Path) -> None:
    """Acceptance criterion: **Bans** that leave the first page short send the walk to the next page.

    The fake serves 8 per page and holds 16, so the first **Batch** is exactly one page. Banning three of
    them leaves five candidates on that page next time — short of the batch size of eight — and the walk
    has to find the other three somewhere.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(16), page_size=8, search_seed="seed-1")
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    banned = {w.id for w in first.wallpapers[:3]}
    for wallpaper_id in banned:
        harness.core.set_draft_verdict(first.id, wallpaper_id, Verdict.BAN)

    following = harness.core.submit_batch(first.id)

    assert isinstance(following, Batch)
    assert len(following.wallpapers) == 8
    assert banned.isdisjoint({w.id for w in following.wallpapers})
    assert [search["page"] for search in harness.wallhaven.searches] == [1, 1, 2]


def test_the_walk_carries_the_seed_wallhaven_returned(db_path: Path) -> None:
    """Acceptance criterion: the walk carries `meta.seed`, so it does not repeat itself.

    Wallhaven's random sorting reshuffles on every call unless the seed is passed back, which would make
    page 2 of a walk as likely to hand back page 1's **Wallpapers** as anything new. The first call carries
    no seed — it is the one that asks for it.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(16), page_size=8, search_seed="seed-1")
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    for wallpaper in first.wallpapers[:3]:
        harness.core.set_draft_verdict(first.id, wallpaper.id, Verdict.BAN)

    harness.core.submit_batch(first.id)

    walk = harness.wallhaven.searches[1:]
    assert walk[0]["seed"] is None
    assert walk[1] == {"sorting": "random", "purity": "100", "page": 2, "seed": "seed-1"}


def test_the_walk_stops_at_four_api_calls_and_ships_the_smaller_batch(db_path: Path) -> None:
    """Acceptance criteria: the walk is capped at four **API calls**, and a short **Batch** still ships.

    One **Wallpaper** per page means the walk can never fill a **Batch** of eight, so the only thing that
    stops it is the cap. The cap bounds how long a page load blocks — four calls at the client's ten second
    timeout is already the worst case worth making somebody sit through — and it is not a rate limiter:
    spending the rest of the 45 per minute is the background refill's job at #6.

    Four **Wallpapers** is a smaller **Batch**, which is an acceptable outcome rather than a failure.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(20), page_size=1)

    batch = harness.core.get_next_batch()

    assert isinstance(batch, Batch)
    assert len(harness.wallhaven.searches) == 4
    assert len(batch.wallpapers) == 4
    assert batch.size == 4


def test_only_a_walk_that_finds_nothing_at_all_is_unavailable(db_path: Path) -> None:
    """Acceptance criterion: unavailable is for finding nothing, not for finding too little.

    Both **Wallpapers** the fake holds are **Banned**, so the walk exhausts the catalogue and comes back
    empty-handed. That is the one case that falls back to the existing "no results" reason.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(2), page_size=1)
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    for wallpaper in first.wallpapers:
        harness.core.set_draft_verdict(first.id, wallpaper.id, Verdict.BAN)

    result = harness.core.submit_batch(first.id)

    assert isinstance(result, BatchUnavailable)
    assert result.reason is BatchUnavailable.Reason.NO_RESULTS


def test_a_failure_part_way_through_the_walk_ships_what_the_earlier_pages_gave(db_path: Path) -> None:
    """Acceptance criterion: a mid-walk failure ends the walk rather than losing the **Batch**.

    The walk adds call sites that can throw where there used to be one. Two pages have already come back
    by the time the third fails, and throwing away two thirds of a built **Batch** because the last call
    timed out would be the wrong trade.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(20), page_size=1, fail_from_call=3)

    batch = harness.core.get_next_batch()

    assert isinstance(batch, Batch)
    assert len(harness.wallhaven.searches) == 3
    assert len(batch.wallpapers) == 2


def test_a_failure_on_the_very_first_call_is_left_exactly_as_it_was(db_path: Path) -> None:
    """Deliberately not handled here. Transport failure on the first call is issue #15.

    This test exists to pin the boundary: the mid-walk rescue above must not quietly widen into a general
    "Wallhaven is down" result, because that would silently close #15 with a **Batch unavailable** page
    nobody designed.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(20), page_size=1, fail_from_call=1)

    with pytest.raises(WallhavenUnreachable):
        harness.core.get_next_batch()
