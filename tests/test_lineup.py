"""The **Line-up**: drawing one beside the live **Batch**, keeping it until answered, and answering it with a
**Pick**, through `lineup` alone.

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


def test_a_reload_shows_the_same_lineup_until_it_is_answered(rig: LineupRig) -> None:
    """A refresh is not an answer (ADR 0002's rule): the same screen, read back from storage, with its mint
    time, **Mix** and **Ranking stamp**, and nothing minted a second time."""
    rig.clock.advance(90)
    minted = rig.lineup()
    rig.clock.advance(60)

    again = rig.lineup()

    assert again == minted
    assert again.created_at == FIXED_NOW + dt.timedelta(seconds=90)
    assert again.ranking == RankingStamp(read=None, space=FALLBACK_SPACE, radius=0.1, decay=4.0)
    assert rig.lineups_minted == ["lineup-1"]


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


# -- discarding, a Pool reset's half (ADR 0025)


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
