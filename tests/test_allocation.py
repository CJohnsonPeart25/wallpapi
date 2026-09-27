"""**Allocation** itself: slots out of a **Mix**, and the order a **Zone** gives its **Wallpapers** up in.

Issue #10. These are unit tests of two pure functions and a validator, which is the exception invariant 1
already makes for `wait_needed` in `test_ratelimit.py`: there is no storage, no **Pool** and no
**Decision log** anywhere near them, and a test that went through the Core service to reach them would be
testing the draw instead. The draw is `test_mix.py`, and it enters through the seam like everything else.

The random source is the real `SeededRandom` with a fixed seed, never a fake — the spec asks for a seeded
source, so a run of these is reproducible by construction.
"""

from __future__ import annotations

from collections import Counter

import pytest

from wallpapi.allocation import ZONE_ORDER, allocate, weighted_order
from wallpapi.core import EXPLORE_MIX, MIX_TOTAL, REFINE_MIX, SettingsRefused, validated_mix
from wallpapi.model import Mix, Zone
from wallpapi.rng import SeededRandom

ROLLS = 4000
"""How many seeded **Allocations** a distribution is read off.

Large enough that a 50/40/10 split is unmistakable at the two-percentage-point tolerance below, and small
enough to run in a moment. Every one of them is a fresh `SeededRandom(n)`, so the whole assertion is a
deterministic function of this number and nothing else.
"""

TOLERANCE = 0.02


def _rolled(mix: Mix, size: int) -> Counter[Zone]:
    """How the slots fell across `ROLLS` **Allocations**, one seed each."""
    counted: Counter[Zone] = Counter()
    for seed in range(ROLLS):
        counted.update(allocate(mix, size, SeededRandom(seed)))
    return counted


def test_the_whole_number_slots_are_guaranteed_at_a_large_batch() -> None:
    """The acceptance criterion at 32 in **Explore**: 24 / 6 / 1, and exactly one slot left to roll.

    `32 * 75 / 100` is 24 exactly, `32 * 20 / 100` is 6.4 and `32 * 5 / 100` is 1.6 — so 31 of the 32
    slots are settled before any random number is spent, and no seed can move them.
    """
    for seed in range(50):
        slots = allocate(EXPLORE_MIX, 32, SeededRandom(seed))

        assert sum(slots.values()) == 32
        assert slots[Zone.UNKNOWN] >= 24
        assert slots[Zone.BANGER] >= 6
        assert slots[Zone.DUD] >= 1
        # Exactly one slot above the guaranteed floor, never two: the remainders sum to one whole slot.
        assert sum(slots.values()) - (24 + 6 + 1) == 1


def test_the_one_leftover_slot_at_thirty_two_is_rolled_by_the_remainders() -> None:
    """The remainders at 32 in **Explore** are 0, 0.4 and 0.6, so the spare slot never goes to **Unknown**.

    The complement of the test above: knowing 31 slots are fixed says nothing about where the
    thirty-second goes, and "proportional to the fractional remainders" is a claim with a number in it.
    """
    counted = _rolled(EXPLORE_MIX, 32)
    spare = {zone: counted[zone] / ROLLS - floor for zone, floor in ((Zone.BANGER, 6), (Zone.DUD, 1))}

    assert counted[Zone.UNKNOWN] == 24 * ROLLS
    assert spare[Zone.BANGER] == pytest.approx(0.4, abs=TOLERANCE)
    assert spare[Zone.DUD] == pytest.approx(0.6, abs=TOLERANCE)


def test_a_batch_of_two_is_one_unknown_and_a_fifty_forty_ten_roll() -> None:
    """The acceptance criterion at the other end. `2 * 75 / 100` is 1.5, `0.4` and `0.1`.

    One **Unknown** is guaranteed and the second slot is rolled at 50 / 40 / 10 — which is the case where
    the roll is the whole story, and the one that would look like a bug if the remainders were normalised
    wrongly: 0.5 / 0.4 / 0.1 already sums to the one slot there is to give away.
    """
    counted = _rolled(EXPLORE_MIX, 2)
    rolled = {zone: (counted[zone] - (ROLLS if zone is Zone.UNKNOWN else 0)) / ROLLS for zone in ZONE_ORDER}

    assert counted[Zone.UNKNOWN] >= ROLLS
    assert rolled[Zone.UNKNOWN] == pytest.approx(0.5, abs=TOLERANCE)
    assert rolled[Zone.BANGER] == pytest.approx(0.4, abs=TOLERANCE)
    assert rolled[Zone.DUD] == pytest.approx(0.1, abs=TOLERANCE)


def test_the_long_run_share_of_each_zone_is_the_mix() -> None:
    """Over many **Allocations** each **Zone** gets exactly the percentage the **Mix** names.

    Both **Mixes**, and at a size where every **Zone** has a remainder to roll, so nothing about this is
    carried by the guaranteed slots alone. This is what "unbiased" means in `allocate`'s docstring: the
    expected share is the percentage, not merely close to it.
    """
    for mix in (EXPLORE_MIX, REFINE_MIX):
        size = 7
        counted = _rolled(mix, size)
        for zone in ZONE_ORDER:
            share = counted[zone] / (ROLLS * size)
            assert share == pytest.approx(mix.percentage(zone) / MIX_TOTAL, abs=TOLERANCE)


def test_the_slots_always_add_up_to_the_batch_that_was_asked_for() -> None:
    """Every size from one to the maximum, under both **Mixes**. A **Batch** short of a slot is a tile
    missing off the page, and it would go unnoticed at exactly the sizes nobody tries by hand."""
    for mix in (EXPLORE_MIX, REFINE_MIX):
        for size in range(1, 65):
            slots = allocate(mix, size, SeededRandom(size))

            assert sum(slots.values()) == size
            assert set(slots) == set(ZONE_ORDER)
            assert all(count >= 0 for count in slots.values())


def test_a_mix_that_divides_exactly_leaves_nothing_to_roll() -> None:
    """At 20 in **Explore** every product is a whole number, so the same **Batch** comes out under any
    seed. The case where the leftover loop must not run at all — and the one where a floating-point
    `floor` would be free to say 14 instead of 15."""
    allocations = {tuple(allocate(EXPLORE_MIX, 20, SeededRandom(seed)).items()) for seed in range(50)}

    assert allocations == {((Zone.UNKNOWN, 15), (Zone.BANGER, 4), (Zone.DUD, 1))}


def test_a_batch_of_nothing_allocates_nothing() -> None:
    """Not reachable through the seam — the batch size is at least one — but `allocate` is a public
    function and "no slots" is the only sane answer to "no **Batch**"."""
    assert allocate(EXPLORE_MIX, 0, SeededRandom(1)) == dict.fromkeys(ZONE_ORDER, 0)


def test_the_same_seed_allocates_the_same_way_and_a_different_one_need_not() -> None:
    """Determinism under the seed, which is the acceptance criterion every other test here rests on."""
    first = allocate(EXPLORE_MIX, 32, SeededRandom(7))
    again = allocate(EXPLORE_MIX, 32, SeededRandom(7))
    assert first == again

    differing = {tuple(allocate(EXPLORE_MIX, 32, SeededRandom(seed)).items()) for seed in range(20)}
    assert len(differing) > 1


def test_a_draw_order_is_a_permutation_and_a_seed_reproduces_it() -> None:
    """`weighted_order` gives back everything it was given, once each, in an order the seed fixes.

    Everything one **Zone** holds, in the order it will give them up in — so anything lost here is a
    **Wallpaper** that can never be drawn, and anything duplicated is one that can be drawn twice.
    """
    items = [f"wp{n:02d}" for n in range(30)]
    weights = [1.0] * len(items)

    ordered = weighted_order(items, weights, SeededRandom(3))

    assert sorted(ordered) == sorted(items)
    assert weighted_order(items, weights, SeededRandom(3)) == ordered
    assert weighted_order(items, weights, SeededRandom(4)) != ordered


def test_equal_weights_are_a_plain_shuffle() -> None:
    """At the 1.0 every **Wallpaper** carries today, no position is favoured.

    The property the **Unknown** and **Dud** slots depend on being "sampled at random", and the baseline
    #11 will move away from deliberately rather than by accident.
    """
    firsts = Counter(
        weighted_order(["a", "b", "c", "d"], [1.0] * 4, SeededRandom(seed))[0] for seed in range(ROLLS)
    )

    for item in "abcd":
        assert firsts[item] / ROLLS == pytest.approx(0.25, abs=TOLERANCE)


def test_a_heavier_item_comes_sooner() -> None:
    """The seam #11 lands on, exercised at a weight that is not 1.0.

    Nothing in wallpapi passes anything but 1.0 today, so without this the seam would be an untested
    claim in a docstring. Ten to one is a ten-to-one favourite for the first place.
    """
    firsts = Counter(
        weighted_order(["heavy", "light"], [10.0, 1.0], SeededRandom(seed))[0] for seed in range(ROLLS)
    )

    assert firsts["heavy"] / ROLLS > 0.8


def test_a_weight_of_nothing_goes_last_rather_than_dividing_by_zero() -> None:
    """Zero is the limit of the weighting, not a crash. #11's revisit weight is a setting, and a setting
    that can be typed can be typed as nought."""
    ordered = weighted_order(["gone", "here", "also"], [0.0, 1.0, 1.0], SeededRandom(1))

    assert ordered[-1] == "gone"


def test_a_mix_must_be_three_whole_percentages_that_sum_to_a_hundred() -> None:
    """The validator, which is the one place that decides what a **Mix** is.

    Called here directly rather than through a storage path, because there is no storage path: nothing
    writes to the `mixes` table but the migration that seeds it, and creating and editing **Mixes** is
    #12. This is the rule that ticket will build its form on.
    """
    assert validated_mix("custom", unknown=50, banger=30, dud=20) == Mix(
        name="custom", unknown=50, banger=30, dud=20
    )
    # A **Zone** at nothing is a **Mix** somebody may well want — **Refine** with no **Duds** at all.
    assert validated_mix("pure", unknown=0, banger=100, dud=0) is not None

    for shares in ((30, 30, 30), (50, 50, 50), (100, 1, 0), (0, 0, 0), (-10, 100, 10)):
        assert (
            validated_mix("wrong", unknown=shares[0], banger=shares[1], dud=shares[2])
            is SettingsRefused.Reason.MIX_PERCENTAGES_INVALID
        )


def test_a_mix_is_refused_rather_than_rounded() -> None:
    """Text is accepted because the form at #12 will post text, but only text that is a whole number.

    "33.4" is refused outright rather than becoming 33, for the reason the batch size refuses "1.5": a
    **Mix** the user did not type is worse than being told to type whole percentages.
    """
    assert validated_mix(" tidy ", unknown="75", banger="20", dud="5") == Mix(
        name="tidy", unknown=75, banger=20, dud=5
    )

    for typed in ("33.4", "", "lots", "20%"):
        assert (
            validated_mix("wrong", unknown=typed, banger="20", dud="5")
            is SettingsRefused.Reason.MIX_PERCENTAGES_INVALID
        )


def test_a_mix_needs_a_name() -> None:
    """A **Mix** is switched to by name, so a blank one is a button with nothing on it."""
    assert validated_mix("   ", unknown=75, banger=20, dud=5) is SettingsRefused.Reason.MIX_NAME_INVALID
