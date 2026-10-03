"""The **Score** maths: pure numpy over a similarity matrix and resolved values, nothing to cache.

`distance = 1 - similarity`. A decided **Wallpaper** contributes

    weight = exp(-decay * distance)     for distance <= radius
    weight = 0                          beyond it

and a **Pool** **Wallpaper**'s **Score** is the sum of `weight * value` over every decided **Wallpaper**:
one masked exponential and one row sum, no loop over pairs. Nothing decided is in the **Pool**, so no row
is ever scored against itself.
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
    """**Score** every row of `similarities` (`len(pool)` x `len(decided)`) against `values`.

    float64, not the matrix's float32: a **Zone** is decided on the *sign* of a sum of values up to
    +/-100, which is exactly what float32 cancellation eats first.
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
    """The **Zone** a **Score** falls in: nothing but the sign.

    Beyond the radius a weight is exactly zero, so "no decided neighbour" is a **Score** of exactly 0.0 and
    needs no branch. Exact zero, never a tolerance: a **Score** that merely rounds to zero still says which
    way the evidence went.
    """
    if score > 0.0:
        return Zone.BANGER
    if score < 0.0:
        return Zone.DUD
    return Zone.UNKNOWN
