"""Minting a **Batch**, the **Zones** it records, and deciding once (ADR 0016): the **Pool** holds no
**Wallpaper** the **Decision log** mentions, kept at submission, at admission and (in `test_migrations.py`)
at migration.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from tests.conftest import FIXED_NOW, Harness, make_harness, write_legacy_clearance
from tests.fakes import catalogue_of, wallpaper
from wallpapi.core import IDLE_RECHECK_SECONDS, Batch
from wallpapi.model import Verdict, Zone


def live(harness: Harness) -> Batch:
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    return batch


@pytest.mark.parametrize("size", [None, 2], ids=["the seeded size", "a size the user set"])
def test_a_batch_has_the_configured_size(db_path: Path, size: int | None) -> None:
    harness = make_harness(db_path)
    if size is not None:
        harness.core.update_settings(batch_size=size)

    batch = live(harness)

    assert len(batch.wallpapers) == batch.size == harness.core.get_settings().batch_size


def test_a_batch_carries_its_id_and_a_utc_timestamp_from_the_clock(harness: Harness) -> None:
    """Never `datetime.now()` (invariant 5): a naive or machine-clock time fails here, and the ISO 8601
    round trip is what the **Decision log** stores."""
    harness.clock.advance(3600)
    expected = FIXED_NOW + dt.timedelta(hours=1)

    batch = live(harness)

    assert batch.id
    assert batch.created_at == expected
    assert batch.created_at.tzinfo is dt.UTC
    assert dt.datetime.fromisoformat(batch.created_at.isoformat()) == expected
    assert all(w.thumbnail_url for w in batch.wallpapers)


def test_asking_again_returns_the_live_batch_rather_than_minting_another(harness: Harness) -> None:
    """A refresh is not a decision: minting per page load would reroll what the user was looking at. Read
    back from storage, so the **Zones** it was minted with come back too. The one search is the harness
    priming the **Pool**."""
    first = live(harness)
    second = live(harness)

    assert second.id == first.id
    assert [w.id for w in second.wallpapers] == [w.id for w in first.wallpapers]
    assert second.zones == first.zones
    assert len(harness.wallhaven.searches) == 1


def test_every_wallpaper_in_a_batch_comes_with_the_zone_it_was_drawn_from(harness: Harness) -> None:
    batch = live(harness)

    assert {w.id for w in batch.wallpapers} == set(batch.zones)
    assert set(batch.zones.values()) == {Zone.UNKNOWN}


def test_the_zones_recorded_are_the_ones_the_pool_was_classified_into(harness: Harness) -> None:
    """Recorded at mint time, so the label is what the draw used rather than a second opinion. Every
    **Wallpaper** is made to resemble the one **Favourite**, well inside the radius."""
    first = live(harness)
    loved = first.wallpapers[0].id
    harness.similarity.similarity_by_pair.update({(f"wp{n:04d}", loved): 0.95 for n in range(24)})
    harness.core.set_draft_verdict(first.id, loved, Verdict.FAVOURITE)

    following = harness.core.submit_batch(first.id)

    assert isinstance(following, Batch)
    classified = {scored.wallpaper.id: scored.zone for scored in harness.core.classify_pool()}
    assert following.zones == {w.id: classified[w.id] for w in following.wallpapers}
    assert set(following.zones.values()) == {Zone.BANGER}


# -- deciding once -----------------------------------------------------------------------------------


def test_submitting_retires_every_shown_wallpaper_and_no_edit_brings_it_back(db_path: Path) -> None:
    """Explicit or not, everything shown leaves, and the next **Batch** is drawn from what nobody has seen.
    **History** changes the **Decision log**, not what may be shown."""
    harness = make_harness(db_path, catalogue=catalogue_of(12))
    first = live(harness)
    shown = {w.id for w in first.wallpapers}
    liked, banned = sorted(shown)[:2]
    harness.core.set_draft_verdict(first.id, liked, Verdict.LIKE)
    harness.core.set_draft_verdict(first.id, banned, Verdict.BAN)

    following = harness.core.submit_batch(first.id)

    assert harness.core.refill_status().pool_size == 4
    assert isinstance(following, Batch)
    assert not {w.id for w in following.wallpapers} & shown
    assert harness.core.edit_verdict(liked, Verdict.FAVOURITE) is None
    assert harness.core.refill_status().pool_size == 4


def test_a_refill_that_meets_a_decided_wallpaper_again_does_not_readmit_it(db_path: Path) -> None:
    """The catalogue is the eight the **Batch** showed, so the walk that comes round again finds nothing
    else, and a refused **Wallpaper** does not count towards the **Pool**."""
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    harness.core.submit_batch(live(harness).id)
    searched_before = len(harness.wallhaven.searches)

    harness.fill_pool(3)

    assert any(search["page"] == 1 for search in harness.wallhaven.searches[searched_before:])
    assert harness.core.refill_status().pool_size == 0


def test_a_wallpaper_whose_only_entry_is_a_legacy_clearance_is_not_readmitted(db_path: Path) -> None:
    """A **Clearance** resolves to nothing but was a decision, so admission asks the log, not resolution.
    Pruned by a **Filter**, cleared, and the **Filter** put back so the walk meets it again."""
    harness = make_harness(db_path, catalogue=(wallpaper("cleared", width=2560, height=1440),))
    harness.core.update_settings(min_width=3840)
    assert harness.core.refill_status().pool_size == 0
    write_legacy_clearance(db_path, "cleared")
    assert harness.core.resolve_verdicts(["cleared"])["cleared"].verdict is None
    harness.core.update_settings(min_width=2560)

    harness.fill_pool(2)

    assert harness.core.refill_status().pool_size == 0


def test_a_retired_wallpaper_still_shapes_the_scores_of_the_pool(db_path: Path) -> None:
    """The decided set is independent of **Pool** membership (ADR 0007): eight retired **Bans** still make
    the one unseen **Wallpaper** like them a **Dud**."""
    ids = [w.id for w in catalogue_of(9)]
    alike = {(a, b): 0.95 for a in ids for b in ids if a != b}
    harness = make_harness(db_path, catalogue=catalogue_of(9), similarities=alike)
    first = live(harness)
    assert harness.core.set_all_draft_verdicts(first.id, Verdict.BAN) is None

    harness.core.submit_batch(first.id)

    (remaining,) = harness.core.classify_pool()
    assert remaining.wallpaper.id not in {w.id for w in first.wallpapers}
    assert remaining.zone is Zone.DUD
    assert remaining.score < 0


def test_lowering_the_target_below_the_pool_trims_nothing(harness: Harness) -> None:
    """A **Pool** above its target drains by submission rather than being cut."""
    assert harness.core.refill_status().pool_size == 24

    harness.core.update_settings(pool_target_size=5)

    status = harness.core.refill_status()
    assert status.pool_size == 24
    assert status.at_target
    assert harness.core.refill_wait() == IDLE_RECHECK_SECONDS


def test_a_submission_takes_a_pool_at_target_below_it_and_wakes_the_refill(harness: Harness) -> None:
    """The bug ADR 0016 was written for: at target nothing ever left the **Pool**, so the refill idled
    for good."""
    harness.core.update_settings(pool_target_size=24)
    assert harness.core.refill_wait() == IDLE_RECHECK_SECONDS

    harness.core.submit_batch(live(harness).id)

    assert not harness.core.refill_status().at_target
    assert harness.core.refill_wait() != IDLE_RECHECK_SECONDS
