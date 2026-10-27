"""The **Judgement**'s shape and its id: a hash of what was shown, what was done, and when.

The id is the **Evidence log**'s primary key and a later merge's test of "already held", so the tests pin
both halves of the rule: everything that is the act changes it, and everything recorded *about* the act
(**Provenance**, the **Ranking stamp**, the context, first-shown time, the local offset, the app) leaves it
alone. One test spells the canonical form out by hand, so a change to it cannot pass unnoticed.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
from typing import Any

import pytest

from wallpapi.taste.judgement import FORMAT_VERSION, Instrument, Judgement, LogPosition, RankingStamp, Shown
from wallpapi.taste.verdict_grid import (
    VERDICT_GRID_V0,
    VERDICT_GRID_V1,
    GridResponse,
    LegacyEntry,
    LegacyTileResponse,
    TileResponse,
    Verdict,
)

AT = dt.datetime(2026, 10, 27, 9, 30, 15, 250000, tzinfo=dt.UTC)
A = "wallhaven:aaaaaa"
B = "wallhaven:bbbbbb"
LIKED = TileResponse(verdict=Verdict.LIKE)
OPENED = TileResponse(preview_opened=True)


def batch(**changes: Any) -> Judgement:
    """A submitted **Batch** of two, every field filled, with `changes` made to it."""
    fields: dict[str, Any] = {
        "instrument": VERDICT_GRID_V1,
        "screen": "batch",
        "shown": (
            Shown(A, LIKED, {"zone": "banger", "slot": "banger", "mix": "Refine"}),
            Shown(B, OPENED, {"zone": "unknown", "slot": "dud", "mix": "Refine"}),
        ),
        "response": GridResponse(submitted=True),
        "recorded_at": AT,
        "utc_offset": dt.timedelta(hours=1),
        "first_shown_at": AT - dt.timedelta(minutes=2),
        "ranking": RankingStamp(
            read=LogPosition(recorded_at=AT - dt.timedelta(hours=1), judgement_id="f" * 64),
            space="clip-vit-b-32",
            radius=0.15,
            decay=4.0,
        ),
        "context": {"theme": "dark"},
        "app": "wallpapi",
        "app_version": "0.1.0",
    }
    return Judgement(**(fields | changes))


def test_equal_content_gives_an_equal_id() -> None:
    assert batch().id == batch().id


def test_the_id_is_a_sha256_in_hex() -> None:
    assert len(batch().id) == 64
    assert set(batch().id) <= set("0123456789abcdef")


@pytest.mark.parametrize(
    "changes",
    [
        {"shown": (Shown(A, LIKED), Shown("wallhaven:cccccc", OPENED))},
        {"shown": (Shown(B, OPENED), Shown(A, LIKED))},
        {"shown": (Shown(A, LIKED),)},
        {"shown": (Shown(A, TileResponse(verdict=Verdict.BAN)), Shown(B, OPENED))},
        {"shown": (Shown(A, LIKED), Shown(B, TileResponse()))},
        {"shown": (Shown(A, TileResponse(favourite=True)), Shown(B, OPENED))},
        {"response": GridResponse(submitted=False)},
        {"recorded_at": AT + dt.timedelta(microseconds=1)},
        {"instrument": Instrument("verdict grid", 2)},
        {"instrument": Instrument("pick", 1)},
        {"screen": "history"},
    ],
    ids=[
        "another item shown",
        "the same items in another order",
        "one fewer item",
        "another verdict",
        "a preview not opened",
        "a favourite instead",
        "skipped rather than submitted",
        "a microsecond later",
        "another instrument version",
        "another instrument",
        "another screen",
    ],
)
def test_any_change_to_what_was_shown_done_or_when_changes_the_id(changes: dict[str, Any]) -> None:
    assert batch(**changes).id != batch().id


@pytest.mark.parametrize(
    "changes",
    [
        {
            "shown": (
                Shown(A, LIKED, {"zone": "dud"}),
                Shown(B, OPENED),
            )
        },
        {"ranking": None},
        {"ranking": RankingStamp(read=None, space="colour-and-category", radius=0.3, decay=1.0)},
        {"context": {}},
        {"context": {"theme": "light", "a name from a later version": 3}},
        {"first_shown_at": None},
        {"utc_offset": None},
        {"utc_offset": dt.timedelta(hours=-5)},
        {"app_version": "0.2.0"},
        {"app": "another host"},
        {"recorded_at": AT.astimezone(dt.timezone(dt.timedelta(hours=1)))},
    ],
    ids=[
        "provenance",
        "no ranking stamp",
        "another ranking stamp",
        "no context",
        "another context",
        "no first-shown time",
        "no local offset",
        "another local offset",
        "another app version",
        "another app",
        "the same moment written in another offset",
    ],
)
def test_what_is_recorded_about_the_act_leaves_the_id_alone(changes: dict[str, Any]) -> None:
    """A converted **Judgement** lacks all of these, and two copies of one log must convert to one id
    whichever app version converts them; a merge must not take one act stamped two ways for two acts."""
    assert batch(**changes).id == batch().id


def test_the_canonical_form_is_sorted_compact_json_with_microseconds_and_the_format_version() -> None:
    """Spelled out by hand: keys sorted, no spaces, the time in UTC with all six microsecond digits, enum
    members as their values, and a judgement-level response of `null` where the Instrument has none."""
    converted = Judgement(
        instrument=VERDICT_GRID_V0,
        screen="batch",
        shown=(
            Shown(A, LegacyTileResponse(LegacyEntry.FAVOURITE)),
            Shown(B, LegacyTileResponse(LegacyEntry.BAN)),
        ),
        recorded_at=dt.datetime(2026, 10, 27, 9, 30, tzinfo=dt.UTC),
        app="wallpapi",
        app_version="0.1.0",
    )
    canonical = (
        '{"format":1,"instrument":{"name":"verdict grid","version":0},'
        '"recorded_at":"2026-10-27T09:30:00.000000+00:00","response":null,"screen":"batch",'
        '"shown":[{"item":"wallhaven:aaaaaa","response":{"entry":"favourite"}},'
        '{"item":"wallhaven:bbbbbb","response":{"entry":"ban"}}]}'
    )

    assert converted.id == hashlib.sha256(canonical.encode("ascii")).hexdigest()


def test_a_judgement_carries_the_format_version() -> None:
    assert batch().format_version == FORMAT_VERSION == 1


def test_the_recorded_time_is_kept_in_utc() -> None:
    judgement = batch(recorded_at=AT.astimezone(dt.timezone(dt.timedelta(hours=-5))))

    assert judgement.recorded_at == AT
    assert judgement.recorded_at.utcoffset() == dt.timedelta(0)


def test_the_shown_set_is_the_item_keys_in_on_screen_order() -> None:
    assert batch().shown_set == (A, B)


def test_the_id_cannot_be_given_only_derived() -> None:
    """Read back from storage, a Judgement's id is worked out again from its content, never taken on trust."""
    assert "id" not in {field.name for field in dataclasses.fields(Judgement) if field.init}


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"recorded_at": AT.replace(tzinfo=None)}, "recorded time"),
        ({"first_shown_at": AT.replace(tzinfo=None)}, "first-shown time"),
        ({"shown": ()}, "shown set"),
        ({"shown": (Shown(A, TileResponse()), Shown(A, TileResponse()))}, "twice"),
        ({"format_version": 2}, "format"),
    ],
    ids=["naive recorded time", "naive first-shown time", "nothing shown", "an item twice", "another format"],
)
def test_a_judgement_that_cannot_be_is_refused(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        batch(**changes)
