"""The evaluation: how well a **Score** orders what you said, by replaying the **Evidence log** in order.

Each **Judgement** is marked against its **Instrument**'s evaluation reading, which turns it into the pairs
it ordered, each pair two **Items** of one **Shown set**, the one you put higher first. A **Judgement** that
orders no pair is never a test answer. The **Verdict grid**'s reading is a frozen answer key kept apart from
its **Reduction**, which gives values rather than pairs: changing a weight must not change the test. A
**Line-up**'s reading is the pairs its **Reduction** gives, each winner over its loser whatever the strength,
so an **Instrument** gets its test answers the moment its **Reduction** is in the table; its pairs are its
**Response** format, which a weight cannot change.

The replay is prequential: the **Judgements** in recorded order, each predicted by `rank` from the Evidence
of every **Judgement** before it and none after, then added. The first `WARM_UP` are never tested. A test
**Judgement** is held out whole, and each candidate's own column is left out of its **Score**, so an
**Item** is never scored against its own Evidence; the other shown **Items**' earlier Evidence still counts.

**Pairwise accuracy** is the share of a **Judgement**'s pairs its **Scores** order your way, a tie half, and
then the mean over test **Judgements**, each weighing once whatever its size, per **Instrument**. Its 95%
range, and the range of a paired difference, come from resampling whole **Judgements** with the seeded
random source.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import numpy as np

from wallpapi.taste.evidence import Evidence, RelativeEvidence
from wallpapi.taste.judgement import Instrument, Judgement
from wallpapi.taste.ranking import Similarity, rank
from wallpapi.taste.reductions import evidence
from wallpapi.taste.rng import SeededRandom
from wallpapi.taste.verdict_grid import (
    VERDICT_GRID_V0,
    VERDICT_GRID_V1,
    GridResponse,
    LegacyEntry,
    LegacyTileResponse,
    TileResponse,
    Verdict,
)

WARM_UP: Final = 10
"""How many **Judgements** open the replay untested, counted in **Judgements** of any kind, a **Skip** or a
**History** edit among them, not in test **Judgements**."""

RESAMPLES: Final = 2000
"""How many resamples of whole test **Judgements** a 95% range is read from."""

SEED: Final = 20261027
"""The seed of every resampling, so the same log gives the same range on every run."""

READING_VERSION: Final = 1
"""The evaluation reading's version. Frozen: a changed answer key is a new version, and old results are run
again against it, never compared across it."""


@dataclass(frozen=True, slots=True)
class Pair:
    """Two **Items** shown together and treated differently: `above` the one you put higher."""

    above: str
    below: str


type Reading = Callable[[Judgement], tuple[Pair, ...]]

_GRID_STANDINGS: Final[Mapping[Verdict, int]] = {Verdict.BAN: 0, Verdict.IGNORE: 1, Verdict.LIKE: 2}
_FAVOURITE_STANDING: Final = 3

_CONVERTED_STANDINGS: Final[Mapping[LegacyEntry, int | None]] = {
    LegacyEntry.BAN: 0,
    LegacyEntry.IGNORE: 1,
    LegacyEntry.LIKE: 2,
    LegacyEntry.FAVOURITE: _FAVOURITE_STANDING,
    LegacyEntry.CLEARED: None,
}
"""A **Clearance** is not a standing: no pair is read with one in it."""


def _read_grid(judgement: Judgement) -> tuple[Pair, ...]:
    """Version 1 of the **Verdict grid**: Ban < Ignore < Like < Favourite, a tile left unmarked on a
    submitted screen an **Ignore**. A **Skip** says nothing; a preview opened changes no standing."""
    response = judgement.response
    if not isinstance(response, GridResponse) or not response.submitted:
        return ()
    return _pairs([(shown.item, _grid_standing(shown.response)) for shown in judgement.shown])


def _read_converted(judgement: Judgement) -> tuple[Pair, ...]:
    """Version 0, the converted **Decision log**: its four old words in the same order, a **Clearance**
    standing nowhere."""
    return _pairs(
        [
            (shown.item, _CONVERTED_STANDINGS[shown.response.entry])
            for shown in judgement.shown
            if isinstance(shown.response, LegacyTileResponse)
        ]
    )


def _grid_standing(response: object) -> int | None:
    if not isinstance(response, TileResponse):
        return None
    if response.favourite:
        return _FAVOURITE_STANDING
    return _GRID_STANDINGS[response.verdict or Verdict.IGNORE]


def _pairs(standings: Sequence[tuple[str, int | None]]) -> tuple[Pair, ...]:
    """Every two **Items** that stand differently, the higher first, in **Shown set** order."""
    found: list[Pair] = []
    for index, (first, first_standing) in enumerate(standings):
        for second, second_standing in standings[index + 1 :]:
            if first_standing is None or second_standing is None or first_standing == second_standing:
                continue
            if first_standing > second_standing:
                found.append(Pair(first, second))
            else:
                found.append(Pair(second, first))
    return tuple(found)


def _read_relative(judgement: Judgement) -> tuple[Pair, ...]:
    """A **Line-up**'s reading: the relative Evidence its **Reduction** gives, each winner over its loser, in
    the order given. A **Favourite** on a tile is absolute Evidence, a value set against nothing on its
    screen, and gives no pair; "none of these" gives none at all."""
    return tuple(
        Pair(piece.winner, piece.loser)
        for piece in evidence([judgement])
        if isinstance(piece, RelativeEvidence)
    )


READINGS: Final[Mapping[Instrument, Reading]] = {
    VERDICT_GRID_V0: _read_converted,
    VERDICT_GRID_V1: _read_grid,
}
"""Evaluation reading version 1's answer keys of their own, one entry per **Instrument** name and version.
Every other **Instrument** is read by its **Reduction**'s relative Evidence: **Pick** and **Order** now, and
**Gauntlet** once its **Reduction** is in the table, with no entry here. One whose **Reduction** gives no
relative Evidence is never a test answer, though its Evidence still counts."""


def reading(judgement: Judgement) -> tuple[Pair, ...]:
    """The pairs a **Judgement** ordered, by what was said in it and nothing later: none for a **Skip**, a
    **History** edit (a **Shown set** of one), a screen where everything stands alike, "none of these", or
    an **Instrument** whose **Reduction** gives no relative Evidence.

    Raises `UnknownInstrument` for an **Instrument** with neither an entry in `READINGS` nor a
    **Reduction**, as `evidence()` does: the replay reads every **Judgement**'s Evidence anyway.
    """
    return READINGS.get(judgement.instrument, _read_relative)(judgement)


@dataclass(frozen=True, slots=True)
class Marked:
    """One test **Judgement**, as the replay marked it."""

    judgement_id: str
    instrument: Instrument
    pairs: int
    """How many pairs its reading gave."""
    accuracy: float
    """The share of them its **Scores** ordered your way, a tie half."""
    items: tuple[str, ...]
    """The **Items** in its pairs, in **Shown set** order."""


@dataclass(frozen=True, slots=True)
class Range:
    """A 95% range: the 2.5th and 97.5th percentiles of the resampled means."""

    low: float
    high: float


@dataclass(frozen=True, slots=True)
class Accuracy:
    """**Pairwise accuracy** for one **Instrument**: the mean over its test **Judgements** and its range."""

    instrument: Instrument
    judgements: int
    accuracy: float
    range: Range


class Outcome(StrEnum):
    """What a paired comparison shows about the candidate."""

    BETTER = "candidate better"
    NO_DIFFERENCE = "no difference shown"
    WORSE = "candidate worse"


@dataclass(frozen=True, slots=True)
class Comparison:
    """A candidate's settings against the baseline's on one **Instrument**'s test **Judgements**."""

    instrument: Instrument
    judgements: int
    baseline: float
    candidate: float
    difference: float
    """The mean per-**Judgement** difference, candidate minus baseline."""
    range: Range
    """The difference's 95% range."""
    outcome: Outcome


def in_log_order(judgements: Iterable[Judgement]) -> list[Judgement]:
    """By recorded time, and on an exact tie the lower id last, as `resolution` decides between them."""
    ordered = sorted(judgements, key=lambda judgement: judgement.id, reverse=True)
    ordered.sort(key=lambda judgement: judgement.recorded_at)
    return ordered


def replay(
    judgements: Iterable[Judgement],
    similarity: Similarity,
    *,
    radius: float,
    decay: float,
    warm_up: int = WARM_UP,
) -> tuple[Marked, ...]:
    """Every test **Judgement**, in log order, marked against the **Scores** `rank` gives its **Items** from
    the Evidence of the **Judgements** before it: one `rank` call, so one similarity call, per test
    **Judgement**. Every **Judgement**, tested or not, is Evidence for those after it, as its **Reduction**
    says."""
    earlier: list[Evidence] = []
    tested: list[Marked] = []
    for index, judgement in enumerate(in_log_order(judgements)):
        pairs = reading(judgement) if index >= warm_up else ()
        if pairs:
            tested.append(_tested(judgement, pairs, earlier, similarity, radius, decay))
        earlier.extend(evidence([judgement]))
    return tuple(tested)


def pairwise_accuracy(
    tested: Iterable[Marked], *, resamples: int = RESAMPLES, seed: int = SEED
) -> tuple[Accuracy, ...]:
    """**Pairwise accuracy** per **Instrument**, by name and then version, each with its count of test
    **Judgements** and a 95% range from resampling them whole."""
    return tuple(
        Accuracy(
            instrument,
            judgements=len(scores),
            accuracy=statistics.fmean(scores),
            range=_bootstrap(scores, resamples, seed),
        )
        for instrument, scores in _by_instrument(tested).items()
    )


def compare(
    baseline: Sequence[Marked],
    candidate: Sequence[Marked],
    *,
    resamples: int = RESAMPLES,
    seed: int = SEED,
) -> tuple[Comparison, ...]:
    """The paired comparison, per **Instrument**: both replays' accuracy on the same test **Judgements**,
    and the mean and 95% range of the per-**Judgement** difference, candidate minus baseline, from
    resampling whole **Judgements**. Replays of one log under two settings test the same **Judgements**; any
    other two raise `ValueError`."""
    if [each.judgement_id for each in baseline] != [each.judgement_id for each in candidate]:
        raise ValueError("a paired comparison scores both sides on the same test Judgements")
    candidates = _by_instrument(candidate)
    found: list[Comparison] = []
    for instrument, before in _by_instrument(baseline).items():
        after = candidates[instrument]
        differences = [then - now for then, now in zip(after, before, strict=True)]
        spread = _bootstrap(differences, resamples, seed)
        found.append(
            Comparison(
                instrument,
                judgements=len(before),
                baseline=statistics.fmean(before),
                candidate=statistics.fmean(after),
                difference=statistics.fmean(differences),
                range=spread,
                outcome=outcome(spread),
            )
        )
    return tuple(found)


def outcome(difference: Range) -> Outcome:
    """What a range of the difference shows: a win only when it excludes zero on the candidate's side, a
    loss only when it excludes zero on the other; touching zero shows nothing."""
    if difference.low > 0.0:
        return Outcome.BETTER
    if difference.high < 0.0:
        return Outcome.WORSE
    return Outcome.NO_DIFFERENCE


def _tested(
    judgement: Judgement,
    pairs: Sequence[Pair],
    earlier: Sequence[Evidence],
    similarity: Similarity,
    radius: float,
    decay: float,
) -> Marked:
    paired = {item for pair in pairs for item in (pair.above, pair.below)}
    items = tuple(item for item in judgement.shown_set if item in paired)
    ranking = rank(earlier, items, similarity, radius=radius, decay=decay, own_evidence=False)
    score = dict(zip(items, ranking.scores, strict=True))
    marks = [_mark(score[pair.above], score[pair.below]) for pair in pairs]
    return Marked(
        judgement_id=judgement.id,
        instrument=judgement.instrument,
        pairs=len(pairs),
        accuracy=sum(marks) / len(marks),
        items=items,
    )


def _mark(above: float, below: float) -> float:
    """1 for a pair the **Scores** order your way, 0 for the other way, and a half for a tie."""
    if above > below:
        return 1.0
    if above < below:
        return 0.0
    return 0.5


def _by_instrument(tested: Iterable[Marked]) -> dict[Instrument, list[float]]:
    grouped: defaultdict[Instrument, list[float]] = defaultdict(list)
    for each in tested:
        grouped[each.instrument].append(each.accuracy)
    return {instrument: grouped[instrument] for instrument in sorted(grouped, key=_instrument_order)}


def _instrument_order(instrument: Instrument) -> tuple[str, int]:
    return (instrument.name, instrument.version)


def _bootstrap(values: Sequence[float], resamples: int, seed: int) -> Range:
    """The 95% range of the mean of `values`, each value one whole **Judgement**, from `resamples` draws of
    as many with replacement. A fresh source per range, so one range never depends on another's draws."""
    source = SeededRandom(seed)
    means = [statistics.fmean(source.choices(values, len(values))) for _ in range(resamples)]
    low, high = np.percentile(means, [2.5, 97.5])
    return Range(float(low), float(high))
