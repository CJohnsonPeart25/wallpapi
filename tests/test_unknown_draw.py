"""The varied **Unknown** draw (ADR 0018): one **Wallpaper** from each look-alike group first.

`varied_order` is tested as the pure function it is, then the **Batch** `draw` makes with those positions,
a row of vectors per classified **Wallpaper**. Groups are placed by hand, a random direction each plus a
little noise, so "every group gave one" is checked against the placement and not against the clustering.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import pytest
from numpy.typing import NDArray

from tests.fakes import catalogue_of
from wallpapi.allocation import ScoredWallpaper, draw, varied_order
from wallpapi.model import Mix, Wallpaper, Zone
from wallpapi.rng import SeededRandom
from wallpapi.settings import EXPLORE_MIX

WIDTH = 32
"""Wide enough that random group directions are nearly orthogonal."""
NOISE = 0.05
ALL_UNKNOWN = Mix(name="all unknown", unknown=100, banger=0, dud=0)


def grouped(sizes: Sequence[int], *, seed: int = 0) -> tuple[NDArray[np.float32], list[int]]:
    """Rows in groups of `sizes`, shuffled, and which group each row was placed in."""
    generator = np.random.default_rng(seed)
    directions = generator.normal(size=(len(sizes), WIDTH))
    labels = [group for group, size in enumerate(sizes) for _ in range(size)]
    order = generator.permutation(len(labels))
    labels = [labels[int(index)] for index in order]
    rows = directions[labels] + NOISE * generator.normal(size=(len(labels), WIDTH))
    return rows.astype(np.float32), labels


def grouped_by_id(
    catalogue: Sequence[Wallpaper], sizes: Sequence[int], *, seed: int = 0
) -> tuple[dict[str, NDArray[np.float32]], dict[str, int]]:
    """A vector per **Wallpaper**, the first `sizes[0]` around one direction and so on, and each group."""
    generator = np.random.default_rng(seed)
    directions = generator.normal(size=(len(sizes), WIDTH))
    groups = [group for group, size in enumerate(sizes) for _ in range(size)]
    vectors = {
        w.id: (directions[group] + NOISE * generator.normal(size=WIDTH)).astype(np.float32)
        for w, group in zip(catalogue, groups, strict=False)
    }
    return vectors, {w.id: group for w, group in zip(catalogue, groups, strict=False)}


# -- the pure function -------------------------------------------------------------------------------


def test_every_group_gives_exactly_one_of_the_first_k() -> None:
    vectors, labels = grouped([10] * 12)

    for seed in range(20):
        order = varied_order(vectors, [100] * len(labels), 12, SeededRandom(seed))

        assert sorted(labels[index] for index in order[:12]) == list(range(12))


def test_a_wallpaper_nobody_has_favourited_is_never_picked_over_one_somebody_has() -> None:
    """The weight is `log(1 + favourites)`, zero at none. The liked one is the last of each group, so
    taking a group's first member in the order given would not pass."""
    vectors, labels = grouped([6] * 4)
    favourites = [0] * len(labels)
    liked = {group: len(labels) - 1 - labels[::-1].index(group) for group in range(4)}
    for index in liked.values():
        favourites[index] = 3

    for seed in range(20):
        order = varied_order(vectors, favourites, 4, SeededRandom(seed))

        assert sorted(order[:4]) == sorted(liked.values())


def test_the_favourite_count_is_a_nudge_and_not_a_ranking() -> None:
    """At 1,000 and 10 favourites, `log(1001) / log(11)` is 2.88: picked 74% of the time, not 99%."""
    vectors, _ = grouped([2])
    draws = 4000

    popular = sum(varied_order(vectors, [1000, 10], 1, SeededRandom(seed))[0] == 0 for seed in range(draws))

    assert abs(popular / draws - 0.742) < 0.02


def test_after_the_picks_come_the_other_embedded_then_the_unembedded_each_in_the_order_given() -> None:
    """Still an ordering of every **Unknown**. Zero rows, no **Embedding**, are never picks and come last."""
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
    vectors, _ = grouped([3, 3])
    vectors[[0, 2]] = 0.0

    assert varied_order(vectors, [100] * 6, 5, SeededRandom(1)) == list(range(6))
    assert varied_order(vectors, [100] * 6, 0, SeededRandom(1)) == list(range(6))


# -- the draw --------------------------------------------------------------------------------------


def drawn(
    catalogue: Sequence[Wallpaper],
    vectors: Mapping[str, NDArray[np.float32]] | None,
    size: int,
    *,
    seed: int = 1,
    mix: Mix = EXPLORE_MIX,
) -> list[str]:
    """The IDs a **Batch** of `size` shows from a **Pool** of `catalogue`, all **Unknown** since nothing is
    decided, with a zero row wherever `vectors` has none."""
    classified = [ScoredWallpaper(wallpaper=w, score=0.0, zone=Zone.UNKNOWN) for w in catalogue]
    rows = None
    if vectors is not None:
        rows = np.stack([vectors.get(w.id, np.zeros(WIDTH, dtype=np.float32)) for w in catalogue])
    return [tile.wallpaper.id for tile in draw(mix, classified, size, SeededRandom(seed), rows)]


def test_the_batch_shows_one_tile_from_each_group_before_any_unembedded_one() -> None:
    """Twelve embedded groups among thirty **Wallpapers** with no **Embedding**, and twelve slots."""
    catalogue = catalogue_of(66)
    vectors, groups = grouped_by_id(catalogue[:36], [3] * 12)

    batch = drawn(catalogue, vectors, 12, mix=ALL_UNKNOWN)

    assert sorted(groups[w] for w in batch) == list(range(12))


def test_a_lopsided_pool_is_covered_more_widely_than_by_todays_draw() -> None:
    """A third of the **Pool** in one group and the rest in fourteen small ones, **Explore** at 16, over
    the same seeds with and without vectors. "More than", as the issue asks: the claim is the direction."""
    catalogue = catalogue_of(100)
    vectors, groups = grouped_by_id(catalogue, [30] + [5] * 14)
    seeds = range(30)

    def covered(given: dict[str, NDArray[np.float32]] | None, seed: int) -> int:
        return len({groups[w] for w in drawn(catalogue, given, 16, seed=seed)})

    assert sum(covered(vectors, seed) for seed in seeds) > sum(covered(None, seed) for seed in seeds)


def test_the_same_seed_gives_the_same_batch() -> None:
    catalogue = catalogue_of(60)
    vectors, _ = grouped_by_id(catalogue, [20, 10, 10, 10, 10])

    assert drawn(catalogue, vectors, 16, seed=4) == drawn(catalogue, vectors, 16, seed=4)


def test_no_vectors_and_too_few_vectors_both_give_todays_draw() -> None:
    """No vectors, every row "no **Embedding**", and fewer embedded **Unknowns** than slots all draw the
    same **Batch** under the same seed."""
    catalogue = catalogue_of(40)
    few, _ = grouped_by_id(catalogue[:5], [5])
    nothing = {w.id: np.zeros(WIDTH, dtype=np.float32) for w in catalogue}

    batches = [drawn(catalogue, given, 16, seed=9) for given in (None, few, nothing)]

    assert batches[1] == batches[0]
    assert batches[2] == batches[0]


def test_a_pool_with_no_embeddings_draws_exactly_the_seeded_shuffle() -> None:
    """ADR 0018: with no positions the draw is the one it was before the varied draw existed. Recorded
    from the code before `vectors` stopped being optional, under the same seed."""
    assert drawn(catalogue_of(40), None, 16, seed=9) == [
        "wp0005", "wp0021", "wp0022", "wp0023", "wp0011", "wp0001", "wp0032", "wp0038",
        "wp0010", "wp0029", "wp0019", "wp0017", "wp0012", "wp0039", "wp0000", "wp0008",
    ]  # fmt: skip


def test_vectors_that_do_not_line_up_with_the_pool_are_refused() -> None:
    """A row per classified **Wallpaper** or nothing: one row short would hand a group to the wrong tile."""
    classified = [ScoredWallpaper(wallpaper=w, score=0.0, zone=Zone.UNKNOWN) for w in catalogue_of(5)]

    with pytest.raises(ValueError, match="4 vectors for 5"):
        draw(EXPLORE_MIX, classified, 4, SeededRandom(1), np.zeros((4, WIDTH), dtype=np.float32))
