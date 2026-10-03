"""Migrations that change what an existing database holds. Each test stops `migrate` at the version before the
step, arranges what an older database held at that point, and lets the rest of the steps run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import make_harness, raw_connection, write_log_entries, write_old_pool
from tests.fakes import catalogue_of
from wallpapi import storage
from wallpapi.core import SUPERSEDED_POOL_TARGET_SIZE, SUPERSEDED_SIMILARITY_RADIUS, Batch
from wallpapi.model import Clearance, Verdict

RETUNED = [
    # (setting, the default it replaced, a value a user chose, the version before the migration)
    pytest.param("similarity_radius", SUPERSEDED_SIMILARITY_RADIUS, 0.42, 8, id="radius, migration 9"),
    pytest.param("pool_target_size", SUPERSEDED_POOL_TARGET_SIZE, 3000, 9, id="pool target, migration 10"),
]


def _stored_at(db_path: Path, version: int, setting: str, value: float) -> None:
    """A database migrated to `version`, holding `value` for `setting` as the settings page stores it."""
    with raw_connection(db_path) as connection:
        storage.migrate(connection, to=version)
        connection.execute("UPDATE settings SET value = ? WHERE key = ?", (str(value), setting))


@pytest.mark.parametrize(("setting", "superseded", "chosen", "before"), RETUNED)
def test_a_new_default_reaches_a_database_that_still_held_the_old_one(
    tmp_path: Path, setting: str, superseded: float, chosen: float, before: int
) -> None:
    """Holding the old default is exactly the state an older database is in."""
    db_path = tmp_path / "old.db"
    _stored_at(db_path, before, setting, superseded)

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
    _stored_at(db_path, before, setting, chosen)

    assert getattr(make_harness(db_path, fill_pool=0).core.get_settings(), setting) == chosen


def test_migration_retires_every_pool_member_the_decision_log_already_mentions(db_path: Path) -> None:
    """Decide once (ADR 0016) at its third enforcement point: an older **Pool** still holds what was decided
    from **History**, which takes nothing out of the **Pool**; a legacy **Clearance** is a mention like any
    other entry."""
    with raw_connection(db_path) as connection:
        storage.migrate(connection, to=9)
    write_old_pool(db_path, catalogue_of(12))
    write_log_entries(
        db_path, {"wp0000": Verdict.LIKE, "wp0001": Verdict.IGNORE, "wp0002": Clearance.CLEARED}
    )

    after = make_harness(db_path, fill_pool=0)

    assert after.core.refill.status().pool_size == 9
    batch = after.core.get_next_batch()
    assert isinstance(batch, Batch)
    assert not {w.id for w in batch.wallpapers} & {"wp0000", "wp0001", "wp0002"}


def test_migration_deletes_the_revisit_weight_row(db_path: Path) -> None:
    """Gone with ADR 0016, so its row goes rather than lingering. Read behind the seam because no setting
    names it any more."""
    with raw_connection(db_path) as connection:
        storage.migrate(connection, to=9)
        connection.execute("INSERT INTO settings (key, value) VALUES ('revisit_weight', '0.2')")

        storage.migrate(connection)

        assert connection.execute("SELECT * FROM settings WHERE key = 'revisit_weight'").fetchall() == []
