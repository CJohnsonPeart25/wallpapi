"""Settings through the Core service: one refusal table across the fields, what is accepted, a partial update,
a restart, and a refusal that writes nothing. What each **Filter** excludes is `test_pool_and_refill.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import Harness, make_harness
from wallpapi.core import (
    MAX_BATCH_SIZE,
    MAX_POOL_TARGET_SIZE,
    MIN_BATCH_SIZE,
    WALLHAVEN_RATIOS,
    Batch,
    Settings,
    SettingsRefused,
)

Reason = SettingsRefused.Reason

REFUSALS = [
    *[("batch_size", bad, Reason.BATCH_SIZE_OUT_OF_RANGE) for bad in ("0", "-1", str(MAX_BATCH_SIZE + 1))],
    *[("batch_size", bad, Reason.BATCH_SIZE_NOT_A_NUMBER) for bad in ("eight", "1.5", "")],
    *[("library_path", bad, Reason.LIBRARY_PATH_EMPTY) for bad in ("", "   ")],
    # Absolute or nothing (invariant 9): a relative path moves with the working directory, and every file
    # written into it is recorded at a path that cannot be found again.
    *[
        ("library_path", bad, Reason.LIBRARY_PATH_NOT_ABSOLUTE)
        for bad in ("Wallpapers", "Pictures/wallpapi", ".")
    ],
    # A Pool of none is a page with nothing on it; above the cap, every whole-Pool scan slows for ever.
    *[
        ("pool_target_size", bad, Reason.POOL_TARGET_SIZE_INVALID)
        for bad in ("0", "-1", "two thousand", "1.5", "", MAX_POOL_TARGET_SIZE + 1)
    ],
    # Zero is "no minimum"; a negative, a fraction or more than any display is a typo that empties the Pool.
    *[("min_width", bad, Reason.MIN_WIDTH_INVALID) for bad in ("-1", "wide", "2560.5", "", 100_000)],
    *[("min_height", bad, Reason.MIN_HEIGHT_INVALID) for bad in ("-1", "wide", "2560.5", "")],
    *[("min_favourites", bad, Reason.MIN_FAVOURITES_INVALID) for bad in ("-1", "lots", "1e3")],
    # Wallhaven does not report an unknown `ratios=`, it quietly searches for something else; and a blank
    # field reads as a mistake, so "every ratio" is written as the full list.
    *[
        ("allowed_ratios", bad, Reason.ALLOWED_RATIOS_INVALID)
        for bad in ("", "   ", "16:9", "16x9,widescreen", "4000x3000", ",")
    ],
]


@pytest.mark.parametrize(("field", "posted", "reason"), REFUSALS)
def test_an_invalid_value_is_refused_with_a_reason_and_changes_nothing(
    harness: Harness, field: str, posted: object, reason: SettingsRefused.Reason
) -> None:
    """A refusal, not an exception, so the page has one error branch."""
    before = harness.core.get_settings()

    refused = harness.core.update_settings(**{field: posted})  # pyright: ignore[reportArgumentType]

    assert isinstance(refused, SettingsRefused)
    assert refused.reason is reason
    assert harness.core.get_settings() == before


@pytest.mark.parametrize(
    ("update", "expected"),
    [
        pytest.param({"batch_size": MIN_BATCH_SIZE}, {"batch_size": MIN_BATCH_SIZE}, id="the smallest batch"),
        pytest.param({"batch_size": MAX_BATCH_SIZE}, {"batch_size": MAX_BATCH_SIZE}, id="the largest batch"),
        pytest.param({"pool_target_size": 50}, {"pool_target_size": 50}, id="a pool target"),
        # Turning a Filter off is setting it to nothing, not a second "enabled" setting.
        pytest.param(
            {"min_width": 0, "min_height": 0, "min_favourites": 0},
            {"min_width": 0, "min_height": 0, "min_favourites": 0},
            id="zero minimums",
        ),
        pytest.param(
            {"allowed_ratios": " 16x9 , 16x10 ,16x9 "},
            {"allowed_ratios": ("16x9", "16x10")},
            id="ratios tidied",
        ),
        pytest.param(
            {"allowed_ratios": ",".join(sorted(WALLHAVEN_RATIOS))},
            {"allowed_ratios": frozenset(WALLHAVEN_RATIOS)},
            id="every ratio Wallhaven documents",
        ),
    ],
)
def test_a_valid_value_is_saved_and_read_back(
    harness: Harness, update: dict[str, object], expected: dict[str, object]
) -> None:
    saved = harness.core.update_settings(**update)  # pyright: ignore[reportArgumentType]

    assert isinstance(saved, Settings)
    for field, value in expected.items():
        stored = getattr(harness.core.get_settings(), field)
        assert stored == getattr(saved, field)
        assert (frozenset(stored) if isinstance(value, frozenset) else stored) == value


def test_the_library_path_is_saved_without_creating_the_folder(harness: Harness, tmp_path: Path) -> None:
    """The writer creates it on the first **Favourite**; creating it here would litter a folder onto every
    path the user typed and thought better of."""
    chosen = tmp_path / "not-yet"

    updated = harness.core.update_settings(library_path=chosen)

    assert isinstance(updated, Settings)
    assert harness.core.get_settings().library_path == chosen
    assert not chosen.exists()


def test_updating_one_setting_leaves_the_others_alone(harness: Harness, tmp_path: Path) -> None:
    """`None` means "leave this one alone" (ADR 0004), so a form that omits a field does not reset it."""
    chosen = tmp_path / "Wallpapers"
    harness.core.update_settings(library_path=chosen, min_width=1920, min_favourites=99)

    harness.core.update_settings(batch_size=4)

    settings = harness.core.get_settings()
    assert (settings.batch_size, settings.library_path) == (4, chosen)
    assert (settings.min_width, settings.min_favourites) == (1920, 99)


def test_a_refused_update_writes_nothing_at_all(harness: Harness, tmp_path: Path) -> None:
    """Every field is validated before any is written, so a good value beside a bad one does not
    half-apply and then get reported as a failure."""
    chosen = tmp_path / "Wallpapers"
    harness.core.update_settings(batch_size=4, library_path=chosen)
    before = harness.core.get_settings()

    refused = harness.core.update_settings(batch_size=2, library_path="Wallpapers", min_width=1920)

    assert isinstance(refused, SettingsRefused)
    assert harness.core.get_settings() == before


def test_settings_survive_a_restart(db_path: Path, tmp_path: Path) -> None:
    """A second Core service over the file, which also proves the seeding migration does not stamp the
    defaults back over what the user chose."""
    chosen = tmp_path / "Wallpapers"
    make_harness(db_path).core.update_settings(
        batch_size=3,
        library_path=chosen,
        min_width=1920,
        min_height=1080,
        allowed_ratios="21x9,32x9",
        min_favourites=250,
    )

    settings = make_harness(db_path).core.get_settings()

    assert (settings.batch_size, settings.library_path) == (3, chosen)
    assert (settings.min_width, settings.min_height, settings.min_favourites) == (1920, 1080, 250)
    assert settings.allowed_ratios == ("21x9", "32x9")


def test_a_new_batch_size_applies_to_the_next_batch_minted_and_not_the_live_one(harness: Harness) -> None:
    """Rebuilding the live **Batch** to fit would discard the **Draft Batch** marked against it."""
    live = harness.core.get_next_batch()
    assert isinstance(live, Batch)

    assert isinstance(harness.core.update_settings(batch_size=2), Settings)
    still_live = harness.core.get_next_batch()
    assert isinstance(still_live, Batch)
    assert (still_live.id, len(still_live.wallpapers)) == (live.id, 8)

    following = harness.core.submit_batch(live.id)

    assert isinstance(following, Batch)
    assert (following.size, len(following.wallpapers)) == (2, 2)
