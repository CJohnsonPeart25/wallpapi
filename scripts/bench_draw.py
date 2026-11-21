"""Time a draw as the **Evidence log** grows: `Batches.ranked`, each part of it, and a mint's write lock.

    uv run python scripts/bench_draw.py
    uv run python scripts/bench_draw.py --sizes 1000 10000 50000 --json after.json --baseline before.json

Each size is a synthetic log of that many **Judgements**, drawn from the seed and built through
`storage.migrate`, `pool.admit` and `evidence_log.append` in `--out` (by default the system temp folder),
never in the repo. **Embeddings** are random unit vectors in a real `EmbeddingCache`, opened through the
public upkeep with a model and embedder that refuse to run, so no model is loaded and nothing touches the
network. The **Thumbnail cache** is one empty file for each **Explicit Verdict** and **Pool** member, what a
submission's eviction keeps, so its eviction is timed as a steady pass that deletes nothing. Each size is
built in one fresh process and timed in another, so the peak memory is the timing's.

Every figure is the median of `--reps` runs after one warm-up, in milliseconds. The parts are timed apart
from `ranked`, through the same public calls it makes; a query timed alone is caught as its public call
sends it, and run again. The rows are the exception: `ranked` never calls `Embeddings.vectors`, which is
timed here over a store already read, standing in for the two calls `measure` makes to build its rows. A
figure marked `*` raised "too many SQL variables" as is, and was timed again with `evidence_log.resolve`
patched to send its ids in chunks: the cost the draw would have if it ran. `--json` keeps a run;
`--baseline` sets a kept run beside this one, so a change is timed on its base commit and on its branch
with the same seed and sizes, on the same machine.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import multiprocessing
import platform
import random
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Final, TypedDict, cast
from unittest.mock import patch

import numpy as np
from numpy.typing import NDArray

from wallpapi import evidence_log, pool, settings, storage, thumbnails
from wallpapi.batches import Batches
from wallpapi.evidence_log import ResolvedVerdict, Theme, item_key, provenance, tile_response
from wallpapi.model import Verdict, Wallpaper, Zone
from wallpapi.pool import RefillStatus, RefillStrategy
from wallpapi.similarity import EmbeddingCache, Embeddings
from wallpapi.taste.clock import SystemClock
from wallpapi.taste.gauntlet import GAUNTLET_V1, GauntletResponse, GauntletTile
from wallpapi.taste.judgement import (
    Instrument,
    ItemResponse,
    Judgement,
    JudgementResponse,
    LogPosition,
    RankingStamp,
    Shown,
)
from wallpapi.taste.order import ORDER_V1, OrderTile
from wallpapi.taste.pick import PICK_V1, PickResponse, PickTile
from wallpapi.taste.ranking import rank
from wallpapi.taste.rng import SeededRandom
from wallpapi.taste.verdict_grid import VERDICT_GRID_V1, GridResponse, TileResponse
from wallpapi.thumbnails import Thumbnails
from wallpapi.wallhaven import SearchPage

REPO: Final = Path(__file__).resolve().parent.parent
DIM: Final = 512
POOL: Final = settings.DEFAULT_POOL_TARGET_SIZE
BATCH: Final = settings.DEFAULT_BATCH_SIZE
LINEUP: Final = settings.DEFAULT_LINEUP_SIZE
START: Final = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
CHUNK: Final = 30_000
"""Ids per statement in the patched `resolve`: under SQLite's default limit of 32,766 parameters."""
EMBED_CHUNK: Final = 10_000

MIX: Final = (
    ("grid", 0.68),
    ("skip", 0.06),
    ("history", 0.02),
    ("pick", 0.12),
    ("order", 0.06),
    ("gauntlet", 0.06),
)
"""Each kind's share of the **Judgements**: submitted grids, **Skips**, **History** edits, **Line-ups**."""

TIMES: Final = (
    ("ranked", "`Batches.ranked`"),
    ("pool.members", "`pool.members`"),
    ("standing", "`evidence_log.standing` (the SQL standing and the newest)"),
    ("newest", "of which: the newest-position query, caught from `standing`"),
    ("near_duplicates", "`Embeddings.near_duplicates` (Pool x Bans)"),
    ("measure", "`Embeddings.measure` (Pool x decided)"),
    ("vectors_for", "of which: `EmbeddingCache.vectors_for`"),
    ("rows", "of which: the rows, `Embeddings.vectors` (Pool and decided) over a store already read"),
    ("rank", "`rank(standing.evidence())`, the matrix given"),
    ("fresh", "`Batches.fresh` (a visit's whole mint)"),
    ("write_lock", "of which: write lock held (pre-marking inside)"),
    ("explicitly_decided", "`evidence_log.explicitly_decided()`"),
    ("resolve_chosen", "`evidence_log.resolve(chosen tiles)` alone"),
    ("evict", "off the lock: `Thumbnails.evict` (a submission's tail), over the files counted below"),
    ("judgements_sql", "off the draw: `judgements()`'s ordered fetch, caught from it"),
)
"""Each timed figure's key and its line in the table: `ranked`, then each part of it, then the mint, then a
submission's eviction, then the ordered fetch `evaluate` makes through `judgements()`."""

COUNTS: Final = (
    ("judgement_items", "judgement_items rows"),
    ("wallpapers", "wallpapers rows"),
    ("decided_columns", "decided columns in the matrix"),
    ("explicit", "explicitly decided"),
    ("thumbnails", "cached thumbnails (each explicitly decided or Pool member)"),
    ("peak_rss_mb", "peak RSS of the timing process, MB"),
    ("digest", "ranking digest"),
)
"""What each size was, rather than how long it took: the same seed must give the same counts and digest."""


@dataclasses.dataclass(frozen=True, slots=True)
class Files:
    log: Path
    embeddings: Path
    thumbnails: Path


def files(out: Path, seed: int, size: int) -> Files:
    folder = out / f"seed-{seed}" / str(size)
    return Files(
        log=folder / "wallpapi.db", embeddings=folder / "embeddings.db", thumbnails=folder / "thumbnails"
    )


# -- the synthetic log ---------------------------------------------------------------------------------------


def _wallpaper_id(n: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    for _ in range(6):
        n, digit = divmod(n, 36)
        out = alphabet[digit] + out
    return out


def _wallpaper(n: int, rng: random.Random) -> Wallpaper:
    w = _wallpaper_id(n)
    colours = tuple(f"#{rng.randrange(0, 1 << 24):06x}" for _ in range(3))
    category = rng.choice(["general", "anime", "people"])
    return Wallpaper(
        id=w,
        width=3840,
        height=2160,
        ratio="1.78",
        category=category,
        purity="sfw",
        favourites=rng.randrange(0, 2000),
        colours=colours,
        thumbnail_url=f"https://th.wallhaven.cc/small/{w[:2]}/{w}.jpg",
        full_url=f"https://w.wallhaven.cc/full/{w[:2]}/wallhaven-{w}.jpg",
        page_url=f"https://wallhaven.cc/w/{w}",
    )


@dataclasses.dataclass(slots=True)
class _Log:
    """The log as it is drawn: its random source, every **Wallpaper** made, each id decided, each
    **Judgement**."""

    rng: random.Random
    created: list[Wallpaper] = dataclasses.field(default_factory=list[Wallpaper])
    decided: list[str] = dataclasses.field(default_factory=list[str])
    judgements: list[Judgement] = dataclasses.field(default_factory=list[Judgement])

    def fresh(self) -> Wallpaper:
        made = _wallpaper(len(self.created) + 1, self.rng)
        self.created.append(made)
        return made


def _judgement(
    instrument: Instrument,
    screen: str,
    shown: Sequence[Shown],
    response: JudgementResponse | None,
    at: dt.datetime,
    *,
    ranking: RankingStamp | None = None,
    shown_for: dt.timedelta | None = None,
) -> Judgement:
    return Judgement(
        instrument=instrument,
        screen=screen,
        shown=tuple(shown),
        recorded_at=at,
        app="wallpapi",
        app_version="0.1.0",
        response=response,
        utc_offset=dt.timedelta(0),
        first_shown_at=None if shown_for is None else at - shown_for,
        ranking=ranking,
        context=evidence_log.context(theme=Theme.DARK),
    )


def _grid_or_skip(
    log: _Log, members: Sequence[Wallpaper], kind: str, at: dt.datetime, stamp: RankingStamp
) -> Judgement:
    """A submitted grid of new **Wallpapers**, mostly left as **Ignores**, or a **Skip** of **Pool**
    members."""
    rng = log.rng
    submitted = kind == "grid"
    tiles = [log.fresh() for _ in range(BATCH)] if submitted else rng.sample(members, BATCH)
    shown: list[Shown] = []
    for tile in tiles:
        zone = Zone(rng.choice(["banger", "unknown", "dud"]))
        drawn = provenance(zone=zone, slot_zone=zone, mix="balanced")
        response = TileResponse()
        if submitted:
            r = rng.random()
            verdict = (
                Verdict.FAVOURITE
                if r < 0.03
                else Verdict.BAN
                if r < 0.06
                else Verdict.LIKE
                if r < 0.16
                else None
            )
            response = tile_response(verdict, preview_opened=rng.random() < 0.1)
            log.decided.append(tile.id)
        shown.append(Shown(item_key(tile.id), response, drawn))
    return _judgement(
        VERDICT_GRID_V1,
        "batch",
        shown,
        GridResponse(submitted=submitted),
        at,
        ranking=stamp,
        shown_for=dt.timedelta(seconds=30),
    )


def _history_edit(log: _Log, at: dt.datetime) -> Judgement:
    target = log.rng.choice(log.decided)
    verdict = log.rng.choice([Verdict.FAVOURITE, Verdict.LIKE, Verdict.IGNORE, Verdict.BAN])
    shown = (Shown(item_key(target), tile_response(verdict)),)
    return _judgement(VERDICT_GRID_V1, "history", shown, GridResponse(submitted=True), at)


def _lineup(
    log: _Log, members: Sequence[Wallpaper], kind: str, at: dt.datetime, stamp: RankingStamp
) -> Judgement:
    """A **Pick**, **Order** or **Gauntlet** of **Pool** members; now and then one is starred, a new one as
    if since retired from the **Pool**."""
    rng = log.rng
    tiles = rng.sample(members, LINEUP)
    starred = -1
    if rng.random() < 0.03:
        starred = rng.randrange(LINEUP)
        tiles[starred] = log.fresh()
        log.decided.append(tiles[starred].id)
    winner = rng.randrange(LINEUP)
    none = rng.random() < 0.1
    drawn = provenance(zone=None, slot_zone=None, mix="balanced")
    shown: list[Shown] = []
    for k, tile in enumerate(tiles):
        response: ItemResponse
        if kind == "pick":
            response = PickTile(picked=k == winner and not none, favourite=k == starred)
        elif kind == "gauntlet":
            response = GauntletTile(picked=k == winner and not none, favourite=k == starred)
        else:
            response = OrderTile(rank=None if k == LINEUP - 1 else k + 1, favourite=k == starred)
        shown.append(Shown(item_key(tile.id), response, drawn))
    instrument: Instrument
    answer: JudgementResponse | None
    if kind == "pick":
        instrument, answer = PICK_V1, PickResponse(none_of_these=none)
    elif kind == "gauntlet":
        instrument, answer = GAUNTLET_V1, GauntletResponse(streak=rng.randrange(0, 3), none_of_these=none)
    else:
        instrument, answer = ORDER_V1, None
    return _judgement(
        instrument, "compare", shown, answer, at, ranking=stamp, shown_for=dt.timedelta(seconds=10)
    )


def synthetic_log(size: int, rng: random.Random) -> tuple[_Log, list[Wallpaper]]:
    """`size` **Judgements** in `MIX`'s shares over a **Pool** of `POOL`, and the **Pool**. Each drawn
    screen's **Ranking stamp** reads the **Judgement** before it. The draws are the research's, call for
    call: seed 0 gives its logs."""
    log = _Log(rng)
    members = [log.fresh() for _ in range(POOL)]
    kinds = [kind for kind, _ in MIX]
    weights = [weight for _, weight in MIX]
    at = START
    for _ in range(size):
        at += dt.timedelta(seconds=rng.randrange(20, 400), microseconds=rng.randrange(0, 999_999))
        kind = rng.choices(kinds, weights)[0]
        if kind == "history" and not log.decided:
            kind = "grid"
        newest = log.judgements[-1] if log.judgements else None
        stamp = RankingStamp(
            read=None if newest is None else LogPosition(newest.recorded_at, newest.id),
            space="clip:ViT-B-32/openai",
            radius=0.1,
            decay=4.0,
        )
        if kind in ("grid", "skip"):
            judgement = _grid_or_skip(log, members, kind, at, stamp)
        elif kind == "history":
            judgement = _history_edit(log, at)
        else:
            judgement = _lineup(log, members, kind, at, stamp)
        log.judgements.append(judgement)
    return log, members


def build(out: Path, seed: int, size: int) -> dict[str, int]:
    """Write the size's two databases afresh: the log, every **Wallpaper** it names, the **Pool**, and an
    **Embedding** for each **Wallpaper**."""
    where = files(out, seed, size)
    where.log.parent.mkdir(parents=True, exist_ok=True)
    for path in (where.log, where.embeddings):
        for suffix in ("", "-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)
    log, members = synthetic_log(size, random.Random(seed + size))

    connection = storage.connect(where.log)
    storage.migrate(connection)
    # Every Wallpaper arrives in the Pool, as the refill admits it, and leaves it once decided (ADR 0016).
    # Filters that pass everything, so none is dropped for its random favourites count.
    admitting = settings.Settings(
        {**vars(settings.get(connection)), "min_width": 0, "min_height": 0, "min_favourites": 0}
    )
    member_ids = {w.id for w in members}
    with storage.write(connection) as write:
        pool.admit(write, log.created, admitting, source=RefillStrategy.RANDOM, at=START)
        pool.retire(write, [w.id for w in log.created if w.id not in member_ids])
        for judgement in log.judgements:
            evidence_log.append(write, judgement)
    write_thumbnails(where.thumbnails, evidence_log.explicitly_decided(connection) | member_ids)
    connection.close()

    # `EmbeddingCache.store` commits one vector at a time; the rows go in its own format in one transaction.
    cache = EmbeddingCache(where.embeddings)
    cache.embedded_ids()  # creates the file and its schema
    vectors = np.random.default_rng(seed + size)
    connection = storage.connect(where.embeddings)
    with storage.write(connection) as write:
        for start in range(0, len(log.created), EMBED_CHUNK):
            chunk = log.created[start : start + EMBED_CHUNK]
            block = vectors.standard_normal((len(chunk), DIM)).astype(np.float32)
            block /= np.linalg.norm(block, axis=1, keepdims=True)
            write.executemany(
                "INSERT INTO embeddings (wallpaper_id, dim, vector) VALUES (?, ?, ?)",
                [(w.id, DIM, row.astype("<f4").tobytes()) for w, row in zip(chunk, block, strict=True)],
            )
    connection.close()
    return {"wallpapers": len(log.created), "decided": len(set(log.decided))}


def write_thumbnails(directory: Path, wallpaper_ids: Iterable[str]) -> None:
    """The **Thumbnail cache** afresh, one empty file per id: what a submission's eviction leaves, every
    **Explicit Verdict** and the **Pool**. Empty, because eviction stats each file whatever its size, and a
    cache over the cap would let the size pass delete **Pool** members between runs."""
    directory.mkdir(parents=True, exist_ok=True)
    for stale in directory.iterdir():
        stale.unlink()
    for wallpaper_id in wallpaper_ids:
        (directory / f"{wallpaper_id}.jpg").touch()


# -- timing ---------------------------------------------------------------------------------------------


class _NoModel:
    """A model that is always on disk, so the upkeep opens the **Similarity provider** without one."""

    def ensure(self, stop_event: threading.Event) -> Path:
        return Path("no-model")


class _NoWallhaven:
    """Wallhaven for a **Thumbnail cache** that is only evicted: anything it would fetch is refused."""

    def search(
        self,
        *,
        sorting: str,
        purity: str,
        categories: str | None = None,
        query: str | None = None,
        page: int = 1,
        seed: str | None = None,
        atleast: str | None = None,
        ratios: str | None = None,
    ) -> SearchPage:
        raise RuntimeError("the benchmark searches nothing")

    def fetch_thumbnail(self, url: str) -> bytes:
        raise RuntimeError("the benchmark fetches nothing: its thumbnails are written already")


def _no_embed(images: Sequence[Path]) -> NDArray[np.float32]:
    """Opens on the empty call the upkeep makes, and refuses anything to embed."""
    if images:
        raise RuntimeError("the benchmark embeds nothing: its Embeddings are stored already")
    return np.zeros((0, DIM), dtype=np.float32)


class _AlreadyRead:
    """A store that hands back, for any ids, the vectors one `EmbeddingCache.vectors_for` already read: what
    `Embeddings` does with them is then timed without the read."""

    def __init__(self, held: Mapping[str, NDArray[np.float32]]) -> None:
        self._held = held

    def vectors_for(self, wallpaper_ids: Sequence[str]) -> Mapping[str, NDArray[np.float32]]:
        return self._held

    def store(self, wallpaper_id: str, vector: NDArray[np.float32]) -> None:
        raise RuntimeError("the benchmark stores nothing")

    def embedded_ids(self) -> set[str]:
        return set(self._held)


def _median_ms[T](run: Callable[[], T], reps: int) -> tuple[float, T]:
    """The median of `reps` runs after one warm-up, in milliseconds, and the last run's answer."""
    answer = run()
    samples: list[float] = []
    for _ in range(reps):
        started = time.perf_counter()
        answer = run()
        samples.append(time.perf_counter() - started)
    return statistics.median(samples) * 1000, answer


@contextmanager
def chunked_resolve() -> Generator[None]:
    """`evidence_log.resolve` sending at most `CHUNK` ids a statement, for as long as the block runs.

    Chunking cannot change a standing: resolution partitions by **Item**.
    """
    original = evidence_log.resolve

    def chunked(connection: sqlite3.Connection, wallpaper_ids: Sequence[str]) -> dict[str, ResolvedVerdict]:
        requested = list(dict.fromkeys(wallpaper_ids))
        resolved: dict[str, ResolvedVerdict] = {}
        for start in range(0, len(requested), CHUNK):
            resolved.update(original(connection, requested[start : start + CHUNK]))
        return resolved

    with patch.object(evidence_log, "resolve", chunked):
        yield


def _too_many_variables(failure: sqlite3.OperationalError) -> bool:
    return "too many SQL variables" in str(failure)


def _as_is_or_chunked[T](run: Callable[[], T], reps: int, key: str, chunked: list[str]) -> tuple[float, T]:
    """`_median_ms` of `run` as is, or, if it raises over the parameter limit, with `resolve` chunked and
    `key` noted in `chunked`."""
    try:
        return _median_ms(run, reps)
    except sqlite3.OperationalError as failure:
        if not _too_many_variables(failure):
            raise
    chunked.append(key)
    with chunked_resolve():
        return _median_ms(run, reps)


@contextmanager
def timed_writes(held: list[float]) -> Generator[None]:
    """Every `storage.write` in the block, timed from `BEGIN IMMEDIATE` to its end, into `held`."""
    original = storage.write

    @contextmanager
    def timed(connection: sqlite3.Connection) -> Generator[sqlite3.Connection]:
        started = time.perf_counter()
        try:
            with original(connection) as write:
                yield write
        finally:
            held.append(time.perf_counter() - started)

    with patch.object(storage, "write", timed):
        yield


ORDERED_READ: Final = "FROM judgements ORDER BY recorded_at"
"""What the one statement of a call that reads `judgements` in recorded-time order says."""


def _ordered_read(connection: sqlite3.Connection, run: Callable[[], object]) -> str:
    """The one statement `run` sends that reads `judgements` by recorded time, caught as it runs, so the SQL
    a public call makes is timed alone without naming a private one. Raises unless exactly one says
    `ORDERED_READ`: a change to the SQL fails the benchmark rather than timing another statement."""
    caught: list[str] = []
    connection.set_trace_callback(caught.append)
    try:
        run()
    finally:
        connection.set_trace_callback(None)
    matched = [statement for statement in caught if ORDERED_READ in statement]
    if len(matched) != 1:
        raise RuntimeError(f"expected one statement saying {ORDERED_READ!r}, caught {matched}")
    return matched[0]


def _digest(batches: Batches, connection: sqlite3.Connection) -> str:
    """The candidates, each **Score** to the bit and the **Ranking stamp** of one `ranked` call, hashed: two
    runs on one seed that draw alike give the same."""
    ranked = batches.ranked(connection)
    lines = [f"{s.wallpaper.id} {s.score!r}" for s in ranked.scored]
    lines.append("" if ranked.stamp is None else evidence_log.stamp_text(ranked.stamp))
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]


def _peak_rss_mb() -> int | None:
    """This process's peak resident memory, where the platform says it; Windows has no `resource`."""
    if sys.platform == "win32":
        return None
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024 * 1024 if sys.platform == "darwin" else 1024))


class SizeRun(TypedDict):
    """One size's figures: `times` in milliseconds by `TIMES` key, `counts` by `COUNTS` key, and the keys of
    the times taken with `resolve` chunked."""

    times: dict[str, float]
    counts: dict[str, int | str | None]
    chunked: list[str]


class Report(TypedDict):
    """A run, as `--json` keeps it and `--baseline` reads it back."""

    commit: str | None
    machine: dict[str, str]
    seed: int
    reps: int
    out: str
    sizes: dict[str, SizeRun]


def measure(out: Path, seed: int, size: int, reps: int) -> SizeRun:
    """Time the draw and its parts on the size's databases, which `build` wrote."""
    where = files(out, seed, size)
    connection = storage.connect(where.log)
    cache = EmbeddingCache(where.embeddings)
    embeddings = Embeddings(cache, _no_embed, _NoModel())
    embeddings.catch_up(where.log.parent / "no-thumbnails", threading.Event())
    notice = embeddings.notice(pool.members(connection))
    if notice is not None:
        raise RuntimeError(f"the Similarity provider is not in its steady state: {notice}")
    status = RefillStatus(
        pool_size=POOL,
        target_size=POOL,
        running=False,
        last_run_at=None,
        last_error=None,
        last_error_at=None,
        last_strategy=None,
        by_strategy={},
    )
    batches = Batches(embeddings, lambda: status, SystemClock(), SeededRandom(seed))
    t: dict[str, float] = {}
    chunked: list[str] = []
    counts: dict[str, int | str | None] = {
        name: int(connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
        for name in ("judgements", "judgement_items", "wallpapers")
    }

    t["pool.members"], members = _median_ms(lambda: pool.members(connection), reps)
    t["standing"], standing = _median_ms(
        lambda: evidence_log.standing(connection, pool.wallpaper_from_row), reps
    )
    newest = _ordered_read(connection, lambda: evidence_log.standing(connection, pool.wallpaper_from_row))
    t["newest"], _ = _median_ms(lambda: connection.execute(newest).fetchone(), reps)
    decided = sorted(piece.item for piece in standing.evidence())
    counts["decided_columns"] = len(decided)

    banned = [d.wallpaper for d in standing.decided if d.resolved.verdict is Verdict.BAN]
    banned_ids = {w.id for w in banned}
    candidates = [w for w in members if w.id not in banned_ids]
    t["near_duplicates"], _ = _median_ms(lambda: embeddings.near_duplicates(candidates, banned), reps)
    by_key = {item_key(w.id): w for w in [*candidates, *(d.wallpaper for d in standing.decided)]}
    columns = [by_key[key] for key in decided]
    t["measure"], measured = _median_ms(lambda: embeddings.measure(candidates, columns), reps)
    t["vectors_for"], read = _median_ms(
        lambda: cache.vectors_for([w.id for w in candidates] + [w.id for w in columns]), reps
    )
    # `vectors` reaches the rows `measure` multiplies through a public call, with no matmul after it; over a
    # store already read, it times building them alone.
    already_read = Embeddings(_AlreadyRead(read), _no_embed, _NoModel())
    t["rows"], _ = _median_ms(lambda: already_read.vectors([*candidates, *columns]), reps)
    current = settings.get(connection)
    keys = [item_key(w.id) for w in candidates]
    t["rank"], _ = _median_ms(
        lambda: rank(
            standing.evidence(),
            keys,
            lambda rows, cols: measured.matrix,
            radius=current.similarity_radius,
            decay=current.similarity_decay,
        ),
        reps,
    )

    # The whole draw after its parts, where the research timed it: timed first, in a process that has not
    # yet held the parts' results, it measured 5 to 10% faster at 10k.
    t["ranked"], _ = _as_is_or_chunked(lambda: batches.ranked(connection), reps, "ranked", chunked)

    # A visit's whole mint, and how long it holds the write lock: pre-marking runs inside it.
    held: list[float] = []
    with timed_writes(held):
        t["fresh"], _ = _as_is_or_chunked(lambda: batches.fresh(connection), reps, "fresh", chunked)
    # One write per mint, the last `reps` of them timed, as is or chunked; a failed one never wrote.
    t["write_lock"] = statistics.median(held[-reps:]) * 1000
    if "fresh" in chunked:
        chunked.append("write_lock")
    t["explicitly_decided"], explicit = _median_ms(lambda: evidence_log.explicitly_decided(connection), reps)
    counts["explicit"] = len(explicit)
    chosen = [w.id for w in members[:BATCH]]
    t["resolve_chosen"], _ = _median_ms(lambda: evidence_log.resolve(connection, chosen), reps)
    # After the mint, so the live Batch is there to keep; every file is kept, so each run is the same pass.
    thumbnail_cache = Thumbnails(where.thumbnails, _NoWallhaven(), SystemClock())
    cap = thumbnails.cap_bytes(settings.get(connection))
    written = sum(1 for _ in where.thumbnails.iterdir())
    t["evict"], _ = _median_ms(lambda: thumbnail_cache.evict(connection, cap), reps)
    counts["thumbnails"] = sum(1 for _ in where.thumbnails.iterdir())
    if counts["thumbnails"] != written:
        raise RuntimeError(
            f"eviction is not in its steady state: {written} files became {counts['thumbnails']}"
        )
    fetch = _ordered_read(connection, lambda: evidence_log.judgements(connection))
    t["judgements_sql"], _ = _median_ms(lambda: connection.execute(fetch).fetchall(), reps)

    with chunked_resolve():
        counts["digest"] = _digest(batches, connection)
    counts["peak_rss_mb"] = _peak_rss_mb()
    connection.close()
    return SizeRun(times=t, counts=counts, chunked=chunked)


# -- the run -------------------------------------------------------------------------------------------


def _in_own_process[**P, R](run: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """`run` in a new interpreter, spawned on every platform, so no size inherits another's memory."""
    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as worker:
        return worker.submit(run, *args, **kwargs).result()


def _commit() -> str | None:
    try:
        described = subprocess.run(
            ["git", "describe", "--always", "--dirty"], cwd=REPO, capture_output=True, text=True, check=True
        )
    except OSError, subprocess.CalledProcessError:
        return None
    return described.stdout.strip()


def _machine() -> dict[str, str]:
    return {
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "numpy": np.__version__,
    }


def _ms(value: float) -> str:
    if value >= 10:
        return f"{value:,.0f}"
    if value >= 1:
        return f"{value:.1f}"
    return f"{value:.2f}"


def _cell(run: SizeRun | None, key: str, *, timed: bool) -> str:
    if run is None:
        return "-"
    if timed:
        value = run["times"].get(key)
        return "-" if value is None else _ms(value) + ("*" if key in run["chunked"] else "")
    count = run["counts"].get(key)
    if count is None:
        return "-"
    return f"{count:,}" if isinstance(count, int) else count


def table(report: Report, baseline: Report | None = None) -> str:
    """The run as a Markdown table, one column per size; with a baseline, each cell `before -> now`. A
    figure shown starred, in either half, brings the footnote saying what `*` means."""
    sizes = list(report["sizes"])
    lines = [
        "| Judgements | " + " | ".join(f"{int(size):,}" for size in sizes) + " |",
        "|---|" + "---:|" * len(sizes),
    ]
    starred = False
    rows = [(key, label, True) for key, label in TIMES] + [(key, label, False) for key, label in COUNTS]
    for key, label, timed in rows:
        cells: list[str] = []
        for size in sizes:
            now = _cell(report["sizes"][size], key, timed=timed)
            if baseline is not None:
                now = f"{_cell(baseline['sizes'].get(size), key, timed=timed)} -> {now}"
            starred = starred or (timed and "*" in now)
            cells.append(now)
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    if starred:
        lines += [
            "",
            "* Failed as is with 'too many SQL variables': timed with evidence_log.resolve chunked at "
            f"{CHUNK:,}.",
        ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--sizes", type=int, nargs="+", default=[1_000, 10_000], help="Judgements per log")
    parser.add_argument("--reps", type=int, default=5, help="timed runs per figure, after one warm-up")
    parser.add_argument("--seed", type=int, default=0, help="each size's log is drawn from seed + size")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(tempfile.gettempdir()) / "wallpapi-bench-draw",
        help="where the databases are built, outside the repo",
    )
    parser.add_argument("--json", type=Path, help="keep this run here, to set beside a later one")
    parser.add_argument("--baseline", type=Path, help="a run kept with --json, shown as 'before -> now'")
    args = parser.parse_args(argv)
    out: Path = args.out.resolve()
    if out.is_relative_to(REPO):
        parser.error(f"--out must be outside the repo, not {out}")
    if args.reps < 1:
        parser.error("--reps must be at least 1")
    baseline: Report | None = None
    if args.baseline is not None:
        baseline = cast(Report, json.loads(args.baseline.read_text(encoding="utf-8")))
        if baseline["seed"] != args.seed:
            parser.error(f"the baseline was run with seed {baseline['seed']}, not {args.seed}")

    sizes: dict[str, SizeRun] = {}
    for size in args.sizes:
        print(f"{size:,} Judgements: building", file=sys.stderr, flush=True)
        built = _in_own_process(build, out, args.seed, size)
        print(f"{size:,} Judgements: timing {built}", file=sys.stderr, flush=True)
        sizes[str(size)] = _in_own_process(measure, out, args.seed, size, args.reps)
    report = Report(
        commit=_commit(), machine=_machine(), seed=args.seed, reps=args.reps, out=str(out), sizes=sizes
    )

    print(f"Commit {report['commit']}, seed {args.seed}, median of {args.reps} after one warm-up, in ms.")
    if baseline is not None:
        print(f"Before -> now: before is {args.baseline}, commit {baseline['commit']}.")
    print()
    print(table(report, baseline))
    if args.json is not None:
        args.json.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
        print(f"Kept in {args.json}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
