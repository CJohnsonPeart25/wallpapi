"""**Scores** and **Zones**: what a **Verdict** spreads to, how far, and what it makes of the **Pool**.

Issue #9. Everything enters through the Core service, with the fake **Similarity provider**'s hand-defined
similarities standing in for a real one — `{(pool id, decided id): similarity}`, a **Wallpaper** against
itself 1.0 and everything unnamed 0.0. Nothing here touches the network and nothing reads storage.

The **Batch** size is turned down to two in most of these so that submitting decides exactly the two
**Wallpapers** the test meant to decide. At the default of eight every **Wallpaper** shown and unmarked
would pick up an **Ignore**, which is a **Verdict** that spreads like any other and would quietly be half
the arrangement.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import Harness, make_harness
from tests.fakes import catalogue_of
from wallpapi.core import Batch, ScoredWallpaper
from wallpapi.model import Verdict, Zone

POOL_SIZE = 5
"""Small enough to name every **Wallpaper** in a test and still leave three undecided ones to classify."""


def _decide_two(harness: Harness) -> tuple[str, str, list[str]]:
    """Show a **Batch** of two and hand back the two IDs plus the undecided rest, still unsubmitted.

    Which two the seeded random source draws is not the test's business, so they are read back off the
    **Batch** rather than assumed. The rest is what is left of the **Pool**, sorted so that unpacking it
    into three names is stable.
    """
    harness.core.update_settings(batch_size=2)
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    shown = [w.id for w in batch.wallpapers]
    rest = sorted({f"wp{n:04d}" for n in range(POOL_SIZE)} - set(shown))
    return shown[0], shown[1], rest


def _zones(harness: Harness) -> dict[str, Zone]:
    return {scored.wallpaper.id: scored.zone for scored in harness.core.classify_pool()}


def _scores(harness: Harness) -> dict[str, float]:
    return {scored.wallpaper.id: scored.score for scored in harness.core.classify_pool()}


def _live_batch_id(harness: Harness) -> str:
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    return batch.id


def test_a_favourite_spreads_a_banger_a_ban_spreads_a_dud_and_the_distant_stay_unknown(
    db_path: Path,
) -> None:
    """The whole rule in one arrangement, and four acceptance criteria at once.

    A **Favourite** and a **Ban** are given to two **Wallpapers**; a third is similar to the **Favourite**,
    a fourth to the **Ban**, and a fifth to neither. The **Score** is the sum of the resolved values fading
    with distance, so the third is positive, the fourth negative and the fifth — beyond the radius of both,
    which is to say with nothing decided anywhere near it — is exactly zero and therefore **Unknown**.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(POOL_SIZE))
    loved, hated, rest = _decide_two(harness)
    like_the_favourite, like_the_ban, unlike_either = rest
    harness.similarity.similarity_by_pair.update(
        {(like_the_favourite, loved): 0.9, (like_the_ban, hated): 0.9}
    )
    batch_id = _live_batch_id(harness)
    harness.core.set_draft_verdict(batch_id, loved, Verdict.FAVOURITE)
    harness.core.set_draft_verdict(batch_id, hated, Verdict.BAN)
    harness.core.submit_batch(batch_id)

    zones = _zones(harness)

    assert zones[like_the_favourite] is Zone.BANGER
    assert zones[like_the_ban] is Zone.DUD
    assert zones[unlike_either] is Zone.UNKNOWN


def test_a_decided_wallpaper_still_in_the_pool_is_classified_from_its_own_value(db_path: Path) -> None:
    """A **Wallpaper** you **Favourited** is a **Banger**, and at full weight.

    It is at distance 0 from itself, so `exp(-decay * 0)` is exactly 1 and its **Score** is exactly the
    value **Verdict resolution** gave it. Asserted as the exact number rather than as a sign, because it is
    the one **Score** in the system that has an exact right answer.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(POOL_SIZE))
    loved, _, _ = _decide_two(harness)
    batch_id = _live_batch_id(harness)
    harness.core.set_draft_verdict(batch_id, loved, Verdict.FAVOURITE)
    harness.core.submit_batch(batch_id)

    assert _scores(harness)[loved] == 100.0
    assert _zones(harness)[loved] is Zone.BANGER


def test_equal_and_opposite_verdicts_cancel_to_exactly_zero_and_so_to_unknown(db_path: Path) -> None:
    """The **Unknown** case that is not "nothing decided nearby": a **Score** of exactly zero.

    Equally similar to a **Favourite** (+100) and to a **Ban** (-100), so the two weights are the same
    number and the two contributions are exact negatives of each other. IEEE arithmetic cancels those to
    exactly 0.0, which is what the rule asks for — a tolerance here would turn a **Wallpaper** the evidence
    genuinely points at into an **Unknown**.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(POOL_SIZE))
    loved, hated, rest = _decide_two(harness)
    torn = rest[0]
    harness.similarity.similarity_by_pair.update({(torn, loved): 0.8, (torn, hated): 0.8})
    batch_id = _live_batch_id(harness)
    harness.core.set_draft_verdict(batch_id, loved, Verdict.FAVOURITE)
    harness.core.set_draft_verdict(batch_id, hated, Verdict.BAN)
    harness.core.submit_batch(batch_id)

    assert _scores(harness)[torn] == 0.0
    assert _zones(harness)[torn] is Zone.UNKNOWN


def test_a_banned_wallpaper_is_in_no_zone_at_all(db_path: Path) -> None:
    """The acceptance criterion, and the one **Zone** rule that is an absence rather than a value.

    **Banned** means never shown again, so there is no **Zone** it could be drawn from — not **Dud**, which
    is a **Wallpaper** you would rather not see and might still be offered. It is left out of the
    classification entirely rather than classified and then filtered, which is why `classify_pool` is also
    what a **Batch** is built from.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(POOL_SIZE))
    _, hated, _ = _decide_two(harness)
    batch_id = _live_batch_id(harness)
    harness.core.set_draft_verdict(batch_id, hated, Verdict.BAN)
    harness.core.submit_batch(batch_id)

    assert hated not in _zones(harness)


def test_a_ban_still_spreads_to_everything_like_it(db_path: Path) -> None:
    """A **Ban** is in no **Zone** itself and is still a column of the matrix.

    That is the point of banning something: "never show me this" is also "show me less of this sort of
    thing". Pinned separately from the **Zone** rule because leaving **Bans** out of the decided set would
    satisfy that rule and silently throw the strongest signal in the system away.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(POOL_SIZE))
    _, hated, rest = _decide_two(harness)
    like_the_ban = rest[0]
    harness.similarity.similarity_by_pair[(like_the_ban, hated)] = 0.9
    batch_id = _live_batch_id(harness)
    harness.core.set_draft_verdict(batch_id, hated, Verdict.BAN)
    harness.core.submit_batch(batch_id)

    assert _scores(harness)[like_the_ban] < 0.0
    assert _zones(harness)[like_the_ban] is Zone.DUD


def test_an_ignore_spreads_as_a_mild_negative(db_path: Path) -> None:
    """**Ignores** are **Verdicts** too, and they spread like the rest of them.

    Nothing is marked, so submitting gives both shown **Wallpapers** the derived **Ignore** that absence
    means. A **Wallpaper** very like one of them is a **Dud** — mildly, at -10 a stack rather than -100 —
    which is what "ignores stack" is supposed to buy once similarity is involved.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(POOL_SIZE))
    ignored, _, rest = _decide_two(harness)
    like_the_ignored = rest[0]
    harness.similarity.similarity_by_pair[(like_the_ignored, ignored)] = 0.95
    harness.core.submit_batch(_live_batch_id(harness))

    assert _zones(harness)[like_the_ignored] is Zone.DUD
    assert -10.0 < _scores(harness)[like_the_ignored] < 0.0


def test_the_next_classification_follows_the_decision_log_with_no_restart(db_path: Path) -> None:
    """**Scores** are derived on every read, so a second **Batch**'s **Verdicts** land immediately.

    The closest thing to an observable for "**Scores** are never stored" that the seam offers: the same
    **Wallpaper** is **Unknown**, then a **Banger**, then a **Dud**, with nothing invalidated and nothing
    restarted in between — only the **Decision log** changing underneath it. A stored or memoised **Score**
    would hold the first answer.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(POOL_SIZE))
    loved, hated, rest = _decide_two(harness)
    swayed = rest[0]
    harness.similarity.similarity_by_pair.update({(swayed, loved): 0.9, (swayed, hated): 0.95})
    assert _zones(harness)[swayed] is Zone.UNKNOWN

    first = _live_batch_id(harness)
    harness.core.set_draft_verdict(first, loved, Verdict.FAVOURITE)
    # The next **Batch** has to show the whole **Pool**, because the **Ban** below is on a **Wallpaper**
    # the **Mix** would be unlikely to draw otherwise: the **Ignore** it picked up above makes it a
    # **Dud**, and a **Dud** is five per cent of an **Explore** **Batch** (#10). Asking for as many as the
    # **Pool** holds draws all of them whatever the **Mix** says, because a **Zone** that runs out is
    # filled from the others. The test below arranges itself the same way, for its own reasons.
    harness.core.update_settings(batch_size=POOL_SIZE)
    harness.core.submit_batch(first)
    assert _zones(harness)[swayed] is Zone.BANGER

    second = _live_batch_id(harness)
    harness.core.set_draft_verdict(second, hated, Verdict.BAN)
    harness.core.submit_batch(second)

    assert _zones(harness)[swayed] is Zone.DUD


def test_a_restarted_core_service_derives_the_same_classification(db_path: Path) -> None:
    """Nothing about a **Score** lives in the process, so a fresh Core service over the same file agrees.

    The other half of "never stored": the first half shows the answer changing when the log does, and this
    one shows it surviving when nothing does. Between them there is nowhere a cached **Score** could hide.
    """
    similarities = {("wp0004", "wp0000"): 0.9}
    harness = make_harness(db_path, catalogue=catalogue_of(POOL_SIZE), similarities=similarities)
    harness.core.update_settings(batch_size=POOL_SIZE)
    batch_id = _live_batch_id(harness)
    harness.core.set_draft_verdict(batch_id, "wp0000", Verdict.FAVOURITE)
    harness.core.submit_batch(batch_id)
    before = _scores(harness)

    restarted = make_harness(
        db_path, catalogue=catalogue_of(POOL_SIZE), fill_pool=0, similarities=similarities
    )

    assert _scores(restarted) == before


def test_widening_the_radius_brings_a_distant_wallpaper_into_a_zone(db_path: Path) -> None:
    """The radius is what decides whether a decided **Wallpaper** is near enough to count at all.

    At a distance of 0.6 the **Favourite** is well outside the default radius and contributes nothing, so
    the **Wallpaper** is **Unknown** for want of anything decided nearby. Widening the radius past 0.6 is
    the only thing that changes, and it is enough to make it a **Banger**.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(POOL_SIZE))
    loved, _, rest = _decide_two(harness)
    distant = rest[0]
    harness.similarity.similarity_by_pair[(distant, loved)] = 0.4
    batch_id = _live_batch_id(harness)
    harness.core.set_draft_verdict(batch_id, loved, Verdict.FAVOURITE)
    harness.core.submit_batch(batch_id)
    assert _zones(harness)[distant] is Zone.UNKNOWN

    harness.core.update_settings(similarity_radius=0.7)

    assert _zones(harness)[distant] is Zone.BANGER


def test_raising_the_decay_shrinks_what_a_distant_verdict_is_worth(db_path: Path) -> None:
    """The decay is how fast the influence fades inside the radius, which is a **Score** and not a **Zone**.

    Both settings are on the page together and it would be easy to assume they do the same thing. They do
    not: the radius is a cliff and the decay is a slope, so raising the decay leaves the sign alone — the
    **Wallpaper** is a **Banger** either way — and takes most of the **Score** away.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(POOL_SIZE))
    loved, _, rest = _decide_two(harness)
    nearby = rest[0]
    # Inside the default radius, which is what "nearby" has to mean for this test to be about the decay:
    # beyond it the weight is exactly zero and raising the decay could not change anything.
    harness.similarity.similarity_by_pair[(nearby, loved)] = 0.9
    batch_id = _live_batch_id(harness)
    harness.core.set_draft_verdict(batch_id, loved, Verdict.FAVOURITE)
    harness.core.submit_batch(batch_id)
    gentle = _scores(harness)[nearby]

    harness.core.update_settings(similarity_decay=10.0)
    steep = _scores(harness)[nearby]

    assert 0.0 < steep < gentle
    assert _zones(harness)[nearby] is Zone.BANGER


def test_an_undecided_pool_is_every_wallpaper_unknown(db_path: Path) -> None:
    """Nothing decided anywhere means nothing to spread, and a **Pool** of **Unknowns** rather than a crash.

    The degenerate shape — a matrix with no columns at all — is the one every new install starts in, so it
    has to be the ordinary path rather than a special case.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(POOL_SIZE))

    classified = harness.core.classify_pool()

    assert len(classified) == POOL_SIZE
    assert {scored.zone for scored in classified} == {Zone.UNKNOWN}
    assert all(scored.score == 0.0 for scored in classified)


def test_the_similarity_provider_is_asked_for_one_matrix_of_the_whole_pool(db_path: Path) -> None:
    """Invariant 2, as the one thing about the seam that has no other observable.

    A matrix of the candidates against the decided set, asked for once — never a pairwise call, and never
    **Pool** against **Pool**. Asserted on the fake's recorded call for the reason the Wallhaven client's
    parameters are: it is a pre-agreed injected seam, and the *shape* of what it was asked is the whole
    invariant.

    The decided side is both **Wallpapers** the **Batch** showed, not only the one that was marked: the
    other picked up the derived **Ignore** that absence means, and an **Ignore** is a **Verdict** with a
    non-zero value like any other.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(POOL_SIZE))
    loved, ignored, _ = _decide_two(harness)
    batch_id = _live_batch_id(harness)
    harness.core.set_draft_verdict(batch_id, loved, Verdict.FAVOURITE)
    harness.core.submit_batch(batch_id)
    harness.similarity.calls.clear()

    harness.core.classify_pool()

    assert len(harness.similarity.calls) == 1
    pool_side, decided_side = harness.similarity.calls[0]
    assert len(pool_side) == POOL_SIZE
    assert set(decided_side) == {loved, ignored}


def test_a_score_is_a_plain_float_and_a_zone_a_glossary_term(db_path: Path) -> None:
    """What `classify_pool` hands back, pinned so the web layer and #10 can rely on it.

    A `float` rather than a numpy scalar, because a template that formatted one would show `np.float64(…)`,
    and a **Zone** that is a `StrEnum` so `data-zone` carries the glossary term.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(POOL_SIZE))

    scored = harness.core.classify_pool()[0]

    assert isinstance(scored, ScoredWallpaper)
    assert type(scored.score) is float
    assert scored.zone.value in {"banger", "dud", "unknown"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("similarity_radius", "-0.1"),
        ("similarity_radius", "1.5"),
        ("similarity_radius", "wide"),
        ("similarity_radius", "nan"),
        ("similarity_decay", "-1"),
        ("similarity_decay", "1000"),
        ("similarity_decay", "fast"),
        ("similarity_decay", "inf"),
    ],
)
def test_the_similarity_settings_refuse_what_is_not_one(harness: Harness, field: str, value: str) -> None:
    """Both settings are numbers with a range, and both refuse rather than falling back.

    `nan` and `inf` are in here because `float()` accepts both: a NaN radius makes every comparison against
    it false and turns the whole **Pool** **Unknown**, which would look like a broken **Similarity
    provider** rather than like a bad setting.
    """
    before = harness.core.get_settings()

    result = harness.core.update_settings(**{field: value})

    assert getattr(result, "reason", None) is not None
    assert harness.core.get_settings() == before
