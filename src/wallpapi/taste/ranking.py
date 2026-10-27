"""Ranking: one **Score** per candidate Item, from the Evidence and a similarity function.

`distance = 1 - similarity`. A decided Item contributes

    weight = exp(-decay * distance)     for distance <= radius
    weight = 0                          beyond it

and a candidate's **Score** is the sum of `weight * value` over every decided Item: one masked exponential and
one row sum, no loop over pairs.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from wallpapi.taste.evidence import Evidence
from wallpapi.taste.resolution import resolve

type Similarity = Callable[[Sequence[str], Sequence[str]], NDArray[np.floating]]
"""Item keys to a candidates x decided matrix of similarities in `[0, 1]`, rows and columns in the order
given. The caller's: it may embed, look up a cache or fall back to metadata."""


@dataclass(frozen=True, slots=True)
class Ranking:
    """What ranking makes of the candidates: `scores[i]` is the **Score** of the `i`th candidate given.

    An object rather than a bare sequence, so a support figure or a veto can join `scores` as a field
    without changing a caller.
    """

    scores: tuple[float, ...]


def rank(
    evidence: Iterable[Evidence],
    candidates: Sequence[str],
    similarity: Similarity,
    *,
    radius: float,
    decay: float,
) -> Ranking:
    """**Score** every candidate against the decided Items.

    Absolute Evidence resolves latest-wins per Item (`resolution.resolve`), and the decided Items are those
    standing at a non-zero value, as columns in **Item** key order, whatever order the Evidence names them in:
    a **Score** is a sum over the columns, and the same floats summed in another order can differ in the last
    bit. `similarity` is called exactly once, for the whole candidates x decided matrix (Invariant 2), even
    when nothing is decided; a matrix of any other shape raises `ValueError`. Relative Evidence is accepted
    and, in the MVP, ignored.

    A candidate that is itself decided is scored against its own Evidence, at distance 0. Holding it out is
    the caller's choice.
    """
    standing = resolve(evidence)
    decided = sorted(item for item, piece in standing.items() if piece.strength != 0)
    similarities = similarity(candidates, decided)
    if similarities.shape != (len(candidates), len(decided)):
        raise ValueError(
            f"the similarity function gave a {similarities.shape} matrix for "
            f"{len(candidates)} x {len(decided)} candidates by decided Items"
        )
    values = [standing[item].strength for item in decided]
    return Ranking(scores=_scores(similarities, values, radius, decay))


def _scores(
    similarities: NDArray[np.floating], values: Sequence[float], radius: float, decay: float
) -> tuple[float, ...]:
    """The masked exponential over a candidates x decided matrix.

    float64, not the matrix's float32: a **Zone** is decided on the *sign* of a sum of values up to
    +/-100, which is exactly what float32 cancellation eats first.
    """
    distance = 1.0 - np.clip(similarities.astype(np.float64), 0.0, 1.0)
    # Masked rather than clipped: beyond the radius a decided Item contributes nothing at all, which is not
    # the same as contributing `exp(-decay * radius)`.
    weights = np.where(distance <= radius, np.exp(-decay * distance), 0.0)
    # An elementwise multiply and a row sum rather than `weights @ values`, which would be the obvious
    # spelling and is wrong here. A BLAS matrix-vector product may reorder its terms and use fused
    # multiply-add, so a candidate equally similar to a **Favourite** and to a **Ban** comes out at a few
    # times 1e-15 instead of at zero — and the **Zone** rule reads the sign of that. `np.sum` adds in a
    # fixed pairwise order, where `w * 100 + w * -100` is exactly 0.0. Same asymptotic cost: `weights` is
    # already candidates x decided, and this multiplies it in place rather than allocating again.
    weights *= np.asarray(values, dtype=np.float64)
    return tuple(weights.sum(axis=1).tolist())
