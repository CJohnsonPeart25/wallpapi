"""`scripts/bench_draw.py`, at a size small enough for the suite: it builds a log through the real modules and
times every figure it reports, and one seed gives one log, which is what a before and after rests on.

The benchmark at size never runs here; a later ticket runs it by hand on its base commit and its branch.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts import bench_draw

from wallpapi.taste.evaluation import ExplicitOnly, MostRecent


def test_a_tiny_log_is_built_and_every_figure_is_timed(tmp_path: Path) -> None:
    built = bench_draw.build(tmp_path, 0, 20)
    run = bench_draw.measure(tmp_path, 0, 20, 1)

    assert run["counts"]["judgements"] == 20
    assert run["counts"]["wallpapers"] == built["wallpapers"]
    explicit = run["counts"]["explicit"]
    assert isinstance(explicit, int)
    assert run["counts"]["thumbnails"] == explicit + bench_draw.POOL, "each Explicit Verdict and Pool member"
    assert set(run["times"]) == {key for key, _ in bench_draw.TIMES}
    assert run["chunked"] == []
    report = bench_draw.Report(commit=None, machine={}, seed=0, reps=1, out=str(tmp_path), sizes={"20": run})
    assert bench_draw.table(report).count("\n") == 1 + len(bench_draw.TIMES) + len(bench_draw.COUNTS)


def test_a_tiny_log_times_each_decided_set_after_the_default_figures(tmp_path: Path) -> None:
    bench_draw.build(tmp_path, 0, 20)
    run = bench_draw.measure(tmp_path, 0, 20, 1, decided_sets=[ExplicitOnly(), MostRecent(5)])

    rows = {f"{row}[{name}]" for row in ("narrow", "measure", "rank") for name in ("explicit", "recent:5")}
    assert set(run["times"]) == {key for key, _ in bench_draw.TIMES} | rows
    assert set(run["counts"]) == {"judgements"} | {key for key, _ in bench_draw.COUNTS} | {
        "decided_columns[explicit]",
        "decided_columns[recent:5]",
    }
    assert run["counts"]["decided_columns[explicit]"] == run["counts"]["explicit"]
    recent, every = run["counts"]["decided_columns[recent:5]"], run["counts"]["decided_columns"]
    assert isinstance(recent, int) and isinstance(every, int)
    assert 0 < recent < every
    report = bench_draw.Report(commit=None, machine={}, seed=0, reps=1, out=str(tmp_path), sizes={"20": run})
    table = bench_draw.table(report)
    assert table.count("\n") == 1 + len(bench_draw.TIMES) + len(bench_draw.COUNTS) + 2 * 4
    assert table.index("| recent:5: ") > table.index("| explicit: ") > table.index("| ranking digest |")


def test_a_decided_set_the_baseline_did_not_time_shows_a_dash_before() -> None:
    report = _report({"1000": []})
    report["sizes"]["1000"]["times"]["rank[explicit]"] = 2.0

    assert "| explicit: `rank`, the narrowed matrix given | - -> 2.0 |" in bench_draw.table(
        report, _report({"1000": []})
    )


def test_a_decided_set_is_refused_as_evaluate_refuses_it(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        bench_draw.main(["--decided", "recent:0"])

    assert "a decided set is all, explicit, or recent:N" in capsys.readouterr().err


def test_one_seed_builds_one_log_and_another_seed_another(tmp_path: Path) -> None:
    digests: list[int | str | None] = []
    # A folder each: on Windows a database this process still holds open cannot be deleted to rebuild it.
    for run, seed in enumerate((0, 0, 1)):
        bench_draw.build(tmp_path / str(run), seed, 20)
        digests.append(bench_draw.measure(tmp_path / str(run), seed, 20, 1)["counts"]["digest"])

    assert digests[0] == digests[1] != digests[2]


def _report(chunked: dict[str, list[str]]) -> bench_draw.Report:
    """A report with a figure for every row at each size given, those listed timed with `resolve` chunked."""
    sizes = {
        size: bench_draw.SizeRun(
            times={key: 1.0 for key, _ in bench_draw.TIMES},
            counts={key: 1 for key, _ in bench_draw.COUNTS},
            chunked=keys,
        )
        for size, keys in chunked.items()
    }
    return bench_draw.Report(commit=None, machine={}, seed=0, reps=1, out="", sizes=sizes)


FOOTNOTE = "\n\n* Failed as is"


def test_a_table_without_a_starred_figure_has_no_footnote() -> None:
    assert FOOTNOTE not in bench_draw.table(_report({"1000": []}), _report({"1000": []}))


def test_a_starred_figure_brings_the_footnote_under_the_table() -> None:
    assert FOOTNOTE in bench_draw.table(_report({"1000": [], "10000": ["ranked"]}))


def test_a_starred_figure_in_the_baselines_half_brings_the_footnote() -> None:
    assert FOOTNOTE in bench_draw.table(_report({"10000": []}), _report({"10000": ["ranked"]}))


def test_a_starred_baseline_size_the_table_does_not_show_brings_no_footnote() -> None:
    assert FOOTNOTE not in bench_draw.table(_report({"1000": []}), _report({"1000": [], "50000": ["ranked"]}))
