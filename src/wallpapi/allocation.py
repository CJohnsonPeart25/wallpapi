"""**Allocation**: turning a **Mix** into **Batch** slots, and the order a **Zone** gives its
**Wallpapers** up in.

Two pure functions and nothing else. Neither knows what a **Pool** is, what a **Score** is or where a
**Mix** was stored — `allocate` is arithmetic over three percentages, and `weighted_order` is a shuffle
with a thumb on the scale. That is what lets the Core service's draw be read as three plain steps
(allocate, order, fill) and lets every one of them be pinned under a seed.

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

from collections.abc import Sequence

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


def weighted_order[T](items: Sequence[T], weights: Sequence[float], random: SeededRandom) -> list[T]:
    """A random permutation of `items` in which a heavier item tends to come sooner.

    This is the whole of "**Dud** and **Unknown** slots are sampled at random": the **Zone** is put into a
    random order once, and taking its first `k` *is* the sample. Ordering rather than sampling is what
    makes the shortfall rule cheap — a **Zone** asked for more than it was allocated simply gives up the
    next ones in the order it already has, and no **Wallpaper** can come out twice.

    **The weight is the seam #11 builds on.** Every weight is 1.0 today, and at 1.0 the key below is just
    the uniform itself, so this is exactly a shuffle. The revisit weight multiplies into the weight and
    nothing here changes.

    Efraimidis and Spirakis' key, `u ** (1 / weight)` sorted descending: the resulting order is a weighted
    sample without replacement at every prefix, which is the property that makes "take the first `k`"
    correct for every `k` at once. A weight of zero or less is not a division — the limit as the weight
    falls to zero is a key of zero, which puts the item last, and that is what "never show this again"
    should mean.
    """
    keyed = [
        (_key(random.fraction(), weight), index, item)
        for index, (item, weight) in enumerate(zip(items, weights, strict=True))
    ]
    # `index` breaks a tie between two equal keys, which only two equal uniforms or two zero weights can
    # produce. Without it `sorted` would fall through to comparing the items themselves, and a
    # `ScoredWallpaper` is not orderable.
    keyed.sort(key=lambda entry: (-entry[0], entry[1]))
    return [item for _, _, item in keyed]


def _key(uniform: float, weight: float) -> float:
    if weight <= 0.0:
        return 0.0
    return uniform ** (1.0 / weight)
