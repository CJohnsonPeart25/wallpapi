"""**Batches**: the draw, the **Draft Batch** and submitting it into the **Decision log**.

`draw` is a pure function and is tested as one, over a classified sequence a test arranges by hand. The rest
goes through `batches` alone, on `conftest.BatchesRig`: a real in-memory database, a real `Embeddings`
falling back to hand-defined similarities, and the fake clock. A **Draft Batch** is not the **Decision log**
(marks set rather than toggle, and only submitting appends), and a **Batch** is submitted once.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from tests.conftest import FIXED_NOW, BatchesRig, batches_rig
from tests.fakes import catalogue_of, wallpaper
from wallpapi import batches, decisions, pool, settings, storage
from wallpapi.allocation import ZONE_ORDER, ScoredWallpaper, allocate, draw
from wallpapi.batches import Batch, BatchUnavailable, SubmissionRefused, Submitted, UnknownShort
from wallpapi.decisions import HistoryEntry, ResolvedVerdict
from wallpapi.model import Mix, Verdict, Zone
from wallpapi.pool import RefillStatus, RefillStrategy
from wallpapi.rng import SeededRandom
from wallpapi.settings import EXPLORE_MIX, MIX_TOTAL, REFINE_MIX
from wallpapi.similarity import NEAR_DUPLICATE_SIMILARITY

SCORE = 50.0
"""A **Score** well clear of zero either way, for an arranged **Banger** or **Dud**."""


def classified(*, bangers: int = 0, duds: int = 0, unknowns: int = 0) -> list[ScoredWallpaper]:
    """A classified **Pool** of exactly these counts, **Bangers** first, then **Duds**, then **Unknowns**."""
    zones = [Zone.BANGER] * bangers + [Zone.DUD] * duds + [Zone.UNKNOWN] * unknowns
    scores = {Zone.BANGER: SCORE, Zone.DUD: -SCORE, Zone.UNKNOWN: 0.0}
    return [
        ScoredWallpaper(wallpaper=wallpaper, score=scores[zone], zone=zone)
        for wallpaper, zone in zip(catalogue_of(len(zones)), zones, strict=True)
    ]


def zone_counts(tiles: list[ScoredWallpaper]) -> Counter[Zone]:
    return Counter(tile.zone for tile in tiles)


# -- the draw ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("mix", [EXPLORE_MIX, REFINE_MIX], ids=["explore", "refine"])
def test_a_well_stocked_pool_gives_a_batch_exactly_its_allocation(mix: Mix) -> None:
    """With every **Zone** deep enough there is no **Shortfall**, so the draw is the **Allocation** the same
    seed rolls."""
    for seed in range(20):
        tiles = draw(mix, classified(bangers=30, duds=30, unknowns=60), 32, SeededRandom(seed))

        assert zone_counts(tiles) == Counter(allocate(mix, 32, SeededRandom(seed)))


def test_a_batch_is_drawn_under_the_mix_it_is_given() -> None:
    """Every slot guaranteed at this size, so this is the **Mix** arriving intact, not a lucky roll."""
    edited = Mix(name="explore", unknown=50, banger=45, dud=5)
    duds_only = Mix(name="duds only", unknown=0, banger=0, dud=100)
    pool = classified(bangers=30, duds=30, unknowns=60)

    assert zone_counts(draw(edited, pool, 20, SeededRandom(1))) == {
        Zone.UNKNOWN: 10,
        Zone.BANGER: 9,
        Zone.DUD: 1,
    }
    assert zone_counts(draw(duds_only, pool, 20, SeededRandom(1))) == {Zone.DUD: 20}


def test_no_wallpaper_is_shown_twice_in_one_batch() -> None:
    """A **Shortfall** taking more from a **Zone** already drawn from is the shape that would duplicate."""
    tiles = draw(EXPLORE_MIX, classified(bangers=4, duds=4, unknowns=8), 16, SeededRandom(1))

    assert len({tile.wallpaper.id for tile in tiles}) == 16


@pytest.mark.parametrize(
    ("bangers", "duds", "unknowns", "size", "expected"),
    [
        # A new Decision log: Explore's Bangers have nowhere to come from, so Unknown takes the Shortfall.
        pytest.param(0, 0, 20, 8, {Zone.UNKNOWN: 8}, id="no bangers: all unknown"),
        # Unknown, then Banger, then Dud: six Unknown slots and no Unknowns, four Bangers, two Duds.
        pytest.param(4, 20, 0, 8, {Zone.BANGER: 4, Zone.DUD: 4}, id="no unknowns: bangers before duds"),
        pytest.param(
            1, 1, 3, 32, {Zone.UNKNOWN: 3, Zone.BANGER: 1, Zone.DUD: 1}, id="a pool smaller than the batch"
        ),
    ],
)
def test_a_shortfall_is_filled_unknown_first_then_banger_then_dud(
    bangers: int, duds: int, unknowns: int, size: int, expected: dict[Zone, int]
) -> None:
    """Every **Zone** exhausted is a shorter **Batch**, never an error and never a repeat. Each tile keeps the
    **Zone** it came from, never the slot it filled."""
    tiles = draw(
        EXPLORE_MIX, classified(bangers=bangers, duds=duds, unknowns=unknowns), size, SeededRandom(1)
    )

    assert zone_counts(tiles) == Counter(expected)
    assert len({tile.wallpaper.id for tile in tiles}) == len(tiles)


def test_a_zone_that_falls_short_is_made_up_from_unknown() -> None:
    """Whatever the roll, the slots one **Banger** and no **Duds** cannot fill come back to **Unknown**: a
    **Batch** shrinks towards discovery, not towards what has already been judged."""
    for seed in range(20):
        counts = zone_counts(draw(EXPLORE_MIX, classified(bangers=1, unknowns=20), 8, SeededRandom(seed)))

        assert counts[Zone.DUD] == 0
        assert counts[Zone.BANGER] <= 1
        assert counts[Zone.UNKNOWN] == 8 - counts[Zone.BANGER]


def test_banger_slots_take_the_highest_scores() -> None:
    """Not a sample: the **Bangers** drawn are the top of the **Score** order."""
    pool = [
        ScoredWallpaper(wallpaper=s.wallpaper, score=SCORE - rank, zone=s.zone)
        if s.zone is Zone.BANGER
        else s
        for rank, s in enumerate(classified(bangers=8, unknowns=20))
    ]

    tiles = draw(REFINE_MIX, pool, 6, SeededRandom(1))

    taken = {tile.wallpaper.id for tile in tiles if tile.zone is Zone.BANGER}
    assert len(taken) >= 4
    assert taken == {w.wallpaper.id for w in pool[: len(taken)]}


def test_unknown_slots_are_sampled_rather_than_taken_in_order() -> None:
    """Different seeds differ and the same seed repeats."""

    def drawn(seed: int) -> set[str]:
        return {
            tile.wallpaper.id for tile in draw(EXPLORE_MIX, classified(unknowns=40), 8, SeededRandom(seed))
        }

    assert drawn(1) != drawn(2)
    assert drawn(1) == drawn(1)


def test_a_tiles_position_does_not_give_its_zone_away() -> None:
    """Shuffled at the end: the **Zones** are not laid out in `ZONE_ORDER` along the **Batch**."""
    orders = {
        tuple(
            tile.zone
            for tile in draw(EXPLORE_MIX, classified(bangers=8, duds=8, unknowns=8), 8, SeededRandom(s))
        )
        for s in range(20)
    }

    assert len(orders) > 1
    assert any(list(order) != sorted(order, key=ZONE_ORDER.index) for order in orders)


def test_the_long_run_zone_proportions_are_the_mix() -> None:
    """The whole draw, allocate to shuffle: fails if any step leaks a **Zone** or drops the leftover roll."""
    draws = 400
    counted: Counter[Zone] = Counter()
    for seed in range(draws):
        counted.update(
            zone_counts(
                draw(EXPLORE_MIX, classified(bangers=30, duds=30, unknowns=60), 32, SeededRandom(seed))
            )
        )

    slots = sum(counted.values())
    assert slots == draws * 32
    for zone in ZONE_ORDER:
        assert counted[zone] / slots == pytest.approx(EXPLORE_MIX.percentage(zone) / MIX_TOTAL, abs=0.01)


# -- through the module ------------------------------------------------------------------------------


@pytest.fixture
def rig() -> Iterator[BatchesRig]:
    """Eight to a **Batch** over a **Pool** of twenty-four, every one **Unknown**."""
    with batches_rig() as made:
        made.configure(batch_size=8)
        made.stock(catalogue_of(24))
        yield made


# -- minting


def test_a_batch_is_minted_with_the_injected_id_the_configured_size_and_utc_from_the_clock(
    rig: BatchesRig,
) -> None:
    """Invariant 5: `created_at` is stored as an ISO 8601 UTC string from the injected clock."""
    rig.clock.advance(3600)

    batch = rig.next()

    assert (batch.id, batch.size, len(batch.wallpapers)) == ("batch-1", 8, 8)
    assert batch.created_at == FIXED_NOW + dt.timedelta(hours=1)
    (stored,) = rig.connection.execute("SELECT created_at FROM batches").fetchone()
    assert stored == batch.created_at.isoformat()
    assert dt.datetime.fromisoformat(stored).tzinfo == dt.UTC


def test_a_new_batch_size_applies_to_the_next_batch_minted_and_not_the_live_one(rig: BatchesRig) -> None:
    """Rebuilding the live **Batch** to fit would discard the **Draft Batch** marked against it."""
    opened = rig.next()

    rig.configure(batch_size=2)
    still_live = rig.next()
    assert (still_live.id, len(still_live.wallpapers)) == (opened.id, 8)

    rig.submit(opened.id)

    following = rig.next()
    assert (following.size, len(following.wallpapers)) == (2, 2)


def test_asking_again_hands_back_the_live_batch_rather_than_minting_another(rig: BatchesRig) -> None:
    """A refresh is not a decision (ADR 0002). Read back from storage, so the **Zones** come back too."""
    first = rig.next()

    again = rig.next()

    assert again == first
    assert rig.minted == ["batch-1"]


def test_a_batch_is_drawn_under_the_active_mix_and_records_where_each_tile_came_from(rig: BatchesRig) -> None:
    """A **Favourite** and a **Ban** outside the **Pool** make it eight **Bangers**, eight **Duds** and eight
    **Unknowns**; a **Mix** of all **Duds** then draws only **Duds**, labelled as the classification has them.
    """
    rig.stock([wallpaper("loved"), wallpaper("loathed")])
    with storage.write(rig.connection) as write:
        decisions.append(
            write, {"loved": Verdict.FAVOURITE, "loathed": Verdict.BAN}, batch_id=None, at=FIXED_NOW
        )
        pool.retire(write, ["loved", "loathed"])
        settings.save_mix(write, "duds only", unknown=0, banger=0, dud=100)
    ids = [w.id for w in catalogue_of(24)]
    rig.similarity.similarity_by_pair.update(
        {(i, "loved"): 0.95 for i in ids[:8]} | {(i, "loathed"): 0.95 for i in ids[8:16]}
    )
    rig.configure(active_mix="duds only")

    batch = rig.next()

    classified = {s.wallpaper.id: s.zone for s in rig.batches.classify(rig.connection)}
    assert Counter(classified.values()) == {Zone.BANGER: 8, Zone.DUD: 8, Zone.UNKNOWN: 8}
    assert {w.id for w in batch.wallpapers} == set(ids[8:16])
    assert batch.zones == {w.id: classified[w.id] for w in batch.wallpapers}


def _ban_near_everything(rig: BatchesRig) -> None:
    """A **Ban** outside the **Pool** that every member sits within the radius of: the whole **Pool** a
    **Dud**, as the maintainer's was at 0.15 (ADR 0023)."""
    rig.stock([wallpaper("loathed")])
    with storage.write(rig.connection) as write:
        decisions.append(write, {"loathed": Verdict.BAN}, batch_id=None, at=FIXED_NOW)
        pool.retire(write, ["loathed"])
    rig.similarity.similarity_by_pair.update({(w.id, "loathed"): 1.0 for w in catalogue_of(24)})


def test_a_batch_minted_from_a_pool_with_no_unknowns_says_the_unknown_zone_is_short(rig: BatchesRig) -> None:
    """**Explore** asks six of eight from **Unknown**, and the radius has left it none."""
    _ban_near_everything(rig)

    batch = rig.next()

    assert batch.unknown_short == UnknownShort(unknowns=0, slots=6, mix=EXPLORE_MIX.name)
    assert {batch.zones[w.id] for w in batch.wallpapers} == {Zone.DUD}


def test_a_batch_minted_with_unknowns_to_spare_says_nothing(rig: BatchesRig) -> None:
    assert rig.next().unknown_short is None


def test_unknowns_exactly_filling_the_slots_are_not_short(rig: BatchesRig) -> None:
    """Six **Unknowns** for **Explore**'s six slots: every roll is served."""
    _ban_near_everything(rig)
    rig.similarity.similarity_by_pair.update({(w.id, "loathed"): 0.0 for w in catalogue_of(6)})

    assert rig.next().unknown_short is None


def test_a_pool_too_small_for_a_batch_is_not_blamed_on_the_radius(rig: BatchesRig) -> None:
    """Every member **Unknown**, but fewer than the slots: the refill line already says why, and lowering
    the radius would not help."""
    with storage.write(rig.connection) as write:
        pool.retire(write, [w.id for w in catalogue_of(24)[3:]])

    batch = rig.next()

    assert len(batch.wallpapers) == 3
    assert batch.unknown_short is None


def test_the_unknown_zone_is_judged_at_mint_and_not_on_a_reload(rig: BatchesRig) -> None:
    """Derived from the classification the mint computes and never stored (ADR 0023): the live **Batch**
    read back has nothing to say."""
    _ban_near_everything(rig)
    minted = rig.next()

    reloaded = rig.next()

    assert minted.unknown_short is not None
    assert reloaded.unknown_short is None
    assert reloaded == minted, "not part of what the Batch is"


def test_an_empty_pool_with_no_refill_yet_says_so(rig: BatchesRig) -> None:
    rig.empty_the_pool()

    result = rig.batches.next(rig.connection)

    assert result == BatchUnavailable(reason=BatchUnavailable.Reason.POOL_EMPTY)
    assert rig.minted == []


def test_an_empty_pool_after_a_failed_refill_says_wallhaven_is_unreachable(rig: BatchesRig) -> None:
    """The error and when, read off the Refill's status: more help than "not working"."""
    rig.empty_the_pool()
    rig.status = RefillStatus(
        pool_size=0,
        target_size=200,
        running=True,
        last_run_at=FIXED_NOW,
        last_error="connection refused",
        last_error_at=FIXED_NOW,
        last_strategy=RefillStrategy.RANDOM,
        by_strategy={RefillStrategy.RANDOM: 0, RefillStrategy.LIKE: 0},
    )

    result = rig.batches.next(rig.connection)

    assert result == BatchUnavailable(
        reason=BatchUnavailable.Reason.WALLHAVEN_UNREACHABLE, error="connection refused", error_at=FIXED_NOW
    )


# -- near-duplicates of a Ban

REPOST = catalogue_of(24)[0].id
"""The **Pool** member arranged against the **Ban**."""


def unit_at(similarity: float) -> NDArray[np.float32]:
    """A unit vector whose mapped similarity to `(1, 0)` is `similarity`."""
    cosine = np.float32(2.0 * similarity - 1.0)
    return np.array([cosine, np.sqrt(1.0 - float(cosine) ** 2)], dtype=np.float32)


def _ban_one_outside_the_pool(rig: BatchesRig, *, repost: NDArray[np.float32] | None) -> None:
    """**Ban** "loathed", retired as every decision is, embedded at `(1, 0)`; `REPOST` gets `repost`, or no
    **Embedding**. The fallback calls the pair 0.95, so without the veto `REPOST` is a **Dud**."""
    rig.stock([wallpaper("loathed")])
    with storage.write(rig.connection) as write:
        decisions.append(write, {"loathed": Verdict.BAN}, batch_id=None, at=FIXED_NOW)
        pool.retire(write, ["loathed"])
    rig.similarity.similarity_by_pair[(REPOST, "loathed")] = 0.95
    rig.store.store("loathed", np.array([1.0, 0.0], dtype=np.float32))
    if repost is not None:
        rig.store.store(REPOST, repost)


def _zones(rig: BatchesRig) -> dict[str, Zone]:
    return {s.wallpaper.id: s.zone for s in rig.batches.classify(rig.connection)}


def test_a_near_duplicate_of_a_ban_is_in_no_zone(rig: BatchesRig) -> None:
    """ADR 0021: a repost of a **Banned** image is excluded the way the **Ban** is, whatever its **Score**.
    It stays in the **Pool**; only the classification leaves it out."""
    _ban_one_outside_the_pool(rig, repost=unit_at(NEAR_DUPLICATE_SIMILARITY))

    zones = _zones(rig)

    assert REPOST not in zones
    assert len(zones) == 23
    assert REPOST in {w.id for w in pool.members(rig.connection)}


def test_a_wallpaper_just_short_of_a_near_duplicate_is_classified_as_before(rig: BatchesRig) -> None:
    """The ordinary spread still reaches it: a look-alike of a **Ban** is a **Dud**, not vetoed."""
    _ban_one_outside_the_pool(rig, repost=unit_at(NEAR_DUPLICATE_SIMILARITY - 0.0001))

    assert _zones(rig)[REPOST] is Zone.DUD


def test_a_history_edit_away_from_ban_brings_the_near_duplicate_back(rig: BatchesRig) -> None:
    """Derived, never stored: nothing to invalidate, the next classification sees the **Ignore**."""
    _ban_one_outside_the_pool(rig, repost=unit_at(1.0))
    assert REPOST not in _zones(rig)

    with storage.write(rig.connection) as write:
        decisions.edit(write, "loathed", Verdict.IGNORE, at=FIXED_NOW)

    assert _zones(rig)[REPOST] is Zone.DUD


def test_an_unembedded_wallpaper_is_never_vetoed_however_alike_the_fallback_calls_it(rig: BatchesRig) -> None:
    """Flat-colour images score 1.0 on colours and category; only two **Embeddings** can say "the same
    image"."""
    _ban_one_outside_the_pool(rig, repost=None)
    rig.similarity.similarity_by_pair[(REPOST, "loathed")] = 1.0

    assert _zones(rig)[REPOST] is Zone.DUD


# -- no Embedding once the model is open (ADR 0024)

NEWCOMER = catalogue_of(24)[0].id
"""The **Pool** member the fallback puts near the **Favourite**."""


def _favour_one_outside_the_pool(rig: BatchesRig, *, embedded: bool) -> None:
    """**Favourite** "loved", retired as every decision is. The fallback calls `NEWCOMER` 0.95 to it, inside
    the radius, so while the fallback answers `NEWCOMER` is a **Banger**."""
    rig.stock([wallpaper("loved")])
    with storage.write(rig.connection) as write:
        decisions.append(write, {"loved": Verdict.FAVOURITE}, batch_id=None, at=FIXED_NOW)
        pool.retire(write, ["loved"])
    rig.similarity.similarity_by_pair[(NEWCOMER, "loved")] = 0.95
    rig.store.store("loved" if embedded else NEWCOMER, np.array([1.0, 0.0], dtype=np.float32))


def _scored(rig: BatchesRig) -> dict[str, ScoredWallpaper]:
    return {s.wallpaper.id: s for s in rig.batches.classify(rig.connection)}


def test_once_the_model_is_open_an_unembedded_pool_member_is_an_honest_unknown(
    rig: BatchesRig, tmp_path: Path
) -> None:
    """Not placed by its palette against embedded neighbours: a **Score** of exactly 0.0."""
    _favour_one_outside_the_pool(rig, embedded=True)
    assert _scored(rig)[NEWCOMER].zone is Zone.BANGER

    rig.open_model(tmp_path / "never_created")

    newcomer = _scored(rig)[NEWCOMER]
    assert (newcomer.score, newcomer.zone) == (0.0, Zone.UNKNOWN)


def test_once_the_model_is_open_an_unembedded_decided_wallpaper_contributes_nothing(
    rig: BatchesRig, tmp_path: Path
) -> None:
    """Its embedded **Pool** neighbour is one the fallback would put inside the radius."""
    _favour_one_outside_the_pool(rig, embedded=False)
    assert _scored(rig)[NEWCOMER].zone is Zone.BANGER

    rig.open_model(tmp_path / "never_created")

    scored = _scored(rig)
    assert len(scored) == 24
    assert {s.score for s in scored.values()} == {0.0}
    assert {s.zone for s in scored.values()} == {Zone.UNKNOWN}


# -- the Draft Batch


def test_a_mark_is_set_rather_than_toggled_and_comes_back_on_the_batch(rig: BatchesRig) -> None:
    """A replayed htmx post must not flip the mark back off; a second control replaces the first, since the
    draft is keyed `(batch, wallpaper)`; and `None` deletes the row, because absence already means
    **Ignore**. Each answer is the **Batch** as it stands after the write."""
    batch = rig.next()
    marked = batch.wallpapers[0].id

    for verdict in (Verdict.FAVOURITE, Verdict.LIKE, Verdict.LIKE, Verdict.BAN):
        answered = rig.set_draft(batch.id, marked, verdict)
        assert isinstance(answered, Batch)
        assert answered.drafts == rig.next().drafts == {marked: verdict}

    cleared = rig.set_draft(batch.id, marked, None)

    assert isinstance(cleared, Batch)
    assert cleared.drafts == rig.next().drafts == {}
    assert rig.drafted_rows() == 0


def test_select_all_replaces_every_mark_and_select_none_deletes_them(rig: BatchesRig) -> None:
    """One transaction for the whole **Batch**, not one per tile. "All" is every tile shown, marked or not."""
    batch = rig.next()
    rig.set_draft(batch.id, batch.wallpapers[0].id, Verdict.FAVOURITE)

    banned = rig.set_all_drafts(batch.id, Verdict.BAN)
    assert isinstance(banned, Batch)
    assert banned.drafts == rig.next().drafts == {w.id: Verdict.BAN for w in batch.wallpapers}

    cleared = rig.set_all_drafts(batch.id, None)
    assert isinstance(cleared, Batch)
    assert cleared.drafts == rig.next().drafts == {}


DRAFTS: dict[str, Callable[[BatchesRig, str, str, Verdict | None], Batch | SubmissionRefused]] = {
    "mark": lambda rig, batch_id, wallpaper_id, verdict: rig.set_draft(batch_id, wallpaper_id, verdict),
    "mark all": lambda rig, batch_id, _, verdict: rig.set_all_drafts(batch_id, verdict),
}


@pytest.mark.parametrize("operation", DRAFTS)
def test_an_ignore_cannot_be_drafted(rig: BatchesRig, operation: str) -> None:
    """**Ignore** is derived at submit from an absent row; a stored one would be a second shape of nothing."""
    batch = rig.next()

    refused = DRAFTS[operation](rig, batch.id, batch.wallpapers[0].id, Verdict.IGNORE)

    assert refused == SubmissionRefused(reason=SubmissionRefused.Reason.IGNORE_DRAFTED)
    assert rig.drafted_rows() == 0


def test_drafting_a_wallpaper_the_batch_does_not_show_writes_nothing(rig: BatchesRig) -> None:
    """No control can post this, but a hand-made post could; it is refused before anything is written."""
    batch = rig.next()
    stray = next(w.id for w in catalogue_of(24) if w not in batch.wallpapers)

    refused = rig.set_draft(batch.id, stray, Verdict.FAVOURITE)

    assert refused == SubmissionRefused(reason=SubmissionRefused.Reason.NOT_IN_BATCH)
    assert rig.drafted_rows() == 0


def test_bulk_marking_one_batch_leaves_an_earlier_batchs_record_alone(rig: BatchesRig) -> None:
    """Only one unsubmitted **Batch** exists at a time, so the reachable neighbour of a bulk write that forgot
    its `WHERE batch_id = ?` is an earlier, recorded **Batch**."""
    first = rig.next()
    rig.set_draft(first.id, first.wallpapers[0].id, Verdict.FAVOURITE)
    rig.submit(first.id)
    recorded_before = rig.logged(first.id)

    second = rig.next()
    rig.set_all_drafts(second.id, Verdict.BAN)

    assert rig.logged(first.id) == recorded_before
    assert rig.next().drafts == {w.id: Verdict.BAN for w in second.wallpapers}


def _shown_twice(rig: BatchesRig, batch: Batch) -> None:
    del batch
    rig.next()


def _three_marked(rig: BatchesRig, batch: Batch) -> None:
    for w, verdict in zip(batch.wallpapers, (Verdict.FAVOURITE, Verdict.LIKE, Verdict.BAN), strict=False):
        rig.set_draft(batch.id, w.id, verdict)


def _all_banned(rig: BatchesRig, batch: Batch) -> None:
    rig.set_all_drafts(batch.id, Verdict.BAN)


@pytest.mark.parametrize(
    "act",
    [_shown_twice, _three_marked, _all_banned],
    ids=["shown and reloaded", "tiles marked", "all banned"],
)
def test_a_batch_that_is_never_submitted_records_nothing(
    rig: BatchesRig, act: Callable[[BatchesRig, Batch], None]
) -> None:
    """Being shown or clicked at is not a **Verdict**: a **Wallpaper** must never be written off by a
    misclick."""
    batch = rig.next()

    act(rig, batch)

    assert rig.logged() == []
    assert pool.members(rig.connection) == list(catalogue_of(24))


# -- the claim

OPERATIONS: dict[str, Callable[[BatchesRig, str], object]] = {
    "mark": lambda rig, batch_id: rig.set_draft(batch_id, "wp0000", Verdict.FAVOURITE),
    "mark all": lambda rig, batch_id: rig.set_all_drafts(batch_id, Verdict.FAVOURITE),
    "submit": lambda rig, batch_id: rig.submit(batch_id),
}


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("already_submitted", [False, True], ids=["unknown batch", "already submitted"])
def test_an_unknown_or_already_submitted_batch_is_refused_and_records_nothing(
    rig: BatchesRig, operation: str, already_submitted: bool
) -> None:
    """Two tabs is a real case: one submits, and a click in the stale one must be told why nothing happened.
    One refusal type for all three, so the page keeps one error branch."""
    batch_id = "no-such-batch"
    if already_submitted:
        batch_id = rig.next().id
        rig.submit(batch_id)
    before = (rig.logged(), rig.drafted_rows())

    refusal = OPERATIONS[operation](rig, batch_id)

    reason = (
        SubmissionRefused.Reason.ALREADY_SUBMITTED
        if already_submitted
        else SubmissionRefused.Reason.UNKNOWN_BATCH
    )
    assert refusal == SubmissionRefused(reason=reason)
    assert (rig.logged(), rig.drafted_rows()) == before


# -- submitting


def test_two_submits_of_one_batch_append_once_and_the_second_is_refused(rig: BatchesRig) -> None:
    """Refused, never ignored: a second success would make the stale tab look as if it had recorded."""
    batch = rig.next()
    rig.set_draft(batch.id, batch.wallpapers[0].id, Verdict.LIKE)

    first = rig.submit(batch.id)
    second = rig.submit(batch.id)

    assert first == Submitted(batch_id=batch.id, recorded=8, ignored=7)
    assert second == SubmissionRefused(reason=SubmissionRefused.Reason.ALREADY_SUBMITTED)
    assert len(rig.logged()) == 8


@pytest.mark.parametrize("cleared_first", [False, True], ids=["untouched", "select-all then select-none"])
def test_an_empty_submission_records_an_ignore_for_every_wallpaper_shown(
    rig: BatchesRig, cleared_first: bool
) -> None:
    """Clearing the **Batch** is not recording nothing: absence in the draft is what an **Ignore** is derived
    from."""
    batch = rig.next()
    if cleared_first:
        rig.set_all_drafts(batch.id, Verdict.FAVOURITE)
        rig.set_all_drafts(batch.id, None)

    summary = rig.submit(batch.id)

    assert summary == Submitted(batch_id=batch.id, recorded=8, ignored=8)
    assert {e.wallpaper_id: e.entry for e in rig.logged(batch.id)} == dict.fromkeys(
        (w.id for w in batch.wallpapers), Verdict.IGNORE
    )


def test_submitting_records_the_drafted_verdicts_plus_an_ignore_for_everything_unmarked(
    rig: BatchesRig,
) -> None:
    """Select-all, then one tile changed and one cleared: exactly what the screen showed."""
    batch = rig.next()
    rig.set_all_drafts(batch.id, Verdict.LIKE)
    favourite, cleared, *_ = [w.id for w in batch.wallpapers]
    rig.set_draft(batch.id, favourite, Verdict.FAVOURITE)
    rig.set_draft(batch.id, cleared, None)

    summary = rig.submit(batch.id)

    assert summary == Submitted(batch_id=batch.id, recorded=8, ignored=1)
    changed = {favourite: Verdict.FAVOURITE, cleared: Verdict.IGNORE}
    assert {e.wallpaper_id: e.entry for e in rig.logged(batch.id)} == {
        w.id: changed.get(w.id, Verdict.LIKE) for w in batch.wallpapers
    }


def test_the_whole_submission_shares_one_utc_timestamp_and_an_ascending_sequence(rig: BatchesRig) -> None:
    """One transaction, since an append-only log cannot retract a half-recorded **Batch**; the shared
    timestamp is what makes the sequence load-bearing (invariant 4). `submitted_at` is the same moment, stored
    as an ISO 8601 UTC string from the injected clock (invariant 5)."""
    batch = rig.next()
    rig.clock.advance(60)
    submitted_at = FIXED_NOW + dt.timedelta(minutes=1)

    rig.submit(batch.id)

    logged = rig.logged(batch.id)
    assert {e.recorded_at for e in logged} == {submitted_at}
    assert [e.seq for e in logged] == sorted({e.seq for e in logged})
    (stored,) = rig.connection.execute("SELECT submitted_at FROM batches").fetchone()
    assert stored == submitted_at.isoformat()
    assert dt.datetime.fromisoformat(stored).tzinfo == dt.UTC


def test_submitting_retires_everything_shown_and_clears_the_draft(rig: BatchesRig) -> None:
    """Decided once (ADR 0016): explicit or not, everything shown leaves the **Pool** in the same transaction,
    the rest stays, and the next **Batch** is drawn from what nobody has seen."""
    batch = rig.next()
    rig.set_draft(batch.id, batch.wallpapers[0].id, Verdict.LIKE)

    rig.submit(batch.id)

    assert pool.members(rig.connection) == [w for w in catalogue_of(24) if w not in batch.wallpapers]
    assert rig.drafted_rows() == 0
    assert not set(batch.wallpapers) & set(rig.next().wallpapers)


def test_a_history_edit_brings_no_submitted_wallpaper_back_into_the_pool(rig: BatchesRig) -> None:
    """**History** changes the **Decision log**, not what may be shown (ADR 0016)."""
    batch = rig.next()
    rig.submit(batch.id)
    left = pool.members(rig.connection)
    liked = batch.wallpapers[0].id

    assert isinstance(rig.edit(liked, Verdict.FAVOURITE), HistoryEntry)

    assert pool.members(rig.connection) == left
    assert liked not in {w.id for w in rig.next().wallpapers}


def test_pruning_leaves_the_live_batch_and_its_drafts_alone(rig: BatchesRig) -> None:
    """A **Batch** holds its own rows, so a **Pool** row going cannot take a tile or its mark with it. The
    settings save and the prune in one write, as the settings page makes them."""
    first = rig.next()
    marked = first.wallpapers[0].id
    rig.set_draft(first.id, marked, Verdict.FAVOURITE)

    with storage.write(rig.connection) as write:
        updated = settings.update(write, allowed_ratios="1x1")
        assert isinstance(updated, settings.Settings)
        pool.prune(write, updated)

    assert pool.members(rig.connection) == []
    still_live = rig.next()
    assert still_live.id == first.id
    assert still_live.wallpapers == first.wallpapers
    assert still_live.drafts[marked] is Verdict.FAVOURITE


def test_showing_is_the_live_batch_until_it_is_submitted(rig: BatchesRig) -> None:
    """What eviction keeps besides the **Pool**: a tile on screen, pruned from the **Pool** or not."""
    assert batches.showing(rig.connection) == set()
    batch = rig.next()

    assert batches.showing(rig.connection) == {w.id for w in batch.wallpapers}

    rig.submit(batch.id)

    assert batches.showing(rig.connection) == set()


# -- History edits and the Batch (ADR 0015)

SUBJECT = "wp0000"


def _down_to(rig: BatchesRig, size: int) -> None:
    """Keep the first `size` of the **Pool** and make a **Batch** that size, so every one is drawn."""
    rig.retire(*(w.id for w in catalogue_of(24)[size:]))
    rig.configure(batch_size=size)


def test_a_ban_from_history_excludes_a_wallpaper_and_an_ignore_makes_it_eligible_again(
    rig: BatchesRig,
) -> None:
    """Un-**Banning** falls out of building **Batches** by *resolved* **Verdict**. Banned before any
    **Batch** showed it, since a shown **Wallpaper** has left the **Pool** for good (ADR 0016)."""
    _down_to(rig, 1)
    assert isinstance(rig.edit(SUBJECT, Verdict.BAN), HistoryEntry)
    assert isinstance(rig.batches.next(rig.connection), BatchUnavailable), "the Ban must exclude it"

    assert isinstance(rig.edit(SUBJECT, Verdict.IGNORE), HistoryEntry)

    reoffered = rig.next()
    assert [w.id for w in reoffered.wallpapers] == [SUBJECT]
    assert reoffered.drafts == {}, "an Ignore is not pre-filled: the tile comes up unmarked"


def test_a_history_edit_does_not_reach_the_batch_already_open(rig: BatchesRig) -> None:
    """Deliberately not handled (ADR 0015): the open tile is unmarked, so submitting it untouched records
    an **Ignore**, which is the latest entry and overturns the edit. The tile shows what it will record."""
    _down_to(rig, 1)
    open_batch = rig.next()

    assert isinstance(rig.edit(SUBJECT, Verdict.LIKE), HistoryEntry)

    assert rig.next() == open_batch, "the edit neither rerolled nor re-marked the open Batch"
    rig.submit(open_batch.id)
    assert decisions.resolve(rig.connection, [SUBJECT])[SUBJECT].verdict is Verdict.IGNORE


def test_a_reshown_wallpaper_comes_up_marked_with_its_latest_verdict(rig: BatchesRig) -> None:
    """Minted with each **Explicit Verdict** already in the **Draft Batch**, wherever it was decided. An
    **Ignore** is not pre-filled, and a **Ban** is never drawn. Decided from **History** and then minted:
    the one way a **Batch** still draws a decided **Wallpaper**, since an edit takes nothing out of the
    **Pool**."""
    _down_to(rig, 8)
    liked, favourite, banned, ignored, overturned = "wp0000", "wp0001", "wp0002", "wp0003", "wp0004"
    assert isinstance(rig.edit(overturned, Verdict.FAVOURITE), HistoryEntry)
    for wallpaper_id, verdict in {
        liked: Verdict.LIKE,
        favourite: Verdict.FAVOURITE,
        banned: Verdict.BAN,
        ignored: Verdict.IGNORE,
        overturned: Verdict.LIKE,
    }.items():
        assert isinstance(rig.edit(wallpaper_id, verdict), HistoryEntry)

    reshown = rig.next()

    assert reshown.drafts == {liked: Verdict.LIKE, favourite: Verdict.FAVOURITE, overturned: Verdict.LIKE}
    assert ignored in {w.id for w in reshown.wallpapers}
    assert banned not in {w.id for w in reshown.wallpapers}, "a Ban is never reshown"
    assert reshown == rig.next(), "read back from storage, so a reload keeps it"


def test_leaving_a_reshown_wallpaper_alone_records_its_verdict_again(rig: BatchesRig) -> None:
    _down_to(rig, 8)
    assert isinstance(rig.edit(SUBJECT, Verdict.LIKE), HistoryEntry)

    rig.submit(rig.next().id)

    assert [e.entry for e in decisions.entries(rig.connection, wallpaper_id=SUBJECT)] == [
        Verdict.LIKE,
        Verdict.LIKE,
    ]
    assert decisions.resolve(rig.connection, [SUBJECT])[SUBJECT] == ResolvedVerdict(
        verdict=Verdict.LIKE, value=50
    )
