"""Reading and updating settings through the Core service. Issue #4.

Settings are one row per key in storage and a typed `Settings` value at the seam. Every test here enters
through the Core service, so what is asserted is the value that comes back out, never the row that went in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import Harness, make_harness
from wallpapi.core import (
    DEFAULT_BATCH_SIZE,
    MAX_BATCH_SIZE,
    MIN_BATCH_SIZE,
    Batch,
    Settings,
    SettingsRefused,
)


def test_a_fresh_database_reads_the_seeded_defaults(harness: Harness) -> None:
    """Acceptance criterion: settings are readable. A fresh database is already configured.

    The defaults are seeded rows rather than a fallback in the reader, so there is one source of truth for
    what "unconfigured" means and the settings page has nothing to special-case.
    """
    settings = harness.core.get_settings()

    assert isinstance(settings, Settings)
    assert settings.batch_size == DEFAULT_BATCH_SIZE == 8
    assert settings.library_path == Path.home() / "Pictures" / "wallpapi"
    assert settings.library_path.is_absolute()


def test_updating_the_batch_size_changes_the_next_batch_minted(harness: Harness) -> None:
    """Acceptance criterion: batch size is configurable, including 2, and applies to the next **Batch**.

    "The next **Batch**" is the next one *minted*, not the one on screen — the live **Batch** is pinned by
    test_the_live_batch_keeps_the_size_it_was_minted_with.
    """
    live = harness.core.get_next_batch()
    assert isinstance(live, Batch)
    assert len(live.wallpapers) == 8

    assert isinstance(harness.core.update_settings(batch_size=2), Settings)
    harness.core.submit_batch(live.id)
    following = harness.core.get_next_batch()

    assert isinstance(following, Batch)
    assert following.size == 2
    assert len(following.wallpapers) == 2


def test_the_live_batch_keeps_the_size_it_was_minted_with(harness: Harness) -> None:
    """Changing the size must not reroll the **Batch** somebody is part way through deciding on.

    A **Batch** persists until it is submitted (ADR 0002), so the new size applies to the next one minted
    after this one is submitted. Rebuilding the live **Batch** to fit would silently discard the
    **Draft Batch** marked against it.
    """
    live = harness.core.get_next_batch()
    assert isinstance(live, Batch)

    harness.core.update_settings(batch_size=2)
    still_live = harness.core.get_next_batch()

    assert isinstance(still_live, Batch)
    assert still_live.id == live.id
    assert len(still_live.wallpapers) == 8


def test_updating_the_library_path_persists(harness: Harness, tmp_path: Path) -> None:
    """Acceptance criterion: the **Library** path is configurable."""
    chosen = tmp_path / "Wallpapers"

    updated = harness.core.update_settings(library_path=chosen)

    assert isinstance(updated, Settings)
    assert updated.library_path == chosen
    assert harness.core.get_settings().library_path == chosen


def test_updating_the_library_path_does_not_create_the_folder(harness: Harness, tmp_path: Path) -> None:
    """Setting where **Favourites** will go is not the same as going there.

    The **Library** writer creates the folder on its first write (#5). Creating it here would litter a
    folder onto every path the user typed and then thought better of.
    """
    chosen = tmp_path / "not-yet"

    harness.core.update_settings(library_path=chosen)

    assert not chosen.exists()


def test_updating_one_setting_leaves_the_others_alone(harness: Harness, tmp_path: Path) -> None:
    """A partial update writes only the keys it was given.

    The page can grow sections — **Filters** at #6, **Mixes** at #10 — without a form that omits a field
    quietly resetting it to a default.
    """
    chosen = tmp_path / "Wallpapers"
    harness.core.update_settings(batch_size=4, library_path=chosen)

    harness.core.update_settings(batch_size=16)

    settings = harness.core.get_settings()
    assert settings.batch_size == 16
    assert settings.library_path == chosen


def test_settings_survive_a_restart(db_path: Path, tmp_path: Path) -> None:
    """Acceptance criterion: settings persist across restarts.

    A second Core service over the same file, which also proves the seeding migration is idempotent: it
    must not stamp the defaults back over what the user chose.
    """
    chosen = tmp_path / "Wallpapers"
    first_run = make_harness(db_path)
    first_run.core.update_settings(batch_size=3, library_path=chosen)

    second_run = make_harness(db_path)

    restored = second_run.core.get_settings()
    assert restored.batch_size == 3
    assert restored.library_path == chosen


def test_a_batch_size_at_the_cap_is_accepted(harness: Harness) -> None:
    """The bounds are inclusive at both ends, so the cap is a usable value rather than a value short."""
    assert isinstance(harness.core.update_settings(batch_size=MIN_BATCH_SIZE), Settings)
    assert harness.core.get_settings().batch_size == MIN_BATCH_SIZE

    assert isinstance(harness.core.update_settings(batch_size=MAX_BATCH_SIZE), Settings)
    assert harness.core.get_settings().batch_size == MAX_BATCH_SIZE


@pytest.mark.parametrize(
    ("posted", "reason"),
    [
        ("0", SettingsRefused.Reason.BATCH_SIZE_OUT_OF_RANGE),
        ("-1", SettingsRefused.Reason.BATCH_SIZE_OUT_OF_RANGE),
        (str(MAX_BATCH_SIZE + 1), SettingsRefused.Reason.BATCH_SIZE_OUT_OF_RANGE),
        ("eight", SettingsRefused.Reason.BATCH_SIZE_NOT_A_NUMBER),
        ("1.5", SettingsRefused.Reason.BATCH_SIZE_NOT_A_NUMBER),
        ("", SettingsRefused.Reason.BATCH_SIZE_NOT_A_NUMBER),
    ],
)
def test_an_invalid_batch_size_is_refused_with_a_reason(
    harness: Harness, posted: str, reason: SettingsRefused.Reason
) -> None:
    """A refusal, not an exception: the page gets one error branch rather than a traceback.

    A **Batch** of zero is the one that matters most — it is a page with nothing to decide on and no way
    back except editing the database.
    """
    refused = harness.core.update_settings(batch_size=posted)

    assert isinstance(refused, SettingsRefused)
    assert refused.reason is reason


@pytest.mark.parametrize(
    ("posted", "reason"),
    [
        ("", SettingsRefused.Reason.LIBRARY_PATH_EMPTY),
        ("   ", SettingsRefused.Reason.LIBRARY_PATH_EMPTY),
        ("Wallpapers", SettingsRefused.Reason.LIBRARY_PATH_NOT_ABSOLUTE),
        ("Pictures/wallpapi", SettingsRefused.Reason.LIBRARY_PATH_NOT_ABSOLUTE),
        (".", SettingsRefused.Reason.LIBRARY_PATH_NOT_ABSOLUTE),
    ],
)
def test_an_invalid_library_path_is_refused_with_a_reason(
    harness: Harness, posted: str, reason: SettingsRefused.Reason
) -> None:
    """Absolute or nothing (invariant 9): a relative path means the **Library** moves with the working
    directory, and every file written into it is recorded at a path that cannot be found again."""
    refused = harness.core.update_settings(library_path=posted)

    assert isinstance(refused, SettingsRefused)
    assert refused.reason is reason


def test_a_refused_update_changes_nothing(harness: Harness, tmp_path: Path) -> None:
    """Every field is validated before any field is written.

    Otherwise a form with a good batch size and a bad path would half-apply, and the page would report a
    failure over a database that had already moved.
    """
    chosen = tmp_path / "Wallpapers"
    harness.core.update_settings(batch_size=4, library_path=chosen)

    refused = harness.core.update_settings(batch_size=2, library_path="Wallpapers")

    assert isinstance(refused, SettingsRefused)
    settings = harness.core.get_settings()
    assert settings.batch_size == 4
    assert settings.library_path == chosen


def test_a_refused_batch_size_leaves_the_next_batch_alone(harness: Harness) -> None:
    """The refusal has to reach past the settings page: a **Batch** of zero would be unusable."""
    harness.core.update_settings(batch_size=0)

    batch = harness.core.get_next_batch()

    assert isinstance(batch, Batch)
    assert len(batch.wallpapers) == 8
