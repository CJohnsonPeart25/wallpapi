"""**Allocation**: turning a **Mix** into **Batch** slots, and ordering the **Unknowns**. Pure arithmetic, all
in integers.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray

from wallpapi.model import Mix, Zone
from wallpapi.rng import SeededRandom

ZONE_ORDER: tuple[Zone, ...] = (Zone.UNKNOWN, Zone.BANGER, Zone.DUD)
"""The order the **Zones** are visited in and a shortfall is filled from. Fixed, so a seed allocates the same
way.
"""

PERCENT = 100


def allocate(mix: Mix, size: int, random: SeededRandom) -> dict[Zone, int]:
    """How many of a **Batch** of `size` each **Zone** is asked for; every **Zone** is present and the counts
    sum to `size`.

    Whole slots are guaranteed; leftovers are rolled by the remainders, which keeps each **Zone**'s expected
    share exact.
    """
    if size <= 0:
        return dict.fromkeys(ZONE_ORDER, 0)

    guaranteed: dict[Zone, int] = {}
    remainders: dict[Zone, int] = {}
    for zone in ZONE_ORDER:
        whole, over = divmod(mix.percentage(zone) * size, PERCENT)
        guaranteed[zone] = whole
        remainders[zone] = over

    leftover = size - sum(guaranteed.values())
    for _ in range(leftover):
        guaranteed[_rolled(remainders, random)] += 1
    return guaranteed


def _rolled(remainders: dict[Zone, int], random: SeededRandom) -> Zone:
    """One **Zone**, in proportion to its remainder: a cumulative walk, so the seed alone reproduces the
    draw.
    """
    total = sum(remainders.values())
    # Only reachable with no leftover slot to roll; answered rather than divided by zero.
    if total <= 0:
        return ZONE_ORDER[0]
    threshold = random.fraction() * total
    running = 0
    for zone in ZONE_ORDER:
        running += remainders[zone]
        if threshold < running:
            return zone
    # Only the rounding of the multiply gets here.
    return ZONE_ORDER[-1]


KMEANS_ITERATIONS = 10
"""Clustering rounds for the varied draw: fixed, so the work is bounded and a seed spends the same numbers."""


def varied_order(
    vectors: NDArray[np.float32], favourites: Sequence[int], k: int, random: SeededRandom
) -> list[int]:
    """The **Unknowns** in draw order, one from each of `k` look-alike groups first (ADR 0018).

    `vectors` has a zero row for no **Embedding**. A permutation of the rows: a pick per cluster, then the
    other embedded rows, then the unembedded.
    """
    norms = np.linalg.norm(vectors, axis=1)
    embedded = [index for index in range(len(vectors)) if norms[index] > 0.0]
    if k <= 0 or len(embedded) < k:
        return list(range(len(vectors)))

    # float64, so an argmax between near-equal cosines is not decided by float32 rounding.
    points = (vectors[embedded] / norms[embedded, None]).astype(np.float64)
    clusters = _spherical_kmeans(points, k, random)
    # A cluster is empty only when rows duplicate each other; its slot comes from `rest`.
    picks = [
        embedded[weighted_order(members, [math.log1p(favourites[embedded[m]]) for m in members], random)[0]]
        for members in clusters
        if members
    ]
    picked = set(picks)
    rest = [index for index in embedded if index not in picked]
    unembedded = [index for index in range(len(vectors)) if norms[index] <= 0.0]
    return picks + rest + unembedded


def _spherical_kmeans(points: NDArray[np.float64], k: int, random: SeededRandom) -> list[list[int]]:
    """`k` clusters of unit-length `points` by cosine. An empty centre stays put: re-seeding at the farthest
    point picks outliers.
    """
    centres = points[_kmeans_plus_plus(points, k, random)]
    assigned = np.zeros(len(points), dtype=np.int64)
    for _ in range(KMEANS_ITERATIONS):
        assigned = np.argmax(points @ centres.T, axis=1)
        sums = np.zeros_like(centres)
        np.add.at(sums, assigned, points)
        lengths = np.linalg.norm(sums, axis=1)
        moved = lengths > 0.0
        centres[moved] = sums[moved] / lengths[moved, None]
    return [np.flatnonzero(assigned == cluster).tolist() for cluster in range(k)]


def _kmeans_plus_plus(points: NDArray[np.float64], k: int, random: SeededRandom) -> list[int]:
    """`k` distinct starting rows: the first uniform, each next in proportion to squared cosine distance."""
    chosen = [min(int(random.fraction() * len(points)), len(points) - 1)]
    nearest = 1.0 - points @ points[chosen[0]]
    while len(chosen) < k:
        weights = np.clip(nearest, 0.0, None) ** 2
        weights[chosen] = 0.0
        chosen.append(_proportional(weights, chosen, random))
        nearest = np.minimum(nearest, 1.0 - points @ points[chosen[-1]])
    return chosen


def _proportional(weights: NDArray[np.float64], chosen: Sequence[int], random: SeededRandom) -> int:
    """One index drawn in proportion to `weights`, never one already in `chosen`."""
    total = float(weights.sum())
    if total <= 0.0:
        # Every row left duplicates one already drawn.
        remaining = [index for index in range(len(weights)) if index not in set(chosen)]
        return remaining[min(int(random.fraction() * len(remaining)), len(remaining) - 1)]
    cumulative = np.cumsum(weights)
    index = int(np.searchsorted(cumulative, random.fraction() * total, side="right"))
    # Past the end only if the multiply rounds up to `total`.
    return index if index < len(weights) else int(np.flatnonzero(weights)[-1])


def weighted_order[T](items: Sequence[T], weights: Sequence[float], random: SeededRandom) -> list[T]:
    """A random permutation of `items` in which a heavier item tends to come sooner; weight zero sorts
    last.
    """
    keyed = [
        (_key(random.fraction(), weight), index, item)
        for index, (item, weight) in enumerate(zip(items, weights, strict=True))
    ]
    # `index` breaks ties, so `sort` never compares the items.
    keyed.sort(key=lambda entry: (-entry[0], entry[1]))
    return [item for _, _, item in keyed]


def _key(uniform: float, weight: float) -> float:
    if weight <= 0.0:
        return 0.0
    return uniform ** (1.0 / weight)
