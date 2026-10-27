"""**Gauntlet**: the **Instrument** where the winner stays. Pick the best of a **Line-up**; it keeps its place
on the next screen, every other tile is replaced, and a crown counts its streak. "None of these" replaces
every tile and ends the streak.

Version 1's **Response** is a `GauntletTile` per shown **Item**, whether it was the one picked and whether it
was given a **Favourite**, and a `GauntletResponse` for the screen: the streak its champion carried onto it,
and whether it was answered "none of these". Its **Reduction**: the winner beats each other tile on that
screen, strength 1 each, whether it is the champion beating its challengers or a challenger beating the
champion and the rest; "none of these" says nothing about any of them. A **Favourite** is absolute 100
whatever was picked, as on every **Instrument**. The streak is recorded and never read.

The shape is **Pick**'s with a streak beside it, but its own classes and its own row in the table, so
neither **Instrument**'s format ever changes for the other's sake.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from wallpapi.taste.evidence import AbsoluteEvidence, Evidence, RelativeEvidence
from wallpapi.taste.judgement import Instrument, ItemResponse, Judgement, JudgementResponse
from wallpapi.taste.pick import PAIR_STRENGTH
from wallpapi.taste.verdict_grid import FAVOURITE_STRENGTH

GAUNTLET_V1: Final = Instrument("gauntlet", 1)
"""A `GauntletTile` per tile and a `GauntletResponse` for the screen."""


@dataclass(frozen=True, slots=True)
class GauntletTile(ItemResponse):
    """Version 1's per-tile **Response**. Blank (`GauntletTile()`) is a tile passed over and not starred."""

    picked: bool = False
    favourite: bool = False


@dataclass(frozen=True, slots=True)
class GauntletResponse(JudgementResponse):
    """Version 1's judgement-level **Response**.

    `streak` is how many screens in a row the champion on this screen had already won, the count its crown
    showed: 0 on a screen with no champion, the first of a **Gauntlet** or the one after "none of these".
    What this screen's answer does to it follows from the tiles: the champion picked again holds one more,
    any other winner starts at 1, and "none of these" ends it. `none_of_these` is whether the screen was
    answered "none of these" rather than with a tile: exactly one tile picked, or none of these.
    """

    streak: int = 0
    none_of_these: bool = False


def reduce_gauntlet(judgement: Judgement) -> list[Evidence]:
    """Version 1. Each starred tile's **Favourite**, 100, in **Shown set** order; then, unless it was none
    of these, the winner over each other tile in on-screen order, strength 1 each. The streak adds nothing.

    A screen with two winners, or with none and not "none of these", or with a winner and "none of these",
    is not in the format and raises `ValueError`, as do a streak that is not a whole number from 0 up and a
    **Response** of another **Instrument**'s shape.
    """
    response = judgement.response
    if not isinstance(response, GauntletResponse):
        raise _not_in_format(judgement, f"a screen response of {response!r}")
    if type(response.streak) is not int or response.streak < 0:
        raise _not_in_format(judgement, f"a streak of {response.streak!r}")
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


def _tile(judgement: Judgement, response: ItemResponse) -> GauntletTile:
    if not isinstance(response, GauntletTile):
        raise _not_in_format(judgement, f"a tile response of {response!r}")
    return response


def _not_in_format(judgement: Judgement, what: str) -> ValueError:
    instrument = judgement.instrument
    return ValueError(f"{what} is not in {instrument.name} version {instrument.version}'s format")
