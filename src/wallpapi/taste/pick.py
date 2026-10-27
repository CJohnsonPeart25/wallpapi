"""**Pick**: the **Instrument** that asks for the best of a **Line-up**.

Version 1's **Response** is a `PickTile` per shown **Item**, whether it was the one picked and whether it was
given a **Favourite**, and a `PickResponse` for the screen, whether it was answered "none of these". Its
**Reduction**: the winner beats each other tile, strength 1 each, and "none of these" says nothing about
any of them. A **Favourite** is absolute 100 whatever was picked, as on every **Instrument**.

The per-tile **Favourite** is part of the format from the first version, although the **Line-up** offers no
star yet: a format is its version, so adding it later would have been a version 2 of every **Pick** made in
the meantime. **Order** and **Gauntlet** are **Instruments** of their own, each with its own module and row
in the table, and never change this format.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from wallpapi.taste.evidence import AbsoluteEvidence, Evidence, RelativeEvidence
from wallpapi.taste.judgement import Instrument, ItemResponse, Judgement, JudgementResponse
from wallpapi.taste.verdict_grid import FAVOURITE_STRENGTH

PICK_V1: Final = Instrument("pick", 1)
"""A `PickTile` per tile and a `PickResponse` for the screen."""

PAIR_STRENGTH: Final = 1.0
"""What one tile beating another is worth: unitless, never on the **Verdicts**' scale."""


@dataclass(frozen=True, slots=True)
class PickTile(ItemResponse):
    """Version 1's per-tile **Response**. Blank (`PickTile()`) is a tile passed over and not starred."""

    picked: bool = False
    favourite: bool = False


@dataclass(frozen=True, slots=True)
class PickResponse(JudgementResponse):
    """Version 1's judgement-level **Response**: whether the screen was answered "none of these" rather than
    with a tile. A **Judgement** says one or the other: exactly one tile picked, or none of these."""

    none_of_these: bool = False


def reduce_pick(judgement: Judgement) -> list[Evidence]:
    """Version 1. Each starred tile's **Favourite**, 100, in **Shown set** order; then, unless it was none
    of these, the winner over each other tile in on-screen order, strength 1 each.

    A screen with two winners, or with none and not "none of these", or with a winner and "none of these",
    is not in the format and raises `ValueError`, as does a **Response** of another **Instrument**'s shape.
    """
    response = judgement.response
    if not isinstance(response, PickResponse):
        raise _not_in_format(judgement, f"a screen response of {response!r}")
    tiles = [(shown.item, _tile(judgement, shown.response)) for shown in judgement.shown]
    winners = [item for item, tile in tiles if tile.picked]
    if len(winners) != (0 if response.none_of_these else 1):
        raise _not_in_format(
            judgement,
            f"{len(winners)} winners on a screen {'' if response.none_of_these else 'not '}"
            "answered none of these",
        )
    found: list[Evidence] = [
        AbsoluteEvidence(
            item=item,
            strength=FAVOURITE_STRENGTH,
            judgement_id=judgement.id,
            recorded_at=judgement.recorded_at,
        )
        for item, tile in tiles
        if tile.favourite
    ]
    for winner in winners:
        found.extend(
            RelativeEvidence(
                winner=winner,
                loser=item,
                strength=PAIR_STRENGTH,
                judgement_id=judgement.id,
                recorded_at=judgement.recorded_at,
            )
            for item, _ in tiles
            if item != winner
        )
    return found


def _tile(judgement: Judgement, response: ItemResponse) -> PickTile:
    if not isinstance(response, PickTile):
        raise _not_in_format(judgement, f"a tile response of {response!r}")
    return response


def _not_in_format(judgement: Judgement, what: str) -> ValueError:
    instrument = judgement.instrument
    return ValueError(f"{what} is not in {instrument.name} version {instrument.version}'s format")
