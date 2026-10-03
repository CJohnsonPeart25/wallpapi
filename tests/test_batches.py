"""**Batches**: the draw, the **Draft Batch** and submitting it into the **Decision log**.

`draw` is a pure function and is tested as one, over a classified sequence a test arranges by hand. The rest
goes through `batches` alone: a real in-memory database, a real `Embeddings` falling back to hand-defined
similarities, and the fake clock. A **Draft Batch** is not the **Decision log** (marks set rather than toggle,
and only submitting appends), and a **Batch** is submitted once.
"""

from __future__ import annotations

from collections import Counter

import pytest

from tests.fakes import catalogue_of
from wallpapi.allocation import ZONE_ORDER, ScoredWallpaper, allocate, draw
from wallpapi.model import Mix, Zone
from wallpapi.rng import SeededRandom
from wallpapi.settings import EXPLORE_MIX, MIX_TOTAL, REFINE_MIX

SCORE = 50.0
"""A **Score** well clear of zero either way, for an arranged **Banger** or **Dud**."""


def classified(*, bangers: int = 0, duds: int = 0, unknowns: int = 0) -> list[ScoredWallpaper]:
    """A classified **Pool** of exactly these counts, **Bangers** first, then **Duds**, then **Unknowns**."""
    zones = [Zone.BANGER] * bangers + [Zone.DUD] * duds + [Zone.UNKNOWN] * unknowns
    scores = {Zone.BANGER: SCORE, Zone.DUD: -SCORE, Zone.UNKNOWN: 0.0}
    return [
        ScoredWallpaper(wallpaper=wallpaper, score=scores[zone], zone=zone)
        for wallpaper, zone in zip(catalogue_of(len(zones)), zones, strict=True)
    ]


def zone_counts(tiles: list[ScoredWallpaper]) -> Counter[Zone]:
    return Counter(tile.zone for tile in tiles)


# -- the draw ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("mix", [EXPLORE_MIX, REFINE_MIX], ids=["explore", "refine"])
def test_a_well_stocked_pool_gives_a_batch_exactly_its_allocation(mix: Mix) -> None:
    """With every **Zone** deep enough there is no **Shortfall**, so the draw is the **Allocation** the same
    seed rolls."""
    for seed in range(20):
        tiles = draw(mix, classified(bangers=30, duds=30, unknowns=60), 32, SeededRandom(seed))

        assert zone_counts(tiles) == Counter(allocate(mix, 32, SeededRandom(seed)))


def test_a_batch_is_drawn_under_the_mix_it_is_given() -> None:
    """Every slot guaranteed at this size, so this is the **Mix** arriving intact, not a lucky roll."""
    edited = Mix(name="explore", unknown=50, banger=45, dud=5)
    duds_only = Mix(name="duds only", unknown=0, banger=0, dud=100)
    pool = classified(bangers=30, duds=30, unknowns=60)

    assert zone_counts(draw(edited, pool, 20, SeededRandom(1))) == {
        Zone.UNKNOWN: 10,
        Zone.BANGER: 9,
        Zone.DUD: 1,
    }
    assert zone_counts(draw(duds_only, pool, 20, SeededRandom(1))) == {Zone.DUD: 20}


def test_no_wallpaper_is_shown_twice_in_one_batch() -> None:
    """A **Shortfall** taking more from a **Zone** already drawn from is the shape that would duplicate."""
    tiles = draw(EXPLORE_MIX, classified(bangers=4, duds=4, unknowns=8), 16, SeededRandom(1))

    assert len({tile.wallpaper.id for tile in tiles}) == 16


@pytest.mark.parametrize(
    ("bangers", "duds", "unknowns", "size", "expected"),
    [
        # A new Decision log: Explore's Bangers have nowhere to come from, so Unknown takes the Shortfall.
        pytest.param(0, 0, 20, 8, {Zone.UNKNOWN: 8}, id="no bangers: all unknown"),
        # Unknown, then Banger, then Dud: six Unknown slots and no Unknowns, four Bangers, two Duds.
        pytest.param(4, 20, 0, 8, {Zone.BANGER: 4, Zone.DUD: 4}, id="no unknowns: bangers before duds"),
        pytest.param(
            1, 1, 3, 32, {Zone.UNKNOWN: 3, Zone.BANGER: 1, Zone.DUD: 1}, id="a pool smaller than the batch"
        ),
    ],
)
def test_a_shortfall_is_filled_unknown_first_then_banger_then_dud(
    bangers: int, duds: int, unknowns: int, size: int, expected: dict[Zone, int]
) -> None:
    """Every **Zone** exhausted is a shorter **Batch**, never an error and never a repeat. Each tile keeps the
    **Zone** it came from, never the slot it filled."""
    tiles = draw(
        EXPLORE_MIX, classified(bangers=bangers, duds=duds, unknowns=unknowns), size, SeededRandom(1)
    )

    assert zone_counts(tiles) == Counter(expected)
    assert len({tile.wallpaper.id for tile in tiles}) == len(tiles)


def test_a_zone_that_falls_short_is_made_up_from_unknown() -> None:
    """Whatever the roll, the slots one **Banger** and no **Duds** cannot fill come back to **Unknown**: a
    **Batch** shrinks towards discovery, not towards what has already been judged."""
    for seed in range(20):
        counts = zone_counts(draw(EXPLORE_MIX, classified(bangers=1, unknowns=20), 8, SeededRandom(seed)))

        assert counts[Zone.DUD] == 0
        assert counts[Zone.BANGER] <= 1
        assert counts[Zone.UNKNOWN] == 8 - counts[Zone.BANGER]


def test_banger_slots_take_the_highest_scores() -> None:
    """Not a sample: the **Bangers** drawn are the top of the **Score** order."""
    pool = [
        ScoredWallpaper(wallpaper=s.wallpaper, score=SCORE - rank, zone=s.zone)
        if s.zone is Zone.BANGER
        else s
        for rank, s in enumerate(classified(bangers=8, unknowns=20))
    ]

    tiles = draw(REFINE_MIX, pool, 6, SeededRandom(1))

    taken = {tile.wallpaper.id for tile in tiles if tile.zone is Zone.BANGER}
    assert len(taken) >= 4
    assert taken == {w.wallpaper.id for w in pool[: len(taken)]}


def test_unknown_slots_are_sampled_rather_than_taken_in_order() -> None:
    """Different seeds differ and the same seed repeats."""

    def drawn(seed: int) -> set[str]:
        return {
            tile.wallpaper.id for tile in draw(EXPLORE_MIX, classified(unknowns=40), 8, SeededRandom(seed))
        }

    assert drawn(1) != drawn(2)
    assert drawn(1) == drawn(1)


def test_a_tiles_position_does_not_give_its_zone_away() -> None:
    """Shuffled at the end: the **Zones** are not laid out in `ZONE_ORDER` along the **Batch**."""
    orders = {
        tuple(
            tile.zone
            for tile in draw(EXPLORE_MIX, classified(bangers=8, duds=8, unknowns=8), 8, SeededRandom(s))
        )
        for s in range(20)
    }

    assert len(orders) > 1
    assert any(list(order) != sorted(order, key=ZONE_ORDER.index) for order in orders)


def test_the_long_run_zone_proportions_are_the_mix() -> None:
    """The whole draw, allocate to shuffle: fails if any step leaks a **Zone** or drops the leftover roll."""
    draws = 400
    counted: Counter[Zone] = Counter()
    for seed in range(draws):
        counted.update(
            zone_counts(
                draw(EXPLORE_MIX, classified(bangers=30, duds=30, unknowns=60), 32, SeededRandom(seed))
            )
        )

    slots = sum(counted.values())
    assert slots == draws * 32
    for zone in ZONE_ORDER:
        assert counted[zone] / slots == pytest.approx(EXPLORE_MIX.percentage(zone) / MIX_TOTAL, abs=0.01)
