"""`scripts/bench_draw.py`, at a size small enough for the suite: it builds a log through the real modules and
times every figure it reports, and one seed gives one log, which is what a before and after rests on.

The benchmark at size never runs here; a later ticket runs it by hand on its base commit and its branch.
"""

from __future__ import annotations

from pathlib import Path

from scripts import bench_draw


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


def test_one_seed_builds_one_log_and_another_seed_another(tmp_path: Path) -> None:
    digests: list[int | str | None] = []
    # A folder each: on Windows a database this process still holds open cannot be deleted to rebuild it.
    for run, seed in enumerate((0, 0, 1)):
        bench_draw.build(tmp_path / str(run), seed, 20)
        digests.append(bench_draw.measure(tmp_path / str(run), seed, 20, 1)["counts"]["digest"])

    assert digests[0] == digests[1] != digests[2]
