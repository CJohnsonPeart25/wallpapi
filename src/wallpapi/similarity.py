"""The **Similarity provider** seam.

Invariant 2: the interface is a matrix — **Pool** x decided — and never a pairwise call, because **Scores**
are derived from the **Decision log** on every read and a Python loop over a 10k **Pool** would make caching
them tempting. Nothing at #2 calls this; **Scoring** and **Zones** arrive at #9. The seam exists now so that
the Core service's five dependencies are settled from the start.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import numpy as np
from numpy.typing import NDArray


class SimilarityProvider(Protocol):
    """How alike **Wallpapers** are. The method is deliberately left open."""

    def similarities(self, pool_ids: Sequence[str], decided_ids: Sequence[str]) -> NDArray[np.float32]:
        """A `len(pool_ids)` x `len(decided_ids)` matrix of distances."""
        ...


class UnbuiltSimilarityProvider:
    """Stands in until the real provider arrives at #9.

    Unreachable at #2: nothing derives a **Score** yet. It raises rather than returning zeros, which would
    quietly make every **Wallpaper** an **Unknown** and look like a working system.
    """

    def similarities(self, pool_ids: Sequence[str], decided_ids: Sequence[str]) -> NDArray[np.float32]:
        raise NotImplementedError("the Similarity provider arrives at #9")
