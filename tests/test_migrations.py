"""Migrations that change what an existing database holds. Each test winds `user_version` back with
`force_remigration` and builds a second Core service over the file, because the Core service migrates in its
constructor and there is no other way to ask it for an un-migrated database.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import force_remigration, make_harness, raw_connection, write_legacy_clearance
from tests.fakes import catalogue_of
from wallpapi.core import SUPERSEDED_POOL_TARGET_SIZE, SUPERSEDED_SIMILARITY_RADIUS, Batch
from wallpapi.model import Verdict

RETUNED = [
    # (setting, the default it replaced, a value a user chose, the version before the migration)
    pytest.param("similarity_radius", SUPERSEDED_SIMILARITY_RADIUS, 0.42, 8, id="radius, migration 9"),
    pytest.param("pool_target_size", SUPERSEDED_POOL_TARGET_SIZE, 3000, 9, id="pool target, migration 10"),
]


def _set(db_path: Path, setting: str, value: float) -> None:
    harness = make_harness(db_path, fill_pool=0)
    harness.core.update_settings(**{setting: value})  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(("setting", "superseded", "chosen", "before"), RETUNED)
def test_a_new_default_reaches_a_database_that_still_held_the_old_one(
    tmp_path: Path, setting: str, superseded: float, chosen: float, before: int
) -> None:
    """Setting it *to* the old default is exactly the state an older database is in."""
    db_path = tmp_path / "old.db"
    _set(db_path, setting, superseded)
    force_remigration(db_path, to_version=before)

    migrated = make_harness(db_path, fill_pool=0).core.get_settings()
    fresh = make_harness(tmp_path / "fresh.db", fill_pool=0).core.get_settings()

    assert getattr(migrated, setting) == getattr(fresh, setting)
    assert getattr(migrated, setting) != superseded


@pytest.mark.parametrize(("setting", "superseded", "chosen", "before"), RETUNED)
def test_a_value_the_user_chose_is_left_exactly_as_they_set_it(
    db_path: Path, setting: str, superseded: float, chosen: float, before: int
) -> None:
    """A migration never undoes a setting: it rewrites only a row still holding the old default."""
    del superseded
    _set(db_path, setting, chosen)
    force_remigration(db_path, to_version=before)

    assert getattr(make_harness(db_path, fill_pool=0).core.get_settings(), setting) == chosen


def test_migration_retires_every_pool_member_the_decision_log_already_mentions(db_path: Path) -> None:
    """Decide once (ADR 0016) at its third enforcement point: an older **Pool** still holds what earlier
    **Batches** decided. Arranged by deciding **Pool** members from **History**, which takes nothing out of
    the **Pool**; a legacy **Clearance** is a mention like any other entry."""
    before = make_harness(db_path, catalogue=catalogue_of(12))
    for wallpaper_id, verdict in (("wp0000", Verdict.LIKE), ("wp0001", Verdict.IGNORE)):
        assert before.core.edit_verdict(wallpaper_id, verdict) is None
    write_legacy_clearance(db_path, "wp0002")
    assert before.core.refill_status().pool_size == 12
    force_remigration(db_path, to_version=9)

    after = make_harness(db_path, fill_pool=0)

    assert after.core.refill_status().pool_size == 9
    batch = after.core.get_next_batch()
    assert isinstance(batch, Batch)
    assert not {w.id for w in batch.wallpapers} & {"wp0000", "wp0001", "wp0002"}


def test_migration_deletes_the_revisit_weight_row(db_path: Path) -> None:
    """Gone with ADR 0016, so its row goes rather than lingering. Read behind the seam because no setting
    names it any more."""
    make_harness(db_path, fill_pool=0)
    with raw_connection(db_path) as connection:
        connection.execute("INSERT INTO settings (key, value) VALUES ('revisit_weight', '0.2')")
    force_remigration(db_path, to_version=9)

    make_harness(db_path, fill_pool=0)

    with raw_connection(db_path) as connection:
        assert connection.execute("SELECT * FROM settings WHERE key = 'revisit_weight'").fetchall() == []
