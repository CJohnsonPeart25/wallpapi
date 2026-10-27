"""The draw by wear (ADR 0028), as the pure function `allocation.draw` is: classified **Wallpapers** built by
hand, the real `SeededRandom`, no **Pool**. A **Line-up** passes a wear per **Wallpaper**; a **Batch** passes
none, and its own draw tests (`test_unknown_draw.py`, `test_batches.py`) stand untouched.

Wear decides which **Wallpaper** fills a **Slot**, never how many **Slots** a **Zone** gets: **Bangers** by
**Score** over one plus wear, **Unknowns** and **Duds** fewest wear first and their own order within one
count. What wear is made of, and reading it from the **Evidence log**, are `test_lineup.py`'s.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence

import numpy as np
import pytest
from numpy.typing import NDArray

from tests.fakes import catalogue_of, wallpaper
from wallpapi.allocation import ScoredWallpaper, Wear, draw
from wallpapi.model import Mix, Zone
from wallpapi.settings import EXPLORE_MIX
from wallpapi.taste.rng import SeededRandom

ONE_BANGER = Mix(name="one banger", unknown=50, banger=50, dud=0)
"""At a size of two, exactly one **Banger** **Slot** and one **Unknown**, nothing rolled."""
ALL_UNKNOWN = Mix(name="all unknown", unknown=100, banger=0, dud=0)
ALL_DUD = Mix(name="all dud", unknown=0, banger=0, dud=100)
SEEDS = range(30)
WIDTH = 32


def scored(zone: Zone, *ids: str, score: float = 0.0) -> list[ScoredWallpaper]:
    return [ScoredWallpaper(wallpaper=wallpaper(w), score=score, zone=zone) for w in ids]


def bangers(**scores: float) -> list[ScoredWallpaper]:
    return [ScoredWallpaper(wallpaper=wallpaper(w), score=s, zone=Zone.BANGER) for w, s in scores.items()]


def drawn(
    mix: Mix,
    classified: Sequence[ScoredWallpaper],
    size: int,
    *,
    seed: int = 1,
    vectors: NDArray[np.float32] | None = None,
    wear: Wear | None = None,
) -> list[str]:
    return [tile.wallpaper.id for tile in draw(mix, classified, size, SeededRandom(seed), vectors, wear=wear)]


# -- Bangers -------------------------------------------------------------------------------------------------


def test_bangers_at_nine_five_and_three_tenths_come_a_then_b_then_a_as_they_wear() -> None:
    """The ticket's case, one **Line-up** at a time: A unworn is 0.9; A once shown is 0.45 against B's 0.5;
    then A at 0.45 beats B at 0.25 and C at 0.3."""
    pool = [*bangers(a=0.9, b=0.5, c=0.3), *scored(Zone.UNKNOWN, "u1", "u2", "u3")]

    def banger(wear: Wear) -> str:
        (shown,) = [w for w in drawn(ONE_BANGER, pool, 2, wear=wear) if w in "abc"]
        return shown

    assert [banger({}), banger({"a": 1}), banger({"a": 1, "b": 1})] == ["a", "b", "a"]


def test_bangers_equal_over_their_wear_are_ordered_by_the_seed() -> None:
    """0.5 shown once and 0.25 never shown are both 0.25: each seed decides, and both turn up."""
    pool = [*bangers(a=0.5, b=0.25), *scored(Zone.UNKNOWN, "u1")]

    shown_a = {"a" in drawn(ONE_BANGER, pool, 2, seed=seed, wear={"a": 1}) for seed in SEEDS}

    assert shown_a == {True, False}


# -- Unknowns and Duds -------------------------------------------------------------------------------------


@pytest.mark.parametrize(("mix", "zone"), [(ALL_UNKNOWN, Zone.UNKNOWN), (ALL_DUD, Zone.DUD)])
def test_an_unknown_or_a_dud_with_no_wear_is_drawn_before_one_with_wear(mix: Mix, zone: Zone) -> None:
    """Ten of a **Zone**, six worn once or more: four **Slots** take exactly the four unworn, whatever the
    seed; six take those four and then the two worn least."""
    pool = scored(zone, *(f"w{n}" for n in range(10)), score=-1.0 if zone is Zone.DUD else 0.0)
    wear = {"w0": 2, "w1": 1, "w2": 3, "w4": 1, "w6": 2, "w8": 5}

    for seed in SEEDS:
        assert sorted(drawn(mix, pool, 4, seed=seed, wear=wear)) == ["w3", "w5", "w7", "w9"]
        assert sorted(drawn(mix, pool, 6, seed=seed, wear=wear)) == ["w1", "w3", "w4", "w5", "w7", "w9"]


@pytest.mark.parametrize(("mix", "zone"), [(ALL_UNKNOWN, Zone.UNKNOWN), (ALL_DUD, Zone.DUD)])
def test_within_one_wear_count_an_unknown_or_dud_is_still_drawn_by_the_seed(mix: Mix, zone: Zone) -> None:
    pool = scored(zone, *(f"w{n}" for n in range(10)), score=-1.0 if zone is Zone.DUD else 0.0)

    shown = {frozenset(drawn(mix, pool, 2, seed=seed, wear={"w0": 1})) for seed in SEEDS}

    assert not any("w0" in pair for pair in shown)
    assert len(shown) > 10, "nine unworn give 36 pairs, and thirty seeds should find many"


def grouped(groups: int, size: int) -> NDArray[np.float32]:
    """`groups` x `size` rows, group by group, each group around its own random direction."""
    generator = np.random.default_rng(0)
    directions = generator.normal(size=(groups, WIDTH))
    rows = np.repeat(directions, size, axis=0) + 0.05 * generator.normal(size=(groups * size, WIDTH))
    return rows.astype(np.float32)


def test_the_unworn_unknowns_are_still_spread_one_per_look_alike_group() -> None:
    """ADR 0018 inside the fewest-wear count: four groups of five, two of each worn, and four **Slots**. Every
    tile is unworn and every group gives one."""
    pool = scored(Zone.UNKNOWN, *(f"g{group}m{member}" for group in range(4) for member in range(5)))
    wear = {f"g{group}m{member}": 1 for group in range(4) for member in (0, 3)}

    for seed in SEEDS:
        shown = drawn(ALL_UNKNOWN, pool, 4, seed=seed, vectors=grouped(4, 5), wear=wear)
        assert not set(shown) & set(wear)
        assert sorted(w[:2] for w in shown) == ["g0", "g1", "g2", "g3"]


def test_the_count_that_fills_the_last_slots_is_grouped_by_the_slots_left() -> None:
    """Two unworn **Unknowns**, three **Slots**, and the rest worn once in three groups. Both unworn are
    shown, and the varied draw over the worn is asked for the one **Slot** left: one group, so its one pick
    is the only worn **Wallpaper** anybody has favourited (ADR 0018's weight). Asked for three, it would
    hand the **Slot** to whichever group it seeded first."""
    names = [f"g{group}m{member}" for group in range(3) for member in range(4)]
    pool = [
        ScoredWallpaper(wallpaper(w, favourites=1000 if w == "g1m2" else 0), score=0.0, zone=Zone.UNKNOWN)
        for w in names
    ]
    wear = {w: 0 if w in ("g0m0", "g0m1") else 1 for w in names}

    for seed in SEEDS:
        shown = drawn(ALL_UNKNOWN, pool, 3, seed=seed, vectors=grouped(3, 4), wear=wear)
        assert sorted(shown) == ["g0m0", "g0m1", "g1m2"]


def test_a_count_reached_once_the_slots_are_full_is_not_grouped_and_spends_no_random_number() -> None:
    """**Explore** at sixteen asks for twelve or thirteen **Unknowns**, and fourteen are unworn. The sixteen
    worn, enough to cluster, never are: the same seed draws the same screen whether or not they have
    **Embeddings**."""
    classified, vectors = mixed_pool(unknowns=30)
    wear = {s.wallpaper.id: 1 for s in classified[:16]}
    unplaced = vectors.copy()
    unplaced[:16] = 0.0

    for seed in SEEDS:
        placed = drawn(EXPLORE_MIX, classified, 16, seed=seed, vectors=vectors, wear=wear)
        assert drawn(EXPLORE_MIX, classified, 16, seed=seed, vectors=unplaced, wear=wear) == placed


# -- what wear leaves alone -------------------------------------------------------------------------------


def mixed_pool(unknowns: int = 20) -> tuple[list[ScoredWallpaper], NDArray[np.float32]]:
    """`unknowns` **Unknowns** in look-alike groups of five, twelve **Bangers** at spread **Scores** and
    eight **Duds**, with a vector each."""
    catalogue = catalogue_of(unknowns + 20)
    zones = [Zone.UNKNOWN] * unknowns + [Zone.BANGER] * 12 + [Zone.DUD] * 8
    scores = [0.0] * unknowns + [0.1 * (n + 1) for n in range(12)] + [-1.0] * 8
    classified = [
        ScoredWallpaper(wallpaper=w, score=s, zone=z)
        for w, s, z in zip(catalogue, scores, zones, strict=True)
    ]
    vectors = np.concatenate([grouped(unknowns // 5, 5), grouped(4, 5)])
    return classified, vectors


def test_no_wear_and_wear_of_nothing_draw_exactly_what_a_batch_draws() -> None:
    """A **Batch** passes no wear. Passing none, an empty one or a zero for every **Wallpaper** spends the
    same random numbers on the same draw."""
    classified, vectors = mixed_pool()
    zeros: Mapping[str, float] = {s.wallpaper.id: 0 for s in classified}

    for seed in SEEDS:
        batch = drawn(EXPLORE_MIX, classified, 16, seed=seed, vectors=vectors)
        assert drawn(EXPLORE_MIX, classified, 16, seed=seed, vectors=vectors, wear={}) == batch
        assert drawn(EXPLORE_MIX, classified, 16, seed=seed, vectors=vectors, wear=zeros) == batch


def test_wear_never_changes_how_many_slots_a_zone_gets() -> None:
    """Every **Banger** worn hard and half the rest worn too: the same seed allocates the same **Slots**."""
    classified, vectors = mixed_pool()
    worn = {s.wallpaper.id: 7 for s in classified[::2]}

    for seed in SEEDS:
        plain = draw(EXPLORE_MIX, classified, 16, SeededRandom(seed), vectors)
        by_wear = draw(EXPLORE_MIX, classified, 16, SeededRandom(seed), vectors, wear=worn)
        assert Counter(tile.slot for tile in by_wear) == Counter(tile.slot for tile in plain)
        assert Counter(tile.zone for tile in by_wear) == Counter(tile.zone for tile in plain)


def test_a_shortfall_is_made_up_by_wear_as_well() -> None:
    """Two **Banger** **Slots** and one **Banger**: the **Unknown** that makes up the **Shortfall** is the
    unworn one."""
    pool = [*bangers(a=0.9), *scored(Zone.UNKNOWN, "u1", "u2", "u3")]
    two_bangers = Mix(name="bangers", unknown=0, banger=100, dud=0)

    for seed in SEEDS:
        assert sorted(drawn(two_bangers, pool, 2, seed=seed, wear={"u1": 1, "u3": 2})) == ["a", "u2"]


def test_a_negative_wear_is_refused() -> None:
    """Wear is how much a **Wallpaper** should wait; below zero would divide a **Score** by nothing."""
    with pytest.raises(ValueError, match="wear of -1"):
        draw(EXPLORE_MIX, bangers(a=0.9), 1, SeededRandom(1), wear={"a": -1})
