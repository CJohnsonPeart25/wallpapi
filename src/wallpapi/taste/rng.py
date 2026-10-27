"""The seeded random source: tests inject this with a fixed seed rather than a stand-in."""

from __future__ import annotations

import random
from collections.abc import Sequence


class SeededRandom:
    """A `random.Random` behind a narrow interface. Same seed, same **Batch**."""

    def __init__(self, seed: int) -> None:
        self._random = random.Random(seed)

    def sample[T](self, population: Sequence[T], k: int) -> list[T]:
        """`k` distinct members of `population`, in a shuffled order."""
        return self._random.sample(population, k)

    def fraction(self) -> float:
        """One number in `[0, 1)`, the primitive the leftover-slot roll is built on."""
        return self._random.random()
