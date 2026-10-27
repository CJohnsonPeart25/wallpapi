"""The entry point: `uv run python -m wallpapi`, wiring the real dependencies together. Bound to `127.0.0.1`
and single-worker (`--workers N` is N writers and N refill threads). `uv run python -m wallpapi reset-pool`
runs a **Pool reset** instead of serving (ADR 0025); `uv run python -m wallpapi evaluate` prints the
evaluation, optionally against a candidate `--radius` and `--decay`.
"""

from __future__ import annotations

import argparse
import math
import os
import secrets
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import uvicorn
from fastapi import FastAPI
from numpy.typing import NDArray

from wallpapi import evidence_log, settings, storage, workflows
from wallpapi.compose import Modules, compose
from wallpapi.library import DownloadingLibraryWriter
from wallpapi.similarity import DownloadedModel, EmbeddingCache, Embeddings, OnnxClipEmbedder
from wallpapi.taste import evaluation
from wallpapi.taste.clock import SystemClock
from wallpapi.taste.evaluation import Accuracy, Comparison
from wallpapi.taste.judgement import Instrument
from wallpapi.taste.rng import SeededRandom
from wallpapi.wallhaven import WallhavenClient
from wallpapi.web.app import create_app

HOST = "127.0.0.1"
DEFAULT_PORT = 8000

MODEL_FILENAME = "clip-vit-b32-vision-quantized.onnx"
"""The image tower's name on disk, under `models/` in the wallpapi home."""


def wallpapi_home() -> Path:
    """Where the **Evidence log**, the **Thumbnail cache** and the model live."""
    configured = os.environ.get("WALLPAPI_HOME")
    return Path(configured) if configured else Path.home() / ".wallpapi"


def build_similarity(root: Path) -> Embeddings:
    """The **Similarity provider** (ADR 0013): nothing opened, imported or fetched until its thread asks."""
    model = root / "models" / MODEL_FILENAME
    return Embeddings(EmbeddingCache(root / "embeddings.db"), OnnxClipEmbedder(model), DownloadedModel(model))


def build_modules(home: Path | None = None) -> Modules:
    """The real modules over the real collaborators. The seed is fresh per boot unless `WALLPAPI_SEED` pins
    it; the Refill draws its own stream from the one after it, and the **Line-up** from the one after that.
    """
    root = wallpapi_home() if home is None else home
    root.mkdir(parents=True, exist_ok=True)
    pinned = os.environ.get("WALLPAPI_SEED")
    seed = int(pinned) if pinned else secrets.randbits(64)
    return compose(
        db_path=root / "wallpapi.db",
        thumbnail_dir=root / "thumbnails",
        wallhaven=WallhavenClient(),
        library_writer=DownloadingLibraryWriter(),
        similarity=build_similarity(root),
        random_source=SeededRandom(seed),
        refill_random_source=SeededRandom(seed + 1),
        lineup_random_source=SeededRandom(seed + 2),
        clock=SystemClock(),
    )


def build_app() -> FastAPI:
    """The real app with its background loops. `refill=True` appears here and nowhere else, and nothing
    tests it: lose that line and nothing fills.
    """
    return create_app(build_modules(), refill=True)


def run_server() -> None:
    port = int(os.environ.get("WALLPAPI_PORT", DEFAULT_PORT))
    uvicorn.run(build_app(), host=HOST, port=port)


def main(
    argv: Sequence[str] | None = None,
    *,
    build: Callable[[], Modules] = build_modules,
    serve: Callable[[], None] = run_server,
) -> None:
    """Serve, or run the command named. A command builds the modules without the background loops, so it is
    safe beside a running server: every write takes `BEGIN IMMEDIATE`.
    """
    parser = argparse.ArgumentParser(prog="wallpapi", description="Serve wallpapi, or run one command.")
    commands = parser.add_subparsers(dest="command", metavar="command")
    commands.add_parser(
        "reset-pool",
        help="Empty the Pool and discard the live Batch and Line-up; the refill restocks to the Pool target "
        "size.",
    )
    evaluate = commands.add_parser(
        "evaluate",
        help="Print how well the Score orders what you said, replaying the Evidence log; with a candidate "
        "radius or decay, whether it does better. Reads only.",
    )
    evaluate.add_argument("--radius", type=_radius, help="A candidate Similarity radius, 0 to 1.")
    evaluate.add_argument(
        "--decay", type=_decay, help=f"A candidate Similarity decay, 0 to {settings.MAX_SIMILARITY_DECAY:g}."
    )
    arguments = parser.parse_args(argv)
    if arguments.command is None:
        serve()
        return
    if arguments.command == "evaluate":
        _print_evaluation(_evaluate(build(), radius=arguments.radius, decay=arguments.decay))
        return
    reset = workflows.reset_pool(build())
    print(f"Pool members removed: {reset.members}")
    print(f"Live Batch discarded: {'yes' if reset.batches_discarded else 'no'}")
    print(f"Live Line-up discarded: {'yes' if reset.lineups_discarded else 'no'}")
    print(f"Thumbnails evicted: {len(reset.eviction.evicted)}")


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A candidate radius and decay, its accuracy and its paired comparison with the current settings."""

    radius: float
    decay: float
    accuracy: tuple[Accuracy, ...]
    comparisons: tuple[Comparison, ...]


@dataclass(frozen=True, slots=True)
class _Evaluated:
    """What `evaluate` prints: the current settings' accuracy, a candidate's and the comparison where one was
    given, and how many tested **Wallpapers** had no **Embedding**."""

    radius: float
    decay: float
    current: tuple[Accuracy, ...]
    candidate: _Candidate | None
    tested: int
    unembedded: int


def _evaluate(modules: Modules, *, radius: float | None, decay: float | None) -> _Evaluated:
    """Replay the **Evidence log** under the current settings and, if either is given, under a candidate
    radius and decay, through one read-only snapshot. **Similarity** is the stored **Embeddings** alone, as
    once the model is open (ADR 0024): the model is never opened or fetched, nor the fallback asked."""
    with storage.read_only(modules.connect()) as connection:
        judgements = evidence_log.judgements(connection)
        current = settings.get(connection)
    embeddings = modules.similarity

    def similarity(rows: Sequence[str], columns: Sequence[str]) -> NDArray[np.float32]:
        return embeddings.stored_similarities(
            [evidence_log.wallpaper_id(key) for key in rows],
            [evidence_log.wallpaper_id(key) for key in columns],
        )

    baseline = evaluation.replay(
        judgements, similarity, radius=current.similarity_radius, decay=current.similarity_decay
    )
    candidate = None
    if radius is not None or decay is not None:
        radius = current.similarity_radius if radius is None else radius
        decay = current.similarity_decay if decay is None else decay
        replayed = evaluation.replay(judgements, similarity, radius=radius, decay=decay)
        candidate = _Candidate(
            radius,
            decay,
            accuracy=evaluation.pairwise_accuracy(replayed),
            comparisons=evaluation.compare(baseline, replayed),
        )
    tested = list(dict.fromkeys(evidence_log.wallpaper_id(key) for each in baseline for key in each.items))
    return _Evaluated(
        radius=current.similarity_radius,
        decay=current.similarity_decay,
        current=evaluation.pairwise_accuracy(baseline),
        candidate=candidate,
        tested=len(tested),
        unembedded=len(embeddings.unembedded(tested)),
    )


def _print_evaluation(evaluated: _Evaluated) -> None:
    print(
        f"Evaluation reading {evaluation.READING_VERSION}: {evaluation.WARM_UP} warm-up Judgements, 95% "
        f"ranges from {evaluation.RESAMPLES} resamples (seed {evaluation.SEED})."
    )
    if not evaluated.current:
        print(
            f"No test Judgements yet: the first {evaluation.WARM_UP} Judgements are warm-up, and only "
            "screens that order two Wallpapers are tested."
        )
        return
    print()
    print(f"Current settings: radius {evaluated.radius:g}, decay {evaluated.decay:g}")
    _print_accuracy(evaluated.current)
    candidate = evaluated.candidate
    if candidate is not None:
        print()
        print(f"Candidate: radius {candidate.radius:g}, decay {candidate.decay:g}")
        _print_accuracy(candidate.accuracy)
        print()
        print("Candidate minus current")
        _print_comparisons(candidate.comparisons)
    print()
    print(f"Tested Wallpapers with no Embedding: {evaluated.unembedded} of {evaluated.tested}")


def _print_accuracy(accuracy: Sequence[Accuracy]) -> None:
    width = _instrument_width(each.instrument for each in accuracy)
    print(f"{'Instrument':<{width}}  Tests  Accuracy  95% range")
    for each in accuracy:
        print(
            f"{_instrument(each.instrument):<{width}}  {each.judgements:>5}  {each.accuracy:>8.3f}  "
            f"{each.range.low:.3f} to {each.range.high:.3f}"
        )


def _print_comparisons(comparisons: Sequence[Comparison]) -> None:
    width = _instrument_width(each.instrument for each in comparisons)
    spans = [f"{each.range.low:+.3f} to {each.range.high:+.3f}" for each in comparisons]
    span_width = max(len("95% range"), *(len(span) for span in spans))
    print(f"{'Instrument':<{width}}  Tests  Difference  {'95% range':<{span_width}}  Verdict")
    for each, span in zip(comparisons, spans, strict=True):
        print(
            f"{_instrument(each.instrument):<{width}}  {each.judgements:>5}  {each.difference:>+10.3f}  "
            f"{span:<{span_width}}  {each.outcome.value}"
        )


def _instrument(instrument: Instrument) -> str:
    return f"{instrument.name} v{instrument.version}"


def _instrument_width(instruments: Iterable[Instrument]) -> int:
    return max(len("Instrument"), *(len(_instrument(each)) for each in instruments))


def _radius(text: str) -> float:
    return _bounded(text, 1.0, "a candidate radius is a number from 0 to 1")


def _decay(text: str) -> float:
    maximum = settings.MAX_SIMILARITY_DECAY
    return _bounded(text, maximum, f"a candidate decay is a number from 0 to {maximum:g}")


def _bounded(text: str, maximum: float, refusal: str) -> float:
    """A finite number in `[0, maximum]`, as the setting it stands in for accepts, or argparse's error."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(refusal) from None
    if not math.isfinite(value) or not 0.0 <= value <= maximum:
        raise argparse.ArgumentTypeError(refusal)
    return value


if __name__ == "__main__":
    main()
