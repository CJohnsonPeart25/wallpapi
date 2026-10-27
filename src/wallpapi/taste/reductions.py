"""The **Reductions**: one plain function per **Instrument** name and version, turning a **Judgement** into
its Evidence, held in one table. Callers see `evidence()`.

A new **Instrument**, or a new version of one, is a new row here. A changed weight is a new `taste/` version
of a row, rebuilt from the same log, never a rewrite of the log.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Final

from wallpapi.taste import gauntlet, order, pick, verdict_grid
from wallpapi.taste.evidence import Evidence
from wallpapi.taste.judgement import Instrument, Judgement

type Reduction = Callable[[Judgement], Sequence[Evidence]]

REDUCTIONS: Final[Mapping[Instrument, Reduction]] = {
    verdict_grid.VERDICT_GRID_V0: verdict_grid.reduce_converted,
    verdict_grid.VERDICT_GRID_V1: verdict_grid.reduce_grid,
    pick.PICK_V1: pick.reduce_pick,
    order.ORDER_V1: order.reduce_order,
    gauntlet.GAUNTLET_V1: gauntlet.reduce_gauntlet,
}


class UnknownInstrument(LookupError):
    """A **Judgement** recorded by an **Instrument** name and version this `taste/` has no **Reduction**
    for. One machine and no sharing, so it is a bug, not data to skip."""


def evidence(judgements: Iterable[Judgement]) -> list[Evidence]:
    """Every **Judgement**'s Evidence, in the order given and, within one, in **Shown set** order.

    Raises `UnknownInstrument` for an **Instrument** not in `REDUCTIONS`, and `ValueError` for a
    **Response** not in its **Instrument**'s format; either way nothing is returned.
    """
    found: list[Evidence] = []
    for judgement in judgements:
        reduction = REDUCTIONS.get(judgement.instrument)
        if reduction is None:
            instrument = judgement.instrument
            raise UnknownInstrument(f"no Reduction for {instrument.name!r} version {instrument.version}")
        found.extend(reduction(judgement))
    return found
