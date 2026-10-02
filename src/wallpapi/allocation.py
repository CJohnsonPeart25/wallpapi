"""**Allocation**: turning a **Mix** into **Batch** slots.

Pure functions and nothing else. Nothing here knows what a **Pool** is, what a **Score** is or where a
**Mix** was stored — `allocate` is arithmetic over three percentages. That is what lets the Core service's
draw be read as three plain steps (allocate, order, fill) and lets every one of them be pinned under a
seed. The order is the Core service's: a shuffle, for **Bangers** a sort by **Score** over it, and for
**Unknowns** `varied_order` over it (#45), which puts one from each look-alike group first. Like
`allocate`, that is arithmetic — over an array of positions — and knows nothing of where they came from.

**The rule.** A **Zone**'s whole-number slots are guaranteed: `floor(percentage * size / 100)`. Those
never add up to the whole **Batch** unless every product divides by 100, so the slots left over are rolled
one at a time, each roll weighted by the fractional remainders the flooring threw away. A **Batch** of 32
in **Explore** is 24 **Unknown**, 6 **Bangers** and 1 **Dud** guaranteed, with the thirty-second slot
rolled at 0 / 40 / 60; a **Batch** of 2 is 1 **Unknown** guaranteed and one slot rolled at 50 / 40 / 10.

All of it in integer arithmetic, `divmod(percentage * size, 100)`. The obvious spelling is
`floor(percentage * size / 100)`, and the obvious worry about it is a product that ought to divide exactly
coming out at 23.999999999999996 and flooring to 23. It does not — a correctly rounded division of two
exact integers lands on the integer when there is one — but "it happens to be exact" is a worse thing to
depend on than not dividing at all.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray

from wallpapi.model import Mix, Zone
from wallpapi.rng import SeededRandom

ZONE_ORDER: tuple[Zone, ...] = (Zone.UNKNOWN, Zone.BANGER, Zone.DUD)
"""The order the three **Zones** are visited in, and — the same tuple because it is the same decision —
the order a shortfall is filled from.

**Unknown**, then **Banger**, then **Dud**, as the spec asks. It reads as a preference for discovery, and
it has a second effect worth saying out loud: with nothing **Favourited** yet there are no **Bangers** at
all, so a **Batch** falls back to being entirely **Unknown** rather than to being padded with **Duds**.
**Dud** is last because a **Wallpaper** the **Decision log** leans against is the one thing the user has
already said something about.

Fixed rather than derived from the **Mix**, so that a seeded run allocates the same way whatever the
percentages are.
"""

PERCENT = 100


def allocate(mix: Mix, size: int, random: SeededRandom) -> dict[Zone, int]:
    """How many of a **Batch** of `size` each **Zone** is asked for.

    Every **Zone** is present in the result, including the ones allocated nothing — a caller that has to
    ask whether a key exists is a caller that will one day forget to. The counts sum to exactly `size`.

    The leftover slots are rolled independently, each weighted by the remainders as they were before any
    rolling. That keeps the allocation *unbiased*: a **Zone**'s expected share is exactly
    `percentage * size / 100`, because the remainders sum to the number of leftover slots and so
    `leftover * remainder / sum(remainders)` is `remainder`. The alternative — striking a **Zone**'s
    remainder out once it has won a slot, so it can never take two — bounds each **Zone** at
    `ceil(percentage * size / 100)` and is what a largest-remainder method would do, at the cost of that
    exactness. Unbiased was chosen: a **Zone** over-drawn by one slot is corrected by the very next
    **Batch**, and there is no case where the extra slot is wrong, only unlikely. In practice it is close
    to moot — three **Zones** can leave at most two slots to roll.

    `size` is what was asked for, not what the **Pool** can supply. A **Zone** with too few eligible
    **Wallpapers** is the *filling*'s problem, and the shortfall rule that answers it is in the Core
    service, where the **Pool** is.
    """
    if size <= 0:
        return dict.fromkeys(ZONE_ORDER, 0)

    guaranteed: dict[Zone, int] = {}
    remainders: dict[Zone, int] = {}
    for zone in ZONE_ORDER:
        # Hundredths, exactly: `percentage * size` is an integer and so is its remainder mod 100.
        whole, over = divmod(mix.percentage(zone) * size, PERCENT)
        guaranteed[zone] = whole
        remainders[zone] = over

    leftover = size - sum(guaranteed.values())
    for _ in range(leftover):
        guaranteed[_rolled(remainders, random)] += 1
    return guaranteed


def _rolled(remainders: dict[Zone, int], random: SeededRandom) -> Zone:
    """One **Zone**, chosen with probability proportional to its share of the leftover.

    A cumulative walk in a fixed **Zone** order rather than `random.choices`, for the reason the whole
    random source is a narrow class: the draw has to be reproducible from the seed, and that is only true
    if nothing about how the number is spent can change underneath it.
    """
    total = sum(remainders.values())
    # Only reachable if every product divided by 100, in which case there is no leftover slot to roll and
    # this is never called. Answered rather than divided by zero, because "the first **Zone**" is a
    # defensible answer and an exception here would be a **Batch** page that 500s.
    if total <= 0:
        return ZONE_ORDER[0]
    threshold = random.fraction() * total
    running = 0
    for zone in ZONE_ORDER:
        running += remainders[zone]
        if threshold < running:
            return zone
    # Unreachable while `threshold < total`, which `fraction()` guarantees; the rounding of the multiply
    # is the only way out and the last **Zone** is where it lands.
    return ZONE_ORDER[-1]


KMEANS_ITERATIONS = 10
"""How many assign-and-update rounds the varied **Unknown** draw's clustering runs, always exactly this many.

Fixed rather than "until nothing moves", so the work a mint does is bounded and the same seed spends the
same random numbers whatever the vectors are. Ten is past where k-means++ seeding usually stops improving
on groups as separate as a **Pool**'s kinds of picture are.
"""


def varied_order(
    vectors: NDArray[np.float32], favourites: Sequence[int], k: int, random: SeededRandom
) -> list[int]:
    """The **Unknowns** in the order they are drawn: one from each of `k` look-alike groups first (#45).

    `vectors` is `len(unknowns)` x d, a row of zeros meaning no **Embedding**, with its rows in today's
    draw order — the seeded shuffle the Core service has already made. `favourites` is Wallhaven's count
    for each row, and `k` is the number of **Unknown** slots allocated. The answer is a permutation of
    `range(len(vectors))`, the contract today's order has, so slot-taking and the **Shortfall** rule walk
    it exactly as they walked that:

    - one pick per cluster first, in cluster order, which is the seeded order k-means++ drew them in;
    - then every other embedded row, in the order given;
    - then every unembedded row, in the order given. Without a position it cannot be in any group.

    **The clusters** are spherical k-means — cosine, on the rows scaled to unit length — with k-means++
    seeding and `KMEANS_ITERATIONS` rounds, every random number from `random`, so a seed reproduces the
    **Batch**. `k` is the slot count, so a small **Batch** gets a few broad groups and a big one many fine
    ones. **No outlier guard**: no minimum cluster size and no merging. A small cluster of junk may get a
    slot; once its tile is **Ignored**, its look-alikes fall inside the **Similarity radius** of something
    decided, stop being **Unknowns** and leave the clustering, so junk costs one tile once (ADR 0018).

    **The pick** within a cluster is weighted by `log(1 + favourites)`: a nudge towards a decent example of
    each kind, about 3x for 1,000 favourites over 10 rather than 100x. No favourites is a weight of zero,
    which `weighted_order` puts last, so such a **Wallpaper** is picked only from a cluster of nothing else.

    **Today's order, unchanged**, when `k` is zero or fewer rows are embedded than `k`: there are not
    enough positions for `k` groups, and no random number is spent finding that out.
    """
    norms = np.linalg.norm(vectors, axis=1)
    embedded = [index for index in range(len(vectors)) if norms[index] > 0.0]
    if k <= 0 or len(embedded) < k:
        return list(range(len(vectors)))

    # float64 so that an argmax between two near-equal cosines is not decided by float32 rounding.
    points = (vectors[embedded] / norms[embedded, None]).astype(np.float64)
    clusters = _spherical_kmeans(points, k, random)
    # A cluster comes out empty only when rows duplicate each other, and then it has nothing to give: the
    # slot it would have filled is taken from `rest`, like any other.
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
    """`k` clusters of unit-length `points` by cosine, as lists of row indices in the order given.

    Each round assigns every point to its most similar centre — one `(n, k)` matmul — and moves each
    centre to the normalised sum of its members, which is their mean direction. A centre left with no
    members stays where it was rather than being re-seeded: re-seeding at the farthest point would be
    farthest-point sampling by the back door, rejected for #45 because CLIP's extreme points are
    disproportionately poor pictures.
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
    """`k` distinct starting rows: the first uniformly, each after it in proportion to its squared cosine
    distance from the nearest one already drawn (Arthur and Vassilvitskii's k-means++).

    That is what spreads the starting centres across the groups rather than leaving two in one group and
    none in another, which a fixed number of rounds would not always undo.
    """
    chosen = [min(int(random.fraction() * len(points)), len(points) - 1)]
    nearest = 1.0 - points @ points[chosen[0]]
    while len(chosen) < k:
        weights = np.clip(nearest, 0.0, None) ** 2
        weights[chosen] = 0.0
        chosen.append(_proportional(weights, chosen, random))
        nearest = np.minimum(nearest, 1.0 - points @ points[chosen[-1]])
    return chosen


def _proportional(weights: NDArray[np.float64], chosen: Sequence[int], random: SeededRandom) -> int:
    """One index drawn with probability proportional to `weights`, never one already in `chosen`."""
    total = float(weights.sum())
    if total <= 0.0:
        # Every row left is a duplicate of one already drawn. Any of them is as good as another.
        remaining = [index for index in range(len(weights)) if index not in set(chosen)]
        return remaining[min(int(random.fraction() * len(remaining)), len(remaining) - 1)]
    cumulative = np.cumsum(weights)
    index = int(np.searchsorted(cumulative, random.fraction() * total, side="right"))
    # Past the end only if the multiply rounds up to `total`. The last row with any weight is where that
    # threshold belongs — never a row already chosen, whose weight is zero.
    return index if index < len(weights) else int(np.flatnonzero(weights)[-1])


def weighted_order[T](items: Sequence[T], weights: Sequence[float], random: SeededRandom) -> list[T]:
    """A random permutation of `items` in which a heavier item tends to come sooner.

    Back for #45, whose pick per cluster is the first of this order. It went with #38's **Revisit
    weight** and is the function it was then: Efraimidis and Spirakis' key, `u ** (1 / weight)` sorted
    descending, so the order is a weighted sample without replacement at every prefix and its first item
    is a weighted pick. A weight of zero or less is a key of zero — the limit as the weight falls to zero —
    which puts the item last.
    """
    keyed = [
        (_key(random.fraction(), weight), index, item)
        for index, (item, weight) in enumerate(zip(items, weights, strict=True))
    ]
    # `index` breaks a tie between two equal keys, so `sort` never falls through to comparing the items.
    keyed.sort(key=lambda entry: (-entry[0], entry[1]))
    return [item for _, _, item in keyed]


def _key(uniform: float, weight: float) -> float:
    if weight <= 0.0:
        return 0.0
    return uniform ** (1.0 / weight)
