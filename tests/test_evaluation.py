"""The evaluation: replaying the **Evidence log** in order and marking each screen against what was said then.

Plain data in and numbers out: **Judgements** written out by hand, and a similarity function over a
hand-written table, as `test_ranking.py` has it. Every expected number is worked by hand in the test's
docstring, never recomputed the way the code does.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence

import numpy as np
import pytest
from numpy.typing import NDArray

from wallpapi.taste.evaluation import (
    Accuracy,
    Comparison,
    Marked,
    Outcome,
    Pair,
    Range,
    compare,
    outcome,
    pairwise_accuracy,
    reading,
    replay,
)
from wallpapi.taste.gauntlet import GAUNTLET_V1, GauntletResponse, GauntletTile
from wallpapi.taste.judgement import Instrument, ItemResponse, Judgement, JudgementResponse, Shown
from wallpapi.taste.order import ORDER_V1, OrderTile
from wallpapi.taste.pick import PICK_V1, PickResponse, PickTile
from wallpapi.taste.reductions import UnknownInstrument
from wallpapi.taste.verdict_grid import (
    VERDICT_GRID_V0,
    VERDICT_GRID_V1,
    GridResponse,
    LegacyEntry,
    LegacyTileResponse,
    TileResponse,
    Verdict,
)

START = dt.datetime(2026, 10, 27, 9, 0, 0, tzinfo=dt.UTC)
RADIUS = 0.1
DECAY = 0.0
"""Every decided Item inside the radius weighs exactly 1, so a hand-worked **Score** is a plain sum."""

FAVOURITE = TileResponse(favourite=True)
LIKE = TileResponse(verdict=Verdict.LIKE)
IGNORE = TileResponse(verdict=Verdict.IGNORE)
BAN = TileResponse(verdict=Verdict.BAN)
UNMARKED = TileResponse()
SUBMITTED = GridResponse(submitted=True)


class Table:
    """A similarity function over a hand-written table: an Item against itself 1.0, any pair it does not name
    0.0. Records each call."""

    def __init__(self, pairs: Mapping[tuple[str, str], float] | None = None) -> None:
        self.pairs = dict(pairs or {})
        self.calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    def __call__(self, candidates: Sequence[str], decided: Sequence[str]) -> NDArray[np.float32]:
        self.calls.append((tuple(candidates), tuple(decided)))
        return np.array(
            [[self._between(row, column) for column in decided] for row in candidates], dtype=np.float32
        ).reshape(len(candidates), len(decided))

    def _between(self, row: str, column: str) -> float:
        return self.pairs.get((row, column), 1.0 if row == column else 0.0)


def judged(
    tiles: Mapping[str, ItemResponse],
    *,
    second: int,
    instrument: Instrument = VERDICT_GRID_V1,
    response: JudgementResponse | None = SUBMITTED,
    screen: str = "batch",
) -> Judgement:
    """A **Judgement** recorded `second` seconds into the log."""
    return Judgement(
        instrument=instrument,
        screen=screen,
        shown=tuple(Shown(item, tile) for item, tile in tiles.items()),
        response=response,
        recorded_at=START + dt.timedelta(seconds=second),
        app="wallpapi",
        app_version="0.1.0",
    )


def edited(item: str, tile: TileResponse, *, second: int) -> Judgement:
    """A **History** edit: one Item, on the history **Screen**, submitted."""
    return judged({item: tile}, second=second, screen="history")


def converted(tiles: Mapping[str, LegacyEntry], *, second: int) -> Judgement:
    return judged(
        {item: LegacyTileResponse(entry) for item, entry in tiles.items()},
        second=second,
        instrument=VERDICT_GRID_V0,
        response=None,
    )


# -- the evaluation reading --------------------------------------------------------------------------


def test_the_grid_reads_as_pairs_between_tiles_that_stand_differently_ban_ignore_like_favourite() -> None:
    """Five tiles: a Favourite, a Like, two Ignores (one left unmarked, as a submitted grid reads it) and a
    Ban. The two Ignores stand alike and make no pair; every other two make one, the higher first: 4 + 3 +
    3 = 10 pairs less the one tie, 9."""
    screen = judged(
        {"fav": FAVOURITE, "like": LIKE, "unmarked": UNMARKED, "ignored": IGNORE, "ban": BAN}, second=0
    )

    assert set(reading(screen)) == {
        Pair("fav", "like"),
        Pair("fav", "unmarked"),
        Pair("fav", "ignored"),
        Pair("fav", "ban"),
        Pair("like", "unmarked"),
        Pair("like", "ignored"),
        Pair("like", "ban"),
        Pair("unmarked", "ban"),
        Pair("ignored", "ban"),
    }
    assert len(reading(screen)) == 9


def test_a_preview_opened_changes_no_answer() -> None:
    plain = judged({"a": LIKE, "b": UNMARKED}, second=0)
    previewed = judged(
        {
            "a": TileResponse(verdict=Verdict.LIKE, preview_opened=True),
            "b": TileResponse(preview_opened=True),
        },
        second=0,
    )

    assert reading(previewed) == reading(plain) == (Pair("a", "b"),)


def test_a_skip_is_never_a_test_answer_whatever_was_marked_or_opened_on_it() -> None:
    skip = judged(
        {"a": LIKE, "b": TileResponse(preview_opened=True), "c": BAN}, second=0, response=GridResponse()
    )

    assert reading(skip) == ()


def test_a_history_edit_is_never_a_test_answer() -> None:
    """A **Shown set** of one: nothing was shown beside it to be ordered against."""
    assert reading(edited("a", FAVOURITE, second=0)) == ()


def test_a_screen_where_everything_stands_alike_is_never_a_test_answer() -> None:
    assert reading(judged({"a": UNMARKED, "b": IGNORE, "c": UNMARKED}, second=0)) == ()


def test_the_converted_grid_reads_its_old_words_and_skips_every_pair_with_a_clearance() -> None:
    """A **Clearance** is not a standing, so neither of its pairs is read; Like over Ban still is."""
    screen = converted({"a": LegacyEntry.LIKE, "b": LegacyEntry.CLEARED, "c": LegacyEntry.BAN}, second=0)

    assert reading(screen) == (Pair("a", "c"),)


def test_the_converted_grid_orders_its_four_old_words_as_the_built_grid_does() -> None:
    screen = converted(
        {
            "ban": LegacyEntry.BAN,
            "ignore": LegacyEntry.IGNORE,
            "like": LegacyEntry.LIKE,
            "favourite": LegacyEntry.FAVOURITE,
        },
        second=0,
    )

    assert set(reading(screen)) == {
        Pair("ignore", "ban"),
        Pair("like", "ban"),
        Pair("like", "ignore"),
        Pair("favourite", "ban"),
        Pair("favourite", "ignore"),
        Pair("favourite", "like"),
    }


# -- replay and pairwise accuracy ---------------------------------------------------------------------


def hand_worked_log() -> list[Judgement]:
    """One warm-up screen and two test screens, of three pairs and of one."""
    return [
        judged({"a": FAVOURITE, "b": BAN}, second=0),
        judged({"c": LIKE, "d": UNMARKED, "e": BAN}, second=1),
        judged({"f": LIKE, "g": BAN}, second=2),
    ]


def hand_worked_table() -> Table:
    return Table(
        {
            ("c", "a"): 0.95,
            ("d", "b"): 0.95,
            ("e", "a"): 0.95,
            ("e", "d"): 0.95,
            ("f", "c"): 0.95,
            ("g", "e"): 0.95,
        }
    )


def test_pairwise_accuracy_on_a_tiny_log_is_the_hand_worked_answer() -> None:
    """Warm-up 1, radius 0.1 and decay 0, so each near decided Item adds exactly its value.

    Screen 2 is predicted from screen 1 alone: c is near the Favourite a (100), d near the Ban b (-100), e
    near a (100). e is also near d, but d's Ignore is said on this very screen and is not yet known. Its
    pairs: c (Like) over d (Ignore), 100 > -100, right; c over e (Ban), 100 = 100, a tie, half; d over e,
    -100 < 100, wrong. 1.5 of 3: 0.5.

    Screen 3 is predicted from screens 1 and 2: f is near c, now a Like (50), g near e, now a Ban (-100).
    f over g, right: 1.0.

    Each screen weighs once: (0.5 + 1.0) / 2 = 0.75, where pooling the four pairs would give 2.5 / 4.
    Resampling two screens, a quarter of the resamples are both 0.5 and a quarter both 1.0, so the 95%
    range is exactly 0.5 to 1.0.
    """
    log = hand_worked_log()

    tested = replay(log, hand_worked_table(), radius=RADIUS, decay=DECAY, warm_up=1)

    assert tested == (
        Marked(log[1].id, VERDICT_GRID_V1, pairs=3, accuracy=0.5, items=("c", "d", "e")),
        Marked(log[2].id, VERDICT_GRID_V1, pairs=1, accuracy=1.0, items=("f", "g")),
    )
    assert pairwise_accuracy(tested) == (
        Accuracy(VERDICT_GRID_V1, judgements=2, accuracy=0.75, range=Range(0.5, 1.0)),
    )


def test_an_item_is_never_scored_against_its_own_evidence_while_its_neighbours_count() -> None:
    """x was made a Favourite from **History**; then a screen showed it again, now a Ban, beside y, a Like
    near x. Against its own Favourite x would score 100 and tie y; held out, x scores 0 and y still gets
    x's 100 from the earlier edit: y over x, right."""
    log = [
        edited("x", FAVOURITE, second=0),
        judged({"x": BAN, "y": LIKE}, second=1),
    ]

    (marked,) = replay(log, Table({("y", "x"): 0.95}), radius=RADIUS, decay=DECAY, warm_up=1)

    assert marked.accuracy == 1.0


def test_a_screen_is_predicted_from_the_screens_before_it_and_never_from_one_after() -> None:
    """Screen 1 alone is all screen 2 knows: adding a later screen, near both, changes no earlier mark."""
    log = hand_worked_log()
    later = judged({"z": FAVOURITE, "y": BAN}, second=3)
    near_everything = Table({**hand_worked_table().pairs, **{(item, "z"): 0.99 for item in "abcdefg"}})

    without = replay(log, near_everything, radius=RADIUS, decay=DECAY, warm_up=1)
    with_later = replay([*log, later], near_everything, radius=RADIUS, decay=DECAY, warm_up=1)

    assert with_later[: len(without)] == without


def test_a_later_history_edit_never_changes_an_earlier_screens_answer() -> None:
    """Screen 2 said c over d (Like over Ignore). A **History** edit then makes c a Ban: the edit is
    Evidence for what follows, but screen 2 is still marked against what was said on it."""
    log = hand_worked_log()
    edit = edited("c", BAN, second=3)

    without = replay(log, hand_worked_table(), radius=RADIUS, decay=DECAY, warm_up=1)
    with_edit = replay([*log, edit], hand_worked_table(), radius=RADIUS, decay=DECAY, warm_up=1)

    assert reading(log[1]) == (Pair("c", "d"), Pair("c", "e"), Pair("d", "e"))
    assert with_edit == without


def test_a_history_edit_is_evidence_for_the_screens_after_it() -> None:
    """Not a test answer, but a Favourite made from **History** pulls a later Like above a Ban."""
    log = [
        edited("a", FAVOURITE, second=0),
        judged({"near": LIKE, "far": BAN}, second=1),
    ]

    (marked,) = replay(log, Table({("near", "a"): 0.95}), radius=RADIUS, decay=DECAY, warm_up=1)

    assert marked.accuracy == 1.0


def test_a_skip_teaches_nothing_and_is_not_tested() -> None:
    """A **Skip** with marks on it emits no Evidence, so the screen after it is all ties: 0.5."""
    log = [
        judged({"a": FAVOURITE, "b": BAN}, second=0, response=GridResponse()),
        judged({"near": LIKE, "far": BAN}, second=1),
    ]

    marked = replay(log, Table({("near", "a"): 0.95}), radius=RADIUS, decay=DECAY, warm_up=0)

    assert [each.judgement_id for each in marked] == [log[1].id]
    assert marked[0].accuracy == 0.5


def test_the_first_ten_judgements_of_any_kind_are_warm_up() -> None:
    """Nine **History** edits and a screen make ten: that screen is warm-up, the next the first test."""
    edits = [edited(f"h{index}", LIKE, second=index) for index in range(9)]
    tenth = judged({"a": LIKE, "b": BAN}, second=9)
    eleventh = judged({"c": LIKE, "d": BAN}, second=10)

    marked = replay([*edits, tenth, eleventh], Table(), radius=RADIUS, decay=DECAY)

    assert [each.judgement_id for each in marked] == [eleventh.id]


def test_screens_recorded_at_the_same_time_replay_with_the_lower_id_last() -> None:
    """As resolution orders them: the lower id is the later, so it is predicted from the other. The first
    knows nothing (every pair a tie, 0.5); the second sees the first's Favourite and Ban (1.0)."""
    one = judged({"a": FAVOURITE, "b": BAN}, second=0)
    two = judged({"c": LIKE, "d": BAN}, second=0)
    lower, higher = sorted([one, two], key=lambda judgement: judgement.id)
    table = Table({("c", "a"): 0.95, ("d", "b"): 0.95, ("a", "c"): 0.95, ("b", "d"): 0.95})

    marked = replay([lower, higher], table, radius=RADIUS, decay=DECAY, warm_up=0)

    assert [(each.judgement_id, each.accuracy) for each in marked] == [(higher.id, 0.5), (lower.id, 1.0)]


def test_each_test_judgement_asks_for_one_matrix() -> None:
    """Invariant 2: one `rank`, so one similarity call, per test **Judgement**, of its paired **Items**."""
    table = hand_worked_table()

    replay(hand_worked_log(), table, radius=RADIUS, decay=DECAY, warm_up=1)

    assert table.calls == [(("c", "d", "e"), ("a", "b")), (("f", "g"), ("a", "b", "c", "d", "e"))]


class RandomPlaces:
    """A similarity function that knows nothing about taste: each Item a fixed random direction, and the
    similarity of two the mapped cosine between them."""

    def __init__(self, items: Sequence[str], seed: int) -> None:
        directions = np.random.default_rng(seed).normal(size=(len(items), 8))
        directions /= np.linalg.norm(directions, axis=1, keepdims=True)
        self.direction_of = dict(zip(items, directions, strict=True))

    def __call__(self, candidates: Sequence[str], decided: Sequence[str]) -> NDArray[np.float32]:
        rows = np.array([self.direction_of[item] for item in candidates]).reshape(len(candidates), 8)
        columns = np.array([self.direction_of[item] for item in decided]).reshape(len(decided), 8)
        return ((1.0 + rows @ columns.T) / 2.0).astype(np.float32)


def test_a_score_that_knows_nothing_comes_out_near_one_half() -> None:
    """200 screens of 8, each tile a Favourite, a Like, unmarked or a Ban at random, and every Item placed
    at random: the **Scores** are blind to the answers, so the accuracy is a coin toss, and its range holds
    0.5. Seeded, so the same numbers every run."""
    tiles = (FAVOURITE, LIKE, UNMARKED, BAN)
    draw = np.random.default_rng(7)
    log = [
        judged({f"s{screen}t{tile}": tiles[draw.integers(len(tiles))] for tile in range(8)}, second=screen)
        for screen in range(200)
    ]
    places = RandomPlaces([item for judgement in log for item in judgement.shown_set], seed=11)

    (result,) = pairwise_accuracy(replay(log, places, radius=0.5, decay=4.0))

    assert result.judgements == 190
    assert abs(result.accuracy - 0.5) < 0.03
    assert result.range.low < 0.5 < result.range.high
    assert result == pairwise_accuracy(replay(log, places, radius=0.5, decay=4.0))[0]


def marked(*accuracies: float) -> tuple[Marked, ...]:
    """Test **Judgements** as a replay would mark them, one per accuracy, the same ids each call."""
    return tuple(
        Marked(f"j{index}", VERDICT_GRID_V1, pairs=1, accuracy=accuracy, items=("a", "b"))
        for index, accuracy in enumerate(accuracies)
    )


def test_each_resample_draws_as_many_whole_judgements_as_were_tested() -> None:
    """Ten screens, five right and five wrong: a resample's mean is k / 10 for k right of 10 drawn, k
    binomial(10, 1/2). 9 or more comes up 11 / 1024 of the time (1.1%), 8 or more 56 / 1024 (5.5%), so the
    97.5th percentile is 0.8, and the 2.5th 0.2 by symmetry. Drawing fewer would widen it."""
    (result,) = pairwise_accuracy(marked(*[1.0, 0.0] * 5))

    assert (result.judgements, result.accuracy, result.range) == (10, 0.5, Range(0.2, 0.8))


def test_a_candidate_wins_only_when_the_range_of_the_difference_clears_zero_on_its_side() -> None:
    assert outcome(Range(0.01, 0.2)) is Outcome.BETTER
    assert outcome(Range(-0.2, -0.01)) is Outcome.WORSE
    assert outcome(Range(-0.1, 0.2)) is Outcome.NO_DIFFERENCE
    assert outcome(Range(0.0, 0.2)) is Outcome.NO_DIFFERENCE
    assert outcome(Range(-0.2, 0.0)) is Outcome.NO_DIFFERENCE


def test_a_candidate_better_on_one_screen_of_two_shows_no_difference() -> None:
    """Differences 0.5 and 0: a quarter of the resamples are both 0, so the range is 0 to 0.5 and holds
    zero. The mean difference, 0.25, is not a win."""
    (result,) = compare(marked(0.5, 1.0), marked(1.0, 1.0))

    assert result == Comparison(
        VERDICT_GRID_V1,
        judgements=2,
        baseline=0.75,
        candidate=1.0,
        difference=0.25,
        range=Range(0.0, 0.5),
        outcome=Outcome.NO_DIFFERENCE,
    )


def test_a_candidate_better_on_every_screen_wins_and_worse_on_every_screen_is_worse() -> None:
    """Every difference +0.5, so every resample's mean is 0.5: the range is 0.5 to 0.5. The other way round,
    -0.5 to -0.5."""
    (better,) = compare(marked(0.5, 0.5, 0.0), marked(1.0, 1.0, 0.5))
    (worse,) = compare(marked(1.0, 1.0, 0.5), marked(0.5, 0.5, 0.0))

    assert (better.range, better.outcome) == (Range(0.5, 0.5), Outcome.BETTER)
    assert (worse.range, worse.outcome) == (Range(-0.5, -0.5), Outcome.WORSE)


def test_a_comparison_needs_the_same_test_judgements_on_both_sides() -> None:
    with pytest.raises(ValueError, match="same test Judgements"):
        compare(marked(0.5, 1.0), marked(0.5))


# -- the Line-up: the pairs its Reductions give ---------------------------------------------------------

PICKED = PickTile(picked=True)
PASSED = PickTile()
STARRED = PickTile(favourite=True)


def picked(tiles: Mapping[str, PickTile], *, second: int, none_of_these: bool = False) -> Judgement:
    """A **Pick** of a **Line-up** on the compare **Screen**, its tiles in on-screen order."""
    return judged(
        tiles,
        second=second,
        instrument=PICK_V1,
        response=PickResponse(none_of_these=none_of_these),
        screen="compare",
    )


def hand_worked_picks() -> list[Judgement]:
    """One warm-up grid screen, a **Pick** of five, a "none of these" and a **Pick** of two."""
    return [
        judged({"a": FAVOURITE, "c": LIKE, "b": BAN}, second=0),
        picked({"p": PASSED, "q": PICKED, "r": PASSED, "s": PASSED, "t": PASSED}, second=1),
        picked({"u": PASSED, "v": PASSED}, second=2, none_of_these=True),
        picked({"u": PASSED, "v": PICKED}, second=3),
    ]


def hand_worked_picks_table() -> Table:
    return Table(
        {
            ("p", "a"): 0.95,
            ("q", "c"): 0.95,
            ("r", "c"): 0.95,
            ("t", "b"): 0.95,
            ("u", "a"): 0.95,
            ("v", "q"): 0.95,
        }
    )


def test_pick_accuracy_on_a_tiny_log_is_the_hand_worked_answer() -> None:
    """Warm-up 1, radius 0.1 and decay 0, so each near decided Item adds exactly its value. The warm-up grid
    decides a (Favourite, 100), c (Like, 50) and b (Ban, -100).

    The first **Pick**: q (near c, 50) over p (near a, 100), wrong, 0; over r (near c, 50), a tie, half;
    over s (near nothing, 0), right, 1; over t (near b, -100), right, 1. 2.5 of 4: 0.625.

    "None of these" gives no pair, so it is no test answer.

    The second **Pick**: v over u. u is near a, 100. v is near q, but q only ever won a **Pick**: relative
    Evidence, which ranking ignores, so q is no decided Item and v scores 0. 0 < 100, wrong: 0.0.

    Each **Pick** weighs once: (0.625 + 0.0) / 2 = 0.3125, where pooling the five pairs would give 2.5 / 5.
    Resampling two **Picks**, a quarter of the resamples are both 0.0 and a quarter both 0.625, so the 95%
    range is exactly 0.0 to 0.625.
    """
    log = hand_worked_picks()

    tested = replay(log, hand_worked_picks_table(), radius=RADIUS, decay=DECAY, warm_up=1)

    assert tested == (
        Marked(log[1].id, PICK_V1, pairs=4, accuracy=0.625, items=("p", "q", "r", "s", "t")),
        Marked(log[3].id, PICK_V1, pairs=1, accuracy=0.0, items=("u", "v")),
    )
    assert pairwise_accuracy(tested) == (
        Accuracy(PICK_V1, judgements=2, accuracy=0.3125, range=Range(0.0, 0.625)),
    )


def test_a_pick_reads_as_its_winner_over_each_other_tile() -> None:
    screen = picked({"p": PASSED, "q": PICKED, "r": PASSED}, second=0)

    assert reading(screen) == (Pair("q", "p"), Pair("q", "r"))


def test_none_of_these_is_never_a_test_answer() -> None:
    assert reading(picked({"p": PASSED, "q": STARRED}, second=0, none_of_these=True)) == ()


def test_a_pick_never_makes_its_winner_a_decided_item() -> None:
    """Relative Evidence still does not feed ranking: after the first **Pick**, won by q, the second
    **Pick**'s matrix has the grid's three decided Items as its columns and no q."""
    table = hand_worked_picks_table()

    replay(hand_worked_picks(), table, radius=RADIUS, decay=DECAY, warm_up=1)

    assert table.calls == [(("p", "q", "r", "s", "t"), ("a", "b", "c")), (("u", "v"), ("a", "b", "c"))]


def test_a_star_on_a_pick_is_no_pair_but_is_evidence_for_the_screens_after_it() -> None:
    """A starred tile is absolute 100, which orders it against nothing on its screen: the **Pick**'s one
    pair is still its winner over the starred loser, and no grid pair is read from it. A later screen sees
    the star: a Like near it over a Ban, right."""
    star = picked({"won": PICKED, "starred": STARRED}, second=0)
    later = judged({"near": LIKE, "far": BAN}, second=1)

    marks = replay([star, later], Table({("near", "starred"): 0.95}), radius=RADIUS, decay=DECAY, warm_up=0)

    assert reading(star) == (Pair("won", "starred"),)
    assert [(each.judgement_id, each.instrument, each.accuracy) for each in marks] == [
        (star.id, PICK_V1, 0.5),
        (later.id, VERDICT_GRID_V1, 1.0),
    ]


def test_a_pick_on_items_decided_since_it_was_drawn_scores_neither_against_its_own_verdict() -> None:
    """A **Batch** drawn after the **Line-up** may show its tiles and be submitted first (the one-way draw
    exclusion): x a Like, y a Ban. The **Pick** of y over x is marked from that, each without its own
    **Verdict**: x near nothing, 0; y near x, 50. y over x, right."""
    log = [
        judged({"x": LIKE, "y": BAN}, second=0),
        picked({"x": PASSED, "y": PICKED}, second=1),
    ]

    marks = replay(log, Table({("y", "x"): 0.95}), radius=RADIUS, decay=DECAY, warm_up=1)

    assert [each.accuracy for each in marks] == [1.0]


def test_grid_and_pick_rows_stand_side_by_side_and_the_comparison_covers_each() -> None:
    """The hand-worked grid log and the hand-worked **Picks** in one log, the **Picks** after: the grid row
    is 0.75 and the **Pick** row 0.3125 as each was alone, the **Picks** now also seeing the grid's test
    screens, which are near none of their tiles. A candidate radius of 0 reaches nothing (decay 0 keeps the
    weight 1): every pair a tie, 0.5 on every screen. Grid differences 0 and -0.5, mean -0.25; **Pick**
    differences -0.125 and +0.5, mean 0.1875."""
    log = [
        *hand_worked_log(),
        picked({"p": PASSED, "q": PICKED, "r": PASSED, "s": PASSED, "t": PASSED}, second=11),
        picked({"u": PASSED, "v": PICKED}, second=12),
    ]
    table = Table(
        {
            **hand_worked_table().pairs,
            ("p", "a"): 0.95,
            ("q", "c"): 0.95,
            ("r", "c"): 0.95,
            ("t", "b"): 0.95,
            ("u", "a"): 0.95,
        }
    )

    baseline = replay(log, table, radius=RADIUS, decay=DECAY, warm_up=1)
    candidate = replay(log, table, radius=0.0, decay=DECAY, warm_up=1)

    assert [each.accuracy for each in baseline] == [0.5, 1.0, 0.625, 0.0]
    assert pairwise_accuracy(baseline) == (
        Accuracy(PICK_V1, judgements=2, accuracy=0.3125, range=Range(0.0, 0.625)),
        Accuracy(VERDICT_GRID_V1, judgements=2, accuracy=0.75, range=Range(0.5, 1.0)),
    )
    assert [(each.instrument, each.judgements, each.difference) for each in compare(baseline, candidate)] == [
        (PICK_V1, 2, 0.1875),
        (VERDICT_GRID_V1, 2, -0.25),
    ]


def test_an_instrument_with_a_reduction_and_no_test_judgements_has_no_row() -> None:
    """**Pick** has a **Reduction**, but here its one answer is warm-up and the other "none of these": the
    grid alone has a row, not a **Pick** row of nought."""
    log = [
        picked({"p": PICKED, "q": PASSED}, second=0),
        *hand_worked_log()[1:],
        picked({"u": PASSED, "v": PASSED}, second=3, none_of_these=True),
    ]

    accuracy = pairwise_accuracy(replay(log, hand_worked_table(), radius=RADIUS, decay=DECAY, warm_up=1))

    assert [each.instrument for each in accuracy] == [VERDICT_GRID_V1]


def test_an_order_gets_its_row_from_its_reduction_alone() -> None:
    """**Order**'s **Reduction** in the table, and nothing in the evaluation: its pairs are its test answers
    and it has a row of its own. Ranked 1 near the Favourite (100), 2 near nothing (0), the unranked near the
    Like (50): first over second right, first over unranked right, second over unranked wrong: 2 of 3."""
    ordered = judged(
        {"first": OrderTile(rank=1), "second": OrderTile(rank=2), "unranked": OrderTile()},
        second=1,
        instrument=ORDER_V1,
        response=None,
        screen="compare",
    )
    log = [judged({"a": FAVOURITE, "c": LIKE}, second=0), ordered]
    table = Table({("first", "a"): 0.95, ("unranked", "c"): 0.95})

    accuracy = pairwise_accuracy(replay(log, table, radius=RADIUS, decay=DECAY, warm_up=1))

    assert reading(ordered) == (
        Pair("first", "second"),
        Pair("first", "unranked"),
        Pair("second", "unranked"),
    )
    assert [(each.instrument, each.judgements, each.accuracy) for each in accuracy] == [(ORDER_V1, 1, 2 / 3)]


def test_a_gauntlet_gets_its_row_from_its_reduction_alone() -> None:
    """**Gauntlet**'s **Reduction** in the table, and nothing in the evaluation: the winner over each other
    tile is its test answer and it has a row of its own; "none of these" is never one. The champion near the
    Favourite (100) beats a challenger near nothing (0), right, and one near the Like (50), right; 2 of 2."""
    defended = judged(
        {"near": GauntletTile(), "champion": GauntletTile(picked=True), "challenger": GauntletTile()},
        second=1,
        instrument=GAUNTLET_V1,
        response=GauntletResponse(streak=1),
        screen="compare",
    )
    dismissed = judged(
        {"champion": GauntletTile(), "challenger": GauntletTile()},
        second=2,
        instrument=GAUNTLET_V1,
        response=GauntletResponse(streak=2, none_of_these=True),
        screen="compare",
    )
    log = [judged({"a": FAVOURITE, "c": LIKE}, second=0), defended, dismissed]
    table = Table({("champion", "a"): 0.95, ("near", "c"): 0.95})

    accuracy = pairwise_accuracy(replay(log, table, radius=RADIUS, decay=DECAY, warm_up=1))

    assert reading(defended) == (Pair("champion", "near"), Pair("champion", "challenger"))
    assert reading(dismissed) == ()
    assert [(each.instrument, each.judgements, each.accuracy) for each in accuracy] == [(GAUNTLET_V1, 1, 1.0)]


def test_an_instrument_with_no_reduction_has_no_reading_either() -> None:
    with pytest.raises(UnknownInstrument):
        reading(
            judged({"a": OrderTile(rank=1), "b": OrderTile()}, second=0, instrument=Instrument("unbuilt", 1))
        )
