"""A **Pool** whose **Zones** a test chooses outright.

Every test for **Allocation** needs a **Pool** with a known number of **Bangers**, **Duds** and
**Unknowns** in it, and a **Zone** is not something the seam lets you set — it is the sign of a **Score**,
derived from the **Decision log** and the **Similarity provider**. So this arranges the two things that
produce one, and it does it through the Core service like everything else (invariant 1).

**How it works.** Two *seed* **Wallpapers** are admitted to the **Pool** on their own, shown as a
**Batch**, and given the only two **Verdicts** that matter here: one **Favourite**, one **Ban**. The
**Filters** are then moved so that the seeds fall out of the **Pool** and the population falls into it,
which is the whole trick — a **Favourite** that a **Filter** change pruned still counts towards every
**Score** (ADR 0007), so the two seeds go on spreading +100 and -100 from outside the **Pool** while
contributing no **Zone** of their own. The population is then whatever the hand-defined similarities say:
near the **Favourite** is a **Banger**, near the **Ban** is a **Dud**, near neither is a **Score** of
exactly 0.0 and so an **Unknown**.

The seeds have to leave the **Pool** for two reasons and not one. The **Favourite** would otherwise be a
**Banger** nobody asked for, and — the reason there is no way around it — an empty **Pool** at the moment
the seeding **Batch** is submitted is what stops a **Batch** being minted on the way out. A live **Batch**
is handed back rather than rerolled (ADR 0002), so a test that left one behind would be testing that
**Batch** rather than the draw it came to test.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tests.conftest import Harness, make_harness
from tests.fakes import wallpaper
from wallpapi.core import Batch
from wallpapi.model import Verdict, Wallpaper

FAVOURED = "sd0000"
LOATHED = "sd0001"
"""The two seeds: the one that is **Favourited** and the one that is **Banned**. Never in the **Pool** by
the time a test looks at it, and never drawn into a **Batch**."""

_SEED_WIDTH = 2560
_SEED_HEIGHT = 1440
_POPULATION_WIDTH = 3840
"""The **Filter** that separates them. The seeds are admitted under the default minimum resolution and
pruned by a higher one; the population clears both. Both are 16x9, or the ratio **Filter** would reject
them before the resolution one was reached."""

_ADMITTING_WIDTH = 3000

NEAR = 0.9
"""How alike a population **Wallpaper** is made to a seed to put it in that seed's **Zone**.

Inside the default radius — a distance of 0.1 against a radius of 0.15 — so the seed's +/-100 arrives at
about `exp(-0.4)` of full strength, which is 67 either way. Nowhere near the boundary the **Zone** is read
off, which is the point: these tests are about **Allocation**, and none of them should be able to fail
because of the **Scoring** arithmetic underneath.

The margin narrowed when the radius default moved to 0.15 with the embedding provider (ADR 0013). A test
here that wants to grade **Wallpapers** apart has to do it in steps small enough to stay inside it.
"""


@dataclass
class ZonedPool:
    """A harness over a **Pool** with known **Zones**, and the IDs in each of them."""

    harness: Harness
    bangers: tuple[str, ...]
    duds: tuple[str, ...]
    unknowns: tuple[str, ...]


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
        # One page holds the whole catalogue, so that "page two is empty, start again at page one" is the
        # only walk this has to reason about. With Wallhaven's real listing size the population would
        # straddle a page boundary and half of it would be skipped when the **Filters** moved.
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

    # Enough steps to be sure the random walk has come back round to page one. Two things make that more
    # than one step: the walk is left on page two, which is empty and is what sends it back to the start,
    # and there is a **Favourite** now, so the like: strategy takes every other turn (#13) and answers an
    # empty page because the fake was given no lookalikes. Six is comfortably more than the four it takes.
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
