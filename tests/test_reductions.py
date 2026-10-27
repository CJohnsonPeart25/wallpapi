"""The **Reductions**: each **Judgement** to its Evidence, through `evidence()`, the one function callers see.

Plain data in and out: a **Judgement** written out by hand, and the Evidence it must come to. The verdict
grid's two versions, **Pick** and **Order** are the **Reductions** built.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping

import pytest

from wallpapi.taste.evidence import AbsoluteEvidence, RelativeEvidence
from wallpapi.taste.judgement import Instrument, ItemResponse, Judgement, JudgementResponse, Shown
from wallpapi.taste.order import ORDER_V1, OrderTile
from wallpapi.taste.pick import PICK_V1, PickResponse, PickTile
from wallpapi.taste.reductions import UnknownInstrument, evidence
from wallpapi.taste.verdict_grid import (
    VERDICT_GRID_V0,
    VERDICT_GRID_V1,
    GridResponse,
    LegacyEntry,
    LegacyTileResponse,
    TileResponse,
    Verdict,
)

AT = dt.datetime(2026, 10, 27, 9, 0, 0, 123456, tzinfo=dt.UTC)
SUBMITTED = GridResponse(submitted=True)
SKIPPED = GridResponse()
UNMARKED = {"wallhaven:a": TileResponse(), "wallhaven:b": TileResponse()}


def judged(
    instrument: Instrument,
    tiles: Mapping[str, ItemResponse],
    response: JudgementResponse | None = None,
    *,
    screen: str = "batch",
    at: dt.datetime = AT,
) -> Judgement:
    return Judgement(
        instrument=instrument,
        screen=screen,
        shown=tuple(Shown(item, tile) for item, tile in tiles.items()),
        response=response,
        recorded_at=at,
        app="wallpapi",
        app_version="0.1.0",
    )


def strengths(judgement: Judgement) -> list[tuple[str, float]]:
    """What the **Judgement** comes to, as (Item, strength) in the order emitted."""
    pieces = evidence([judgement])
    assert all(isinstance(piece, AbsoluteEvidence) for piece in pieces)
    return [(piece.item, piece.strength) for piece in pieces if isinstance(piece, AbsoluteEvidence)]


def test_the_converted_grid_gives_each_entry_its_old_value_and_a_clearance_zero() -> None:
    """Version 0: Favourite 100, Like 50, Ignore -10, Ban -100, and a **Clearance** 0, which is still
    Evidence: as the latest it wins, and adds nothing."""
    judgement = judged(
        VERDICT_GRID_V0,
        {
            "wallhaven:f": LegacyTileResponse(LegacyEntry.FAVOURITE),
            "wallhaven:l": LegacyTileResponse(LegacyEntry.LIKE),
            "wallhaven:i": LegacyTileResponse(LegacyEntry.IGNORE),
            "wallhaven:b": LegacyTileResponse(LegacyEntry.BAN),
            "wallhaven:c": LegacyTileResponse(LegacyEntry.CLEARED),
        },
    )

    assert strengths(judgement) == [
        ("wallhaven:f", 100.0),
        ("wallhaven:l", 50.0),
        ("wallhaven:i", -10.0),
        ("wallhaven:b", -100.0),
        ("wallhaven:c", 0.0),
    ]


def test_a_converted_history_edit_reduces_the_same() -> None:
    judgement = judged(
        VERDICT_GRID_V0, {"wallhaven:x": LegacyTileResponse(LegacyEntry.BAN)}, screen="history"
    )

    assert strengths(judgement) == [("wallhaven:x", -100.0)]


def test_a_submitted_batch_gives_each_tile_its_mark_and_an_unmarked_tile_an_ignore() -> None:
    """Version 1: a **Favourite** 100, a **Like** 50, a **Ban** -100, and a tile left unmarked an
    **Ignore**, -10."""
    judgement = judged(
        VERDICT_GRID_V1,
        {
            "wallhaven:f": TileResponse(favourite=True),
            "wallhaven:l": TileResponse(verdict=Verdict.LIKE),
            "wallhaven:u": TileResponse(),
            "wallhaven:b": TileResponse(verdict=Verdict.BAN),
        },
        SUBMITTED,
    )

    assert strengths(judgement) == [
        ("wallhaven:f", 100.0),
        ("wallhaven:l", 50.0),
        ("wallhaven:u", -10.0),
        ("wallhaven:b", -100.0),
    ]


def test_a_submitted_batch_with_nothing_marked_is_all_ignores() -> None:
    judgement = judged(
        VERDICT_GRID_V1, {"wallhaven:a": TileResponse(), "wallhaven:b": TileResponse()}, SUBMITTED
    )

    assert strengths(judgement) == [("wallhaven:a", -10.0), ("wallhaven:b", -10.0)]


def test_a_skip_emits_nothing() -> None:
    """Every **Response** blank: the same tiles as an all-**Ignore** submission, but passed over."""
    judgement = judged(
        VERDICT_GRID_V1, {"wallhaven:a": TileResponse(), "wallhaven:b": TileResponse()}, SKIPPED
    )

    assert evidence([judgement]) == []


def test_a_skip_emits_nothing_whatever_was_marked_or_opened_on_it() -> None:
    """Should a **Skip** ever keep its **Draft Batch**'s marks or its opened previews, they stay raw in the
    log and count for nothing."""
    judgement = judged(
        VERDICT_GRID_V1,
        {
            "wallhaven:a": TileResponse(favourite=True, preview_opened=True),
            "wallhaven:b": TileResponse(verdict=Verdict.BAN),
        },
        SKIPPED,
    )

    assert evidence([judgement]) == []


def test_a_preview_opened_adds_nothing_to_what_the_tile_says() -> None:
    judgement = judged(
        VERDICT_GRID_V1,
        {
            "wallhaven:l": TileResponse(verdict=Verdict.LIKE, preview_opened=True),
            "wallhaven:u": TileResponse(preview_opened=True),
        },
        SUBMITTED,
    )

    assert strengths(judgement) == [("wallhaven:l", 50.0), ("wallhaven:u", -10.0)]


@pytest.mark.parametrize(
    ("tile", "strength"),
    [
        (TileResponse(favourite=True), 100.0),
        (TileResponse(verdict=Verdict.LIKE), 50.0),
        (TileResponse(verdict=Verdict.IGNORE), -10.0),
        (TileResponse(verdict=Verdict.BAN), -100.0),
    ],
    ids=["favourite", "like", "ignore", "ban"],
)
def test_a_history_edit_reduces_the_same(tile: TileResponse, strength: float) -> None:
    """One **Item** on the **History** **Screen**, an **Ignore** chosen there rather than left unmarked."""
    judgement = judged(VERDICT_GRID_V1, {"wallhaven:x": tile}, SUBMITTED, screen="history")

    assert strengths(judgement) == [("wallhaven:x", strength)]


def test_a_tile_is_never_both_a_favourite_and_a_verdict() -> None:
    with pytest.raises(ValueError, match="never both"):
        TileResponse(verdict=Verdict.BAN, favourite=True)


def test_each_piece_carries_its_judgements_id_and_recorded_time_in_judgement_order() -> None:
    first = judged(VERDICT_GRID_V1, {"wallhaven:a": TileResponse()}, SUBMITTED)
    later = AT + dt.timedelta(seconds=1)
    second = judged(VERDICT_GRID_V1, {"wallhaven:a": TileResponse(verdict=Verdict.LIKE)}, SUBMITTED, at=later)

    assert evidence([first, second]) == [
        AbsoluteEvidence(item="wallhaven:a", strength=-10.0, judgement_id=first.id, recorded_at=AT),
        AbsoluteEvidence(item="wallhaven:a", strength=50.0, judgement_id=second.id, recorded_at=later),
    ]


@pytest.mark.parametrize(
    "instrument",
    [Instrument("verdict grid", 2), Instrument("stream", 1), Instrument("Verdict grid", 1)],
    ids=["a later version", "an instrument not built", "a misspelt name"],
)
def test_an_unknown_instrument_raises(instrument: Instrument) -> None:
    judgement = judged(instrument, {"wallhaven:a": TileResponse()}, SUBMITTED)

    with pytest.raises(UnknownInstrument, match=instrument.name):
        evidence([judgement])


# -- Pick ----------------------------------------------------------------------------------------------


PICKED = PickTile(picked=True)
PASSED = PickTile()
ANSWERED = PickResponse()
NONE_OF_THESE = PickResponse(none_of_these=True)


def picked(tiles: Mapping[str, ItemResponse], response: JudgementResponse | None = ANSWERED) -> Judgement:
    """A **Pick** of a **Line-up** on the compare **Screen**, its tiles in on-screen order."""
    return judged(PICK_V1, tiles, response, screen="compare")


def pairs(judgement: Judgement) -> list[tuple[str, str, float]]:
    """What the **Judgement** comes to, as (winner, loser, strength) in the order emitted."""
    pieces = evidence([judgement])
    assert all(isinstance(piece, RelativeEvidence) for piece in pieces)
    return [
        (piece.winner, piece.loser, piece.strength) for piece in pieces if isinstance(piece, RelativeEvidence)
    ]


def test_the_pick_beats_each_other_tile_at_strength_one() -> None:
    """Three pairs from a **Line-up** of four, the losers in on-screen order, and no absolute Evidence: a
    **Pick** says which was best, not what any of them is worth."""
    judgement = picked(
        {"wallhaven:a": PASSED, "wallhaven:b": PICKED, "wallhaven:c": PASSED, "wallhaven:d": PASSED}
    )

    assert pairs(judgement) == [
        ("wallhaven:b", "wallhaven:a", 1.0),
        ("wallhaven:b", "wallhaven:c", 1.0),
        ("wallhaven:b", "wallhaven:d", 1.0),
    ]


def test_a_pick_carries_its_judgements_id_and_recorded_time() -> None:
    judgement = picked({"wallhaven:a": PICKED, "wallhaven:b": PASSED})

    assert evidence([judgement]) == [
        RelativeEvidence(
            winner="wallhaven:a", loser="wallhaven:b", strength=1.0, judgement_id=judgement.id, recorded_at=AT
        )
    ]


def test_none_of_these_emits_nothing() -> None:
    """Not an **Ignore**: nothing is said about any tile, and they all stay in the **Pool**."""
    judgement = picked({"wallhaven:a": PASSED, "wallhaven:b": PASSED}, NONE_OF_THESE)

    assert evidence([judgement]) == []


def test_a_starred_tile_is_a_favourite_beside_whatever_was_picked() -> None:
    """A **Favourite** on any tile of any **Instrument** is absolute 100, the grid's own, and it says nothing
    about the **Pick**: a starred loser is still beaten, and a star on a "none of these" still counts."""
    starred_loser = picked({"wallhaven:a": PICKED, "wallhaven:b": PickTile(favourite=True)})
    starred_winner = picked({"wallhaven:a": PickTile(picked=True, favourite=True), "wallhaven:b": PASSED})
    starred_none = picked({"wallhaven:a": PickTile(favourite=True), "wallhaven:b": PASSED}, NONE_OF_THESE)

    assert evidence([starred_loser]) == [
        AbsoluteEvidence("wallhaven:b", 100.0, judgement_id=starred_loser.id, recorded_at=AT),
        RelativeEvidence("wallhaven:a", "wallhaven:b", 1.0, judgement_id=starred_loser.id, recorded_at=AT),
    ]
    assert evidence([starred_winner]) == [
        AbsoluteEvidence("wallhaven:a", 100.0, judgement_id=starred_winner.id, recorded_at=AT),
        RelativeEvidence("wallhaven:a", "wallhaven:b", 1.0, judgement_id=starred_winner.id, recorded_at=AT),
    ]
    assert strengths(starred_none) == [("wallhaven:a", 100.0)]


# -- Order ---------------------------------------------------------------------------------------------


UNRANKED = OrderTile()


def ordered(*ranks: int | None, starred: tuple[int, ...] = ()) -> Judgement:
    """An **Order** of a **Line-up** on the compare **Screen**, tiles `wallhaven:a`, `b`, ... in on-screen
    order with these ranks (`None` for unranked), the tiles at the `starred` positions given a star."""
    return judged(
        ORDER_V1,
        {
            f"wallhaven:{chr(ord('a') + at)}": OrderTile(rank=rank, favourite=at in starred)
            for at, rank in enumerate(ranks)
        },
        screen="compare",
    )


def test_a_complete_order_gives_every_pair_best_first_each_winners_losers_in_on_screen_order() -> None:
    """Four tiles ranked c, a, d, b: six pairs, each the higher rank over the lower, strength 1 each, and no
    absolute Evidence. Each carries the **Judgement**'s id and recorded time."""
    judgement = ordered(2, 4, 1, 3)

    assert pairs(judgement) == [
        ("wallhaven:c", "wallhaven:a", 1.0),
        ("wallhaven:c", "wallhaven:b", 1.0),
        ("wallhaven:c", "wallhaven:d", 1.0),
        ("wallhaven:a", "wallhaven:b", 1.0),
        ("wallhaven:a", "wallhaven:d", 1.0),
        ("wallhaven:d", "wallhaven:b", 1.0),
    ]
    assert {(piece.judgement_id, piece.recorded_at) for piece in evidence([judgement])} == {
        (judgement.id, AT)
    }


def test_a_partial_order_ranks_its_tiles_over_each_other_and_every_unranked_one() -> None:
    """Two of five ranked: the first beats the second and the three unranked, the second the three
    unranked, and the unranked say nothing about each other."""
    judgement = ordered(None, 2, None, 1, None)

    assert pairs(judgement) == [
        ("wallhaven:d", "wallhaven:a", 1.0),
        ("wallhaven:d", "wallhaven:b", 1.0),
        ("wallhaven:d", "wallhaven:c", 1.0),
        ("wallhaven:d", "wallhaven:e", 1.0),
        ("wallhaven:b", "wallhaven:a", 1.0),
        ("wallhaven:b", "wallhaven:c", 1.0),
        ("wallhaven:b", "wallhaven:e", 1.0),
    ]


def test_one_ranked_tile_makes_the_same_claim_as_a_pick_of_it() -> None:
    order = ordered(None, 1, None)
    pick = picked({"wallhaven:a": PASSED, "wallhaven:b": PICKED, "wallhaven:c": PASSED})

    assert pairs(order) == pairs(pick)


def test_all_but_the_last_ranked_gives_the_pairs_of_the_complete_order_it_implies() -> None:
    """Ranking all but the last submits, the last place implied: whether the last tile is recorded at rank n
    or left unranked, it loses to every other and nothing else changes."""
    assert pairs(ordered(2, None, 1)) == pairs(ordered(2, 3, 1))


def test_an_empty_order_emits_nothing() -> None:
    """Nothing ranked is a blank **Response**: not an **Ignore** of anything, and every tile stays in the
    **Pool**."""
    assert evidence([ordered(None, None, None, None)]) == []


def test_a_starred_tile_on_an_order_is_a_favourite_whatever_its_rank() -> None:
    """A **Favourite** first, in **Shown set** order, as on a **Pick**: a starred tile left unranked is still
    beaten, and a star on an empty **Order** still counts."""
    starred_unranked = ordered(1, None, starred=(1,))
    starred_empty = ordered(None, None, None, starred=(2, 0))

    assert evidence([starred_unranked]) == [
        AbsoluteEvidence("wallhaven:b", 100.0, judgement_id=starred_unranked.id, recorded_at=AT),
        RelativeEvidence("wallhaven:a", "wallhaven:b", 1.0, judgement_id=starred_unranked.id, recorded_at=AT),
    ]
    assert strengths(starred_empty) == [("wallhaven:a", 100.0), ("wallhaven:c", 100.0)]


@pytest.mark.parametrize(
    "judgement",
    [
        judged(VERDICT_GRID_V1, {"wallhaven:a": LegacyTileResponse(LegacyEntry.LIKE)}, SUBMITTED),
        judged(VERDICT_GRID_V1, {"wallhaven:a": TileResponse()}),
        judged(VERDICT_GRID_V0, {"wallhaven:a": TileResponse()}),
        judged(VERDICT_GRID_V0, {"wallhaven:a": LegacyTileResponse(LegacyEntry.LIKE)}, SUBMITTED),
        picked({"wallhaven:a": PICKED, "wallhaven:b": PICKED}),
        picked({"wallhaven:a": PASSED, "wallhaven:b": PASSED}),
        picked({"wallhaven:a": PICKED, "wallhaven:b": PASSED}, NONE_OF_THESE),
        picked({"wallhaven:a": PICKED, "wallhaven:b": PASSED}, None),
        picked({"wallhaven:a": PICKED, "wallhaven:b": PASSED}, SUBMITTED),
        picked({"wallhaven:a": PICKED, "wallhaven:b": TileResponse()}),
        ordered(1, 1, None),
        ordered(1, 3, 2, 4, None, 6),
        ordered(2, None),
        ordered(0, 1),
        judged(ORDER_V1, {"wallhaven:a": OrderTile(rank=True)}, screen="compare"),
        judged(ORDER_V1, {"wallhaven:a": OrderTile(rank=1)}, PickResponse(), screen="compare"),
        judged(ORDER_V1, {"wallhaven:a": PICKED}, screen="compare"),
        judged(PICK_V1, {"wallhaven:a": OrderTile(rank=1)}, ANSWERED, screen="compare"),
    ],
    ids=[
        "v1 with a converted tile",
        "v1 with no screen response",
        "v0 with a v1 tile",
        "v0 with a screen response",
        "a pick of two winners",
        "a pick of no winner, not none of these",
        "a pick with a winner and none of these",
        "a pick with no screen response",
        "a pick with a grid's screen response",
        "a pick with a grid tile",
        "an order with a rank shared",
        "an order with a gap",
        "an order not starting at 1",
        "an order with a rank of 0",
        "an order with a rank that is not a whole number",
        "an order with a screen response",
        "an order with a pick tile",
        "a pick with an order tile",
    ],
)
def test_a_response_not_in_its_instruments_format_is_refused(judgement: Judgement) -> None:
    with pytest.raises(ValueError, match="format"):
        evidence([judgement])
