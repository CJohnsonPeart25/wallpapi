"""#14's measuring stick: build a **Pool** and a **Decision log**, fill the caches, and compare providers.

Run it in this order, from the repo root:

    uv run python scripts/similarity_spike.py build       # ~15 API calls, builds the Pool and the log
    uv run python scripts/similarity_spike.py tags        # one API call per Wallpaper, rate limited
    uv run python scripts/similarity_spike.py thumbs      # thumbnails, a separate host, throttled
    uv run python scripts/similarity_spike.py model       # 85MiB, once ever
    uv run python scripts/similarity_spike.py embed       # a model run per Wallpaper, CPU
    uv run python scripts/similarity_spike.py evaluate    # the comparison
    uv run python scripts/similarity_spike.py neighbours  # the qualitative look

Everything it writes goes under `.spike/` in the worktree, except the model, which goes where the app
looks for it. It never opens `~/.wallpapi/wallpapi.db`.

**Why the Decision log here is synthetic.** There is no real one on this machine — `~/.wallpapi` does not
exist, so there are no **Explicit Verdicts** to leave one out of. So the **Wallpapers** are real, pulled
from five Wallhaven searches, and the **Verdicts** over them are made up to a rule stated in `THEMES`
below: two themes liked, two banned, one left undecided as a distractor.

The rule matters more than the numbers do, so it is worth being plain about it. The ground truth is
*which search a Wallpaper came back from* — Wallhaven's own notion of what an expression means — and not
any feature a provider can see. That is deliberate: a synthetic taste defined by colour would hand the
baseline the answer, and one defined by tags would hand it to the tag provider. What the evaluation
therefore measures is how well each provider recovers a subject-level grouping it was not told about.

Two things it does not measure, and no synthetic log can. It cannot say whether a real person's taste
*is* that cleanly themed — a real **Decision log** has **Favourites** scattered across subjects that only
share a mood — and it cannot say how any provider behaves as the log grows past a few dozen entries. Both
are in the comparison's own caveats. This is a spike; it ends in a decision by a person.

Not collected by pytest: `testpaths` is `tests`, and nothing here imports into the suite. That is the
point — the suite stays free of the network and of the model.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from wallpapi.model import Verdict, Wallpaper, Zone
from wallpapi.ratelimit import CALLS_PER_MINUTE, WINDOW_SECONDS, wait_needed
from wallpapi.scoring import classify, zone_of
from wallpapi.similarity import MetadataSimilarityProvider, SimilarityProvider
from wallpapi.similarity_embedding import (
    EmbeddingCache,
    EmbeddingSimilarityProvider,
    OnnxClipEmbedder,
    download_model,
)
from wallpapi.similarity_tags import TagCache, TagSimilarityProvider
from wallpapi.wallhaven import RateLimited, Tag, WallhavenClient

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / ".spike"
"""Everything the spike writes, inside the worktree and ignored by git."""

POOL_FILE = WORK / "pool.json"
THUMBNAILS = WORK / "thumbnails"
TAGS_DB = WORK / "tags.db"
EMBEDDINGS_DB = WORK / "embeddings.db"
MODEL = Path.home() / ".wallpapi" / "models" / "clip-vit-b32-vision-quantized.onnx"

PAGES_PER_THEME = 3
"""Three pages of 24 is 72 **Wallpapers** a theme, 360 in all — enough for a **Pool** whose neighbours are
not all trivially near, and few enough that tagging it costs eight minutes of the 45-a-minute budget
rather than an afternoon."""

EXPLICIT_VALUES = {Verdict.FAVOURITE: 100, Verdict.LIKE: 50, Verdict.BAN: -100}
IGNORE_VALUE = -10
"""**Verdict resolution**'s numbers, as `core.py` has them. Repeated here rather than imported because
they are private to the Core service and the spike must not reach through the seam to get at them — if
they ever disagree, this file is wrong and the app is right."""

RADIUS = 0.5
DECAY = 4.0
"""The **Similarity radius** and **Similarity decay** the app ships with — "starting points, not tuned
values", per ADR 0007. The headline numbers use these, because they are what the app would actually do;
`evaluate` then sweeps the radius, because a provider whose similarities live in a narrow band needs a
different one and it would be unfair to judge it without saying so."""

SWEEP_RADII = (0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.75, 1.0)
SWEEP_DECAYS = (2.0, 4.0, 8.0, 16.0, 32.0)
"""The grid `evaluate --sweep` searches, over both settings rather than only the **Similarity radius**.

Both, because the two do different work and a provider can be starved of either. The radius decides who
is a neighbour at all; the decay decides how much louder a near neighbour is than a far one. A provider
whose similarities all sit between 0.63 and 0.93 — which is what CLIP cosines actually do — has every
**Wallpaper** inside the shipped radius of 0.5 and separated only by the decay, so sweeping the radius
alone would say it is undiscriminating when what it is, is undertuned.
"""


@dataclass(frozen=True, slots=True)
class Theme:
    """One Wallhaven search, and the made-up taste applied to what comes back.

    `favourites`, `likes`, `bans` and `ignores` are counts taken off the front of the theme's results in
    the order Wallhaven returned them. Off the front rather than at random so that a second run of
    `build` over the same searches produces the same **Decision log**.
    """

    name: str
    query: str
    favourites: int = 0
    likes: int = 0
    bans: int = 0
    ignores: int = 0


THEMES = (
    Theme("mountains", "mountains landscape", favourites=6, likes=10),
    Theme("space", "nebula space", likes=10),
    Theme("anime", "anime girl", bans=8, ignores=8),
    Theme("cars", "sports car", bans=8),
    Theme(
        "cats",
        "cat",
    ),
)
"""The synthetic taste: mountains and space liked, anime and cars banned, cats left undecided.

Four themes carry **Explicit Verdicts** — 42 of them, which is 42 leave-one-out folds — and the fifth
carries none at all, so that the **Unknown** share means something: a **Pool** where every **Wallpaper**
belongs to a decided theme would make every provider look decisive.

Two liked themes and two banned ones rather than one of each, so that a provider cannot do well by
learning a single axis. Mountains and space share almost no palette; anime and cars share almost no
subject.
"""


@dataclass(frozen=True, slots=True)
class Entry:
    """One **Pool** member, the theme it came from, and the **Verdict** the synthetic log gives it."""

    wallpaper: Wallpaper
    theme: str
    verdict: Verdict | None

    @property
    def value(self) -> int:
        """**Verdict resolution**'s value. One entry per **Wallpaper**, so no **Ignore** ever stacks."""
        if self.verdict is None:
            return 0
        return IGNORE_VALUE if self.verdict is Verdict.IGNORE else EXPLICIT_VALUES[self.verdict]


# -- building the Pool and the Decision log ------------------------------------------------------------


def build(_: argparse.Namespace) -> None:
    """Fetch the five themes from Wallhaven and write the **Pool** and the synthetic **Decision log**."""
    client = WallhavenClient()
    calls: deque[float] = deque(maxlen=CALLS_PER_MINUTE)
    seen: set[str] = set()
    entries: list[Entry] = []

    for theme in THEMES:
        found: list[Wallpaper] = []
        seed: str | None = None
        for page in range(1, PAGES_PER_THEME + 1):
            _throttle(calls)
            result = client.search(sorting="relevance", purity="100", query=theme.query, page=page, seed=seed)
            seed = result.seed
            for candidate in result.wallpapers:
                if candidate.id not in seen:
                    seen.add(candidate.id)
                    found.append(candidate)
        entries.extend(_with_verdicts(theme, found))
        print(f"{theme.name:>10}: {len(found):>3} wallpapers")

    WORK.mkdir(parents=True, exist_ok=True)
    POOL_FILE.write_text(json.dumps([_as_json(e) for e in entries], indent=1), encoding="utf-8")
    explicit = sum(1 for e in entries if e.verdict in EXPLICIT_VALUES)
    print(f"\n{len(entries)} Wallpapers, {explicit} Explicit Verdicts, written to {POOL_FILE}")


def _with_verdicts(theme: Theme, found: Sequence[Wallpaper]) -> list[Entry]:
    """Apply the theme's counts to its results, in the order Wallhaven returned them."""
    plan: list[Verdict | None] = []
    plan += [Verdict.FAVOURITE] * theme.favourites
    plan += [Verdict.LIKE] * theme.likes
    plan += [Verdict.BAN] * theme.bans
    plan += [Verdict.IGNORE] * theme.ignores
    plan += [None] * max(0, len(found) - len(plan))
    return [Entry(wallpaper=w, theme=theme.name, verdict=v) for w, v in zip(found, plan, strict=False)]


def _throttle(calls: deque[float]) -> None:
    """Wait out Wallhaven's 45 a minute before the next **API call**, using the app's own limiter.

    A plain `time.sleep` rather than invariant 12's cancellable wait: there is no `stop_event` in a script
    somebody is watching, and Ctrl-C is the shutdown story. The limiter itself is the app's, so what this
    spends is counted exactly the way the refill counts it.
    """
    now = time.monotonic()
    delay = wait_needed(calls, now=now)
    # Evenly paced as well as inside the window, which the limiter alone does not give: `wait_needed`
    # starts every process with an empty window and so happily spends all 45 in the first six seconds.
    # Wallhaven counts its 45 across everything this machine does — it answered 429 here after `build`'s
    # 15 searches and 30 tag calls, which is exactly 45 inside one of its minutes — so a second process
    # that bursts is spending the refill thread's budget without being able to see it. One call every
    # `WINDOW_SECONDS / CALLS_PER_MINUTE` never bursts and so never surprises anybody.
    if calls:
        delay = max(delay, calls[-1] + WINDOW_SECONDS / CALLS_PER_MINUTE - now)
    if delay > 0.0:
        time.sleep(delay)
    calls.append(time.monotonic())


# -- filling the caches --------------------------------------------------------------------------------


def tags(_: argparse.Namespace) -> None:
    """One **API call** per **Wallpaper**, inside the 45-a-minute window, resumable."""
    entries = _load()
    cache = TagCache(TAGS_DB)
    client = WallhavenClient()
    calls: deque[float] = deque(maxlen=CALLS_PER_MINUTE)
    already = cache.fetched_ids()
    outstanding = [e for e in entries if e.wallpaper.id not in already]
    print(
        f"{len(already)} already cached, {len(outstanding)} to fetch — about "
        f"{len(outstanding) / CALLS_PER_MINUTE:.1f} minutes"
    )

    started = time.monotonic()
    for done, entry in enumerate(outstanding, start=1):
        _throttle(calls)
        cache.store(entry.wallpaper.id, _fetched_with_backoff(client, entry.wallpaper.id), fetched_at=_now())
        if done % 25 == 0 or done == len(outstanding):
            print(f"  {done}/{len(outstanding)} in {time.monotonic() - started:.0f}s")
    print(f"tag cache: {cache.size_bytes() / 1024:.0f} KiB at {TAGS_DB}")


def _fetched_with_backoff(client: WallhavenClient, wallpaper_id: str) -> tuple[Tag, ...]:
    """One **Wallpaper**'s tags, waiting Wallhaven out rather than losing the run to a 429.

    The whole minute rather than `Retry-After`: Wallhaven sends no usable header on this endpoint, and its
    window is a minute, so a minute is the honest guess. Three attempts, because a fourth would mean the
    problem is not the rate limit.
    """
    for attempt in range(3):
        try:
            return client.fetch_tags(wallpaper_id)
        except RateLimited as limited:
            delay = limited.retry_after or WINDOW_SECONDS * (attempt + 1)
            print(f"  429 on {wallpaper_id}; waiting {delay:.0f}s")
            time.sleep(delay)
    return client.fetch_tags(wallpaper_id)


def thumbs(_: argparse.Namespace) -> None:
    """The images the embedding provider reads. A separate host from the API, throttled modestly."""
    entries = _load()
    THUMBNAILS.mkdir(parents=True, exist_ok=True)
    client = WallhavenClient()
    fetched = 0
    for entry in entries:
        destination = _thumbnail(entry.wallpaper)
        if destination.exists():
            continue
        destination.write_bytes(client.fetch_thumbnail(entry.wallpaper.thumbnail_url))
        fetched += 1
        # Invariant 11: th.wallhaven.cc publishes no limit and sits behind DDoS protection. Modest, and
        # once ever per **Wallpaper**.
        time.sleep(0.15)
    total = sum(p.stat().st_size for p in THUMBNAILS.iterdir())
    print(f"{fetched} fetched, {len(entries)} cached, {total / 1e6:.1f} MB in {THUMBNAILS}")


def model(_: argparse.Namespace) -> None:
    """Download the CLIP image tower once and verify it against the recorded checksum."""
    path = download_model(MODEL)
    print(f"{path} — {path.stat().st_size / 1e6:.0f} MB")


def embed(_: argparse.Namespace) -> None:
    """A model run per **Wallpaper**, on the CPU, resumable."""
    entries = _load()
    cache = EmbeddingCache(EMBEDDINGS_DB)
    embedder = OnnxClipEmbedder(MODEL)
    already = cache.embedded_ids()
    outstanding = [e for e in entries if e.wallpaper.id not in already and _thumbnail(e.wallpaper).exists()]
    print(f"{len(already)} already embedded, {len(outstanding)} to run")

    started = time.monotonic()
    for start in range(0, len(outstanding), 16):
        batch = outstanding[start : start + 16]
        vectors = embedder([_thumbnail(e.wallpaper) for e in batch])
        for entry, vector in zip(batch, vectors, strict=True):
            cache.store(entry.wallpaper.id, vector)
    elapsed = time.monotonic() - started
    per = elapsed / len(outstanding) if outstanding else 0.0
    print(f"{len(outstanding)} embedded in {elapsed:.1f}s ({per * 1000:.0f} ms each)")
    print(f"embedding cache: {cache.size_bytes() / 1024:.0f} KiB at {EMBEDDINGS_DB}")


# -- the comparison ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Result:
    """What one provider scored, at one **Similarity radius** and **Similarity decay**."""

    name: str
    radius: float
    decay: float
    right_sign: int
    wrong_sign: int
    unknown: int
    banger_precision: float
    banger_recall: float
    dud_precision: float
    dud_recall: float
    separation: float
    pool_unknown_share: float
    seconds: float


def evaluate(arguments: argparse.Namespace) -> None:
    """Leave-one-out over every **Explicit Verdict**, for each provider, at the shipped settings and —
    unless `--no-sweep` — over a grid of the **Similarity radius** and **Similarity decay**."""
    entries = _load()
    providers = _providers()

    print(
        f"Pool: {len(entries)} Wallpapers. "
        f"Explicit Verdicts: {sum(1 for e in entries if e.verdict in EXPLICIT_VALUES)}. "
        f"Decided (non-zero value): {sum(1 for e in entries if e.value != 0)}.\n"
    )

    print(f"At the shipped settings (radius {RADIUS}, decay {DECAY}):\n")
    _print_header()
    for name, provider in providers:
        _print_row(_leave_one_out(name, provider, entries, radius=RADIUS, decay=DECAY))

    if not arguments.no_sweep:
        print("\nBest of the grid, per provider:\n")
        _print_header()
        best: dict[str, Result] = {}
        for name, provider in providers:
            grid = [
                _leave_one_out(name, provider, entries, radius=radius, decay=decay)
                for radius in SWEEP_RADII
                for decay in SWEEP_DECAYS
            ]
            best[name] = max(grid, key=lambda result: (result.right_sign, result.separation))
            _print_row(best[name])
            _print_grid(name, grid, len(_held_out(entries)))

    print("\nCost:\n")
    print(f"{'provider':<12}{'API calls':>12}{'2k Pool':>12}{'cache KiB':>12}{'classify ms':>14}")
    for name, provider in providers:
        calls = len(entries) if name == "tags" else 0
        projected = "2000" if name == "tags" else "0"
        print(
            f"{name:<12}{calls:>12}{projected:>12}{_cache_size(name) / 1024:>12.0f}"
            f"{_classify_ms(provider, entries):>14.1f}"
        )


def _print_grid(name: str, grid: Sequence[Result], folds: int) -> None:
    """Right-sign share over the whole grid, decay down the side and radius across the top."""
    print(f"\n  {name}: right-sign share of {folds} folds, decay (rows) by radius (columns)")
    print("  " + " " * 7 + "".join(f"{radius:>7}" for radius in SWEEP_RADII))
    by_setting = {(result.radius, result.decay): result for result in grid}
    for decay in SWEEP_DECAYS:
        cells = "".join(
            f"{_share(by_setting[(radius, decay)].right_sign, folds):>7.2f}" for radius in SWEEP_RADII
        )
        print(f"  {decay:>7}{cells}")
    print()


def _providers() -> list[tuple[str, SimilarityProvider]]:
    """The three, in the order the comparison reads them: baseline first, then the two candidates."""
    return [
        ("metadata", MetadataSimilarityProvider()),
        ("tags", TagSimilarityProvider(TagCache(TAGS_DB))),
        ("embedding", EmbeddingSimilarityProvider(EmbeddingCache(EMBEDDINGS_DB))),
    ]


def _held_out(entries: Sequence[Entry]) -> list[Entry]:
    """The folds: every **Explicit Verdict**. **Ignores** stay in the decided set but are never hidden —
    an **Ignore** is the absence of an opinion, and "would this have been a **Banger**" is not a question
    about it."""
    return [e for e in entries if e.verdict in EXPLICIT_VALUES]


def _leave_one_out(
    name: str, provider: SimilarityProvider, entries: Sequence[Entry], *, radius: float, decay: float
) -> Result:
    """Hide one **Explicit Verdict** at a time, classify with the rest, and read the sign.

    The held-out **Wallpaper** stays in the **Pool** — it is the row being read — but its own **Verdict**
    leaves the decided set, which is exactly the question being asked: with everything else the user has
    said, would this **Wallpaper** have been put in front of them as a **Banger** or kept back as a
    **Dud**?

    One pass, and the **Scores** are kept rather than only their **Zones**, because how far from zero an
    answer sits is half of what the comparison is about: a **Zone** is a sign, and a provider that is
    right by a hair will be wrong the moment one more **Verdict** is recorded.
    """
    held_out = _held_out(entries)
    started = time.monotonic()
    scores: list[float] = []
    for hidden in held_out:
        decided = [e for e in entries if e.value != 0 and e.wallpaper.id != hidden.wallpaper.id]
        matrix = provider.similarities([hidden.wallpaper], [e.wallpaper for e in decided])
        result = classify(matrix, [e.value for e in decided], radius=radius, decay=decay)
        scores.append(float(result.scores[0]))
    seconds = time.monotonic() - started

    predicted = [zone_of(score) for score in scores]
    wanted = [Zone.BANGER if e.value > 0 else Zone.DUD for e in held_out]
    right = sum(1 for want, got in zip(wanted, predicted, strict=True) if want is got)
    unknown = sum(1 for got in predicted if got is Zone.UNKNOWN)

    return Result(
        name=name,
        radius=radius,
        decay=decay,
        right_sign=right,
        wrong_sign=len(held_out) - right - unknown,
        unknown=unknown,
        banger_precision=_precision(wanted, predicted, Zone.BANGER),
        banger_recall=_recall(wanted, predicted, Zone.BANGER),
        dud_precision=_precision(wanted, predicted, Zone.DUD),
        dud_recall=_recall(wanted, predicted, Zone.DUD),
        separation=_separation(np.array(scores, dtype=np.float64)),
        pool_unknown_share=_pool_unknown_share(provider, entries, radius=radius, decay=decay),
        seconds=seconds,
    )


def _separation(scores: NDArray[np.float64]) -> float:
    """How far from zero the answers sit, as a share of the largest answer any of them got.

    Scale-free on purpose: the three providers' **Scores** are not on one scale — they are sums of the
    same **Verdict** values through different weights — so the comparison has to be of shape rather than
    of magnitude. Mean |score| over the largest |score|. Near 1.0 means the folds are decided about
    equally firmly; near 0.0 means most of them hang on a thread one more **Verdict** would cut.
    """
    if scores.size == 0:
        return 0.0
    largest = float(np.abs(scores).max())
    return 0.0 if largest == 0.0 else float(np.abs(scores).mean()) / largest


def _pool_unknown_share(
    provider: SimilarityProvider, entries: Sequence[Entry], *, radius: float, decay: float
) -> float:
    """What share of the whole **Pool** lands in **Unknown** with the whole **Decision log** in hand.

    The other half of quality, and the one a leave-one-out cannot see: a provider that gets every
    held-out **Verdict** right and calls the rest of the **Pool** **Unknown** has learned nothing that
    helps a **Mix** pick tomorrow's **Batch**. The reverse is a failure too — nothing **Unknown** at all
    means the **Explore** half of a **Mix** has nothing to draw from.
    """
    zones = _classify_pool(provider, entries, radius=radius, decay=decay)
    return _share(sum(1 for zone in zones if zone is Zone.UNKNOWN), len(zones))


def _classify_pool(
    provider: SimilarityProvider, entries: Sequence[Entry], *, radius: float, decay: float
) -> tuple[Zone, ...]:
    """One pass over the whole **Pool**, the way `CoreService.classify_pool` does it.

    **Bans** are excluded from the rows and kept in the columns, per ADR 0007.
    """
    decided = [e for e in entries if e.value != 0]
    candidates = [e for e in entries if e.verdict is not Verdict.BAN]
    matrix = provider.similarities([e.wallpaper for e in candidates], [e.wallpaper for e in decided])
    return classify(matrix, [e.value for e in decided], radius=radius, decay=decay).zones


def _classify_ms(provider: SimilarityProvider, entries: Sequence[Entry]) -> float:
    """Wall time for one whole-**Pool** classification, best of three.

    Best of three rather than a mean: what is being measured is how long the work takes, and a mean over
    three runs on a laptop measures whatever else the laptop was doing.
    """
    timings: list[float] = []
    for _ in range(3):
        started = time.monotonic()
        _classify_pool(provider, entries, radius=RADIUS, decay=DECAY)
        timings.append((time.monotonic() - started) * 1000.0)
    return min(timings)


def neighbours(arguments: argparse.Namespace) -> None:
    """The qualitative look: what each provider thinks is nearest one **Favourite**.

    The numbers in the table say whether a provider gets the sign right; this says whether the answer is
    the one a person would have given, which is the half of "quality" no metric here reaches.
    """
    entries = _load()
    by_id = {e.wallpaper.id: e for e in entries}
    favourites = [e for e in entries if e.verdict is Verdict.FAVOURITE]
    subject = by_id[arguments.wallpaper] if arguments.wallpaper else favourites[0]
    others = [e for e in entries if e.wallpaper.id != subject.wallpaper.id]

    print(f"Nearest to {subject.wallpaper.id} ({subject.theme}, {subject.wallpaper.page_url})\n")
    print(f"  its tags: {', '.join(TagCache(TAGS_DB).names_for(subject.wallpaper.id)) or '(none cached)'}\n")

    for name, provider in _providers():
        column = provider.similarities([e.wallpaper for e in others], [subject.wallpaper])[:, 0]
        order = np.argsort(-column)[: arguments.top]
        print(f"  {name}:")
        for rank, index in enumerate(order, start=1):
            entry = others[int(index)]
            print(
                f"    {rank}. {float(column[int(index)]):.3f}  {entry.theme:<10} {entry.wallpaper.page_url}"
            )
        themes = [others[int(i)].theme for i in np.argsort(-column)[:10]]
        print(f"    top ten themes: {', '.join(themes)}\n")


# -- odds and ends -------------------------------------------------------------------------------------


def _print_header() -> None:
    print(
        f"{'provider':<12}{'radius':>8}{'decay':>7}{'right':>7}{'wrong':>7}{'unknown':>9}"
        f"{'B prec':>8}{'B rec':>7}{'D prec':>8}{'D rec':>7}{'separ':>7}{'pool ?':>8}"
    )


def _print_row(result: Result) -> None:
    print(
        f"{result.name:<12}{result.radius:>8}{result.decay:>7}"
        f"{result.right_sign:>7}{result.wrong_sign:>7}{result.unknown:>9}"
        f"{result.banger_precision:>8.2f}{result.banger_recall:>7.2f}"
        f"{result.dud_precision:>8.2f}{result.dud_recall:>7.2f}"
        f"{result.separation:>7.2f}{result.pool_unknown_share:>8.2f}"
    )


def _precision(wanted: Sequence[Zone], predicted: Sequence[Zone], zone: Zone) -> float:
    called = [want for want, got in zip(wanted, predicted, strict=True) if got is zone]
    return _share(sum(1 for want in called if want is zone), len(called))


def _recall(wanted: Sequence[Zone], predicted: Sequence[Zone], zone: Zone) -> float:
    actual = [got for want, got in zip(wanted, predicted, strict=True) if want is zone]
    return _share(sum(1 for got in actual if got is zone), len(actual))


def _share(part: int, whole: int) -> float:
    return 0.0 if whole == 0 else part / whole


def _cache_size(name: str) -> int:
    if name == "tags":
        return TagCache(TAGS_DB).size_bytes()
    if name == "embedding":
        return EmbeddingCache(EMBEDDINGS_DB).size_bytes()
    return 0


def _thumbnail(wallpaper: Wallpaper) -> Path:
    return THUMBNAILS / f"{wallpaper.id}.jpg"


def _now() -> Any:
    import datetime as dt

    return dt.datetime.now(dt.UTC)


def _as_json(entry: Entry) -> dict[str, Any]:
    wallpaper = entry.wallpaper
    return {
        "id": wallpaper.id,
        "width": wallpaper.width,
        "height": wallpaper.height,
        "ratio": wallpaper.ratio,
        "category": wallpaper.category,
        "purity": wallpaper.purity,
        "favourites": wallpaper.favourites,
        "colours": list(wallpaper.colours),
        "thumbnail_url": wallpaper.thumbnail_url,
        "full_url": wallpaper.full_url,
        "page_url": wallpaper.page_url,
        "theme": entry.theme,
        "verdict": None if entry.verdict is None else entry.verdict.value,
    }


def _load() -> list[Entry]:
    if not POOL_FILE.exists():
        raise SystemExit(f"no Pool at {POOL_FILE} — run `build` first")
    raw: list[dict[str, Any]] = json.loads(POOL_FILE.read_text(encoding="utf-8"))
    entries: list[Entry] = []
    for item in raw:
        wallpaper = Wallpaper(
            id=str(item["id"]),
            width=int(item["width"]),
            height=int(item["height"]),
            ratio=str(item["ratio"]),
            category=str(item["category"]),
            purity=str(item["purity"]),
            favourites=int(item["favourites"]),
            colours=tuple(str(c) for c in item["colours"]),
            thumbnail_url=str(item["thumbnail_url"]),
            full_url=str(item["full_url"]),
            page_url=str(item["page_url"]),
        )
        verdict = item["verdict"]
        entries.append(
            Entry(
                wallpaper=wallpaper,
                theme=str(item["theme"]),
                verdict=None if verdict is None else Verdict(str(verdict)),
            )
        )
    return entries


COMMANDS: dict[str, Callable[[argparse.Namespace], None]] = {
    "build": build,
    "tags": tags,
    "thumbs": thumbs,
    "model": model,
    "embed": embed,
    "evaluate": evaluate,
    "neighbours": neighbours,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        subparser = subparsers.add_parser(name)
        if name == "evaluate":
            subparser.add_argument("--no-sweep", action="store_true")
        if name == "neighbours":
            subparser.add_argument("--wallpaper", default=None)
            subparser.add_argument("--top", type=int, default=4)
    arguments = parser.parse_args()
    COMMANDS[str(arguments.command)](arguments)


if __name__ == "__main__":
    main()
