"""The **Line-up**: drawing one beside the live **Batch** on each visit, holding its stars while it is live,
and answering it with a **Pick**, through `lineup` alone.

On `conftest.LineupRig`: a real in-memory database, `Lineups` drawing through the rig's own `Batches.ranked`
and **Embeddings** as the composition root wires it, a real `Embeddings` falling back to hand-defined
similarities, and the fake clock. What a **Pick** is worth is `test_reductions.py`'s; what the **Evidence
log** makes of one is `test_evidence_log.py`'s.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Iterator

import pytest

from tests.conftest import FIXED_NOW, LineupRig, lineup_rig
from tests.fakes import catalogue_of, wallpaper
from wallpapi import evidence_log, lineup, pool, settings, storage
from wallpapi.batches import Batch
from wallpapi.evidence_log import Theme
from wallpapi.lineup import Lineup, LineupRefused, LineupUnavailable, Picked
from wallpapi.model import Verdict, Zone
from wallpapi.pool import RefillStatus, RefillStrategy
from wallpapi.settings import EXPLORE_MIX
from wallpapi.similarity import FALLBACK_SPACE
from wallpapi.taste.judgement import Judgement, RankingStamp, Shown
from wallpapi.taste.pick import PICK_V1, PickResponse, PickTile

STOCK = catalogue_of(12)
IDS = [w.id for w in STOCK]


@pytest.fixture
def rig() -> Iterator[LineupRig]:
    """Twelve in the **Pool**, every one **Unknown**: a **Batch** of eight leaves four for a **Line-up**."""
    with lineup_rig() as made:
        made.configure(batch_size=8)
        made.stock(STOCK)
        yield made


def ids(shown: Batch | Lineup) -> list[str]:
    """The **Wallpaper** ids a **Batch** or a **Line-up** shows, in on-screen order."""
    return [w.id for w in shown.wallpapers]


# -- drawing -----------------------------------------------------------------------------------------------


def test_a_lineup_is_drawn_from_the_pool_without_the_live_batchs_tiles(rig: LineupRig) -> None:
    """Twelve less the **Batch**'s eight is exactly the four a **Line-up** of four asks for, whatever the
    seed draws."""
    batch = rig.next()

    shown = rig.lineup()

    assert sorted(ids(shown)) == sorted(set(IDS) - set(ids(batch)))
    assert shown.id == "lineup-1"


def test_with_no_batch_live_a_lineup_of_the_configured_size_comes_from_the_whole_pool(rig: LineupRig) -> None:
    rig.configure(lineup_size=6)

    shown = rig.lineup()

    assert len(ids(shown)) == 6
    assert set(ids(shown)) <= set(IDS)


def test_a_lineup_is_drawn_under_the_active_mix_and_records_where_each_tile_came_from(rig: LineupRig) -> None:
    """A **Favourite** outside the **Pool** makes four **Bangers**; a **Mix** of all **Bangers** then
    lines up exactly those four, each labelled as the classification has it, under that **Mix**'s name."""
    rig.stock([wallpaper("loved")])
    rig.decide(Verdict.FAVOURITE, "loved")
    rig.retire("loved")
    rig.similarity.similarity_by_pair.update({(i, "loved"): 0.95 for i in IDS[:4]})
    with storage.write(rig.connection) as write:
        settings.save_mix(write, "bangers only", unknown=0, banger=100, dud=0)
    rig.configure(active_mix="bangers only")

    shown = rig.lineup()

    assert sorted(ids(shown)) == IDS[:4]
    assert shown.zones == dict.fromkeys(IDS[:4], Zone.BANGER)
    assert shown.slots == dict.fromkeys(IDS[:4], Zone.BANGER)
    assert shown.mix == "bangers only"


def test_a_tile_drawn_into_a_shortfall_remembers_the_zone_its_slot_was_meant_for(rig: LineupRig) -> None:
    """Half and half of four from an all-**Unknown** **Pool**: two **Banger** slots made up from
    **Unknown**."""
    with storage.write(rig.connection) as write:
        settings.save_mix(write, "halves", unknown=50, banger=50, dud=0)
    rig.configure(active_mix="halves")

    shown = rig.lineup()

    assert Counter((shown.zones[w], shown.slots[w]) for w in ids(shown)) == {
        (Zone.UNKNOWN, Zone.UNKNOWN): 2,
        (Zone.UNKNOWN, Zone.BANGER): 2,
    }


def test_asking_for_the_next_hands_back_the_live_lineup_until_it_is_answered(rig: LineupRig) -> None:
    """What a refused answer is shown (ADR 0029): the same screen, read back from storage, with its mint
    time, **Mix** and **Ranking stamp**, and nothing minted a second time."""
    rig.clock.advance(90)
    minted = rig.lineup()
    rig.clock.advance(60)

    again = rig.lineup()

    assert again == minted
    assert again.created_at == FIXED_NOW + dt.timedelta(seconds=90)
    assert again.ranking == RankingStamp(read=None, space=FALLBACK_SPACE, radius=0.1, decay=4.0)
    assert rig.lineups_minted == ["lineup-1"]


# -- a visit draws a new Line-up (ADR 0029)


def test_two_visits_draw_two_lineups_and_the_first_is_never_recorded(rig: LineupRig) -> None:
    """A visit replaces the live **Line-up**: nothing is recorded, so it adds no **Wear**, and its tiles stay
    in the **Pool**."""
    members = pool.members(rig.connection)
    first = rig.fresh_lineup()

    second = rig.fresh_lineup()

    assert (first.id, second.id) == ("lineup-1", "lineup-2")
    assert rig.lineup() == second
    assert rig.connection.execute("SELECT COUNT(*) FROM lineups").fetchone()[0] == 1
    assert rig.logged() == []
    assert set(evidence_log.times_compared(rig.connection, ids(first)).values()) == {0}
    assert pool.members(rig.connection) == members


def test_a_visit_drops_the_stars_of_the_lineup_it_replaces_and_records_none(rig: LineupRig) -> None:
    """A star lives only while its **Line-up** does: replaced, it is gone, nothing is a **Favourite**, and the
    starred **Wallpaper** stays in the **Pool**."""
    members = pool.members(rig.connection)
    first = rig.fresh_lineup()
    rig.star(first.id, ids(first)[0])

    second = rig.fresh_lineup()

    assert second.starred == frozenset()
    assert rig.starred_rows() == 0
    assert rig.logged() == []
    assert evidence_log.favourites(rig.connection) == []
    assert pool.members(rig.connection) == members


def test_answering_or_starring_a_replaced_lineup_is_refused_as_unknown_and_records_nothing(
    rig: LineupRig,
) -> None:
    """Two tabs on the compare tab: the second visit replaced the first tab's **Line-up**, so its answer and
    its star are refused, and the **Line-up** live now is untouched."""
    stale = rig.fresh_lineup()
    now_live = rig.fresh_lineup()

    picked = rig.pick(stale.id, ids(stale)[0])
    starred = rig.star(stale.id, ids(stale)[0])

    unknown = LineupRefused(reason=LineupRefused.Reason.UNKNOWN_LINEUP)
    assert (picked, starred) == (unknown, unknown)
    assert rig.logged() == []
    assert rig.lineup() == now_live


def test_a_visit_still_draws_without_the_live_batchs_tiles(rig: LineupRig) -> None:
    batch = rig.next()
    rig.fresh_lineup()

    shown = rig.fresh_lineup()

    assert sorted(ids(shown)) == sorted(set(IDS) - set(ids(batch)))


def test_a_visit_with_too_few_to_draw_still_drops_the_live_lineup(rig: LineupRig) -> None:
    """The **Line-up** drawn, then a **Batch** of eleven leaves one: a visit says there are too few, and the
    screen before it has ended all the same, its stars with it."""
    stale = rig.fresh_lineup()
    rig.star(stale.id, ids(stale)[0])
    rig.configure(batch_size=11)
    rig.fresh()

    result = rig.lineups.fresh(rig.connection)

    assert result == LineupUnavailable(reason=LineupUnavailable.Reason.TOO_FEW)
    assert lineup.live(rig.connection) is None
    assert rig.starred_rows() == 0


def test_a_new_size_or_mix_applies_to_the_next_lineup_and_not_the_one_on_screen(rig: LineupRig) -> None:
    rig.stock(catalogue_of(12, prefix="more"))
    minted = rig.lineup()

    rig.configure(lineup_size=8, active_mix="refine")
    still = rig.lineup()
    rig.pick(minted.id, None)
    following = rig.lineup()

    assert (still.id, len(ids(still)), still.mix) == (minted.id, 4, EXPLORE_MIX.name)
    assert (len(ids(following)), following.mix) == (8, "refine")


def test_the_live_lineup_is_read_without_minting_one(rig: LineupRig) -> None:
    assert lineup.live(rig.connection) is None
    assert lineup.showing(rig.connection) == set()

    shown = rig.lineup()

    assert lineup.live(rig.connection) == shown
    assert lineup.showing(rig.connection) == set(ids(shown))


@pytest.mark.parametrize("left", [0, 1], ids=["an empty pool", "one wallpaper"])
def test_fewer_than_two_to_compare_is_no_lineup(rig: LineupRig, left: int) -> None:
    """One **Wallpaper** compares with nothing, so it is no **Line-up** either."""
    rig.retire(*IDS[left:])

    assert rig.lineups.next(rig.connection) == LineupUnavailable(reason=LineupUnavailable.Reason.TOO_FEW)
    assert rig.lineups_minted == []


def test_a_pool_all_on_the_live_batch_is_too_few_to_line_up(rig: LineupRig) -> None:
    rig.configure(batch_size=12)
    rig.next()

    assert rig.lineups.next(rig.connection) == LineupUnavailable(reason=LineupUnavailable.Reason.TOO_FEW)


def test_too_few_after_a_failed_refill_says_wallhaven_is_unreachable(rig: LineupRig) -> None:
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

    assert rig.lineups.next(rig.connection) == LineupUnavailable(
        reason=LineupUnavailable.Reason.WALLHAVEN_UNREACHABLE, error="connection refused", error_at=FIXED_NOW
    )


# -- wear (ADR 0028) ---------------------------------------------------------------------------------------


def test_a_banger_shown_waits_until_its_score_over_one_plus_wear_is_the_best_again(rig: LineupRig) -> None:
    """Bangers A, B and C at about 90, 50 and 35 (a **Favourite** and a **Like** outside the **Pool**), one
    **Banger** **Slot** and the other an **Unknown**'s. A is shown first; once worn, A is worth 45 and B
    comes; with B worn too, A at 45 beats B at 25 and C at 35 again."""
    rig.stock([wallpaper("loved"), wallpaper("liked"), wallpaper("a"), wallpaper("b"), wallpaper("c")])
    rig.decide(Verdict.FAVOURITE, "loved")
    rig.decide(Verdict.LIKE, "liked")
    rig.retire("loved", "liked")
    rig.similarity.similarity_by_pair.update(
        {("a", "loved"): 0.975, ("b", "liked"): 1.0, ("c", "liked"): 0.91}
    )
    with storage.write(rig.connection) as write:
        settings.save_mix(write, "halves", unknown=50, banger=50, dud=0)
    rig.configure(active_mix="halves", lineup_size=2)

    bangers: list[str] = []
    for _ in range(3):
        shown = rig.lineup()
        (banger,) = [w for w in ids(shown) if shown.zones[w] is Zone.BANGER]
        bangers.append(banger)
        rig.pick(shown.id, banger)

    assert bangers == ["a", "b", "a"]


def test_every_unknown_is_lined_up_once_before_any_is_lined_up_twice(rig: LineupRig) -> None:
    """Twelve **Unknowns** and three **Line-ups** of four, picked or passed over: twelve different tiles. A
    draw blind to wear would repeat one with a chance of all but 1 in 495."""
    shown: list[str] = []
    for answer in range(3):
        lined_up = rig.lineup()
        shown += ids(lined_up)
        rig.pick(lined_up.id, None if answer == 1 else ids(lined_up)[0])

    assert sorted(shown) == IDS


def test_a_lineup_discarded_by_a_pool_reset_adds_no_wear(rig: LineupRig) -> None:
    """Only an answered **Line-up** is recorded, so only an answered one wears its tiles."""
    discarded = rig.lineup()
    rig.discard_lineup()
    answered = rig.lineup()
    rig.pick(answered.id, None)

    worn = evidence_log.times_compared(rig.connection, IDS)

    assert {w for w, times in worn.items() if times} == set(ids(answered))
    assert set(ids(discarded)) - set(ids(answered)), "the seed drew the discarded tiles again: no test"


# -- answering ---------------------------------------------------------------------------------------------


def _shown(shown: Lineup, tiles: dict[str, PickTile]) -> tuple[Shown, ...]:
    """The **Shown set** a **Judgement** of `shown` holds: each tile in order with why it was there."""
    return tuple(
        Shown(
            f"wallhaven:{w}",
            tiles.get(w, PickTile()),
            {"zone": shown.zones[w].value, "slot_zone": shown.slots[w].value, "mix": EXPLORE_MIX.name},
        )
        for w in ids(shown)
    )


def test_a_pick_records_one_judgement_of_the_whole_lineup_and_the_next_is_a_new_one(rig: LineupRig) -> None:
    """**Pick** version 1 on the compare **Screen**: every tile in on-screen order, the winner marked, why
    each was there, the stamp it was drawn under, first shown when it was minted, recorded at the clock's
    time with its local offset and the page's theme."""
    rig.clock.offset = dt.timedelta(hours=1)
    shown = rig.lineup()
    winner = ids(shown)[1]
    rig.clock.advance(5)

    picked = rig.pick(shown.id, winner, theme=Theme.DARK)

    assert picked == Picked(lineup_id=shown.id, winner=winner)
    assert rig.logged() == [
        Judgement(
            instrument=PICK_V1,
            screen="compare",
            shown=_shown(shown, {winner: PickTile(picked=True)}),
            recorded_at=FIXED_NOW + dt.timedelta(seconds=5),
            app="wallpapi",
            app_version=evidence_log.APP_VERSION,
            response=PickResponse(),
            utc_offset=dt.timedelta(hours=1),
            first_shown_at=FIXED_NOW,
            ranking=RankingStamp(read=None, space=FALLBACK_SPACE, radius=0.1, decay=4.0),
            context={"theme": "dark"},
        )
    ]
    assert rig.lineup().id == "lineup-2"


def test_none_of_these_records_a_judgement_with_no_winner_and_the_next_is_a_new_one(rig: LineupRig) -> None:
    shown = rig.lineup()

    picked = rig.pick(shown.id, None)

    assert picked == Picked(lineup_id=shown.id, winner=None)
    (judgement,) = rig.logged()
    assert (judgement.instrument, judgement.screen) == (PICK_V1, "compare")
    assert judgement.shown == _shown(shown, {})
    assert judgement.response == PickResponse(none_of_these=True)
    assert judgement.context == {}, "no theme sent, none stored"
    assert rig.lineup().id == "lineup-2"


def test_answering_takes_nothing_out_of_the_pool_and_decides_nothing(rig: LineupRig) -> None:
    """Being compared is not being decided (ADR 0027): every tile, picked or passed over, stays in the
    **Pool**, may be drawn again, and has no standing in **History**."""
    members = pool.members(rig.connection)
    first = rig.lineup()
    rig.pick(first.id, ids(first)[0])
    second = rig.lineup()
    rig.pick(second.id, None)

    assert pool.members(rig.connection) == members
    assert pool.decided(rig.connection) == []
    assert evidence_log.history(rig.connection).total == 0
    assert len(rig.logged()) == 2


def test_a_lineup_already_answered_or_never_minted_is_refused_and_records_nothing(rig: LineupRig) -> None:
    """Two tabs, or a key pressed twice: the second answer is about a screen that is gone."""
    shown = rig.lineup()
    rig.pick(shown.id, ids(shown)[0])

    again = rig.pick(shown.id, ids(shown)[1])
    unknown = rig.pick("never-minted", None)

    gone = LineupRefused(reason=LineupRefused.Reason.UNKNOWN_LINEUP)
    assert (again, unknown) == (gone, gone)
    assert len(rig.logged()) == 1


def test_a_winner_the_lineup_does_not_show_is_refused_and_records_nothing(rig: LineupRig) -> None:
    batch = rig.next()
    shown = rig.lineup()

    refused = rig.pick(shown.id, ids(batch)[0])

    assert refused == LineupRefused(reason=LineupRefused.Reason.NOT_IN_LINEUP)
    assert rig.logged() == []
    assert rig.lineup() == shown


# -- the star: a Favourite on any tile, held against the live Line-up while it is live


def test_a_star_is_held_against_the_live_lineup_and_read_back_while_it_is_live(rig: LineupRig) -> None:
    """Starring is not answering: nothing is recorded, nothing leaves the **Pool** and no new **Line-up** is
    drawn. The live **Line-up** read back carries the star."""
    members = pool.members(rig.connection)
    shown = rig.lineup()
    starred = ids(shown)[2]

    held = rig.star(shown.id, starred)
    read_back = rig.lineup()

    assert held == read_back
    assert read_back.id == shown.id
    assert read_back.starred == frozenset({starred})
    assert rig.lineups_minted == ["lineup-1"]
    assert rig.logged() == []
    assert pool.members(rig.connection) == members


def test_a_star_is_set_rather_than_toggled_and_taken_off_the_same_way(rig: LineupRig) -> None:
    """A replayed click lands on the same state, as a mark on the **Draft Batch** does."""
    shown = rig.lineup()
    first, second = ids(shown)[:2]

    rig.star(shown.id, first)
    rig.star(shown.id, first)
    both = rig.star(shown.id, second)
    assert isinstance(both, Lineup)
    assert both.starred == frozenset({first, second})
    assert rig.starred_rows() == 2

    rig.star(shown.id, first, starred=False)
    after = rig.star(shown.id, first, starred=False)

    assert isinstance(after, Lineup)
    assert after.starred == frozenset({second})
    assert rig.lineup().starred == frozenset({second})


def test_a_star_on_a_lineup_gone_or_a_tile_it_does_not_show_is_refused_and_writes_nothing(
    rig: LineupRig,
) -> None:
    batch = rig.next()
    shown = rig.lineup()

    not_shown = rig.star(shown.id, ids(batch)[0])
    unknown = rig.star("never-minted", ids(shown)[0])

    assert not_shown == LineupRefused(reason=LineupRefused.Reason.NOT_IN_LINEUP)
    assert unknown == LineupRefused(reason=LineupRefused.Reason.UNKNOWN_LINEUP)
    assert rig.starred_rows() == 0


@pytest.mark.parametrize("winner_at", [1, 0], ids=["another tile wins", "the starred tile wins"])
def test_star_then_pick_records_one_judgement_with_the_favourite_and_the_pick(
    rig: LineupRig, winner_at: int
) -> None:
    """The star rides in the one **Pick** **Judgement**, on its tile, as `PickTile.favourite`: the starred
    **Wallpaper** is then a **Favourite**, whichever tile won."""
    shown = rig.lineup()
    starred, winner = ids(shown)[0], ids(shown)[winner_at]
    rig.star(shown.id, starred)

    picked = rig.pick(shown.id, winner)

    assert picked == Picked(lineup_id=shown.id, winner=winner, starred=(starred,))
    (judgement,) = rig.logged()
    assert judgement.shown == _shown(
        shown, {starred: PickTile(favourite=True), winner: PickTile(picked=True, favourite=winner == starred)}
    )
    assert judgement.response == PickResponse()
    assert evidence_log.favourites(rig.connection) == [starred]
    assert evidence_log.resolve(rig.connection, [starred])[starred].verdict is Verdict.FAVOURITE


def test_none_of_these_with_a_starred_tile_still_records_the_favourite(rig: LineupRig) -> None:
    """Passing the screen over says nothing relative, but the star is still a **Favourite**."""
    shown = rig.lineup()
    first, third = ids(shown)[0], ids(shown)[2]
    rig.star(shown.id, third)
    rig.star(shown.id, first)

    picked = rig.pick(shown.id, None)

    assert picked == Picked(lineup_id=shown.id, winner=None, starred=(first, third)), "on-screen order"
    (judgement,) = rig.logged()
    assert judgement.shown == _shown(shown, dict.fromkeys((first, third), PickTile(favourite=True)))
    assert judgement.response == PickResponse(none_of_these=True)
    assert evidence_log.favourites(rig.connection) == sorted([first, third])


def test_only_the_starred_item_leaves_the_pool(rig: LineupRig) -> None:
    """A **Favourite** decides its **Wallpaper**, which retires it (ADR 0016); the winner and the rest were
    only compared, so they stay, undecided, and may be drawn again."""
    members = pool.members(rig.connection)
    shown = rig.lineup()
    starred, winner = ids(shown)[0], ids(shown)[1]
    rig.star(shown.id, starred)

    rig.pick(shown.id, winner)

    assert pool.members(rig.connection) == [w for w in members if w.id != starred]
    assert [w.id for w in pool.decided(rig.connection)] == [starred]
    assert starred not in ids(rig.lineup())


def test_the_stars_go_with_the_lineup_when_it_is_answered(rig: LineupRig) -> None:
    """Held against that **Line-up** alone: the next one starts with none."""
    shown = rig.lineup()
    rig.star(shown.id, ids(shown)[0])

    rig.pick(shown.id, None)

    assert rig.starred_rows() == 0
    assert rig.lineup().starred == frozenset()


# -- discarding, a Pool reset's half (ADR 0025)


def test_discarding_drops_a_starred_lineup_and_its_stars_and_decides_nothing(rig: LineupRig) -> None:
    """A star is not recorded until the **Line-up** is answered, so a discarded one records nothing and its
    **Wallpaper** stays in the **Pool**."""
    members = pool.members(rig.connection)
    shown = rig.lineup()
    rig.star(shown.id, ids(shown)[0])

    assert rig.discard_lineup() == 1

    assert rig.starred_rows() == 0
    assert rig.logged() == []
    assert pool.members(rig.connection) == members


def test_discarding_drops_the_live_lineup_without_recording_it(rig: LineupRig) -> None:
    """Housekeeping, not an answer: nothing recorded, and the next **Line-up** is a new one."""
    shown = rig.lineup()

    assert rig.discard_lineup() == 1

    assert lineup.live(rig.connection) is None
    assert rig.pick(shown.id, None) == LineupRefused(reason=LineupRefused.Reason.UNKNOWN_LINEUP)
    assert rig.logged() == []
    assert rig.lineup().id == "lineup-2"


def test_discarding_with_no_live_lineup_discards_nothing(rig: LineupRig) -> None:
    assert rig.discard_lineup() == 0
