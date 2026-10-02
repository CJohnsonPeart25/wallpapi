"""The varied **Unknown** draw as a pure function: one **Wallpaper** from each look-alike group. Issue #45.

`varied_order` is `allocate`'s neighbour in `allocation.py` and is tested the way `test_allocation.py`
tests that: directly, with the real `SeededRandom` and no **Pool**, storage or **Decision log** anywhere
near it. What the Core service does with it — which **Wallpapers** it hands over, and what it does when the
provider has no vectors — is `test_varied_unknowns.py`, through the seam.

Every vector here is generated. Groups are placed by hand: a random unit direction per group and a little
noise around it, so that which group a row belongs to is known independently of anything the clustering
decides, and "every group gave one" is checked against that and not against the clusters.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray

from wallpapi.allocation import varied_order
from wallpapi.rng import SeededRandom

WIDTH = 32
"""Wide enough that random group directions are nearly orthogonal, small enough to cost nothing."""

NOISE = 0.05
"""How far a member strays from its group's direction. Small against the gap between two directions."""


def grouped(sizes: Sequence[int], *, seed: int = 0) -> tuple[NDArray[np.float32], list[int]]:
    """Rows in groups of `sizes`, shuffled, and which group each row was placed in."""
    generator = np.random.default_rng(seed)
    directions = generator.normal(size=(len(sizes), WIDTH))
    labels = [group for group, size in enumerate(sizes) for _ in range(size)]
    order = generator.permutation(len(labels))
    labels = [labels[int(index)] for index in order]
    rows = directions[labels] + NOISE * generator.normal(size=(len(labels), WIDTH))
    return rows.astype(np.float32), labels


def test_every_group_gives_exactly_one_of_the_first_k() -> None:
    """Twelve clear groups and twelve **Unknown** slots: the first twelve are one from each group."""
    vectors, labels = grouped([10] * 12)

    for seed in range(20):
        order = varied_order(vectors, [100] * len(labels), 12, SeededRandom(seed))

        assert sorted(labels[index] for index in order[:12]) == list(range(12))


def test_a_wallpaper_nobody_has_favourited_is_never_picked_over_one_somebody_has() -> None:
    """The weight is `log(1 + favourites)`, which is zero at no favourites: within a group, one with any
    favourites at all is always the pick."""
    vectors, labels = grouped([6] * 4)
    favourites = [0] * len(labels)
    # The last of each group, so that taking a group's first member in the order given is not enough.
    liked = {group: len(labels) - 1 - labels[::-1].index(group) for group in range(4)}
    for index in liked.values():
        favourites[index] = 3

    for seed in range(20):
        order = varied_order(vectors, favourites, 4, SeededRandom(seed))

        assert sorted(order[:4]) == sorted(liked.values())


def test_the_favourite_count_is_a_nudge_and_not_a_ranking() -> None:
    """One group of two, at 1,000 and 10 favourites. The spec's worked figure: about 3x as likely, not 100x.

    `log(1001) / log(11)` is 2.88, so the 1,000-favourite one is picked 2.88 / 3.88 = 74% of the time.
    """
    vectors, _ = grouped([2])
    draws = 4000

    popular = sum(varied_order(vectors, [1000, 10], 1, SeededRandom(seed))[0] == 0 for seed in range(draws))

    assert abs(popular / draws - 0.742) < 0.02


def test_after_the_picks_come_the_other_embedded_then_the_unembedded_each_in_the_order_given() -> None:
    """The output is still an ordering of every **Unknown**, the contract today's draw has.

    Three groups of four, with a zero row — no **Embedding** — at every third position: the zero rows
    can never be picks and come last, and everything between keeps the order it was handed in.
    """
    vectors, _ = grouped([4, 4, 4])
    unembedded = [0, 3, 6, 9, 12]
    vectors = np.insert(vectors, [0, 2, 4, 6, 8], 0.0, axis=0)
    count = len(vectors)

    order = varied_order(vectors, [100] * count, 3, SeededRandom(7))

    picks, rest, tail = order[:3], order[3 : count - len(unembedded)], order[count - len(unembedded) :]
    assert sorted(order) == list(range(count))
    assert not set(picks) & set(unembedded)
    assert rest == [index for index in range(count) if index not in picks and index not in unembedded]
    assert tail == unembedded


def test_fewer_embedded_unknowns_than_slots_is_the_order_given() -> None:
    """Today's draw, unchanged: there are not enough positions to make `k` groups of."""
    vectors, _ = grouped([3, 3])
    vectors[[0, 2]] = 0.0

    assert varied_order(vectors, [100] * 6, 5, SeededRandom(1)) == list(range(6))
    assert varied_order(vectors, [100] * 6, 0, SeededRandom(1)) == list(range(6))


def test_the_same_seed_gives_the_same_order() -> None:
    vectors, labels = grouped([30, 8, 8, 8, 8, 8, 8, 8, 8, 6])
    favourites = [n * 37 % 500 for n in range(len(labels))]

    first = varied_order(vectors, favourites, 12, SeededRandom(3))

    assert varied_order(vectors, favourites, 12, SeededRandom(3)) == first
