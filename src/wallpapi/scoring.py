"""The **Score** maths: resolved values spread across the **Pool** by similarity, and the **Zones** that
come out of it.

Pure numpy over the matrix the **Similarity provider** returned and the values **Verdict resolution**
produced. It knows nothing about storage, about the **Decision log** or about how the similarities were
arrived at — which is what keeps invariant 2 honest: there is nothing here to cache, because there is
nothing here but arithmetic over what the caller already has.

**The formula.** `distance = 1 - similarity`. A decided **Wallpaper** contributes

    weight = exp(-decay * distance)     for distance <= radius
    weight = 0                          beyond it

and a **Pool** **Wallpaper**'s **Score** is the sum of `weight * value` over every decided **Wallpaper**.
The whole thing is one masked exponential and one matrix-vector product; there is no loop over pairs.

A decided **Wallpaper** that is itself in the **Pool** is at distance 0 from itself, so it contributes its
own value at full weight. That is deliberate: a **Wallpaper** you **Favourited** is a **Banger**.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from wallpapi.model import Zone


@dataclass(frozen=True, slots=True)
class Classification:
    """One **Score** and one **Zone** per row of the similarity matrix, in the order it was given in."""

    scores: NDArray[np.float64]
    zones: tuple[Zone, ...]


def classify(
    similarities: NDArray[np.float32], values: Sequence[int], *, radius: float, decay: float
) -> Classification:
    """**Score** every row of `similarities` against `values`, and say which **Zone** each lands in.

    `similarities` is `len(pool)` x `len(decided)` and `values` is one resolved value per decided
    **Wallpaper**, so the **Scores** are `similarities`-weighted sums of `values` — a matrix-vector product
    once the weights are in hand.

    Computed in float64 rather than in the matrix's float32. The values run to +/-100 and are summed over
    every decided **Wallpaper**; float32 has about seven digits, and a **Zone** is decided on the *sign* of
    the total, which is exactly the thing cancellation eats first.
    """
    distance = 1.0 - np.clip(similarities.astype(np.float64), 0.0, 1.0)
    # Masked rather than clipped: beyond the radius a decided **Wallpaper** contributes nothing at all,
    # which is not the same as contributing `exp(-decay * radius)`.
    weights = np.where(distance <= radius, np.exp(-decay * distance), 0.0)
    # An elementwise multiply and a row sum rather than `weights @ values`, which would be the obvious
    # spelling and is wrong here. A BLAS matrix-vector product may reorder its terms and use fused
    # multiply-add, so a **Wallpaper** equally similar to a **Favourite** and to a **Ban** comes out at a
    # few times 1e-15 instead of at zero — and the **Zone** rule reads the sign of that. `np.sum` adds in a
    # fixed pairwise order, where `w * 100 + w * -100` is exactly 0.0. Same asymptotic cost: `weights` is
    # already `len(pool)` x `len(decided)`, and this multiplies it in place rather than allocating again.
    weights *= np.asarray(values, dtype=np.float64)
    scores = weights.sum(axis=1)
    return Classification(scores=scores, zones=tuple(zone_of(float(score)) for score in scores))


def zone_of(score: float) -> Zone:
    """The **Zone** a **Score** falls in.

    Nothing but the sign, and that is the whole rule rather than half of it. "No decided **Wallpaper**
    within the similarity radius" is the other half of **Unknown** as the spec states it, and it needs no
    branch here: every decided **Wallpaper** beyond the radius is weighted at exactly zero, and every one
    in the decided set has a non-zero value by construction, so a **Pool** **Wallpaper** with no decided
    neighbour inside the radius has a **Score** of exactly 0.0 and is **Unknown** for that reason.

    Exact zero, never a tolerance. Equal and opposite contributions cancel to exactly 0.0 in IEEE
    arithmetic, and a **Score** that merely *rounds* to zero has a sign that says which way the evidence
    went — turning that into an **Unknown** would throw away the answer.

    A **Banned** **Wallpaper** never reaches this: it is in no **Zone**, so it is not classified at all.
    """
    if score > 0.0:
        return Zone.BANGER
    if score < 0.0:
        return Zone.DUD
    return Zone.UNKNOWN
