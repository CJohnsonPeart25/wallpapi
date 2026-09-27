"""**Banned Wallpapers** never come back. Issue #3, carried forward to the **Pool** at #6.

The page walk these tests used to cover is gone. **Batch** building draws from the **Pool** now and makes
no **API call**, so "walk to the next page when **Bans** leave you short" has nothing to walk: the **Pool**
holds hundreds of candidates and a **Ban** simply removes one of them from the draw. See ADR 0005.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import make_harness
from tests.fakes import catalogue_of
from wallpapi.core import Batch, BatchUnavailable
from wallpapi.model import Verdict


def test_a_banned_wallpaper_never_appears_in_a_later_batch_including_after_a_restart(
    db_path: Path,
) -> None:
    """The acceptance criterion, and the reason **Ban** is the strongest negative **Verdict**.

    The restart half matters because the exclusion has to come from the **Decision log** rather than from
    anything the process was holding: a second Core service over the same file must exclude it too. It also
    pins that a **Ban** excludes a **Wallpaper** from the draw rather than from the **Pool** — the **Pool**
    row survives the restart, and the **Ban** still has to be applied to it.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(24))
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    banned = first.wallpapers[0].id
    harness.core.set_draft_verdict(first.id, banned, Verdict.BAN)

    following = harness.core.submit_batch(first.id)

    assert isinstance(following, Batch)
    assert banned not in {w.id for w in following.wallpapers}

    restarted = make_harness(db_path, catalogue=catalogue_of(24), fill_pool=0)
    live = restarted.core.get_next_batch()
    assert isinstance(live, Batch)
    after_restart = restarted.core.submit_batch(live.id)
    assert isinstance(after_restart, Batch)
    assert banned not in {w.id for w in after_restart.wallpapers}


def test_bans_thin_a_batch_rather_than_costing_an_api_call(db_path: Path) -> None:
    """What replaced the walk: a **Batch** short of its size is drawn from what is left, silently.

    The **Pool** holds ten, eight of which are **Banned**, so the next **Batch** can only be two. It must
    still be a **Batch** — a short one is an acceptable outcome — and it must cost nothing, because the
    refill is what talks to Wallhaven now and the page load path does not.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(10))
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    for shown in first.wallpapers:
        harness.core.set_draft_verdict(first.id, shown.id, Verdict.BAN)
    calls_before = len(harness.wallhaven.searches)

    following = harness.core.submit_batch(first.id)

    assert isinstance(following, Batch)
    assert len(following.wallpapers) == 2
    assert len(harness.wallhaven.searches) == calls_before


def test_a_pool_of_nothing_but_bans_is_a_batch_unavailable(db_path: Path) -> None:
    """Unavailable is for having nothing to show, not for having too little.

    Every **Wallpaper** in the **Pool** is **Banned**, so there is nothing left to draw — and no refill has
    failed, so the reason is that the **Pool** is empty rather than that Wallhaven is unreachable.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(2))
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    for shown in first.wallpapers:
        harness.core.set_draft_verdict(first.id, shown.id, Verdict.BAN)

    result = harness.core.submit_batch(first.id)

    assert isinstance(result, BatchUnavailable)
    assert result.reason is BatchUnavailable.Reason.POOL_EMPTY
