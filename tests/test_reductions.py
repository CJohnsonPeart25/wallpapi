"""The **Reductions**: each **Judgement** to its Evidence, through `evidence()`, the one function callers see.

Plain data in and out: a **Judgement** written out by hand, and the Evidence it must come to. The verdict
grid's two versions are the only **Reductions** built.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping

import pytest

from wallpapi.taste.evidence import AbsoluteEvidence
from wallpapi.taste.judgement import Instrument, ItemResponse, Judgement, JudgementResponse, Shown
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
    [Instrument("verdict grid", 2), Instrument("pick", 1), Instrument("Verdict grid", 1)],
    ids=["a later version", "an instrument not built", "a misspelt name"],
)
def test_an_unknown_instrument_raises(instrument: Instrument) -> None:
    judgement = judged(instrument, {"wallhaven:a": TileResponse()}, SUBMITTED)

    with pytest.raises(UnknownInstrument, match=instrument.name):
        evidence([judgement])


@pytest.mark.parametrize(
    "judgement",
    [
        judged(VERDICT_GRID_V1, {"wallhaven:a": LegacyTileResponse(LegacyEntry.LIKE)}, SUBMITTED),
        judged(VERDICT_GRID_V1, {"wallhaven:a": TileResponse()}),
        judged(VERDICT_GRID_V0, {"wallhaven:a": TileResponse()}),
        judged(VERDICT_GRID_V0, {"wallhaven:a": LegacyTileResponse(LegacyEntry.LIKE)}, SUBMITTED),
    ],
    ids=[
        "v1 with a converted tile",
        "v1 with no screen response",
        "v0 with a v1 tile",
        "v0 with a screen response",
    ],
)
def test_a_response_not_in_its_instruments_format_is_refused(judgement: Judgement) -> None:
    with pytest.raises(ValueError, match="format"):
        evidence([judgement])
