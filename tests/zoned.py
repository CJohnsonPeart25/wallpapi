"""A **Pool** whose **Zones** a test chooses, arranged through the Core service.

A **Zone** is the sign of a derived **Score**, so it cannot be set. Two seeds are shown as a **Batch** and
given a **Favourite** and a **Ban**; the **Filters** then move so the seeds leave the **Pool** and the
population enters it. A pruned **Favourite** still counts towards every **Score** (ADR 0007), so the seeds
spread +100 and -100 from outside the **Pool** and the hand-defined similarities decide each member's
**Zone**: near the **Favourite** a **Banger**, near the **Ban** a **Dud**, near neither exactly 0.0 and
**Unknown**. The **Pool** is empty while the seeds are submitted, which leaves no live **Batch** behind to
be handed back in place of a fresh draw (ADR 0002).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from tests.conftest import Harness, make_harness
from tests.fakes import wallpaper
from wallpapi.core import Batch
from wallpapi.model import Verdict, Wallpaper, Zone

FAVOURED = "sd0000"
LOATHED = "sd0001"
"""The two seeds: the one that is **Favourited** and the one that is **Banned**. Never in the **Pool** by
the time a test looks at it, and never drawn into a **Batch**."""

_SEED_WIDTH = 2560
_SEED_HEIGHT = 1440
_POPULATION_WIDTH = 3840
"""The seeds pass the default minimum width and fail a higher one; the population passes both."""

_ADMITTING_WIDTH = 3000

NEAR = 0.9
"""How alike a population **Wallpaper** is made to a seed to put it in that seed's **Zone**: a distance
of 0.1 inside the default radius of 0.15, so about 67 either way, far from the **Zone** boundary. A test
grading **Wallpapers** apart has to stay inside the radius."""


@dataclass
class ZonedPool:
    """A harness over a **Pool** with known **Zones**, and the IDs in each of them."""

    harness: Harness
    bangers: tuple[str, ...]
    duds: tuple[str, ...]
    unknowns: tuple[str, ...]


def drawn(pool: ZonedPool, size: int) -> Batch:
    """One **Batch** of `size` off an arranged **Pool**."""
    pool.harness.core.update_settings(batch_size=size)
    batch = pool.harness.core.get_next_batch()
    assert isinstance(batch, Batch), batch
    return batch


def zone_counts(batch: Batch) -> Counter[Zone]:
    return Counter(batch.zones[w.id] for w in batch.wallpapers)


def zoned_pool(
    db_path: Path, *, bangers: int = 0, duds: int = 0, unknowns: int = 0, seed: int = 1
) -> ZonedPool:
    """A **Pool** of exactly `bangers` **Bangers**, `duds` **Duds** and `unknowns` **Unknowns**.

    No live **Batch** on the way out, and no **Verdict** in the **Decision log** against any population
    **Wallpaper** — so a **Batch** drawn from this **Pool** is the draw and nothing else.
    """
    population = tuple(
        wallpaper(f"wp{n:04d}", width=_POPULATION_WIDTH, favourites=0)
        for n in range(bangers + duds + unknowns)
    )
    harness = make_harness(
        db_path,
        catalogue=(*_seeds(), *population),
        # One page holds the whole catalogue, so no page boundary is skipped when the Filters move.
        page_size=1000,
        seed=seed,
    )

    harness.core.update_settings(batch_size=2)
    seeding = harness.core.get_next_batch()
    assert isinstance(seeding, Batch), seeding
    assert {w.id for w in seeding.wallpapers} == {FAVOURED, LOATHED}
    harness.core.set_draft_verdict(seeding.id, FAVOURED, Verdict.FAVOURITE)
    harness.core.set_draft_verdict(seeding.id, LOATHED, Verdict.BAN)

    # Both **Filters** move at once, before the submit: the seeds are pruned out of the **Pool** and the
    # population becomes admissible. The **Pool** is empty for the length of the submit, which is what
    # leaves no **Batch** minted behind it.
    harness.core.update_settings(min_width=_ADMITTING_WIDTH, min_favourites=0)
    harness.core.submit_batch(seeding.id)

    # Enough steps for the random walk to come back round to page one: it is left on the empty page two,
    # and the like: strategy now takes every other step. Four would do.
    harness.fill_pool(6)

    ids = tuple(w.id for w in population)
    banger_ids, rest = ids[:bangers], ids[bangers:]
    dud_ids, unknown_ids = rest[:duds], rest[duds:]
    harness.similarity.similarity_by_pair.update(
        {(banger, FAVOURED): NEAR for banger in banger_ids} | {(dud, LOATHED): NEAR for dud in dud_ids}
    )
    return ZonedPool(harness=harness, bangers=banger_ids, duds=dud_ids, unknowns=unknown_ids)


def _seeds() -> tuple[Wallpaper, ...]:
    """The **Favourite** and the **Ban** every arrangement is built on.

    `favourites=100` against the population's `0`, so the default minimum-**Favourites** **Filter** admits
    these two and nothing else on the first refill.
    """
    return (
        wallpaper(FAVOURED, width=_SEED_WIDTH, height=_SEED_HEIGHT, favourites=100),
        wallpaper(LOATHED, width=_SEED_WIDTH, height=_SEED_HEIGHT, favourites=100),
    )
