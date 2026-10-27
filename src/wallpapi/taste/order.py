"""**Order**: the **Instrument** that asks for a **Line-up** in order of preference.

Version 1's **Response** is an `OrderTile` per shown **Item**: its rank, 1 the best, or none for a tile left
unranked, and whether it was given a **Favourite**. There is no judgement-level **Response**: a partial
**Order**, a complete one and a blank one (nothing ranked) are all told apart by the ranks alone. Its
**Reduction**: each ranked tile beats every lower-ranked and every unranked tile, strength 1 each; unranked
tiles say nothing about each other, and nothing ranked says nothing. A **Favourite** is absolute 100 whatever
the ranks, as on every **Instrument**.

Ranking one tile and leaving the rest makes the same claim as a **Pick** of it, and a complete **Order** of
n tiles gives every one of its n(n-1)/2 pairs. Ranks are 1 to k for k ranked tiles, so the order is all a
rank carries: the order of the clicks that made it is never recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from wallpapi.taste.evidence import AbsoluteEvidence, Evidence, RelativeEvidence
from wallpapi.taste.judgement import Instrument, ItemResponse, Judgement
from wallpapi.taste.pick import PAIR_STRENGTH
from wallpapi.taste.verdict_grid import FAVOURITE_STRENGTH

ORDER_V1: Final = Instrument("order", 1)
"""An `OrderTile` per tile, and no screen response."""


@dataclass(frozen=True, slots=True)
class OrderTile(ItemResponse):
    """Version 1's per-tile **Response**. Blank (`OrderTile()`) is a tile left unranked and not starred."""

    rank: int | None = None
    """1 for the best; `None` for a tile not ranked."""
    favourite: bool = False


def reduce_order(judgement: Judgement) -> list[Evidence]:
    """Version 1. Each starred tile's **Favourite**, 100, in **Shown set** order; then, for each ranked tile
    from the best down, the tile over each one it beats in on-screen order, strength 1 each: every tile
    ranked below it and every tile not ranked.

    Ranks that are not 1 to k for k ranked tiles (a rank shared, a gap, one below 1 or not a whole number)
    are not in the format and raise `ValueError`, as do a screen response and a **Response** of another
    **Instrument**'s shape.
    """
    if judgement.response is not None:
        raise _not_in_format(judgement, f"a screen response of {judgement.response!r}")
    tiles = [(shown.item, _tile(judgement, shown.response)) for shown in judgement.shown]
    ranked = sorted((tile.rank, item) for item, tile in tiles if tile.rank is not None)
    if [rank for rank, _ in ranked] != list(range(1, len(ranked) + 1)):
        raise _not_in_format(judgement, f"the ranks {[rank for rank, _ in ranked]}")
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
    for rank, winner in ranked:
        found.extend(
            RelativeEvidence(
                winner=winner,
                loser=item,
                strength=PAIR_STRENGTH,
                judgement_id=judgement.id,
                recorded_at=judgement.recorded_at,
            )
            for item, tile in tiles
            if tile.rank is None or tile.rank > rank
        )
    return found


def _tile(judgement: Judgement, response: ItemResponse) -> OrderTile:
    if not isinstance(response, OrderTile):
        raise _not_in_format(judgement, f"a tile response of {response!r}")
    if response.rank is not None and type(response.rank) is not int:
        raise _not_in_format(judgement, f"a rank of {response.rank!r}")
    return response


def _not_in_format(judgement: Judgement, what: str) -> ValueError:
    instrument = judgement.instrument
    return ValueError(f"{what} is not in {instrument.name} version {instrument.version}'s format")
