"""Drawing a **Batch** by the active **Mix**. Issue #10.

Everything here enters through the Core service (invariant 1). A **Zone** cannot be set from outside — it
is the sign of a derived **Score** — so `tests/zoned.py` arranges one **Favourite** and one **Ban** and
then moves the **Filters** so that the two seeds spread their values into a **Pool** they are no longer
part of. What a test asks for is the **Pool** it gets: so many **Bangers**, so many **Duds**, so many
**Unknowns**, and no **Verdict** against any of them.

The arithmetic of **Allocation** is pinned in `test_allocation.py` against the pure function. What is
pinned here is everything the **Pool** brings with it — the shortfall rule, which **Wallpapers** a
**Zone** gives up first, and that switching applies to the next **Batch** rather than this one.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from tests.conftest import make_harness
from tests.fakes import catalogue_of
from tests.zoned import FAVOURED, NEAR, ZonedPool, zoned_pool
from wallpapi.core import EXPLORE_MIX, MIX_TOTAL, REFINE_MIX, Batch, SettingsRefused
from wallpapi.model import Zone

DRAWS = 40
"""How many **Batches** the long-run proportions are read off.

A whole **Pool** is arranged from scratch for each one, under its own seed, because a **Batch** cannot be
drawn twice without submitting the first — and submitting writes an **Ignore** against every **Wallpaper**
it showed, which is a **Verdict**, which moves the very **Zones** the test is counting. Forty arrangements
is a third of a second and forty independent rolls, which at a **Batch** of 32 is plenty: 31 of every 32
slots are guaranteed, so the rolls are all there is to average over.
"""


def _drawn(pool: ZonedPool, size: int) -> Batch:
    """One **Batch** of `size` off an arranged **Pool**."""
    pool.harness.core.update_settings(batch_size=size)
    batch = pool.harness.core.get_next_batch()
    assert isinstance(batch, Batch), batch
    return batch


def _zone_counts(batch: Batch) -> Counter[Zone]:
    return Counter(batch.zones[w.id] for w in batch.wallpapers)


def test_the_mixes_survive_a_restart(db_path: Path) -> None:
    """Stored rather than hard-coded, which is what #12 needs to be able to edit them.

    A second Core service over the same file finds the **Mixes** migration 7 seeded and the **Mix** the
    first one was switched to — not the defaults recomputed in Python, which would look identical here if
    they were never written down at all.
    """
    first = make_harness(db_path)
    first.core.update_settings(active_mix="refine")

    restarted = make_harness(db_path)

    assert restarted.core.list_mixes() == (EXPLORE_MIX, REFINE_MIX)
    assert restarted.core.active_mix() == REFINE_MIX


def test_a_mix_nobody_has_heard_of_is_refused(db_path: Path) -> None:
    """Refused, and the stored **Mix** is left alone. A name accepted and then ignored would leave the
    switcher naming a **Mix** the draw has never heard of."""
    harness = make_harness(db_path)

    refused = harness.core.update_settings(active_mix="nope")

    assert isinstance(refused, SettingsRefused)
    assert refused.reason is SettingsRefused.Reason.ACTIVE_MIX_UNKNOWN
    assert harness.core.get_settings().active_mix == "explore"


def test_a_well_stocked_pool_gives_a_batch_the_shape_of_the_mix(db_path: Path) -> None:
    """The acceptance criterion at 32 in **Explore**, through the seam: 24 / 6 / 1 and one rolled slot.

    Every **Zone** holds far more than its slots, so nothing here is the shortfall rule — this is the
    **Allocation** arriving on the page intact, with the **Zones** read off the **Batch** rather than
    recomputed.
    """
    pool = zoned_pool(db_path, bangers=30, duds=30, unknowns=60)

    counts = _zone_counts(_drawn(pool, 32))

    assert sum(counts.values()) == 32
    assert counts[Zone.UNKNOWN] >= 24
    assert counts[Zone.BANGER] >= 6
    assert counts[Zone.DUD] >= 1


def test_no_wallpaper_is_shown_twice_in_one_batch(db_path: Path) -> None:
    """Three **Zones** feeding one **Batch**, plus a shortfall that takes more from a **Zone** already
    drawn from, is exactly the shape that produces a duplicate if the draw ever restarts a **Zone**."""
    pool = zoned_pool(db_path, bangers=4, duds=4, unknowns=8)

    batch = _drawn(pool, 16)

    assert len({w.id for w in batch.wallpapers}) == 16


def test_with_no_bangers_at_all_the_batch_is_every_unknown_there_is(db_path: Path) -> None:
    """The acceptance criterion for an empty **Banger** **Zone**, which is what a new **Decision log**
    looks like: nothing has been **Favourited**, so **Explore**'s twenty per cent has nowhere to come from
    and the shortfall goes to **Unknown** first."""
    pool = zoned_pool(db_path, bangers=0, duds=0, unknowns=20)

    counts = _zone_counts(_drawn(pool, 8))

    assert counts == Counter({Zone.UNKNOWN: 8})


def test_with_no_unknowns_the_shortfall_goes_to_bangers_before_duds(db_path: Path) -> None:
    """**Unknown**, then **Banger**, then **Dud** — read off the middle of that order.

    **Explore** asks for six **Unknowns** out of eight and there are none, so six slots are going
    somewhere. Four **Bangers** take four of them and the last two fall through to the **Duds**, which is
    the whole rule in one **Batch**: **Bangers** are exhausted before a **Dud** is drawn.
    """
    pool = zoned_pool(db_path, bangers=4, duds=20, unknowns=0)

    counts = _zone_counts(_drawn(pool, 8))

    assert counts[Zone.UNKNOWN] == 0
    assert counts[Zone.BANGER] == 4
    assert counts[Zone.DUD] == 4


def test_a_zone_that_falls_short_is_made_up_from_unknown(db_path: Path) -> None:
    """The other direction: plenty of **Unknowns**, one **Banger**, no **Duds**.

    **Explore** wants six **Unknowns**, one or two **Bangers** and none or one **Dud**. Whatever the roll,
    the **Bangers** and **Duds** run out and every slot they cannot fill comes back to **Unknown** —
    which is what makes a **Batch** shrink towards discovery rather than towards what the user has already
    judged.
    """
    pool = zoned_pool(db_path, bangers=1, duds=0, unknowns=20)

    counts = _zone_counts(_drawn(pool, 8))

    assert counts[Zone.DUD] == 0
    assert counts[Zone.BANGER] <= 1
    assert counts[Zone.UNKNOWN] == 8 - counts[Zone.BANGER]


def test_a_pool_smaller_than_the_batch_ships_the_smaller_batch(db_path: Path) -> None:
    """Every **Zone** exhausted is a shorter **Batch**, never an error and never a repeat.

    This was already true of the uniform draw; what it pins now is that three **Zones** and a shortfall
    rule have not turned "there isn't enough" into a **Batch** page that cannot render.
    """
    pool = zoned_pool(db_path, bangers=1, duds=1, unknowns=3)

    batch = _drawn(pool, 32)

    assert len(batch.wallpapers) == 5
    assert len({w.id for w in batch.wallpapers}) == 5


def test_banger_slots_take_the_highest_scores(db_path: Path) -> None:
    """The acceptance criterion for what a **Banger** slot picks: the best of them, in **Score** order.

    The **Bangers** are graded by how like the **Favourite** they are, so their **Scores** are strictly
    ordered, and **Refine** is used because it asks for enough **Banger** slots to tell an ordering from a
    coincidence. Whatever the leftover slot rolls — four **Banger** slots are guaranteed at this size and
    the fifth is not — the ones drawn have to be the top of that order and nothing else, because this part
    of the draw is not a sample at all.
    """
    pool = zoned_pool(db_path, bangers=8, duds=0, unknowns=20)
    # A fine grading rather than a coarse one: every **Banger** has to stay inside the **Similarity
    # radius**, or the ones at the bottom of the order would be **Unknown** rather than badly ranked and
    # this would be testing the **Scoring** arithmetic instead of the draw.
    graded = {banger: NEAR - 0.005 * rank for rank, banger in enumerate(pool.bangers)}
    pool.harness.similarity.similarity_by_pair.update(
        {(banger, FAVOURED): value for banger, value in graded.items()}
    )
    pool.harness.core.update_settings(active_mix="refine")

    batch = _drawn(pool, 6)

    ranked = sorted(graded, key=lambda banger: graded[banger], reverse=True)
    drawn = {w.id for w in batch.wallpapers if batch.zones[w.id] is Zone.BANGER}
    assert len(drawn) >= 4
    assert drawn == set(ranked[: len(drawn)])


def test_unknown_slots_are_sampled_rather_than_taken_in_order(tmp_path: Path) -> None:
    """The other half of that criterion: **Unknown** and **Dud** slots are random.

    Two seeds over the same arrangement draw different **Wallpapers**, and the same seed draws the same
    ones. Without the first assertion the draw could be taking whatever the **Pool** query returned first,
    which would show the same **Batch** for ever; without the second it would not be reproducible at all.

    A database of its own per draw, including for the repeat: a second **Batch** off the same file would
    be the live one handed back (ADR 0002), which would pass this test while proving nothing.
    """

    def drawn(name: str, seed: int) -> set[str]:
        pool = zoned_pool(tmp_path / name, bangers=0, duds=0, unknowns=40, seed=seed)
        return {w.id for w in _drawn(pool, 8).wallpapers}

    assert drawn("a.db", 1) != drawn("b.db", 2)
    assert drawn("c.db", 1) == drawn("d.db", 1)


def test_the_long_run_zone_proportions_are_the_active_mix(tmp_path: Path) -> None:
    """The acceptance criterion over many **Batches**, with every **Zone** stocked well enough to answer.

    Not a restatement of the pure test: this is the whole path — classify, allocate, order, fill, record —
    and it would fail if any step leaked a **Zone**, dropped the leftover roll, or filled a shortfall that
    was not there.
    """
    counted: Counter[Zone] = Counter()
    for seed in range(DRAWS):
        pool = zoned_pool(tmp_path / f"{seed}.db", bangers=30, duds=30, unknowns=60, seed=seed)
        counted.update(_zone_counts(_drawn(pool, 32)))

    slots = sum(counted.values())
    assert slots == DRAWS * 32
    for zone in (Zone.UNKNOWN, Zone.BANGER, Zone.DUD):
        assert counted[zone] / slots == pytest.approx(EXPLORE_MIX.percentage(zone) / MIX_TOTAL, abs=0.03)


def test_switching_to_refine_changes_what_the_next_batch_is_made_of(tmp_path: Path) -> None:
    """The acceptance criterion for the switch: **Refine** is mostly **Bangers** where **Explore** is
    mostly **Unknowns**.

    The same arranged **Pool** under both **Mixes**, so the only thing that differs between the two
    **Batches** is the **Mix** they were drawn with.
    """
    exploring = _zone_counts(_drawn(zoned_pool(tmp_path / "a.db", bangers=30, duds=30, unknowns=60), 32))

    refining = zoned_pool(tmp_path / "b.db", bangers=30, duds=30, unknowns=60)
    refining.harness.core.update_settings(active_mix="refine")
    counts = _zone_counts(_drawn(refining, 32))

    assert exploring[Zone.UNKNOWN] >= 24
    assert counts[Zone.BANGER] >= 22
    assert counts[Zone.UNKNOWN] >= 8


def test_switching_leaves_the_batch_on_screen_alone(db_path: Path) -> None:
    """The **Mix** is read when a **Batch** is minted, exactly as the batch size is (#4).

    A switcher that rerolled the live **Batch** would throw away a **Draft Batch** the user was part way
    through — so the same **Batch**, with the same **Wallpapers** in the same **Zones**, is what the next
    page load gives back.
    """
    pool = zoned_pool(db_path, bangers=30, duds=30, unknowns=60)
    live = _drawn(pool, 32)

    pool.harness.core.update_settings(active_mix="refine")
    reloaded = pool.harness.core.get_next_batch()

    assert isinstance(reloaded, Batch)
    assert reloaded.id == live.id
    assert reloaded.wallpapers == live.wallpapers
    assert reloaded.zones == live.zones


def test_a_shortfall_records_the_zone_the_wallpaper_came_from(db_path: Path) -> None:
    """The **Zone** on a tile is the **Wallpaper**'s own, never the slot's.

    **Explore** asks for **Bangers** and there are none, so six of these eight slots were allocated to a
    **Zone** they were not filled from. Every tile still says **Unknown**, because that is what the
    **Wallpaper** is — labelling it **Banger** for occupying a **Banger** slot would be the page saying
    something the **Decision log** does not.
    """
    pool = zoned_pool(db_path, bangers=0, duds=0, unknowns=20)

    batch = _drawn(pool, 8)

    assert set(batch.zones.values()) == {Zone.UNKNOWN}
    classified = {s.wallpaper.id: s.zone for s in pool.harness.core.classify_pool()}
    assert batch.zones == {w.id: classified[w.id] for w in batch.wallpapers}


def test_an_unclassifiable_pool_is_still_a_batch_unavailable(db_path: Path) -> None:
    """Nothing to draw from is the page that says so, not a **Batch** of nothing.

    The draw got a new way to return an empty list — three **Zones** that are all empty — and the branch
    above it that answers "why is there nothing to show" has to still be the one that runs.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(0), fill_pool=0)

    assert not isinstance(harness.core.get_next_batch(), Batch)
