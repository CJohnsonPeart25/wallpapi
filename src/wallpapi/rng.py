"""The seeded random source.

The spec asks for "a seeded random source", not a fake one, so the tests inject this with a fixed seed
rather than a stand-in. It is a class rather than a bare `random.Random` so the Core service depends on the
handful of operations it actually needs.
"""

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
        """One number in `[0, 1)`.

        The primitive **Allocation** is built on: the leftover-slot roll needs a raw uniform, and it is
        not a `sample`. Kept as narrow as the rest of this class — no
        `randrange`, no `choices`, because nothing needs them.
        """
        return self._random.random()
