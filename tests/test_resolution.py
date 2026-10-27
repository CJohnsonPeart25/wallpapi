"""**Verdict resolution**, the latest-wins rule, held by its example cases.

The cases are data, so the **Evidence log**'s SQL can load the same ones and be held to the same answers;
here they run against `taste/`'s own resolution. A recorded value becomes Evidence by the converted verdict
grid's strengths, the vocabulary the cases are written in.
"""

from __future__ import annotations

import datetime as dt

import pytest

from wallpapi.taste.evidence import AbsoluteEvidence, RelativeEvidence
from wallpapi.taste.resolution import EXAMPLE_CASES, ExampleCase, resolve
from wallpapi.taste.verdict_grid import LEGACY_STRENGTHS, LegacyEntry

AT = dt.datetime(2026, 10, 27, 9, 0, 0, tzinfo=dt.UTC)


def as_evidence(case: ExampleCase) -> list[AbsoluteEvidence]:
    return [
        AbsoluteEvidence(
            item=said.item,
            strength=LEGACY_STRENGTHS[said.value],
            judgement_id=said.judgement_id,
            recorded_at=said.recorded_at,
        )
        for said in case.said
    ]


@pytest.mark.parametrize("case", EXAMPLE_CASES, ids=[case.name for case in EXAMPLE_CASES])
def test_each_example_case_resolves_to_its_expected_standing(case: ExampleCase) -> None:
    resolved = resolve(as_evidence(case))

    assert {item: piece.judgement_id for item, piece in resolved.items()} == {
        item: standing.judgement_id for item, standing in case.standing.items()
    }
    assert {item: piece.strength for item, piece in resolved.items()} == {
        item: LEGACY_STRENGTHS[standing.value] for item, standing in case.standing.items()
    }


@pytest.mark.parametrize("case", EXAMPLE_CASES, ids=[case.name for case in EXAMPLE_CASES])
def test_each_example_case_resolves_the_same_whatever_order_its_evidence_comes_in(case: ExampleCase) -> None:
    """SQL has no order but the one it is told: the rule must not lean on the list's."""
    forwards = resolve(as_evidence(case))
    backwards = resolve(reversed(as_evidence(case)))

    assert {item: piece.judgement_id for item, piece in backwards.items()} == {
        item: piece.judgement_id for item, piece in forwards.items()
    }


def test_the_cases_include_the_three_the_rule_is_known_by() -> None:
    """A Favourite overturned by a later Ban, a Clearance last, and a same-time tie to the lower id: if one
    of these went missing, the SQL would stop being held to it."""
    names = {case.name for case in EXAMPLE_CASES}

    assert {"a favourite overturned by a later ban", "a clearance last", "a same-time tie"} <= names


def test_the_cases_are_plain_data_a_sql_test_can_load() -> None:
    """Strings and UTC-aware times only: an Item key, a Judgement id, a value in the converted grid's words
    (a plain `str`), and a recorded time."""
    for case in EXAMPLE_CASES:
        for said in case.said:
            assert isinstance(said.item, str)
            assert isinstance(said.judgement_id, str)
            assert isinstance(said.value, str)
            assert said.recorded_at.utcoffset() == dt.timedelta(0)
        for item, standing in case.standing.items():
            assert isinstance(item, str)
            assert (standing.judgement_id, standing.value) in {
                (said.judgement_id, said.value) for said in case.said if said.item == item
            }


def test_a_clearance_stands_at_zero_rather_than_letting_the_earlier_favourite_back() -> None:
    favourite = AbsoluteEvidence("wallhaven:a", 100, "j1", AT)
    cleared_strength = LEGACY_STRENGTHS[LegacyEntry.CLEARED]
    cleared = AbsoluteEvidence("wallhaven:a", cleared_strength, "j2", AT.replace(second=1))

    assert resolve([favourite, cleared]) == {"wallhaven:a": cleared}


def test_relative_evidence_is_never_resolved() -> None:
    comparison = RelativeEvidence(
        winner="wallhaven:a", loser="wallhaven:b", strength=1, judgement_id="j1", recorded_at=AT
    )

    assert resolve([comparison]) == {}


def test_items_are_keyed_in_the_order_the_evidence_first_names_them() -> None:
    """Ranking's decided columns follow this order, and the float sums with them."""
    evidence = [
        AbsoluteEvidence("wallhaven:b", 50, "j1", AT),
        AbsoluteEvidence("wallhaven:a", -10, "j1", AT),
        AbsoluteEvidence("wallhaven:b", -100, "j2", AT.replace(second=1)),
    ]

    assert list(resolve(evidence)) == ["wallhaven:b", "wallhaven:a"]
