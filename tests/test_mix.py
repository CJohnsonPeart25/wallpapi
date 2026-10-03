"""**Mixes**: the pure **Allocation** of slots, the draw a **Batch** gets under the active **Mix**, and
making, editing and deleting **Mixes**.

**Allocation** is a pure function and is tested as one, with a seeded source. Everything else enters through
the Core service; a **Zone** cannot be set from outside, so `tests/zoned.py` arranges the **Pool** a test
asks for.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from tests.conftest import make_harness
from tests.zoned import FAVOURED, NEAR, drawn, zone_counts, zoned_pool
from wallpapi.allocation import ZONE_ORDER, allocate
from wallpapi.model import Mix, Zone
from wallpapi.rng import SeededRandom
from wallpapi.settings import EXPLORE_MIX, MAX_MIX_NAME_LENGTH, MIX_TOTAL, REFINE_MIX, SettingsRefused

ROLLS = 4000
"""Seeded **Allocations** a distribution is read off: a 50/40/10 split is unmistakable at the tolerance."""
TOLERANCE = 0.02

DRAWS = 40
"""Whole **Pools** arranged for the long-run draw. A **Batch** cannot be drawn twice without submitting,
and submitting writes **Ignores** that move the **Zones** being counted."""

EDITED_EXPLORE = Mix(name="explore", unknown=50, banger=45, dud=5)
EXACT_BATCH = 20
"""Divides every percentage of `EDITED_EXPLORE` exactly, so a draw under it is arithmetic, not a roll."""


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


# -- the draw ----------------------------------------------------------------------------------------


def test_a_well_stocked_pool_gives_a_batch_the_shape_of_the_mix(db_path: Path) -> None:
    """The **Allocation** arriving on the page intact, read off the **Batch**'s own **Zones**."""
    counts = zone_counts(drawn(zoned_pool(db_path, bangers=30, duds=30, unknowns=60), 32))

    assert sum(counts.values()) == 32
    assert counts[Zone.UNKNOWN] >= 24
    assert counts[Zone.BANGER] >= 6
    assert counts[Zone.DUD] >= 1


def test_no_wallpaper_is_shown_twice_in_one_batch(db_path: Path) -> None:
    """A shortfall taking more from a **Zone** already drawn from is the shape that would duplicate."""
    batch = drawn(zoned_pool(db_path, bangers=4, duds=4, unknowns=8), 16)

    assert len({w.id for w in batch.wallpapers}) == 16


@pytest.mark.parametrize(
    ("bangers", "duds", "unknowns", "size", "expected"),
    [
        # A new Decision log: Explore's Bangers have nowhere to come from, so Unknown takes the shortfall.
        pytest.param(0, 0, 20, 8, {Zone.UNKNOWN: 8}, id="no bangers: all unknown"),
        # Unknown, then Banger, then Dud: six Unknown slots and no Unknowns, four Bangers, two Duds.
        pytest.param(4, 20, 0, 8, {Zone.BANGER: 4, Zone.DUD: 4}, id="no unknowns: bangers before duds"),
        pytest.param(
            1, 1, 3, 32, {Zone.UNKNOWN: 3, Zone.BANGER: 1, Zone.DUD: 1}, id="a pool smaller than the batch"
        ),
    ],
)
def test_a_shortfall_is_filled_unknown_first_then_banger_then_dud(
    db_path: Path, bangers: int, duds: int, unknowns: int, size: int, expected: dict[Zone, int]
) -> None:
    """Every **Zone** exhausted is a shorter **Batch**, never an error and never a repeat."""
    batch = drawn(zoned_pool(db_path, bangers=bangers, duds=duds, unknowns=unknowns), size)

    assert zone_counts(batch) == Counter(expected)
    assert len({w.id for w in batch.wallpapers}) == len(batch.wallpapers)


def test_a_zone_that_falls_short_is_made_up_from_unknown(db_path: Path) -> None:
    """Whatever the roll, the slots one **Banger** and no **Duds** cannot fill come back to **Unknown**: a
    **Batch** shrinks towards discovery, not towards what has already been judged."""
    counts = zone_counts(drawn(zoned_pool(db_path, bangers=1, duds=0, unknowns=20), 8))

    assert counts[Zone.DUD] == 0
    assert counts[Zone.BANGER] <= 1
    assert counts[Zone.UNKNOWN] == 8 - counts[Zone.BANGER]


def test_a_shortfall_records_the_zone_the_wallpaper_came_from(db_path: Path) -> None:
    """The tile says what the **Wallpaper** is, never the slot it filled."""
    pool = zoned_pool(db_path, bangers=0, duds=0, unknowns=20)

    batch = drawn(pool, 8)

    classified = {s.wallpaper.id: s.zone for s in pool.harness.core.classify_pool()}
    assert batch.zones == {w.id: classified[w.id] for w in batch.wallpapers}
    assert set(batch.zones.values()) == {Zone.UNKNOWN}


def test_banger_slots_take_the_highest_scores(db_path: Path) -> None:
    """Not a sample: the **Bangers** drawn are the top of the **Score** order. Graded finely enough to
    stay inside the radius, or the lowest would be **Unknowns**; **Refine** for enough slots to tell."""
    pool = zoned_pool(db_path, bangers=8, duds=0, unknowns=20)
    graded = {banger: NEAR - 0.005 * rank for rank, banger in enumerate(pool.bangers)}
    pool.harness.similarity.similarity_by_pair.update({(b, FAVOURED): value for b, value in graded.items()})
    pool.harness.core.update_settings(active_mix="refine")

    batch = drawn(pool, 6)

    ranked = sorted(graded, key=lambda banger: graded[banger], reverse=True)
    taken = {w.id for w in batch.wallpapers if batch.zones[w.id] is Zone.BANGER}
    assert len(taken) >= 4
    assert taken == set(ranked[: len(taken)])


def test_unknown_slots_are_sampled_rather_than_taken_in_order(tmp_path: Path) -> None:
    """Different seeds differ and the same seed repeats. A database per draw, since a second **Batch** off
    one file would be the live one handed back."""

    def ids_drawn(name: str, seed: int) -> set[str]:
        return {w.id for w in drawn(zoned_pool(tmp_path / name, unknowns=40, seed=seed), 8).wallpapers}

    assert ids_drawn("a.db", 1) != ids_drawn("b.db", 2)
    assert ids_drawn("c.db", 1) == ids_drawn("d.db", 1)


def test_the_long_run_zone_proportions_are_the_active_mix(tmp_path: Path) -> None:
    """The whole path, classify to record: fails if any step leaks a **Zone** or drops the leftover roll."""
    counted: Counter[Zone] = Counter()
    for seed in range(DRAWS):
        counted.update(
            zone_counts(
                drawn(zoned_pool(tmp_path / f"{seed}.db", bangers=30, duds=30, unknowns=60, seed=seed), 32)
            )
        )

    slots = sum(counted.values())
    assert slots == DRAWS * 32
    for zone in ZONE_ORDER:
        assert counted[zone] / slots == pytest.approx(EXPLORE_MIX.percentage(zone) / MIX_TOTAL, abs=0.03)


def test_switching_to_refine_changes_what_the_next_batch_is_made_of(tmp_path: Path) -> None:
    exploring = zone_counts(drawn(zoned_pool(tmp_path / "a.db", bangers=30, duds=30, unknowns=60), 32))

    refining = zoned_pool(tmp_path / "b.db", bangers=30, duds=30, unknowns=60)
    refining.harness.core.update_settings(active_mix="refine")
    counts = zone_counts(drawn(refining, 32))

    assert exploring[Zone.UNKNOWN] >= 24
    assert counts[Zone.BANGER] >= 22
    assert counts[Zone.UNKNOWN] >= 8


def test_a_mix_nobody_has_heard_of_is_refused(db_path: Path) -> None:
    """Accepted and ignored, it would leave the switcher naming a **Mix** the draw has never heard of."""
    harness = make_harness(db_path)

    refused = harness.core.update_settings(active_mix="nope")

    assert isinstance(refused, SettingsRefused)
    assert refused.reason is SettingsRefused.Reason.ACTIVE_MIX_UNKNOWN
    assert harness.core.get_settings().active_mix == "explore"


# -- making and editing ------------------------------------------------------------------------------


def test_editing_explore_changes_what_the_next_batch_is_made_of(db_path: Path) -> None:
    """Every slot guaranteed at this size, so this is the edit arriving intact, not a lucky roll."""
    pool = zoned_pool(db_path, bangers=30, duds=30, unknowns=60)

    assert pool.harness.core.save_mix("explore", unknown=50, banger=45, dud=5) == EDITED_EXPLORE

    assert pool.harness.core.active_mix() == EDITED_EXPLORE
    assert zone_counts(drawn(pool, EXACT_BATCH)) == Counter({Zone.UNKNOWN: 10, Zone.BANGER: 9, Zone.DUD: 1})


def test_editing_a_mix_does_not_change_which_one_is_active(db_path: Path) -> None:
    harness = make_harness(db_path)

    harness.core.save_mix("refine", unknown=10, banger=90, dud=0)

    assert harness.core.active_mix() == EXPLORE_MIX
    assert Mix(name="refine", unknown=10, banger=90, dud=0) in harness.core.list_mixes()


def test_a_custom_mix_can_be_made_selected_and_drawn_under(db_path: Path) -> None:
    pool = zoned_pool(db_path, bangers=30, duds=30, unknowns=60)

    made = pool.harness.core.save_mix("duds only", unknown=0, banger=0, dud=100)
    pool.harness.core.update_settings(active_mix="duds only")

    assert made == Mix(name="duds only", unknown=0, banger=0, dud=100)
    assert pool.harness.core.active_mix() == made
    assert zone_counts(drawn(pool, EXACT_BATCH)) == Counter({Zone.DUD: EXACT_BATCH})


def test_mixes_and_the_active_one_survive_a_restart(db_path: Path) -> None:
    """Stored rather than recomputed in Python: an edit, a custom **Mix**, a deleted one and the switch."""
    first = make_harness(db_path)
    first.core.save_mix("explore", unknown=50, banger=45, dud=5)
    first.core.save_mix("all in", unknown=0, banger=100, dud=0)
    first.core.save_mix("gone", unknown=0, banger=0, dud=100)
    assert first.core.delete_mix("gone") is None
    first.core.update_settings(active_mix="refine")

    restarted = make_harness(db_path)

    assert restarted.core.list_mixes() == (
        Mix(name="all in", unknown=0, banger=100, dud=0),
        EDITED_EXPLORE,
        REFINE_MIX,
    )
    assert restarted.core.active_mix() == REFINE_MIX


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
    db_path: Path, deleted: str, active: str, reason: SettingsRefused.Reason
) -> None:
    harness = make_harness(db_path)
    harness.core.save_mix("duds only", unknown=0, banger=0, dud=100)
    harness.core.update_settings(active_mix=active)
    before = harness.core.list_mixes()

    refused = harness.core.delete_mix(deleted)

    assert isinstance(refused, SettingsRefused)
    assert refused.reason is reason
    assert harness.core.list_mixes() == before
    assert harness.core.active_mix().name == active


def test_a_custom_mix_that_is_not_active_can_be_deleted(db_path: Path) -> None:
    harness = make_harness(db_path)
    harness.core.save_mix("duds only", unknown=0, banger=0, dud=100)

    assert harness.core.delete_mix("duds only") is None
    assert harness.core.list_mixes() == (EXPLORE_MIX, REFINE_MIX)


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
    db_path: Path, name: str, shares: tuple[int | str, int | str, int | str], reason: SettingsRefused.Reason
) -> None:
    """The table is checked as well as the refusal, because a validator that refused after writing would
    pass on the refusal alone. One rule, `validated_mix`, which also drops a hand-edited row."""
    harness = make_harness(db_path)

    refused = harness.core.save_mix(name, unknown=shares[0], banger=shares[1], dud=shares[2])

    assert isinstance(refused, SettingsRefused)
    assert refused.reason is reason
    assert harness.core.list_mixes() == (EXPLORE_MIX, REFINE_MIX)


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
    db_path: Path, name: str, shares: tuple[int | str, int | str, int | str], saved: Mix
) -> None:
    harness = make_harness(db_path)

    assert harness.core.save_mix(name, unknown=shares[0], banger=shares[1], dud=shares[2]) == saved
    assert saved in harness.core.list_mixes()


def test_a_name_is_trimmed_and_kept_as_it_was_typed(db_path: Path) -> None:
    """Case-sensitive, because the user typed two things; only the surrounding whitespace is the form's.
    Saving the same trimmed name again edits rather than duplicates."""
    harness = make_harness(db_path)

    harness.core.save_mix("  Night  ", unknown=10, banger=80, dud=10)
    harness.core.save_mix("night", unknown=20, banger=70, dud=10)
    harness.core.save_mix("Night", unknown=0, banger=90, dud=10)

    assert harness.core.list_mixes() == (
        Mix(name="Night", unknown=0, banger=90, dud=10),
        EXPLORE_MIX,
        Mix(name="night", unknown=20, banger=70, dud=10),
        REFINE_MIX,
    )
    assert harness.core.delete_mix(" night ") is None
    assert {mix.name for mix in harness.core.list_mixes()} == {"Night", "explore", "refine"}
