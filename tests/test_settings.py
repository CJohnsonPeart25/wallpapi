"""The settings rules, through `settings` alone: a real database, no Core service. One refusal table across
the fields, what is accepted, a partial update, a restart, and a refusal that writes nothing; and which
**Mixes** can be deleted. What each **Filter** excludes is `test_pool.py`; the **Mix** rules in a
**Batch** are `test_mix.py`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from tests.conftest import Harness, live
from wallpapi import settings, storage, workflows
from wallpapi.model import Mix
from wallpapi.settings import (
    EXPLORE_MIX,
    FIELDS,
    MAX_BATCH_SIZE,
    MAX_FILTER_PIXELS,
    MAX_POOL_TARGET_SIZE,
    MAX_SIMILARITY_DECAY,
    MIN_BATCH_SIZE,
    REFINE_MIX,
    WALLHAVEN_RATIOS,
    MixListing,
    Settings,
    SettingsRefused,
)

Reason = SettingsRefused.Reason


@pytest.fixture
def memory() -> Iterator[sqlite3.Connection]:
    with closing(storage.connect(":memory:")) as connection:
        storage.migrate(connection)
        yield connection


def update(connection: sqlite3.Connection, **fields: object) -> Settings | SettingsRefused:
    """One `settings.update` in its own write transaction, as the Core service calls it."""
    with storage.write(connection) as write:
        return settings.update(write, **fields)


REFUSALS = [
    *[("batch_size", bad, Reason.OUT_OF_RANGE) for bad in ("0", "-1", str(MAX_BATCH_SIZE + 1))],
    *[("batch_size", bad, Reason.NOT_A_NUMBER) for bad in ("eight", "1.5", "")],
    *[("library_path", bad, Reason.EMPTY) for bad in ("", "   ")],
    # Absolute or nothing (invariant 9): a relative path moves with the working directory, and every file
    # written into it is recorded at a path that cannot be found again.
    *[("library_path", bad, Reason.NOT_ABSOLUTE) for bad in ("Wallpapers", "Pictures/wallpapi", ".")],
    # A Pool of none is a page with nothing on it; above the cap, every whole-Pool scan slows for ever.
    *[("pool_target_size", bad, Reason.OUT_OF_RANGE) for bad in ("0", "-1", MAX_POOL_TARGET_SIZE + 1)],
    *[("pool_target_size", bad, Reason.NOT_A_NUMBER) for bad in ("two thousand", "1.5", "")],
    # Zero is "no minimum"; a negative or more than any display is a typo that empties the Pool.
    *[
        (axis, bad, Reason.OUT_OF_RANGE)
        for axis in ("min_width", "min_height")
        for bad in ("-1", MAX_FILTER_PIXELS + 1)
    ],
    *[
        (axis, bad, Reason.NOT_A_NUMBER)
        for axis in ("min_width", "min_height")
        for bad in ("wide", "2560.5", "")
    ],
    *[("min_favourites", "-1", Reason.OUT_OF_RANGE)],
    *[("min_favourites", bad, Reason.NOT_A_NUMBER) for bad in ("lots", "1e3")],
    # Wallhaven does not report an unknown `ratios=`, it quietly searches for something else; and a blank
    # field reads as a mistake, so "every ratio" is written as the full list.
    *[("allowed_ratios", bad, Reason.EMPTY) for bad in ("", "   ", ",")],
    *[
        ("allowed_ratios", bad, Reason.NOT_A_WALLHAVEN_RATIO)
        for bad in ("16:9", "16x9,widescreen", "4000x3000")
    ],
    *[("similarity_radius", bad, Reason.OUT_OF_RANGE) for bad in ("-0.1", "1.5")],
    # `float()` accepts nan and inf, and a NaN radius would turn the whole Pool Unknown, looking like a
    # broken Similarity provider rather than a bad setting.
    *[("similarity_radius", bad, Reason.NOT_A_NUMBER) for bad in ("wide", "nan")],
    *[("similarity_decay", bad, Reason.OUT_OF_RANGE) for bad in ("-1", str(MAX_SIMILARITY_DECAY + 1))],
    *[("similarity_decay", bad, Reason.NOT_A_NUMBER) for bad in ("fast", "inf")],
    *[("thumbnail_cache_max_mb", "-1", Reason.OUT_OF_RANGE)],
    *[("thumbnail_cache_max_mb", bad, Reason.NOT_A_NUMBER) for bad in ("lots", "0.5", "")],
    *[("active_mix", bad, Reason.ACTIVE_MIX_UNKNOWN) for bad in ("nope", "", "  ")],
]


def test_the_refusal_table_covers_every_field() -> None:
    """A field added to the table without a row here is a rule nobody checked."""
    assert {field.key for field in FIELDS} == {field for field, _, _ in REFUSALS}


@pytest.mark.parametrize(("field", "posted", "reason"), REFUSALS)
def test_an_invalid_value_is_refused_with_a_reason_and_words_and_changes_nothing(
    memory: sqlite3.Connection, field: str, posted: object, reason: SettingsRefused.Reason
) -> None:
    """A refusal, not an exception, so the page has one error branch; and it says which field, in words."""
    before = settings.get(memory)

    refused = update(memory, **{field: posted})

    assert isinstance(refused, SettingsRefused)
    assert (refused.reason, refused.field) == (reason, field)
    assert refused.message and reason.value not in refused.message
    assert settings.get(memory) == before


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        pytest.param({"batch_size": MIN_BATCH_SIZE}, {"batch_size": MIN_BATCH_SIZE}, id="the smallest batch"),
        pytest.param({"batch_size": MAX_BATCH_SIZE}, {"batch_size": MAX_BATCH_SIZE}, id="the largest batch"),
        pytest.param({"pool_target_size": "50"}, {"pool_target_size": 50}, id="a pool target, as text"),
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
            {"allowed_ratios": tuple(sorted(WALLHAVEN_RATIOS))},
            id="every ratio Wallhaven documents",
        ),
        pytest.param(
            {"similarity_radius": "0.7", "similarity_decay": 0},
            {"similarity_radius": 0.7, "similarity_decay": 0.0},
            id="similarity",
        ),
        # Zero is accepted: the cap never evicts an Explicit Verdict, so History still renders.
        pytest.param({"thumbnail_cache_max_mb": "0"}, {"thumbnail_cache_max_mb": 0}, id="no thumbnail cache"),
        pytest.param({"active_mix": " refine "}, {"active_mix": "refine"}, id="a stored mix, trimmed"),
    ],
)
def test_a_valid_value_is_saved_typed_and_read_back(
    memory: sqlite3.Connection, fields: dict[str, object], expected: dict[str, object]
) -> None:
    saved = update(memory, **fields)

    assert isinstance(saved, Settings)
    for field, value in expected.items():
        assert getattr(saved, field) == getattr(settings.get(memory), field) == value


def test_an_unknown_field_is_a_programming_error(memory: sqlite3.Connection) -> None:
    """Not a refusal: no form posts a field the table does not have."""
    with pytest.raises(TypeError, match="revisit_weight"):
        update(memory, revisit_weight="2")


def test_the_library_path_is_saved_without_creating_the_folder(
    memory: sqlite3.Connection, tmp_path: Path
) -> None:
    """The writer creates it on the first **Favourite**; creating it here would litter a folder onto every
    path the user typed and thought better of."""
    chosen = tmp_path / "not-yet"

    assert isinstance(update(memory, library_path=chosen), Settings)

    assert settings.get(memory).library_path == chosen
    assert not chosen.exists()


def test_updating_one_setting_leaves_the_others_alone(memory: sqlite3.Connection, tmp_path: Path) -> None:
    """`None` means "leave this one alone" (ADR 0004), so a form that omits a field does not reset it."""
    chosen = tmp_path / "Wallpapers"
    update(memory, library_path=chosen, min_width=1920, min_favourites=99)

    update(memory, batch_size=4, min_height=None)

    current = settings.get(memory)
    assert (current.batch_size, current.library_path) == (4, chosen)
    assert (current.min_width, current.min_height, current.min_favourites) == (1920, 1440, 99)


def test_a_refused_update_writes_nothing_at_all(memory: sqlite3.Connection, tmp_path: Path) -> None:
    """Every field is validated before any is written, so a good value beside a bad one does not
    half-apply and then get reported as a failure."""
    update(memory, batch_size=4, library_path=tmp_path / "Wallpapers")
    before = settings.get(memory)

    refused = update(memory, batch_size=2, library_path="Wallpapers", min_width=1920)

    assert isinstance(refused, SettingsRefused)
    assert settings.get(memory) == before


def test_settings_survive_a_restart(db_path: Path, tmp_path: Path) -> None:
    """A second connection over the file, migrated again, which also proves the seeding migration does not
    stamp the defaults back over what the user chose."""
    chosen = tmp_path / "Wallpapers"
    with closing(storage.connect(db_path)) as first:
        storage.migrate(first)
        update(
            first,
            batch_size=3,
            library_path=chosen,
            min_width=1920,
            min_height=1080,
            allowed_ratios="21x9,32x9",
            min_favourites=250,
        )

    with closing(storage.connect(db_path)) as second:
        storage.migrate(second)
        current = settings.get(second)

    assert (current.batch_size, current.library_path) == (3, chosen)
    assert (current.min_width, current.min_height, current.min_favourites) == (1920, 1080, 250)
    assert current.allowed_ratios == ("21x9", "32x9")


def test_a_hand_edited_bad_row_reads_as_its_default(memory: sqlite3.Connection) -> None:
    """`get` never refuses, so the settings page still opens to fix the row."""
    seeded = settings.get(memory)
    memory.execute("UPDATE settings SET value = 'lots' WHERE key = 'batch_size'")
    memory.execute("DELETE FROM settings WHERE key = 'min_width'")

    assert settings.get(memory) == seeded


def test_the_form_values_are_the_settings_as_text_and_read_back_unchanged(memory: sqlite3.Connection) -> None:
    """What the page shows is what saving it unedited would store: a round trip changes nothing."""
    before = settings.get(memory)

    values = settings.form_values(before)

    assert "active_mix" not in values
    assert isinstance(update(memory, **values), Settings)
    assert settings.get(memory) == before


def test_every_mix_says_whether_it_can_be_deleted(memory: sqlite3.Connection) -> None:
    """**Explore** and **Refine** never, the active **Mix** not while it is active, anything else yes."""
    with storage.write(memory) as write:
        settings.save_mix(write, "duds only", unknown=0, banger=0, dud=100)
        settings.save_mix(write, "night", unknown=10, banger=80, dud=10)
        settings.update(write, active_mix="night")

    assert settings.list_mixes(memory) == (
        MixListing(Mix(name="duds only", unknown=0, banger=0, dud=100), deletable=True),
        MixListing(EXPLORE_MIX, deletable=False),
        MixListing(Mix(name="night", unknown=10, banger=80, dud=10), deletable=False),
        MixListing(REFINE_MIX, deletable=False),
    )


def test_a_new_batch_size_applies_to_the_next_batch_minted_and_not_the_live_one(harness: Harness) -> None:
    """Rebuilding the live **Batch** to fit would discard the **Draft Batch** marked against it."""
    opened = live(harness)

    assert isinstance(workflows.save_settings(harness.modules, batch_size=2), Settings)
    still_live = live(harness)
    assert (still_live.id, len(still_live.wallpapers)) == (opened.id, 8)

    workflows.submit(harness.modules, opened.id)

    following = live(harness)
    assert (following.size, len(following.wallpapers)) == (2, 2)
