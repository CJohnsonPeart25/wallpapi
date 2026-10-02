"""The varied **Unknown** draw through the Core service: one **Wallpaper** from each look-alike group. #45.

Everything enters through `get_next_batch` (invariant 1). Positions come from the fake **Similarity
provider**'s `vectors`, generated here and keyed by **Wallpaper** ID; no model, no network. A **Pool**
nobody has decided anything about is all **Unknown**, which is the **Zone** under test, so no **Verdict**
has to be arranged at all.

The clustering itself — the weighted pick, what comes after the picks — is pinned as a pure function in
`test_varied_order.py`. What is pinned here is what the **Batch** shows, and that a provider with nothing
to say leaves today's draw exactly as it was.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from tests.conftest import Harness, make_harness
from tests.fakes import catalogue_of
from wallpapi.core import Batch
from wallpapi.model import Wallpaper

WIDTH = 32
NOISE = 0.05
ALL_UNKNOWN = "all unknown"


def grouped(
    catalogue: Sequence[Wallpaper], sizes: Sequence[int], *, seed: int = 0
) -> tuple[dict[str, NDArray[np.float32]], dict[str, int]]:
    """A vector per **Wallpaper**, the first `sizes[0]` around one direction, the next around another, and
    so on; and which group each was placed in."""
    generator = np.random.default_rng(seed)
    directions = generator.normal(size=(len(sizes), WIDTH))
    groups = [group for group, size in enumerate(sizes) for _ in range(size)]
    vectors = {
        w.id: (directions[group] + NOISE * generator.normal(size=WIDTH)).astype(np.float32)
        for w, group in zip(catalogue, groups, strict=False)
    }
    return vectors, {w.id: group for w, group in zip(catalogue, groups, strict=False)}


def harness(
    db_path: Path,
    catalogue: Sequence[Wallpaper],
    vectors: dict[str, NDArray[np.float32]] | None,
    *,
    seed: int = 1,
) -> Harness:
    """A Core service whose whole **Pool** is `catalogue`, filled in one refill step."""
    return make_harness(db_path, catalogue=catalogue, page_size=len(catalogue), seed=seed, vectors=vectors)


def drawn(harness: Harness, size: int, *, mix: str | None = None) -> Batch:
    if mix is not None:
        harness.core.save_mix(mix, unknown=100, banger=0, dud=0)
        harness.core.update_settings(active_mix=mix)
    harness.core.update_settings(batch_size=size)
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch), batch
    return batch


def test_twelve_groups_and_twelve_unknown_slots_give_one_tile_from_each(tmp_path: Path) -> None:
    catalogue = catalogue_of(96)
    vectors, groups = grouped(catalogue, [8] * 12)

    for seed in range(5):
        batch = drawn(harness(tmp_path / f"{seed}.db", catalogue, vectors, seed=seed), 12, mix=ALL_UNKNOWN)

        assert sorted(groups[w.id] for w in batch.wallpapers) == list(range(12))


def test_a_lopsided_pool_is_covered_more_widely_than_by_todays_draw(tmp_path: Path) -> None:
    """A third of the **Pool** in one group and the rest in fourteen small ones, **Explore** at 16.

    Averaged over the same seeds with and without vectors, the clustered draw shows more groups per
    **Batch**. A plain "more than", as the issue asks: the claim is the direction, not a margin.
    """
    catalogue = catalogue_of(100)
    vectors, groups = grouped(catalogue, [30] + [5] * 14)
    seeds = range(30)

    def covered(given: dict[str, NDArray[np.float32]] | None, seed: int) -> int:
        batch = drawn(harness(tmp_path / f"{seed}-{given is None}.db", catalogue, given, seed=seed), 16)
        return len({groups[w.id] for w in batch.wallpapers})

    varied = sum(covered(vectors, seed) for seed in seeds)
    today = sum(covered(None, seed) for seed in seeds)

    assert varied > today


def test_the_same_seed_gives_the_same_batch(tmp_path: Path) -> None:
    catalogue = catalogue_of(60)
    vectors, _ = grouped(catalogue, [20, 10, 10, 10, 10])

    first = drawn(harness(tmp_path / "a.db", catalogue, vectors, seed=4), 16)
    second = drawn(harness(tmp_path / "b.db", catalogue, vectors, seed=4), 16)

    assert [w.id for w in second.wallpapers] == [w.id for w in first.wallpapers]


def test_no_vectors_and_too_few_vectors_both_give_todays_draw(tmp_path: Path) -> None:
    """A provider that has no vectors, one whose rows are all "no **Embedding**", and one with fewer
    embedded **Unknowns** than **Unknown** slots all draw the same **Batch** under the same seed."""
    catalogue = catalogue_of(40)
    few, _ = grouped(catalogue[:5], [5])
    nothing = {w.id: np.zeros(WIDTH, dtype=np.float32) for w in catalogue}

    batches = [
        [w.id for w in drawn(harness(tmp_path / f"{n}.db", catalogue, given, seed=9), 16).wallpapers]
        for n, given in enumerate((None, few, nothing))
    ]

    assert batches[1] == batches[0]
    assert batches[2] == batches[0]


def test_unembedded_unknowns_come_after_the_clustered_picks(tmp_path: Path) -> None:
    """Twelve embedded groups among thirty **Wallpapers** with no **Embedding**: a **Batch** with exactly
    twelve **Unknown** slots shows the twelve picks and none of the thirty."""
    catalogue = catalogue_of(66)
    vectors, groups = grouped(catalogue[:36], [3] * 12)

    batch = drawn(harness(tmp_path / "w.db", catalogue, vectors), 12, mix=ALL_UNKNOWN)

    assert sorted(groups[w.id] for w in batch.wallpapers) == list(range(12))
