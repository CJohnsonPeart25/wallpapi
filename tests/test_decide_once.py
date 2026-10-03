"""Decide once (#38, ADR 0016): the **Pool** holds no **Wallpaper** the **Decision log** mentions.

Every **Verdict**, **Ignore** included, retires a **Wallpaper** from the **Pool**, so each submitted **Batch**
makes room and the refill has something to top up. The rule is kept at three points — submission, admission
and migration — and each is pinned here through the Core service.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from tests.conftest import FIXED_NOW, Harness, make_harness
from tests.fakes import catalogue_of, wallpaper
from wallpapi.core import IDLE_RECHECK_SECONDS, SUPERSEDED_POOL_TARGET_SIZE, Batch
from wallpapi.model import Clearance, Verdict, Zone


def test_submitting_a_batch_retires_every_shown_wallpaper_from_the_pool(db_path: Path) -> None:
    """Explicit or not, everything shown leaves: the **Pool** of 12 loses the 8 the **Batch** showed, and
    the **Batch** minted next is drawn from the 4 nobody has seen."""
    harness = make_harness(db_path, catalogue=catalogue_of(12))
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    shown = {w.id for w in first.wallpapers}
    liked, banned = sorted(shown)[:2]
    harness.core.set_draft_verdict(first.id, liked, Verdict.LIKE)
    harness.core.set_draft_verdict(first.id, banned, Verdict.BAN)

    following = harness.core.submit_batch(first.id)

    assert harness.core.refill_status().pool_size == 4
    assert isinstance(following, Batch)
    assert not {w.id for w in following.wallpapers} & shown


def test_a_refill_that_meets_a_decided_wallpaper_again_does_not_readmit_it(db_path: Path) -> None:
    """Admission: a random walk is free to rediscover what has been decided, and must not put it back.

    The fake's catalogue is the 8 the **Batch** showed, so the walk that runs once they have gone finds
    nothing else — and a refused **Wallpaper** does not count towards the **Pool**."""
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    harness.core.submit_batch(first.id)
    searched_before = len(harness.wallhaven.searches)

    harness.fill_pool(3)

    assert any(search["page"] == 1 for search in harness.wallhaven.searches[searched_before:])
    assert harness.core.refill_status().pool_size == 0


def test_a_wallpaper_whose_only_entry_is_a_legacy_clearance_is_not_readmitted(db_path: Path) -> None:
    """ "Mentions" means any entry. A **Clearance** resolves to nothing, exactly as never being seen does,
    but it was decided — so admission asks the log, not resolution.

    The **Wallpaper** leaves the **Pool** by a **Filter** prune, gains a **Clearance** (written behind the
    seam, because nothing can make one any more), and the **Filter** is put back so the walk meets it
    again."""
    harness = make_harness(db_path, catalogue=(wallpaper("cleared", width=2560, height=1440),))
    harness.core.update_settings(min_width=3840)
    assert harness.core.refill_status().pool_size == 0
    with _connection(db_path) as connection:
        connection.execute(
            "INSERT INTO decision_log (wallpaper_id, batch_id, verdict, recorded_at) VALUES (?, NULL, ?, ?)",
            ("cleared", Clearance.CLEARED.value, FIXED_NOW.isoformat()),
        )
    assert harness.core.resolve_verdicts(["cleared"])["cleared"].verdict is None
    harness.core.update_settings(min_width=2560)

    harness.fill_pool(2)

    assert harness.core.refill_status().pool_size == 0


# -- migration 10 ---------------------------------------------------------------------------------------


def test_the_new_target_reaches_a_database_that_still_held_the_old_one(db_path: Path) -> None:
    """Migration 9's precedent: setting it *to* the old default is exactly the state a pre-#38 database is
    in, and the state migration 10 has to recognise."""
    before = make_harness(db_path, fill_pool=0)
    before.core.update_settings(pool_target_size=SUPERSEDED_POOL_TARGET_SIZE)
    _force_remigration(db_path)

    after = make_harness(db_path, fill_pool=0)

    assert after.core.get_settings().pool_target_size == 500


def test_a_target_the_user_chose_is_left_exactly_as_they_set_it(db_path: Path) -> None:
    """A migration never undoes a setting: 3000 is not the old default, so it stays."""
    chosen = make_harness(db_path, fill_pool=0)
    chosen.core.update_settings(pool_target_size=3000)
    _force_remigration(db_path)

    after = make_harness(db_path, fill_pool=0)

    assert after.core.get_settings().pool_target_size == 3000


def test_migration_retires_every_pool_member_the_decision_log_already_mentions(db_path: Path) -> None:
    """The third enforcement point: a pre-#38 **Pool** still holds what earlier **Batches** decided.

    That state is arranged through the seam by deciding **Pool** members from **History**, which appends
    to the log and takes nothing out of the **Pool**. A legacy **Clearance** counts as a mention like any
    other entry, and is the one row here written behind the seam because nothing can make one any more.
    """
    before = make_harness(db_path, catalogue=catalogue_of(12))
    for wallpaper_id, verdict in (("wp0000", Verdict.LIKE), ("wp0001", Verdict.IGNORE)):
        assert before.core.edit_verdict(wallpaper_id, verdict) is None
    with _connection(db_path) as connection:
        connection.execute(
            "INSERT INTO decision_log (wallpaper_id, batch_id, verdict, recorded_at) VALUES (?, NULL, ?, ?)",
            ("wp0002", Clearance.CLEARED.value, FIXED_NOW.isoformat()),
        )
    assert before.core.refill_status().pool_size == 12
    _force_remigration(db_path)

    after = make_harness(db_path, fill_pool=0)

    assert after.core.refill_status().pool_size == 9
    batch = after.core.get_next_batch()
    assert isinstance(batch, Batch)
    assert not {w.id for w in batch.wallpapers} & {"wp0000", "wp0001", "wp0002"}


def test_migration_deletes_the_revisit_weight_row(db_path: Path) -> None:
    """The **Revisit weight** is gone (ADR 0016 supersedes ADR 0012), so its stored row goes too rather
    than lingering as a setting nothing reads. Read behind the seam because no setting names it any more."""
    make_harness(db_path, fill_pool=0)
    with _connection(db_path) as connection:
        connection.execute("INSERT INTO settings (key, value) VALUES ('revisit_weight', '0.2')")
    _force_remigration(db_path)

    make_harness(db_path, fill_pool=0)

    with _connection(db_path) as connection:
        assert connection.execute("SELECT * FROM settings WHERE key = 'revisit_weight'").fetchall() == []


def _force_remigration(db_path: Path) -> None:
    """Wind `user_version` back to 9 so the next Core service over this file applies migration 10 again.

    A reach past the seam for the reason `test_settings.py` gives: a migration only runs against a database
    that has not had it, and the Core service migrates in its constructor.
    """
    with _connection(db_path) as connection:
        connection.execute("PRAGMA user_version = 9")


@contextmanager
def _connection(db_path: Path) -> Generator[sqlite3.Connection]:
    connection = sqlite3.connect(db_path, isolation_level=None)
    try:
        yield connection
    finally:
        connection.close()


# -- what retiring does not change ----------------------------------------------------------------------


def test_a_retired_wallpaper_still_shapes_the_scores_of_the_pool(db_path: Path) -> None:
    """ADR 0007 unchanged: the decided set is independent of **Pool** membership. Eight **Bans**, every
    one retired by the submission that recorded it, still make the one unseen **Wallpaper** like them a
    **Dud**."""
    ids = [w.id for w in catalogue_of(9)]
    alike = {(a, b): 0.95 for a in ids for b in ids if a != b}
    harness = make_harness(db_path, catalogue=catalogue_of(9), similarities=alike)
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    assert harness.core.set_all_draft_verdicts(first.id, Verdict.BAN) is None

    harness.core.submit_batch(first.id)

    (remaining,) = harness.core.classify_pool()
    assert remaining.wallpaper.id not in {w.id for w in first.wallpapers}
    assert remaining.zone is Zone.DUD
    assert remaining.score < 0


def test_a_history_edit_does_not_put_a_wallpaper_back_in_the_pool(db_path: Path) -> None:
    """Decided once: **History** is the only way to revisit a decision, and revisiting it there changes the
    **Decision log**, not what may be shown."""
    harness = make_harness(db_path, catalogue=catalogue_of(12))
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    harness.core.submit_batch(first.id)
    retired = first.wallpapers[0].id

    assert harness.core.edit_verdict(retired, Verdict.FAVOURITE) is None

    assert harness.core.refill_status().pool_size == 4


def test_lowering_the_target_below_the_pool_trims_nothing(harness: Harness) -> None:
    """A **Pool** above its target drains rather than being cut: what was fetched, filtered and thumbnailed
    stays, and the refill idles until submissions take the **Pool** below the new target."""
    assert harness.core.refill_status().pool_size == 24

    harness.core.update_settings(pool_target_size=5)

    status = harness.core.refill_status()
    assert status.pool_size == 24
    assert status.at_target
    assert harness.core.refill_wait() == IDLE_RECHECK_SECONDS


def test_a_submission_takes_a_pool_at_target_below_it_and_wakes_the_refill(harness: Harness) -> None:
    """The bug #38 was filed for: at target, nothing ever left the **Pool**, so the refill idled for good.
    Now the submission is what makes room, and the refill stops idling."""
    harness.core.update_settings(pool_target_size=24)
    assert harness.core.refill_wait() == IDLE_RECHECK_SECONDS
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)

    harness.core.submit_batch(batch.id)

    assert not harness.core.refill_status().at_target
    assert harness.core.refill_wait() != IDLE_RECHECK_SECONDS
