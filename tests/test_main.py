"""The entry point's commands: `reset-pool`, `evaluate`, and serving when none is named.

Uses `make_harness`, because its subject is an adapter over a workflow, as the web tests are: `main` is handed
the harness's modules through its builder and a stand-in for serving, so nothing boots, fetches or listens.
What the **Pool reset** removes is `test_pool.py`'s, and what the evaluation computes is
`test_evaluation.py`'s; this is that each command runs through the modules and says so.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from tests.conftest import Harness, live, make_harness, submit_with
from tests.fakes import catalogue_of
from wallpapi import batches, lineup, pool, settings, workflows
from wallpapi.compose import Modules
from wallpapi.main import main
from wallpapi.model import Verdict


@pytest.fixture
def stocked(db_path: Path) -> Harness:
    """Six in the **Pool**, each thumbnail cached, and nothing decided."""
    harness = make_harness(db_path, catalogue=catalogue_of(6))
    workflows.save_settings(harness.modules, batch_size=2)
    for w in catalogue_of(6):
        harness.modules.thumbnails.get(harness.connect(), w.id)
    return harness


def never_serve() -> None:
    raise AssertionError("a command must not serve")


def test_reset_pool_resets_through_the_modules_it_builds_and_says_what_it_removed(
    stocked: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    live(stocked)
    stocked.modules.lineups.next(stocked.connect())

    main(["reset-pool"], build=lambda: stocked.modules, serve=never_serve)

    assert pool.members(stocked.connect()) == []
    assert batches.showing(stocked.connect()) == lineup.showing(stocked.connect()) == set()
    assert capsys.readouterr().out == (
        "Pool members removed: 6\nLive Batch discarded: yes\nLive Line-up discarded: yes\n"
        "Thumbnails evicted: 6\n"
    )


def test_reset_pool_says_when_there_was_no_live_batch_or_lineup(
    stocked: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["reset-pool"], build=lambda: stocked.modules, serve=never_serve)

    out = capsys.readouterr().out
    assert "Live Batch discarded: no\n" in out
    assert "Live Line-up discarded: no\n" in out


def test_no_command_serves_and_builds_nothing() -> None:
    served: list[bool] = []

    def never_build() -> Modules:
        raise AssertionError("serving builds its own app")

    main([], build=never_build, serve=lambda: served.append(True))

    assert served == [True]


# -- evaluate ----------------------------------------------------------------------------------------


def unit(*values: float) -> NDArray[np.float32]:
    raw = np.array(values, dtype=np.float32)
    return (raw / np.linalg.norm(raw)).astype(np.float32)


LIKED = unit(1.0, 0.0)
PASSED_OVER = unit(0.0, 1.0)
NEAR_LIKED = unit(0.9, 0.4358899)
"""A cosine of 0.9 to `LIKED`: a mapped similarity of 0.95, so a distance of 0.05."""
NEAR_LIKED_THE_OTHER_WAY = unit(0.9, -0.4358899)
"""As near `LIKED`, on its other side: a cosine of 0.62 to `NEAR_LIKED`, a distance of 0.19."""
NEAR_PASSED_OVER = unit(0.4358899, 0.9)


class Judged:
    """Twelve **Batches** of two submitted through the harness, a **Like** and an unmarked tile each: ten
    warm-up **Judgements** and two test ones."""

    def __init__(self, harness: Harness) -> None:
        self.liked: list[str] = []
        self.passed_over: list[str] = []
        for _ in range(12):
            first, second = (w.id for w in live(harness).wallpapers)
            submit_with(harness, {first: Verdict.LIKE})
            harness.clock.advance(1)
            self.liked.append(first)
            self.passed_over.append(second)


@pytest.fixture
def judged(db_path: Path) -> tuple[Harness, Judged]:
    harness = make_harness(db_path, catalogue=catalogue_of(24))
    workflows.save_settings(harness.modules, batch_size=2)
    return harness, Judged(harness)


def database_bytes(db_path: Path) -> dict[str, str]:
    """A digest of the database file and of its write-ahead log or rollback journal, whichever exist. Not
    the shared-memory index, which a reader may touch."""
    files = [db_path, *(db_path.with_name(db_path.name + suffix) for suffix in ("-wal", "-journal"))]
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files if path.exists()}


def test_evaluate_prints_the_accuracy_of_the_current_settings_from_the_stored_embeddings_alone(
    judged: tuple[Harness, Judged], capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing embedded: every **Score** is 0, each test **Batch**'s one pair a tie, 0.5. The fallback would
    have called each test **Like** the twin of an earlier one, and is never asked (ADR 0024)."""
    harness, log = judged
    for test_like in log.liked[10:]:
        for earlier in log.liked[:10]:
            harness.similarity.similarity_by_pair[(test_like, earlier)] = 1.0

    main(["evaluate"], build=lambda: harness.modules, serve=never_serve)

    assert capsys.readouterr().out == (
        "Evaluation reading 1: 10 warm-up Judgements, 95% ranges from 2000 resamples (seed 20261027).\n"
        "\n"
        "Current settings: radius 0.1, decay 4\n"
        "Instrument       Tests  Accuracy  95% range\n"
        "verdict grid v1      2     0.500  0.500 to 0.500\n"
        "\n"
        "Tested Wallpapers with no Embedding: 4 of 4\n"
    )


def test_evaluate_with_a_candidate_prints_both_the_difference_and_the_verdict(
    judged: tuple[Harness, Judged], capsys: pytest.CaptureFixture[str]
) -> None:
    """Every warm-up **Like** at `LIKED` and every warm-up tile passed over at `PASSED_OVER`. The test
    **Likes** sit 0.05 from the **Likes**, on either side of them so 0.19 from each other; the first test
    tile passed over sits 0.05 from the **Ignores**, and the second has no **Embedding**, so it scores 0.
    Radius 0.1 reaches the warm-up: Like over Ignore, right, 1.0 on both screens. A candidate radius of
    0.01 reaches nothing: two ties, 0.5. The difference is -0.5 on every screen, so its range is -0.5 to
    -0.5 and excludes zero on the worse side."""
    harness, log = judged
    vectors = harness.store.vector_by_id
    vectors.update({w: LIKED for w in log.liked[:10]})
    vectors.update({w: PASSED_OVER for w in log.passed_over[:10]})
    vectors[log.liked[10]], vectors[log.liked[11]] = NEAR_LIKED, NEAR_LIKED_THE_OTHER_WAY
    vectors[log.passed_over[10]] = NEAR_PASSED_OVER

    main(["evaluate", "--radius", "0.01", "--decay", "4"], build=lambda: harness.modules, serve=never_serve)

    assert capsys.readouterr().out == (
        "Evaluation reading 1: 10 warm-up Judgements, 95% ranges from 2000 resamples (seed 20261027).\n"
        "\n"
        "Current settings: radius 0.1, decay 4\n"
        "Instrument       Tests  Accuracy  95% range\n"
        "verdict grid v1      2     1.000  1.000 to 1.000\n"
        "\n"
        "Candidate: radius 0.01, decay 4\n"
        "Instrument       Tests  Accuracy  95% range\n"
        "verdict grid v1      2     0.500  0.500 to 0.500\n"
        "\n"
        "Candidate minus current\n"
        "Instrument       Tests  Difference  95% range         Verdict\n"
        "verdict grid v1      2      -0.500  -0.500 to -0.500  candidate worse\n"
        "\n"
        "Tested Wallpapers with no Embedding: 1 of 4\n"
    )


def test_a_candidate_names_its_radius_or_its_decay_and_takes_the_other_from_the_settings(
    judged: tuple[Harness, Judged], capsys: pytest.CaptureFixture[str]
) -> None:
    harness, _ = judged

    main(["evaluate", "--decay", "2.5"], build=lambda: harness.modules, serve=never_serve)

    assert "Candidate: radius 0.1, decay 2.5\n" in capsys.readouterr().out


@pytest.mark.parametrize(
    "arguments", [["--radius", "1.5"], ["--radius", "-0.1"], ["--decay", "nan"], ["--decay", "51"]]
)
def test_a_candidate_outside_what_the_settings_allow_is_refused(arguments: list[str]) -> None:
    with pytest.raises(SystemExit):
        main(["evaluate", *arguments], build=never_build, serve=never_serve)


def never_build() -> Modules:
    raise AssertionError("a refused command builds nothing")


def test_evaluate_writes_nothing_and_never_asks_for_the_model(
    judged: tuple[Harness, Judged], db_path: Path
) -> None:
    """Safe beside a running server: the database's files are byte for byte as they were, the model and the
    embed step are never asked for anything, and the connection is handed back able to write."""
    harness, log = judged
    harness.store.vector_by_id.update({w: LIKED for w in log.liked})
    before = database_bytes(db_path)
    model_calls, embedded = harness.model.calls, list(harness.embed.seen)

    main(["evaluate", "--radius", "0.2"], build=lambda: harness.modules, serve=never_serve)

    assert database_bytes(db_path) == before
    assert (harness.model.calls, harness.embed.seen) == (model_calls, embedded)
    assert isinstance(workflows.save_settings(harness.modules, batch_size=3), settings.Settings)


def test_evaluate_says_so_when_the_log_is_all_warm_up(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    harness = make_harness(db_path, catalogue=catalogue_of(6))
    workflows.save_settings(harness.modules, batch_size=2)
    submit_with(harness, {})

    main(["evaluate"], build=lambda: harness.modules, serve=never_serve)

    assert capsys.readouterr().out.endswith(
        "No test Judgements yet: the first 10 Judgements are warm-up, and only screens that order two "
        "Wallpapers are tested.\n"
    )
