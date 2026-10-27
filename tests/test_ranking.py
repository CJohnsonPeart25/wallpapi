"""Ranking: the **Score** of each candidate from the Evidence, as a pure function on plain data.

No database, no rig and no fake: the similarity function is a hand-written table of candidate x decided
similarities, recording what it is asked for, and the Evidence is written out. What a **Score** makes of the
**Pool** (**Zones**, **Bans**, allocation) is wallpapi's, and `test_scoring.py` holds it through **Batches**.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence

import numpy as np
import pytest
from numpy.typing import NDArray

from wallpapi.taste.evidence import AbsoluteEvidence, Evidence, RelativeEvidence
from wallpapi.taste.ranking import Ranking, rank
from wallpapi.taste.resolution import EXAMPLE_CASES, ExampleCase
from wallpapi.taste.verdict_grid import LEGACY_STRENGTHS

RADIUS = 0.15
DECAY = 4.0
AT = dt.datetime(2026, 10, 27, 9, 0, 0, tzinfo=dt.UTC)
LATER = AT + dt.timedelta(microseconds=1)


class Table:
    """A similarity function over a hand-written table: any pair it does not name is 0.0, beyond every
    radius. Records each call, so a test can count them and see which Items were the decided columns."""

    def __init__(self, pairs: Mapping[tuple[str, str], float] | None = None) -> None:
        self.pairs = dict(pairs or {})
        self.calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    def __call__(self, candidates: Sequence[str], decided: Sequence[str]) -> NDArray[np.float32]:
        self.calls.append((tuple(candidates), tuple(decided)))
        return np.array(
            [[self.pairs.get((row, column), 0.0) for column in decided] for row in candidates],
            dtype=np.float32,
        ).reshape(len(candidates), len(decided))


def absolute(item: str, strength: float, judgement_id: str = "j1", at: dt.datetime = AT) -> AbsoluteEvidence:
    return AbsoluteEvidence(item=item, strength=strength, judgement_id=judgement_id, recorded_at=at)


def scores(evidence: Sequence[Evidence], candidates: Sequence[str], table: Table) -> tuple[float, ...]:
    return rank(evidence, candidates, table, radius=RADIUS, decay=DECAY).scores


def test_a_decided_item_inside_the_radius_adds_its_value_faded_by_distance() -> None:
    """Distance 0.05 at decay 4 is a weight of exp(-0.2), 0.818730753...: a **Favourite** there is worth
    81.87, and a **Ban** at distance 0 exactly its -100."""
    table = Table({("near", "loved"): 0.95, ("same", "hated"): 1.0})

    near, same = scores([absolute("loved", 100), absolute("hated", -100)], ["near", "same"], table)

    assert near == pytest.approx(81.87307530779818)
    assert same == -100.0


def test_the_radius_is_a_cliff_not_a_clip() -> None:
    """Just inside the radius a **Favourite** still counts exp(-4 * 0.15), 0.5488...; just beyond it, nothing
    at all rather than that same weight."""
    table = Table({("inside", "loved"): 0.85, ("beyond", "loved"): 0.84})

    inside, beyond = scores([absolute("loved", 100)], ["inside", "beyond"], table)

    assert inside == pytest.approx(54.88116360940264)
    assert beyond == 0.0


def test_a_score_sums_every_decided_item_with_its_sign() -> None:
    """A **Like** at distance 0 and an **Ignore** at 0.1: 50 - 10 * exp(-0.4), 43.2967..."""
    table = Table({("torn", "liked"): 1.0, ("torn", "ignored"): 0.9})

    (torn,) = scores([absolute("liked", 50), absolute("ignored", -10)], ["torn"], table)

    assert torn == pytest.approx(43.29679953964361)


def test_one_score_per_candidate_in_the_order_given_each_a_plain_float() -> None:
    """A `float`, not a numpy scalar a template would print as `np.float64(…)`."""
    table = Table({("c", "loved"): 1.0, ("a", "hated"): 1.0})

    ranking = rank(
        [absolute("loved", 100), absolute("hated", -100)], ["c", "b", "a"], table, radius=0.1, decay=1
    )

    assert isinstance(ranking, Ranking)
    assert ranking.scores == (100.0, 0.0, -100.0)
    assert all(type(score) is float for score in ranking.scores)


def test_the_similarity_function_is_called_once_for_the_whole_matrix() -> None:
    """Invariant 2: once per ranking, candidates by decided, never pairwise."""
    table = Table()

    rank([absolute("x", 100), absolute("y", -10)], ["a", "b", "c"], table, radius=RADIUS, decay=DECAY)

    assert table.calls == [(("a", "b", "c"), ("x", "y"))]


def test_nothing_decided_is_every_score_exactly_zero_from_one_empty_matrix() -> None:
    """Where every install starts: a matrix with no columns is the ordinary path, still asked for once."""
    table = Table()

    assert scores([], ["a", "b"], table) == (0.0, 0.0)
    assert table.calls == [(("a", "b"), ())]


def test_the_latest_absolute_evidence_decides_whatever_order_it_comes_in() -> None:
    """A **Favourite** overturned by a later **Ban** is a **Ban**, even listed first."""
    table = Table({("same", "changed"): 1.0})
    ban = absolute("changed", -100, "j2", LATER)
    favourite = absolute("changed", 100, "j1", AT)

    assert scores([ban, favourite], ["same"], table) == (-100.0,)
    assert scores([favourite, ban], ["same"], table) == (-100.0,)


def test_an_exact_tie_in_time_falls_to_the_lower_judgement_id() -> None:
    table = Table({("same", "tied"): 1.0})
    lower = absolute("tied", 50, "a1", AT)
    higher = absolute("tied", -100, "b2", AT)

    assert scores([lower, higher], ["same"], table) == (50.0,)
    assert scores([higher, lower], ["same"], table) == (50.0,)


def test_an_item_standing_at_zero_is_not_a_decided_column() -> None:
    """A legacy **Clearance** last is worth 0: the Item is undecided, not a column of zeros."""
    table = Table({("same", "cleared"): 1.0})

    result = scores(
        [absolute("cleared", 100, "j1", AT), absolute("cleared", 0, "j2", LATER)], ["same"], table
    )

    assert result == (0.0,)
    assert table.calls == [(("same",), ())]


def test_relative_evidence_is_accepted_and_changes_nothing() -> None:
    """The MVP's Score is today's: a comparison neither moves a **Score** nor adds a decided column."""
    table = Table({("near", "loved"): 0.95, ("near", "winner"): 1.0, ("near", "loser"): 1.0})
    verdicts: list[Evidence] = [absolute("loved", 100)]
    comparison = RelativeEvidence(
        winner="winner", loser="loser", strength=1, judgement_id="j9", recorded_at=AT
    )

    without = scores(verdicts, ["near"], table)
    with_it = scores([*verdicts, comparison], ["near"], table)

    assert with_it == without
    assert table.calls[-1] == (("near",), ("loved",))


def test_a_matrix_of_the_wrong_shape_is_refused() -> None:
    """A transposed or short matrix would broadcast into a wrong **Score** rather than fail."""

    def transposed(candidates: Sequence[str], decided: Sequence[str]) -> NDArray[np.float32]:
        return np.zeros((len(decided), len(candidates)), dtype=np.float32)

    with pytest.raises(ValueError, match="2 x 3"):
        rank([absolute(item, 100) for item in "xyz"], ["a", "b"], transposed, radius=RADIUS, decay=DECAY)


def test_a_favourite_and_a_ban_equally_near_sum_to_exactly_zero_at_any_similarity() -> None:
    """`(weights * values).sum(axis=1)`, never `weights @ values`: on this machine's BLAS the matrix product
    leaves a few times 1e-15 on most of these rows, and the **Zone** reads the sign."""
    near = np.random.default_rng(1).uniform(0.86, 1.0, size=500).astype(np.float32)
    candidates = [f"c{index}" for index in range(len(near))]
    table = Table(
        {
            (candidate, decided): float(value)
            for candidate, value in zip(candidates, near, strict=True)
            for decided in ("loved", "hated")
        }
    )

    result = scores([absolute("loved", 100), absolute("hated", -100)], candidates, table)

    assert result == (0.0,) * len(candidates)


@pytest.mark.parametrize("case", EXAMPLE_CASES, ids=[case.name for case in EXAMPLE_CASES])
def test_ranking_resolves_by_the_latest_wins_example_cases(case: ExampleCase) -> None:
    """Each Item a candidate at distance 0 from itself and beyond the radius of every other: its **Score** is
    exactly the strength its standing gives, a **Clearance** last scoring 0 as an undecided Item."""
    items = list(case.standing)
    table = Table({(item, item): 1.0 for item in items})
    evidence = [
        absolute(said.item, LEGACY_STRENGTHS[said.value], said.judgement_id, said.recorded_at)
        for said in case.said
    ]

    result = scores(evidence, items, table)

    assert result == tuple(LEGACY_STRENGTHS[case.standing[item].value] for item in items)
