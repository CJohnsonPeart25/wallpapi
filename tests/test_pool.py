"""The **Pool**: what gets into it, what a **Batch** draws from it, and what a **Filter** change does.

Issue #6. Everything enters through the Core service with the fake Wallhaven client and the fake clock.
The refill is driven a step at a time by hand — no thread, no sleeping, nothing racing.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from tests.conftest import Harness, make_harness
from tests.fakes import catalogue_of, wallpaper
from wallpapi.core import Batch, BatchUnavailable
from wallpapi.model import Verdict


def test_a_batch_is_drawn_from_the_pool_without_calling_wallhaven(harness: Harness) -> None:
    """The acceptance criterion this whole ticket turns on, and the half of #15 that is a design change.

    Once **Batches** come from the **Pool** the page load path makes no **API call** at all, so there is no
    network call left on it to fail with a 500. The refill's one call is already recorded by the harness;
    loading a page must not add to it.
    """
    calls_before = len(harness.wallhaven.searches)

    batch = harness.core.get_next_batch()

    assert isinstance(batch, Batch)
    assert len(batch.wallpapers) == 8
    assert len(harness.wallhaven.searches) == calls_before


def test_the_refill_searches_with_every_filter_and_the_fixed_masks(harness: Harness) -> None:
    """Acceptance criteria: the **Filters** go into the query, purity is SFW and every category is on.

    Asserted against the fake's recorded call rather than an outcome, which is normally an anti-pattern and
    is justified here for the reason #3's version was: the Wallhaven client is a pre-agreed injected seam,
    and "searched with these parameters" has no other observable. The minimum **Favourites** is absent on
    purpose — Wallhaven has no parameter for it, so it is applied locally. So is `q`: a random search asks
    for nothing in particular, and the like: strategy (#13) is what fills it in.
    """
    assert harness.wallhaven.searches[0] == {
        "sorting": "random",
        "purity": "100",
        "categories": "111",
        "query": None,
        "page": 1,
        "seed": None,
        "atleast": "2560x1440",
        "ratios": "16x9,16x10,21x9",
    }


def test_the_search_follows_the_filters_when_they_change(harness: Harness) -> None:
    """The query is built from the settings at the time of the call, not at start-up."""
    harness.core.update_settings(min_width=1920, min_height=1080, allowed_ratios="21x9")

    harness.core.refill_step()

    latest = harness.wallhaven.searches[-1]
    assert latest["atleast"] == "1920x1080"
    assert latest["ratios"] == "21x9"


def test_wallpapers_below_the_minimum_resolution_never_enter_the_pool(db_path: Path) -> None:
    """Acceptance criterion: a **Wallpaper** failing any **Filter** never enters the **Pool**.

    Checked locally even though `atleast` was in the query. The API is trusted but not relied upon: a
    parameter silently ignored must not be able to put a 1280x720 **Wallpaper** in front of anybody.
    """
    harness = make_harness(
        db_path,
        catalogue=(
            wallpaper("toosmall", width=1280, height=720),
            wallpaper("shortside", width=3840, height=1000),
            wallpaper("bigenough"),
        ),
    )

    batch = harness.core.get_next_batch()

    assert isinstance(batch, Batch)
    assert [w.id for w in batch.wallpapers] == ["bigenough"]


def test_wallpapers_of_the_wrong_shape_never_enter_the_pool(db_path: Path) -> None:
    """Acceptance criterion: the allowed ratios **Filter** excludes outright.

    4:3 at 2800x2100 passes the resolution **Filter** and fails the shape one, so this cannot pass by
    accident. The ultrawide is the opposite case: 3440x1440 is 2.39 rather than 21x9's 2.33, and Wallhaven
    serves it under `21x9`, so the local check has to admit it or it would prune what the API correctly
    returned.
    """
    harness = make_harness(
        db_path,
        catalogue=(
            wallpaper("fourbythree", width=2800, height=2100),
            wallpaper("ultrawide", width=3440, height=1440),
        ),
    )

    batch = harness.core.get_next_batch()

    assert isinstance(batch, Batch)
    assert [w.id for w in batch.wallpapers] == ["ultrawide"]


def test_wallpapers_below_the_minimum_favourites_never_enter_the_pool(db_path: Path) -> None:
    """Acceptance criterion: the minimum **Favourites** **Filter**, which is the local one.

    Wallhaven's search has no parameter for it, so this one is not even nominally the API's job.
    """
    harness = make_harness(
        db_path,
        catalogue=(wallpaper("unloved", favourites=0), wallpaper("popular", favourites=10)),
    )

    batch = harness.core.get_next_batch()

    assert isinstance(batch, Batch)
    assert [w.id for w in batch.wallpapers] == ["popular"]


def test_a_wallpaper_that_is_not_sfw_never_enters_the_pool(db_path: Path) -> None:
    """Acceptance criterion: purity is always SFW.

    The query fixes it, and this fixes it again. Of every **Filter** this is the one where trusting the API
    and being wrong is worst, and no API key is asked for — which is what NSFW would require.
    """
    harness = make_harness(
        db_path,
        catalogue=(
            replace(wallpaper("sketchy"), purity="sketchy"),
            replace(wallpaper("explicit"), purity="nsfw"),
            wallpaper("wholesome"),
        ),
    )

    batch = harness.core.get_next_batch()

    assert isinstance(batch, Batch)
    assert [w.id for w in batch.wallpapers] == ["wholesome"]


def test_the_same_wallpaper_met_twice_joins_the_pool_once(db_path: Path) -> None:
    """A random search can return the same **Wallpaper** on two pages, and the **Pool** is a set.

    Ten distinct **Wallpapers** repeated three times each. A **Batch** of eight must still be eight
    distinct ones, which a **Pool** holding duplicates could not guarantee.
    """
    repeated = tuple(wallpaper(f"dup{n:02d}") for n in range(10) for _ in range(3))
    harness = make_harness(db_path, catalogue=repeated, page_size=30)

    harness.core.refill_step()
    batch = harness.core.get_next_batch()

    assert isinstance(batch, Batch)
    assert len({w.id for w in batch.wallpapers}) == 8


def test_an_empty_pool_with_no_refill_yet_says_so(db_path: Path) -> None:
    """#15: the **Batch unavailable** result tells "nothing has arrived yet" from "Wallhaven is down"."""
    harness = make_harness(db_path, fill_pool=0)

    result = harness.core.get_next_batch()

    assert isinstance(result, BatchUnavailable)
    assert result.reason is BatchUnavailable.Reason.POOL_EMPTY
    assert result.error is None


def test_changing_the_filters_prunes_undecided_pool_wallpapers_that_no_longer_pass(
    db_path: Path,
) -> None:
    """Acceptance criterion: a **Wallpaper** failing any **Filter** is not in the **Pool** — including one
    that was in it before the **Filter** moved.

    The **Pool** holds a 1440p and a 4K **Wallpaper**; raising the minimum to 4K must leave one.
    """
    harness = make_harness(
        db_path,
        catalogue=(wallpaper("modest", width=2560, height=1440), wallpaper("huge", width=3840, height=2160)),
    )

    harness.core.update_settings(min_width=3840, min_height=2160)
    batch = harness.core.get_next_batch()

    assert isinstance(batch, Batch)
    assert [w.id for w in batch.wallpapers] == ["huge"]


def test_pruning_drops_decided_wallpapers_too_and_keeps_their_log_entries(db_path: Path) -> None:
    """A **Filter** governs what may be shown, and a **Verdict** does not exempt a **Wallpaper** from it.

    A **Liked** 1080p **Wallpaper** must stop appearing once the minimum is raised to 1440p exactly as an
    undecided one does. Nothing is lost by it: only the **Pool** membership row goes, so the **Decision
    log** is untouched, **Verdict resolution** is unchanged, and **History** at #7 still renders it.

    Since #38 a submission retires what it showed (ADR 0016), so the decided **Pool** member here is one
    **Liked** from **History** before any **Batch** drew it — the one way left to have one.
    """
    harness = make_harness(
        db_path,
        catalogue=(wallpaper("judged", width=2560, height=1440), wallpaper("huge", width=3840, height=2160)),
    )
    assert harness.core.edit_verdict("judged", Verdict.LIKE) is None
    entries_before = harness.core.list_history()

    harness.core.update_settings(min_width=3840, min_height=2160)

    assert harness.core.list_history() == entries_before, "the Decision log is append-only"
    assert harness.core.resolve_verdicts(["judged"])["judged"].verdict is Verdict.LIKE
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    assert [w.id for w in batch.wallpapers] == ["huge"]


def test_pruning_leaves_the_live_batch_alone(db_path: Path) -> None:
    """Changing a **Filter** must not empty the page somebody is part way through deciding on.

    It falls out of **Pool** membership being its own table: a **Batch** holds `batch_wallpapers` rows, so
    a **Pool** row going away cannot take a tile with it — nor the **Draft Batch** marked against it.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(10))
    live = harness.core.get_next_batch()
    assert isinstance(live, Batch)
    marked = live.wallpapers[0].id
    harness.core.set_draft_verdict(live.id, marked, Verdict.FAVOURITE)

    harness.core.update_settings(min_width=3840, min_height=2160, allowed_ratios="1x1")

    still_live = harness.core.get_next_batch()
    assert isinstance(still_live, Batch)
    assert still_live.id == live.id
    assert [w.id for w in still_live.wallpapers] == [w.id for w in live.wallpapers]
    assert still_live.drafts[marked] is Verdict.FAVOURITE


def test_a_settings_change_that_touches_no_filter_prunes_nothing(harness: Harness) -> None:
    """The prune runs after every settings write, so it has to be a no-op when nothing relevant moved.

    Saving the batch size and the **Library** folder is the commonest thing the settings page does, and it
    must not quietly empty the **Pool** on the way past.
    """
    before = harness.core.refill_status().pool_size

    harness.core.update_settings(batch_size=4, library_path=Path.home() / "elsewhere")

    assert harness.core.refill_status().pool_size == before
