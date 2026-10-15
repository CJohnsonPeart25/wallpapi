"""**Mixes**: the pure **Allocation** of slots, and making, editing and deleting **Mixes**.

**Allocation** is a pure function and is tested as one, with a seeded source; the draw a **Batch** gets
under a **Mix** is `test_batches.py`'s. The **Mixes** themselves go through `settings` alone, over the
`memory` connection, each call in its own write as the workflows make it; choosing one is the `/mix` route's
test in `test_web_routes.py`.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

import pytest

from wallpapi import settings, storage
from wallpapi.allocation import ZONE_ORDER, allocate
from wallpapi.model import Mix, Zone
from wallpapi.rng import SeededRandom
from wallpapi.settings import (
    EXPLORE_MIX,
    MAX_MIX_NAME_LENGTH,
    MIX_TOTAL,
    REFINE_MIX,
    Settings,
    SettingsRefused,
)

ROLLS = 4000
"""Seeded **Allocations** a distribution is read off: a 50/40/10 split is unmistakable at the tolerance."""
TOLERANCE = 0.02

EDITED_EXPLORE = Mix(name="explore", unknown=50, banger=45, dud=5)


def _rolled(mix: Mix, size: int) -> Counter[Zone]:
    counted: Counter[Zone] = Counter()
    for seed in range(ROLLS):
        counted.update(allocate(mix, size, SeededRandom(seed)))
    return counted


# -- Allocation --------------------------------------------------------------------------------------


def test_at_thirty_two_the_whole_slots_are_fixed_and_the_spare_one_is_rolled_by_the_remainders() -> None:
    """32 in **Explore** is 24, 6.4 and 1.6: 31 slots settled before any random number is spent, and the
    thirty-second goes to **Banger** or **Dud** at 0.4 and 0.6, never to **Unknown**."""
    for seed in range(50):
        slots = allocate(EXPLORE_MIX, 32, SeededRandom(seed))
        assert sum(slots.values()) == 32
        assert (slots[Zone.UNKNOWN], slots[Zone.BANGER] >= 6, slots[Zone.DUD] >= 1) == (24, True, True)

    counted = _rolled(EXPLORE_MIX, 32)

    assert counted[Zone.BANGER] / ROLLS - 6 == pytest.approx(0.4, abs=TOLERANCE)
    assert counted[Zone.DUD] / ROLLS - 1 == pytest.approx(0.6, abs=TOLERANCE)


def test_a_batch_of_two_is_one_unknown_and_a_fifty_forty_ten_roll() -> None:
    """1.5, 0.4 and 0.1: the remainders already sum to the one slot there is, so a wrong normalisation
    shows here first."""
    counted = _rolled(EXPLORE_MIX, 2)
    rolled = {zone: (counted[zone] - (ROLLS if zone is Zone.UNKNOWN else 0)) / ROLLS for zone in ZONE_ORDER}

    assert counted[Zone.UNKNOWN] >= ROLLS
    assert rolled[Zone.UNKNOWN] == pytest.approx(0.5, abs=TOLERANCE)
    assert rolled[Zone.BANGER] == pytest.approx(0.4, abs=TOLERANCE)
    assert rolled[Zone.DUD] == pytest.approx(0.1, abs=TOLERANCE)


@pytest.mark.parametrize("mix", [EXPLORE_MIX, REFINE_MIX], ids=["explore", "refine"])
def test_the_long_run_share_of_each_zone_is_the_mix(mix: Mix) -> None:
    """Unbiased: at a size where every **Zone** has a remainder, the expected share is the percentage."""
    counted = _rolled(mix, 7)

    for zone in ZONE_ORDER:
        assert counted[zone] / (ROLLS * 7) == pytest.approx(mix.percentage(zone) / MIX_TOTAL, abs=TOLERANCE)


@pytest.mark.parametrize("mix", [EXPLORE_MIX, REFINE_MIX], ids=["explore", "refine"])
def test_the_slots_always_add_up_to_the_batch_that_was_asked_for(mix: Mix) -> None:
    """A slot short is a tile missing, at exactly the sizes nobody tries by hand."""
    for size in range(1, 65):
        slots = allocate(mix, size, SeededRandom(size))

        assert sum(slots.values()) == size
        assert set(slots) == set(ZONE_ORDER)
        assert all(count >= 0 for count in slots.values())
    assert allocate(mix, 0, SeededRandom(1)) == dict.fromkeys(ZONE_ORDER, 0)


def test_a_mix_that_divides_exactly_leaves_nothing_to_roll() -> None:
    """Where a floating-point `floor` would be free to say 14 instead of 15."""
    allocations = {tuple(allocate(EXPLORE_MIX, 20, SeededRandom(seed)).items()) for seed in range(50)}

    assert allocations == {((Zone.UNKNOWN, 15), (Zone.BANGER, 4), (Zone.DUD, 1))}


def test_the_same_seed_allocates_the_same_way_and_a_different_one_need_not() -> None:
    assert allocate(EXPLORE_MIX, 32, SeededRandom(7)) == allocate(EXPLORE_MIX, 32, SeededRandom(7))
    assert len({tuple(allocate(EXPLORE_MIX, 32, SeededRandom(seed)).items()) for seed in range(20)}) > 1


# -- making and editing ------------------------------------------------------------------------------


def save_mix(
    connection: sqlite3.Connection, name: str, *, unknown: int | str, banger: int | str, dud: int | str
) -> Mix | SettingsRefused:
    with storage.write(connection) as write:
        return settings.save_mix(write, name, unknown=unknown, banger=banger, dud=dud)


def delete_mix(connection: sqlite3.Connection, name: str) -> SettingsRefused | None:
    with storage.write(connection) as write:
        return settings.delete_mix(write, name)


def choose(connection: sqlite3.Connection, name: str) -> None:
    with storage.write(connection) as write:
        assert isinstance(settings.update(write, active_mix=name), Settings)


def listed(connection: sqlite3.Connection) -> tuple[Mix, ...]:
    return tuple(listing.mix for listing in settings.list_mixes(connection))


def test_editing_explore_changes_the_active_mix_the_next_batch_is_drawn_under(
    memory: sqlite3.Connection,
) -> None:
    assert save_mix(memory, "explore", unknown=50, banger=45, dud=5) == EDITED_EXPLORE

    assert settings.active_mix(memory) == EDITED_EXPLORE


def test_editing_a_mix_does_not_change_which_one_is_active(memory: sqlite3.Connection) -> None:
    save_mix(memory, "refine", unknown=10, banger=90, dud=0)

    assert settings.active_mix(memory) == EXPLORE_MIX
    assert Mix(name="refine", unknown=10, banger=90, dud=0) in listed(memory)


def test_a_custom_mix_can_be_made_and_selected(memory: sqlite3.Connection) -> None:
    made = save_mix(memory, "duds only", unknown=0, banger=0, dud=100)
    choose(memory, "duds only")

    assert made == Mix(name="duds only", unknown=0, banger=0, dud=100)
    assert settings.active_mix(memory) == made


def test_mixes_and_the_active_one_survive_a_restart(tmp_path: Path) -> None:
    """Stored rather than recomputed in Python: an edit, a custom **Mix**, a deleted one and the switch, read
    back by a second connection over the same file, migrated again."""
    path = tmp_path / "wallpapi.db"
    with closing(storage.connect(path)) as first:
        storage.migrate(first)
        save_mix(first, "explore", unknown=50, banger=45, dud=5)
        save_mix(first, "all in", unknown=0, banger=100, dud=0)
        save_mix(first, "gone", unknown=0, banger=0, dud=100)
        assert delete_mix(first, "gone") is None
        choose(first, "refine")

    with closing(storage.connect(path)) as restarted:
        storage.migrate(restarted)

        assert listed(restarted) == (
            Mix(name="all in", unknown=0, banger=100, dud=0),
            EDITED_EXPLORE,
            REFINE_MIX,
        )
        assert settings.active_mix(restarted) == REFINE_MIX


@pytest.mark.parametrize(
    ("deleted", "active", "reason"),
    [
        # A control that silently changed what the next Batch is made of is not a delete button.
        pytest.param("duds only", "duds only", SettingsRefused.Reason.MIX_IN_USE, id="the active mix"),
        # Editable, but the spec's own vocabulary, and the last thing the draw could fall back to.
        pytest.param("explore", "refine", SettingsRefused.Reason.MIX_NOT_DELETABLE, id="explore"),
        pytest.param("refine", "explore", SettingsRefused.Reason.MIX_NOT_DELETABLE, id="refine"),
        pytest.param("nope", "explore", SettingsRefused.Reason.MIX_UNKNOWN, id="a mix nobody has heard of"),
    ],
)
def test_a_delete_that_cannot_happen_is_refused_and_removes_nothing(
    memory: sqlite3.Connection, deleted: str, active: str, reason: SettingsRefused.Reason
) -> None:
    save_mix(memory, "duds only", unknown=0, banger=0, dud=100)
    choose(memory, active)
    before = listed(memory)

    refused = delete_mix(memory, deleted)

    assert isinstance(refused, SettingsRefused)
    assert refused.reason is reason
    assert listed(memory) == before
    assert settings.active_mix(memory).name == active


def test_a_custom_mix_that_is_not_active_can_be_deleted(memory: sqlite3.Connection) -> None:
    save_mix(memory, "duds only", unknown=0, banger=0, dud=100)

    assert delete_mix(memory, "duds only") is None
    assert listed(memory) == (EXPLORE_MIX, REFINE_MIX)


INVALID_PERCENTAGES = [
    (30, 30, 30),
    (60, 60, 60),
    (50, 50, 50),
    (100, 1, 0),
    (0, 0, 0),
    (110, -5, -5),
    (-10, 100, 10),
    # Refused rather than rounded: a Mix the user did not type is worse than being told to type whole ones.
    ("33.3", "33.3", "33.4"),
    ("", "50", "50"),
    ("lots", "20", "5"),
    ("20%", "20", "5"),
]


@pytest.mark.parametrize(
    ("name", "shares", "reason"),
    [
        *[
            ("wrong", shares, SettingsRefused.Reason.MIX_PERCENTAGES_INVALID)
            for shares in INVALID_PERCENTAGES
        ],
        pytest.param("explore", (30, 30, 30), SettingsRefused.Reason.MIX_PERCENTAGES_INVALID, id="an edit"),
        ("", (50, 45, 5), SettingsRefused.Reason.MIX_NAME_INVALID),
        ("   ", (50, 45, 5), SettingsRefused.Reason.MIX_NAME_INVALID),
        ("x" * (MAX_MIX_NAME_LENGTH + 1), (50, 45, 5), SettingsRefused.Reason.MIX_NAME_INVALID),
    ],
)
def test_what_is_not_a_mix_is_refused_and_nothing_is_stored(
    memory: sqlite3.Connection,
    name: str,
    shares: tuple[int | str, int | str, int | str],
    reason: SettingsRefused.Reason,
) -> None:
    """The table is checked as well as the refusal, because a validator that refused after writing would
    pass on the refusal alone. One rule, `validated_mix`, which also drops a hand-edited row."""
    refused = save_mix(memory, name, unknown=shares[0], banger=shares[1], dud=shares[2])

    assert isinstance(refused, SettingsRefused)
    assert refused.reason is reason
    assert listed(memory) == (EXPLORE_MIX, REFINE_MIX)


@pytest.mark.parametrize(
    ("name", "shares", "saved"),
    [
        # A Zone at nothing is a Mix somebody may well want: the draw just never allocates it a slot.
        pytest.param("no duds", (30, 70, 0), Mix("no duds", 30, 70, 0), id="a zone at zero"),
        pytest.param("pure", (0, 100, 0), Mix("pure", 0, 100, 0), id="one zone only"),
        pytest.param(" tidy ", ("75", "20", "5"), Mix("tidy", 75, 20, 5), id="typed text, trimmed"),
    ],
)
def test_a_valid_mix_is_saved(
    memory: sqlite3.Connection, name: str, shares: tuple[int | str, int | str, int | str], saved: Mix
) -> None:
    assert save_mix(memory, name, unknown=shares[0], banger=shares[1], dud=shares[2]) == saved
    assert saved in listed(memory)


def test_a_name_is_trimmed_and_kept_as_it_was_typed(memory: sqlite3.Connection) -> None:
    """Case-sensitive, because the user typed two things; only the surrounding whitespace is the form's.
    Saving the same trimmed name again edits rather than duplicates."""
    save_mix(memory, "  Night  ", unknown=10, banger=80, dud=10)
    save_mix(memory, "night", unknown=20, banger=70, dud=10)
    save_mix(memory, "Night", unknown=0, banger=90, dud=10)

    assert listed(memory) == (
        Mix(name="Night", unknown=0, banger=90, dud=10),
        EXPLORE_MIX,
        Mix(name="night", unknown=20, banger=70, dud=10),
        REFINE_MIX,
    )
    assert delete_mix(memory, " night ") is None
    assert {mix.name for mix in listed(memory)} == {"Night", "explore", "refine"}
