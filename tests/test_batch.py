"""A **Batch** and the **Pool** through the Core service: deciding once (ADR 0016), so the **Pool** holds no
**Wallpaper** the **Decision log** mentions, kept at submission, at admission and (in `test_migrations.py`)
at migration; and what a submission leaves behind. The draw, the **Draft Batch** and submit itself are
`test_batches.py`'s.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import Harness, make_harness, write_legacy_clearance
from tests.fakes import catalogue_of, wallpaper
from wallpapi.core import Batch
from wallpapi.model import Verdict, Zone
from wallpapi.pool import IDLE_RECHECK_SECONDS


def live(harness: Harness) -> Batch:
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    return batch


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

    harness.core.submit_batch(first.id)

    following = live(harness)
    assert harness.core.refill.status().pool_size == 4
    assert not {w.id for w in following.wallpapers} & shown
    assert harness.core.edit_verdict(liked, Verdict.FAVOURITE) is None
    assert harness.core.refill.status().pool_size == 4


def test_a_refill_that_meets_a_decided_wallpaper_again_does_not_readmit_it(db_path: Path) -> None:
    """The catalogue is the eight the **Batch** showed, so the walk that comes round again finds nothing
    else, and a refused **Wallpaper** does not count towards the **Pool**."""
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    harness.core.submit_batch(live(harness).id)
    searched_before = len(harness.wallhaven.searches)

    harness.fill_pool(3)

    assert any(search["page"] == 1 for search in harness.wallhaven.searches[searched_before:])
    assert harness.core.refill.status().pool_size == 0


def test_a_wallpaper_whose_only_entry_is_a_legacy_clearance_is_not_readmitted(db_path: Path) -> None:
    """A **Clearance** resolves to nothing but was a decision, so admission asks the log, not resolution.
    Pruned by a **Filter**, cleared, and the **Filter** put back so the walk meets it again."""
    harness = make_harness(db_path, catalogue=(wallpaper("cleared", width=2560, height=1440),))
    harness.core.update_settings(min_width=3840)
    assert harness.core.refill.status().pool_size == 0
    write_legacy_clearance(db_path, "cleared")
    assert harness.core.resolve_verdicts(["cleared"])["cleared"].verdict is None
    harness.core.update_settings(min_width=2560)

    harness.fill_pool(2)

    assert harness.core.refill.status().pool_size == 0


def test_a_retired_wallpaper_still_shapes_the_scores_of_the_pool(db_path: Path) -> None:
    """The decided set is independent of **Pool** membership (ADR 0007): eight retired **Bans** still make
    the one unseen **Wallpaper** like them a **Dud**."""
    ids = [w.id for w in catalogue_of(9)]
    alike = {(a, b): 0.95 for a in ids for b in ids if a != b}
    harness = make_harness(db_path, catalogue=catalogue_of(9), similarities=alike)
    first = live(harness)
    harness.core.set_all_draft_verdicts(first.id, Verdict.BAN)

    harness.core.submit_batch(first.id)

    (remaining,) = harness.core.classify_pool()
    assert remaining.wallpaper.id not in {w.id for w in first.wallpapers}
    assert remaining.zone is Zone.DUD
    assert remaining.score < 0


def test_lowering_the_target_below_the_pool_trims_nothing(harness: Harness) -> None:
    """A **Pool** above its target drains by submission rather than being cut."""
    assert harness.core.refill.status().pool_size == 24

    harness.core.update_settings(pool_target_size=5)

    status = harness.core.refill.status()
    assert status.pool_size == 24
    assert status.at_target
    assert harness.core.refill.wait() == IDLE_RECHECK_SECONDS


def test_a_submission_takes_a_pool_at_target_below_it_and_wakes_the_refill(harness: Harness) -> None:
    """The bug ADR 0016 was written for: at target nothing ever left the **Pool**, so the refill idled
    for good."""
    harness.core.update_settings(pool_target_size=24)
    assert harness.core.refill.wait() == IDLE_RECHECK_SECONDS

    harness.core.submit_batch(live(harness).id)

    assert not harness.core.refill.status().at_target
    assert harness.core.refill.wait() != IDLE_RECHECK_SECONDS


def test_a_batch_is_drawn_from_the_pool_without_calling_wallhaven(harness: Harness) -> None:
    """The page load path makes no **API call**, so there is no network call on it to fail with a 500."""
    calls_before = len(harness.wallhaven.searches)

    batch = live(harness)

    assert len(batch.wallpapers) == 8
    assert len(harness.wallhaven.searches) == calls_before


def test_changing_the_filters_prunes_the_pool_in_the_same_save(db_path: Path) -> None:
    """What each **Filter** excludes is `test_pool.py`'s; this is that the settings save prunes at all."""
    harness = make_harness(
        db_path,
        catalogue=(wallpaper("modest", width=2560, height=1440), wallpaper("huge", width=3840, height=2160)),
    )

    harness.core.update_settings(min_width=3840, min_height=2160)

    assert [w.id for w in live(harness).wallpapers] == ["huge"]


def test_pruning_leaves_the_live_batch_and_its_drafts_alone(db_path: Path) -> None:
    """A **Batch** holds its own rows, so a **Pool** row going cannot take a tile or its mark with it."""
    harness = make_harness(db_path, catalogue=catalogue_of(10))
    first = live(harness)
    marked = first.wallpapers[0].id
    harness.core.set_draft_verdict(first.id, marked, Verdict.FAVOURITE)

    harness.core.update_settings(min_width=3840, min_height=2160, allowed_ratios="1x1")

    still_live = live(harness)
    assert still_live.id == first.id
    assert [w.id for w in still_live.wallpapers] == [w.id for w in first.wallpapers]
    assert still_live.drafts[marked] is Verdict.FAVOURITE


def test_a_second_ignore_is_appended_not_folded_into_the_first(db_path: Path) -> None:
    """Append-only. **History** is the only way to decide a submitted **Wallpaper** again."""
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    first = live(harness)
    harness.core.submit_batch(first.id)

    for w in first.wallpapers:
        assert harness.core.edit_verdict(w.id, Verdict.IGNORE) is None

    history = harness.core.list_history()
    assert len(history) == 16
    for w in first.wallpapers:
        assert [e.entry for e in history if e.wallpaper_id == w.id] == [Verdict.IGNORE, Verdict.IGNORE]


def test_the_decision_log_survives_a_restart(db_path: Path) -> None:
    """A second Core service over the same file, which also proves the migrations are idempotent."""
    first_run = make_harness(db_path)
    first_run.core.submit_batch(live(first_run).id)
    recorded = first_run.core.list_history()

    restored = make_harness(db_path).core.list_history()

    assert len(restored) == 8
    assert [(e.wallpaper_id, e.entry, e.recorded_at) for e in restored] == [
        (e.wallpaper_id, e.entry, e.recorded_at) for e in recorded
    ]
