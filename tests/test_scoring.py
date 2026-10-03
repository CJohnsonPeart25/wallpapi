"""**Scores** and **Zones**: what a **Verdict** spreads to, how far, and what it makes of the **Pool**.

The fake provider's hand-defined similarities stand in for a real one. Most tests mint a **Batch** of two so
that submitting decides exactly the two **Wallpapers** meant: at eight, every unmarked one would pick up an
**Ignore**, which spreads like any other **Verdict**.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import Harness, make_harness
from tests.fakes import catalogue_of
from wallpapi.core import Batch, ScoredWallpaper
from wallpapi.model import Verdict, Zone

POOL_SIZE = 5


def _decide_two(harness: Harness) -> tuple[str, str, list[str]]:
    """Show a **Batch** of two, unsubmitted, and hand back its two IDs and the undecided rest, sorted."""
    harness.core.update_settings(batch_size=2)
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    shown = [w.id for w in batch.wallpapers]
    rest = sorted({f"wp{n:04d}" for n in range(POOL_SIZE)} - set(shown))
    return shown[0], shown[1], rest


def _submit(harness: Harness, **marks: Verdict) -> None:
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    for wallpaper_id, verdict in marks.items():
        harness.core.set_draft_verdict(batch.id, wallpaper_id, verdict)
    harness.core.submit_batch(batch.id)


def _zones(harness: Harness) -> dict[str, Zone]:
    return {scored.wallpaper.id: scored.zone for scored in harness.core.classify_pool()}


def _scores(harness: Harness) -> dict[str, float]:
    return {scored.wallpaper.id: scored.score for scored in harness.core.classify_pool()}


def test_a_favourite_spreads_a_banger_a_ban_spreads_a_dud_and_the_distant_stay_unknown(db_path: Path) -> None:
    """The **Score** is the resolved values fading with distance: one like the **Favourite** is positive,
    one like the **Ban** negative, and one beyond the radius of both exactly zero, so **Unknown**."""
    harness = make_harness(db_path, catalogue=catalogue_of(POOL_SIZE))
    loved, hated, (like_the_favourite, like_the_ban, unlike_either) = _decide_two(harness)
    harness.similarity.similarity_by_pair.update(
        {(like_the_favourite, loved): 0.9, (like_the_ban, hated): 0.9}
    )
    _submit(harness, **{loved: Verdict.FAVOURITE, hated: Verdict.BAN})

    zones = _zones(harness)

    assert zones[like_the_favourite] is Zone.BANGER
    assert zones[like_the_ban] is Zone.DUD
    assert zones[unlike_either] is Zone.UNKNOWN


def test_a_decided_pool_member_scores_its_own_value_and_a_banned_one_is_in_no_zone(db_path: Path) -> None:
    """At distance 0 the weight is exactly 1, so a **Favourite** scores exactly its resolved value. A
    **Ban** means never shown, so it is left out of the classification rather than called a **Dud**.

    Decided from **History** before a **Batch** shows them, which a user can do by posting the edit, and
    is the one way a decided **Wallpaper** is still in the **Pool** (ADR 0016): after a submission a
    **Banned** one is absent whatever its **Verdict**, so that could not tell a **Ban** from an **Ignore**.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(POOL_SIZE))
    assert harness.core.edit_verdict("wp0000", Verdict.FAVOURITE) is None
    assert harness.core.edit_verdict("wp0001", Verdict.BAN) is None
    assert harness.core.edit_verdict("wp0002", Verdict.IGNORE) is None

    scores, zones = _scores(harness), _zones(harness)

    assert scores["wp0000"] == 100.0
    assert zones["wp0000"] is Zone.BANGER
    assert "wp0001" not in zones
    assert zones["wp0002"] is Zone.DUD, "an Ignore stays in a Zone; only a Ban leaves them all"


def test_equal_and_opposite_verdicts_cancel_to_exactly_zero_and_so_to_unknown(db_path: Path) -> None:
    """IEEE arithmetic cancels exact negatives to 0.0, which is what the rule asks for: a tolerance would
    turn a **Wallpaper** the evidence points at into an **Unknown**."""
    harness = make_harness(db_path, catalogue=catalogue_of(POOL_SIZE))
    loved, hated, rest = _decide_two(harness)
    torn = rest[0]
    harness.similarity.similarity_by_pair.update({(torn, loved): 0.8, (torn, hated): 0.8})
    _submit(harness, **{loved: Verdict.FAVOURITE, hated: Verdict.BAN})

    assert _scores(harness)[torn] == 0.0
    assert _zones(harness)[torn] is Zone.UNKNOWN


def test_a_ban_still_spreads_to_everything_like_it(db_path: Path) -> None:
    """ "Never show me this" is also "show me less of this sort of thing": leaving **Bans** out of the
    decided set would keep the **Zone** rule and throw the strongest signal away. Below -10, so an
    **Ignore** in the same place could not pass for it."""
    harness = make_harness(db_path, catalogue=catalogue_of(POOL_SIZE))
    _, hated, rest = _decide_two(harness)
    like_the_ban = rest[0]
    harness.similarity.similarity_by_pair[(like_the_ban, hated)] = 0.9
    _submit(harness, **{hated: Verdict.BAN})

    assert _scores(harness)[like_the_ban] < -10.0
    assert _zones(harness)[like_the_ban] is Zone.DUD


def test_an_ignore_spreads_as_a_mild_negative(db_path: Path) -> None:
    """Nothing is marked, so both shown **Wallpapers** get the derived **Ignore**, at -10 not -100."""
    harness = make_harness(db_path, catalogue=catalogue_of(POOL_SIZE))
    ignored, _, rest = _decide_two(harness)
    like_the_ignored = rest[0]
    harness.similarity.similarity_by_pair[(like_the_ignored, ignored)] = 0.95
    _submit(harness)

    assert _zones(harness)[like_the_ignored] is Zone.DUD
    assert -10.0 < _scores(harness)[like_the_ignored] < 0.0


def test_the_next_classification_follows_the_decision_log_with_no_restart(db_path: Path) -> None:
    """**Scores** are derived on every read (invariant 2): a stored or memoised one would hold the first
    answer while the log changed underneath it."""
    harness = make_harness(db_path, catalogue=catalogue_of(POOL_SIZE))
    loved, hated, rest = _decide_two(harness)
    swayed = rest[0]
    harness.similarity.similarity_by_pair.update({(swayed, loved): 0.9, (swayed, hated): 0.95})
    assert _zones(harness)[swayed] is Zone.UNKNOWN

    _submit(harness, **{loved: Verdict.FAVOURITE})
    assert _zones(harness)[swayed] is Zone.BANGER

    assert harness.core.edit_verdict(hated, Verdict.BAN) is None

    assert _zones(harness)[swayed] is Zone.DUD


def test_a_restarted_core_service_derives_the_same_classification(db_path: Path) -> None:
    """The other half of never stored: the answer survives when nothing changes."""
    similarities = {("wp0004", "wp0000"): 0.9}
    harness = make_harness(db_path, catalogue=catalogue_of(POOL_SIZE), similarities=similarities)
    harness.core.update_settings(batch_size=POOL_SIZE)
    _submit(harness, wp0000=Verdict.FAVOURITE)
    before = _scores(harness)

    restarted = make_harness(
        db_path, catalogue=catalogue_of(POOL_SIZE), fill_pool=0, similarities=similarities
    )

    assert _scores(restarted) == before


def test_widening_the_radius_brings_a_distant_wallpaper_into_a_zone(db_path: Path) -> None:
    """The radius is a cliff: at distance 0.6 the **Favourite** counts for nothing until it is wider."""
    harness = make_harness(db_path, catalogue=catalogue_of(POOL_SIZE))
    loved, _, rest = _decide_two(harness)
    distant = rest[0]
    harness.similarity.similarity_by_pair[(distant, loved)] = 0.4
    _submit(harness, **{loved: Verdict.FAVOURITE})
    assert _zones(harness)[distant] is Zone.UNKNOWN

    harness.core.update_settings(similarity_radius=0.7)

    assert _zones(harness)[distant] is Zone.BANGER


def test_raising_the_decay_shrinks_what_a_distant_verdict_is_worth(db_path: Path) -> None:
    """The decay is a slope inside the radius: it takes **Score** away and leaves the sign alone."""
    harness = make_harness(db_path, catalogue=catalogue_of(POOL_SIZE))
    loved, _, rest = _decide_two(harness)
    nearby = rest[0]
    harness.similarity.similarity_by_pair[(nearby, loved)] = 0.9
    _submit(harness, **{loved: Verdict.FAVOURITE})
    gentle = _scores(harness)[nearby]

    harness.core.update_settings(similarity_decay=10.0)
    steep = _scores(harness)[nearby]

    assert 0.0 < steep < gentle
    assert _zones(harness)[nearby] is Zone.BANGER


def test_an_undecided_pool_is_every_wallpaper_unknown_and_a_score_a_plain_float(db_path: Path) -> None:
    """A matrix with no columns is where every install starts, so it is the ordinary path. A `float`, not
    a numpy scalar a template would print as `np.float64(…)`."""
    harness = make_harness(db_path, catalogue=catalogue_of(POOL_SIZE))

    classified = harness.core.classify_pool()

    assert len(classified) == POOL_SIZE
    assert all(isinstance(scored, ScoredWallpaper) and type(scored.score) is float for scored in classified)
    assert {scored.zone for scored in classified} == {Zone.UNKNOWN}
    assert all(scored.score == 0.0 for scored in classified)


def test_the_similarity_provider_is_asked_for_one_matrix_of_the_whole_pool(db_path: Path) -> None:
    """Invariant 2's shape, which has no observable but the fake's record: once, **Pool** by decided. The
    decided side includes the derived **Ignore** and the retired **Wallpapers**, which are columns and
    never rows (ADR 0007)."""
    harness = make_harness(db_path, catalogue=catalogue_of(POOL_SIZE))
    loved, ignored, _ = _decide_two(harness)
    _submit(harness, **{loved: Verdict.FAVOURITE})
    harness.similarity.calls.clear()

    harness.core.classify_pool()

    assert len(harness.similarity.calls) == 1
    pool_side, decided_side = harness.similarity.calls[0]
    assert len(pool_side) == POOL_SIZE - 2
    assert set(decided_side) == {loved, ignored}
    assert not set(pool_side) & set(decided_side), "a retired Wallpaper is a column, never a row"
