"""**Scores** and **Zones**: what a **Verdict** spreads to, how far, and what it makes of the **Pool**.

Through `Batches.classify` on `conftest.BatchesRig`: a real in-memory database, and the fake provider's
hand-defined similarities standing in for a real one. A submission is stood for by what it does to these
tables: a **Verdict** appended for each **Wallpaper** shown, an unmarked one an **Ignore**, and every one of
them retired from the **Pool**.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

from tests.conftest import BatchesRig, batches_rig
from tests.fakes import catalogue_of
from wallpapi.allocation import ScoredWallpaper
from wallpapi.decisions import HistoryEntry
from wallpapi.model import Verdict, Zone
from wallpapi.scoring import classify

POOL_SIZE = 5
SHOWN = ("wp0000", "wp0001")
"""The two a submission decides; the rest of the **Pool** is undecided."""
REST = ("wp0002", "wp0003", "wp0004")


@pytest.fixture
def rig() -> Iterator[BatchesRig]:
    with batches_rig() as made:
        made.stock(catalogue_of(POOL_SIZE))
        yield made


def _submit(rig: BatchesRig, **marks: Verdict) -> None:
    """Submit the two `SHOWN`: each its mark, or an **Ignore** if it has none, and both retired."""
    for wallpaper_id in SHOWN:
        rig.decide(marks.get(wallpaper_id, Verdict.IGNORE), wallpaper_id)
    rig.retire(*SHOWN)


def _zones(rig: BatchesRig) -> dict[str, Zone]:
    return {scored.wallpaper.id: scored.zone for scored in rig.batches.classify(rig.connection)}


def _scores(rig: BatchesRig) -> dict[str, float]:
    return {scored.wallpaper.id: scored.score for scored in rig.batches.classify(rig.connection)}


def test_a_favourite_spreads_a_banger_a_ban_spreads_a_dud_and_the_distant_stay_unknown(
    rig: BatchesRig,
) -> None:
    """The **Score** is the resolved values fading with distance: one like the **Favourite** is positive,
    one like the **Ban** negative, and one beyond the radius of both exactly zero, so **Unknown**."""
    loved, hated = SHOWN
    like_the_favourite, like_the_ban, unlike_either = REST
    rig.similarity.similarity_by_pair.update({(like_the_favourite, loved): 0.95, (like_the_ban, hated): 0.95})
    _submit(rig, **{loved: Verdict.FAVOURITE, hated: Verdict.BAN})

    zones = _zones(rig)

    assert zones[like_the_favourite] is Zone.BANGER
    assert zones[like_the_ban] is Zone.DUD
    assert zones[unlike_either] is Zone.UNKNOWN


def test_a_decided_pool_member_scores_its_own_value_and_a_banned_one_is_in_no_zone(rig: BatchesRig) -> None:
    """At distance 0 the weight is exactly 1, so a **Favourite** scores exactly its resolved value. A
    **Ban** means never shown, so it is left out of the classification rather than called a **Dud**.

    Decided from **History** before a **Batch** shows them, which a user can do by posting the edit, and
    is the one way a decided **Wallpaper** is still in the **Pool** (ADR 0016): after a submission a
    **Banned** one is absent whatever its **Verdict**, so that could not tell a **Ban** from an **Ignore**.
    """
    assert isinstance(rig.edit("wp0000", Verdict.FAVOURITE), HistoryEntry)
    assert isinstance(rig.edit("wp0001", Verdict.BAN), HistoryEntry)
    assert isinstance(rig.edit("wp0002", Verdict.IGNORE), HistoryEntry)

    scores, zones = _scores(rig), _zones(rig)

    assert scores["wp0000"] == 100.0
    assert zones["wp0000"] is Zone.BANGER
    assert "wp0001" not in zones
    assert zones["wp0002"] is Zone.DUD, "an Ignore stays in a Zone; only a Ban leaves them all"


def test_equal_and_opposite_verdicts_cancel_to_exactly_zero_and_so_to_unknown(rig: BatchesRig) -> None:
    """IEEE arithmetic cancels exact negatives to 0.0, which is what the rule asks for: a tolerance would
    turn a **Wallpaper** the evidence points at into an **Unknown**."""
    loved, hated = SHOWN
    torn = REST[0]
    rig.similarity.similarity_by_pair.update({(torn, loved): 0.95, (torn, hated): 0.95})
    _submit(rig, **{loved: Verdict.FAVOURITE, hated: Verdict.BAN})

    assert _scores(rig)[torn] == 0.0
    assert _zones(rig)[torn] is Zone.UNKNOWN


def test_a_ban_still_spreads_to_everything_like_it(rig: BatchesRig) -> None:
    """ "Never show me this" is also "show me less of this sort of thing": leaving **Bans** out of the
    decided set would keep the **Zone** rule and throw the strongest signal away. Below -10, so an
    **Ignore** in the same place could not pass for it."""
    hated = SHOWN[1]
    like_the_ban = REST[0]
    rig.similarity.similarity_by_pair[(like_the_ban, hated)] = 0.95
    _submit(rig, **{hated: Verdict.BAN})

    assert _scores(rig)[like_the_ban] < -10.0
    assert _zones(rig)[like_the_ban] is Zone.DUD


def test_an_ignore_spreads_as_a_mild_negative(rig: BatchesRig) -> None:
    """Nothing is marked, so both shown **Wallpapers** get the derived **Ignore**, at -10 not -100."""
    ignored = SHOWN[0]
    like_the_ignored = REST[0]
    rig.similarity.similarity_by_pair[(like_the_ignored, ignored)] = 0.95
    _submit(rig)

    assert _zones(rig)[like_the_ignored] is Zone.DUD
    assert -10.0 < _scores(rig)[like_the_ignored] < 0.0


def test_a_retired_wallpaper_still_shapes_the_scores_of_the_pool(rig: BatchesRig) -> None:
    """The decided set is independent of **Pool** membership (ADR 0007): a retired **Ban** still makes the
    unseen **Wallpaper** like it a **Dud**."""
    remaining = REST[0]
    rig.similarity.similarity_by_pair.update({(remaining, decided): 0.95 for decided in SHOWN})
    _submit(rig, **dict.fromkeys(SHOWN, Verdict.BAN))

    scored = {s.wallpaper.id: s for s in rig.batches.classify(rig.connection)}

    assert not set(SHOWN) & set(scored), "retired, so no longer classified"
    assert scored[remaining].zone is Zone.DUD
    assert scored[remaining].score < 0


def test_the_next_classification_follows_the_decision_log_with_no_restart(rig: BatchesRig) -> None:
    """**Scores** are derived on every read (invariant 2): a stored or memoised one would hold the first
    answer while the log changed underneath it."""
    loved, hated = SHOWN
    swayed = REST[0]
    rig.similarity.similarity_by_pair.update({(swayed, loved): 0.93, (swayed, hated): 0.97})
    assert _zones(rig)[swayed] is Zone.UNKNOWN

    _submit(rig, **{loved: Verdict.FAVOURITE})
    assert _zones(rig)[swayed] is Zone.BANGER

    assert isinstance(rig.edit(hated, Verdict.BAN), HistoryEntry)

    assert _zones(rig)[swayed] is Zone.DUD


def test_a_restart_derives_the_same_classification(tmp_path: Path) -> None:
    """The other half of never stored: a second connection over the same file, with nothing changed,
    derives the same answer."""
    database = tmp_path / "wallpapi.db"
    similarities = {("wp0004", "wp0000"): 0.95}
    with batches_rig(database, similarities) as first:
        first.stock(catalogue_of(POOL_SIZE))
        first.decide(Verdict.FAVOURITE, "wp0000")
        first.retire("wp0000")
        before = _scores(first)

    with batches_rig(database, similarities) as restarted:
        after = _scores(restarted)

    assert before["wp0004"] > 0.0
    assert after == before


def test_widening_the_radius_brings_a_distant_wallpaper_into_a_zone(rig: BatchesRig) -> None:
    """The radius is a cliff: at distance 0.6 the **Favourite** counts for nothing until it is wider."""
    loved = SHOWN[0]
    distant = REST[0]
    rig.similarity.similarity_by_pair[(distant, loved)] = 0.4
    _submit(rig, **{loved: Verdict.FAVOURITE})
    assert _zones(rig)[distant] is Zone.UNKNOWN

    rig.configure(similarity_radius=0.7)

    assert _zones(rig)[distant] is Zone.BANGER


def test_raising_the_decay_shrinks_what_a_distant_verdict_is_worth(rig: BatchesRig) -> None:
    """The decay is a slope inside the radius: it takes **Score** away and leaves the sign alone."""
    loved = SHOWN[0]
    nearby = REST[0]
    rig.similarity.similarity_by_pair[(nearby, loved)] = 0.95
    _submit(rig, **{loved: Verdict.FAVOURITE})
    gentle = _scores(rig)[nearby]

    rig.configure(similarity_decay=10.0)
    steep = _scores(rig)[nearby]

    assert 0.0 < steep < gentle
    assert _zones(rig)[nearby] is Zone.BANGER


def test_an_undecided_pool_is_every_wallpaper_unknown_and_a_score_a_plain_float(rig: BatchesRig) -> None:
    """A matrix with no columns is where every install starts, so it is the ordinary path. A `float`, not
    a numpy scalar a template would print as `np.float64(…)`."""
    classified = rig.batches.classify(rig.connection)

    assert len(classified) == POOL_SIZE
    assert all(isinstance(scored, ScoredWallpaper) and type(scored.score) is float for scored in classified)
    assert {scored.zone for scored in classified} == {Zone.UNKNOWN}
    assert all(scored.score == 0.0 for scored in classified)


def test_the_similarity_provider_is_asked_for_one_matrix_of_the_whole_pool(rig: BatchesRig) -> None:
    """Invariant 2's shape, which has no observable but the fake's record: once, **Pool** by decided. The
    decided side includes the derived **Ignore** and the retired **Wallpapers**, which are columns and
    never rows (ADR 0007)."""
    loved, ignored = SHOWN
    _submit(rig, **{loved: Verdict.FAVOURITE})
    rig.similarity.calls.clear()

    rig.batches.classify(rig.connection)

    assert len(rig.similarity.calls) == 1
    pool_side, decided_side = rig.similarity.calls[0]
    assert len(pool_side) == POOL_SIZE - 2
    assert set(decided_side) == {loved, ignored}
    assert not set(pool_side) & set(decided_side), "a retired Wallpaper is a column, never a row"


def test_a_favourite_and_a_ban_equally_near_sum_to_exactly_zero_at_any_similarity() -> None:
    """`(weights * values).sum(axis=1)`, never `weights @ values`: on this machine's BLAS the matrix product
    leaves a few times 1e-15 on most of these rows, and the **Zone** reads the sign."""
    near = np.random.default_rng(1).uniform(0.86, 1.0, size=(500, 1)).astype(np.float32)

    classification = classify(np.repeat(near, 2, axis=1), [100, -100], radius=0.15, decay=4.0)

    assert not np.any(classification.scores)
    assert set(classification.zones) == {Zone.UNKNOWN}
