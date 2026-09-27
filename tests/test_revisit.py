"""The **Revisit weight**: how much less often a decided **Wallpaper** comes round again. Issue #11.

Everything here enters through the Core service (invariant 1). What makes that possible is one
arrangement, used by almost every test below: **every Wallpaper is made to resemble nothing, itself
included.** The fake **Similarity provider** answers 1.0 for a **Wallpaper** against itself unless a test
says otherwise, and saying otherwise is what these tests do — so every **Score** is exactly 0.0, every
**Wallpaper** is an **Unknown**, and the only thing that can move between one **Batch** and the next is the
**Revisit weight**. A **Favourite** that scored its own +100 would be a **Banger** the moment it was
marked, and the thing under test would be tangled up with **Scoring**.

That also makes drawing repeatedly off one database legitimate here, which ADR 0010 warns it is not for
**Zone** proportions. The warning is that submitting writes an **Ignore** against every **Wallpaper** it
showed, which moves the **Zones** being counted; with no **Wallpaper** resembling anything, no **Verdict**
can move a **Zone** at all. The tests that want a genuinely *undecided* comparator still take a fresh
database each, because an **Ignore** is what an undecided **Wallpaper** stops being the moment it is shown.

The **Banger** test is the exception: it arranges real **Scores**, because a **Banger** slot ranks by
**Score** and the whole question there is what the weight does to that ranking.

The random source is the real `SeededRandom` under a fixed seed, so every proportion below is a
deterministic function of the seeds in `range(DRAWS)` and nothing else. The thresholds are set well clear
of what those seeds actually produce; they are not knife-edge.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import Harness, make_harness
from tests.fakes import catalogue_of
from wallpapi.core import DEFAULT_REVISIT_WEIGHT, Batch, SettingsRefused
from wallpapi.model import Verdict, Zone
from wallpapi.web.app import create_app

POOL_SIZE = 60
JUDGED = 30
BATCH = 20
"""Half the **Pool** is judged and the other half is not, and a **Batch** takes a third of the **Pool**.

Halves, so that "as often as" and "markedly less often than" are read off one number — the judged share of
a **Batch** — against an even split of 0.5. A **Batch** of 20 is 20 observations per draw, which is what
makes twenty draws enough to tell 0.5 from 0.2 without a thousand databases.
"""

DRAWS = 20
"""How many **Batches** a proportion is read off, each from a database of its own.

A fresh database per draw is what keeps the comparator *undecided* rather than **Ignore**-only: submitting
is the only way to draw again, and submitting writes an **Ignore** against every **Wallpaper** the
**Batch** showed. Twenty draws is four hundred slots, and about a second.
"""


def _indifferent_pool(db_path: Path, *, seed: int = 1, size: int = POOL_SIZE) -> Harness:
    """A **Pool** in which nothing resembles anything, a **Wallpaper** and itself included.

    Every **Score** is therefore exactly 0.0 and every **Wallpaper** an **Unknown**, whatever the
    **Decision log** comes to say. One page holds the whole catalogue, so a single refill step admits all
    of it.
    """
    catalogue = catalogue_of(size)
    return make_harness(
        db_path,
        catalogue=catalogue,
        page_size=1000,
        seed=seed,
        similarities={(w.id, w.id): 0.0 for w in catalogue},
    )


def _judge_half_then_draw(
    db_path: Path, *, verdict: Verdict | None, weight: float, seed: int = 1
) -> tuple[Harness, frozenset[str], Batch]:
    """Give half the **Pool** `verdict`, then draw one **Batch** under `weight`.

    Returns the harness, the judged half and the **Batch** that was drawn. `verdict=None` marks nothing,
    so the submission's derived **Ignores** are the only entries the judged half gets — which is the
    **Ignore**-only case.

    The **Batch** size and the weight are set *before* the submit, because submitting mints the next
    **Batch** (ADR 0002) and that **Batch** is the draw being observed. The other half is never shown, so
    it is undecided in the strict sense: no entry of any kind stands against it.
    """
    harness = _indifferent_pool(db_path, seed=seed)
    harness.core.update_settings(batch_size=JUDGED)
    seeding = harness.core.get_next_batch()
    assert isinstance(seeding, Batch), seeding
    judged = frozenset(w.id for w in seeding.wallpapers)
    if verdict is not None:
        for wallpaper_id in judged:
            harness.core.set_draft_verdict(seeding.id, wallpaper_id, verdict)

    harness.core.update_settings(batch_size=BATCH, revisit_weight=weight)
    drawn = harness.core.submit_batch(seeding.id)

    assert isinstance(drawn, Batch), drawn
    return harness, judged, drawn


def _judged_share(tmp_path: Path, *, verdict: Verdict | None, weight: float) -> float:
    """What fraction of all the slots drawn across `DRAWS` **Batches** went to the judged half."""
    slots = 0
    judged_slots = 0
    for seed in range(DRAWS):
        _, judged, drawn = _judge_half_then_draw(
            tmp_path / f"{seed}.db", verdict=verdict, weight=weight, seed=seed
        )
        drawn_ids = {w.id for w in drawn.wallpapers}
        slots += len(drawn_ids)
        judged_slots += len(drawn_ids & judged)
    assert slots == DRAWS * BATCH, "every draw should be a full Batch"
    return judged_slots / slots


def test_a_decided_wallpaper_comes_round_markedly_less_often_than_an_undecided_one(
    tmp_path: Path,
) -> None:
    """The headline acceptance criterion, at the default weight.

    Half the **Pool** is **Liked** and half has never been shown, and every one of them is an **Unknown**
    with a **Score** of exactly 0.0 — so the only difference between the two halves is the **Explicit
    Verdict**, and the only thing that can act on it is the **Revisit weight**. An even split would be half
    the slots; a third of them is the reduction biting.
    """
    share = _judged_share(tmp_path, verdict=Verdict.LIKE, weight=DEFAULT_REVISIT_WEIGHT)

    assert share < 1 / 3


def test_at_a_weight_of_one_a_decided_wallpaper_is_no_less_likely(tmp_path: Path) -> None:
    """The setting's top end means what it says: no reduction at all.

    The complement of the test above, and the one that stops it passing for the wrong reason. A draw that
    quietly excluded anything with a **Verdict** would pass "markedly less often" and fail here.
    """
    share = _judged_share(tmp_path, verdict=Verdict.LIKE, weight=1.0)

    assert share == pytest.approx(0.5, abs=0.08)


def test_at_a_weight_of_nothing_a_decided_wallpaper_never_comes_round_again(db_path: Path) -> None:
    """The setting's bottom end, over successive **Batches** rather than one.

    Six **Batches** off one database, which is the shape the claim is actually made in: "never again" is
    about the rest of the session, not about the next **Batch**. Legitimate on one database here because
    nothing resembles anything, so the **Ignores** each submission writes cannot move a **Zone** — see the
    module docstring.

    "Never" is a weight of nothing putting a **Wallpaper** last in its **Zone**'s draw order, not excluding
    it: thirty undecided **Wallpapers** can fill a **Batch** of twenty, so last is never reached. A
    **Pool** too small to fill the **Batch** would show the decided ones rather than show a short
    **Batch**, which is the shortfall rule (ADR 0010) and is the right way round.
    """
    harness, judged, batch = _judge_half_then_draw(db_path, verdict=Verdict.LIKE, weight=0.0)
    sizes = [len(batch.wallpapers)]
    seen = {w.id for w in batch.wallpapers}
    for _ in range(5):
        following = harness.core.submit_batch(batch.id)
        assert isinstance(following, Batch), following
        batch = following
        sizes.append(len(batch.wallpapers))
        seen |= {w.id for w in batch.wallpapers}

    assert not seen & judged
    assert sizes == [BATCH] * 6, "still a full Batch every time, just never from the decided half"


def test_an_ignore_only_wallpaper_is_drawn_as_often_as_one_nobody_has_seen(tmp_path: Path) -> None:
    """**Ignores** are not an **Explicit Verdict**, so the weight must not touch them.

    Read at a weight of nothing, which is the most unforgiving place to ask: if an **Ignore** counted, the
    half that was shown and left unmarked would never be drawn again at all, and the share would be zero
    rather than a half. This is **Verdict resolution**'s rule being used rather than restated — the reason
    the triage note on #11 asked for it.
    """
    share = _judged_share(tmp_path, verdict=None, weight=0.0)

    assert share == pytest.approx(0.5, abs=0.08)


@pytest.mark.parametrize("weight", [0.0, DEFAULT_REVISIT_WEIGHT, 1.0])
def test_a_banned_wallpaper_never_appears_at_any_weight(db_path: Path, weight: float) -> None:
    """A **Ban** is not a reduction, and no setting can turn it into one.

    A **Banned** **Wallpaper** is in no **Zone** and so is never weighted at all — the exclusion happens
    before the draw order is built. At a weight of 1.0 there is no reduction to hide behind, which is the
    case worth pinning.
    """
    _, banned, drawn = _judge_half_then_draw(db_path, verdict=Verdict.BAN, weight=weight)

    drawn_ids = {w.id for w in drawn.wallpapers}
    assert not drawn_ids & banned
    assert len(drawn_ids) == BATCH


BANGER_NEAR = 0.9
"""How like the **Favourite** an undecided **Banger** is made: a distance of 0.1, well inside the default
radius of 0.5, so it inherits about `exp(-0.4)` of the +50 a **Like** is worth — around 33 against the 50
the **Liked** **Wallpaper** scores on itself. Comfortably ordered, and nowhere near the sign change the
**Zone** is read off."""


def _banger_pool(db_path: Path, *, weight: float) -> tuple[frozenset[str], frozenset[str], Batch]:
    """A **Pool** of decided **Bangers** and undecided ones, drawn under **Refine**.

    Ten **Wallpapers** are **Liked** and resemble themselves, so each scores the +50 a **Like** is worth
    and is a **Banger**. Ten more resemble one of them and inherit about 33, so they are **Bangers** too
    and are *outscored* by the decided ten. The rest resemble nothing and stay **Unknown**.

    That ordering is the whole point: at a weight of 1.0 a **Banger** slot must take the decided ten,
    because they score highest; at the default weight their ranking is cut to about 10 and the undecided
    ten must take those slots instead, even though nothing about their **Scores** changed.

    **Refine** at a **Batch** of 10 allocates exactly seven **Banger** slots, so there is no roll to
    explain away.
    """
    harness = _indifferent_pool(db_path, size=40)
    harness.core.update_settings(batch_size=10)
    seeding = harness.core.get_next_batch()
    assert isinstance(seeding, Batch), seeding
    decided = frozenset(w.id for w in seeding.wallpapers)
    for wallpaper_id in decided:
        harness.core.set_draft_verdict(seeding.id, wallpaper_id, Verdict.LIKE)

    anchor = sorted(decided)[0]
    undecided = frozenset(sorted({f"wp{n:04d}" for n in range(40)} - decided)[:10])
    harness.similarity.similarity_by_pair.update(
        {(wallpaper_id, wallpaper_id): 1.0 for wallpaper_id in decided}
        | {(wallpaper_id, anchor): BANGER_NEAR for wallpaper_id in undecided}
    )
    harness.core.update_settings(batch_size=10, active_mix="refine", revisit_weight=weight)
    drawn = harness.core.submit_batch(seeding.id)

    assert isinstance(drawn, Batch), drawn
    return decided, undecided, drawn


def _bangers_of(batch: Batch) -> set[str]:
    return {w.id for w in batch.wallpapers if batch.zones[w.id] is Zone.BANGER}


def test_a_banger_slot_takes_the_best_score_when_nothing_is_being_held_back(db_path: Path) -> None:
    """At a weight of 1.0, **Banger** slots go to the highest **Scores** — which here are the decided ten.

    The baseline the next test moves away from, and the property ADR 0010 pinned: a **Banger** slot favours
    the best **Score**, and a weight that changes nothing must leave that exactly as it was.
    """
    decided, undecided, drawn = _banger_pool(db_path, weight=1.0)

    bangers = _bangers_of(drawn)
    assert len(bangers) == 7
    assert bangers <= decided
    assert not bangers & undecided


def test_the_revisit_weight_bites_in_banger_slots_too(db_path: Path) -> None:
    """The same **Pool** at the default weight hands its **Banger** slots to the undecided ten.

    A **Banger** slot ranks by **Score**, so a reduction that only touched the random draw order would do
    nothing here at all — the decided ten would go on filling every **Banger** slot of every **Batch** for
    ever, which is the one place "already judged, show me less of it" matters most. The weight multiplies
    into the **Score** the ranking uses: 50 becomes 10, which is below the 33 the undecided ten inherit.

    Highest **Score** first still decides — among **Wallpapers** carrying the same weight, which is every
    comparison the user could have an opinion about.
    """
    decided, undecided, drawn = _banger_pool(db_path, weight=DEFAULT_REVISIT_WEIGHT)

    bangers = _bangers_of(drawn)
    assert len(bangers) == 7
    assert bangers <= undecided
    assert not bangers & decided


def test_the_revisit_weight_is_a_setting_with_a_default_that_survives_a_restart(db_path: Path) -> None:
    """The first acceptance criterion: persisted, with a sensible default.

    0.2 by default — a decided **Wallpaper** is a fifth as likely to be drawn as one nobody has judged,
    which is markedly rarer without being gone. Re-opened over the same file rather than mutated in
    memory, because "persisted" is a claim about the database.
    """
    harness = make_harness(db_path)
    assert harness.core.get_settings().revisit_weight == DEFAULT_REVISIT_WEIGHT

    harness.core.update_settings(revisit_weight="0.35")

    assert _reopened(db_path).core.get_settings().revisit_weight == 0.35


@pytest.mark.parametrize("value", ["-0.1", "1.5", "lots", "", "nan", "inf"])
def test_the_revisit_weight_refuses_what_is_not_one(harness: Harness, value: str) -> None:
    """Out of range, not a number, and the two floats that are neither: refused with a reason.

    `nan` and `inf` are here for the reason they are on the similarity settings — `float()` accepts both,
    and a NaN weight would make every draw order key a NaN and sort the **Pool** by nothing at all.
    """
    before = harness.core.get_settings()

    refused = harness.core.update_settings(revisit_weight=value)

    assert isinstance(refused, SettingsRefused)
    assert refused.reason is SettingsRefused.Reason.REVISIT_WEIGHT_INVALID
    assert harness.core.get_settings() == before


@pytest.mark.parametrize("value", ["0", "1", "0.5"])
def test_the_ends_of_the_range_are_settings_like_any_other(harness: Harness, value: str) -> None:
    """Nought and one are the two values the whole feature is described by, so neither may be off by one."""
    assert harness.core.update_settings(revisit_weight=value) == harness.core.get_settings()
    assert harness.core.get_settings().revisit_weight == float(value)


def test_the_settings_page_round_trips_the_revisit_weight(db_path: Path) -> None:
    """The acceptance criterion as the user meets it: a field on the settings page that saves."""
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        before = client.get("/settings")
        saved = client.post("/settings", data={"revisit_weight": "0.4"}, follow_redirects=False)
        after = client.get("/settings")

    assert 'name="revisit_weight"' in before.text
    assert f'value="{DEFAULT_REVISIT_WEIGHT}"' in before.text
    assert saved.status_code == 303
    assert harness.core.get_settings().revisit_weight == 0.4
    assert 'value="0.4"' in after.text


def test_a_revisit_weight_that_is_not_one_is_a_rendered_refusal(db_path: Path) -> None:
    """A refusal the page has words for, like every other field — never the enum value on screen."""
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.post("/settings", data={"revisit_weight": "2"})

    assert response.status_code == 400
    assert "revisit_weight_invalid" not in response.text, "the reason needs words, not its enum value"
    assert harness.core.get_settings().revisit_weight == DEFAULT_REVISIT_WEIGHT


def _reopened(db_path: Path) -> Harness:
    """A second Core service over the same file, with no refill — the **Pool** is already in there."""
    return make_harness(db_path, fill_pool=0)
