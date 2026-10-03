"""A **Similarity provider** that reads Wallhaven's tags.

Tags come only from `GET /api/v1/w/{id}`, one **API call** per **Wallpaper** out of the refill's 45 a minute,
so fetching is a step somebody runs on purpose, never wired into the refill. A pair where both sides have tags
scores `TAG_SHARE * jaccard + (1 - TAG_SHARE) * baseline`; any other pair scores the baseline alone, so an
untagged **Wallpaper** is not called unlike everything. Jaccard, not cosine, because it notices a size
mismatch between tag sets.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from wallpapi.model import Wallpaper
from wallpapi.similarity import NOTHING_TO_CATCH_UP, MetadataSimilarityProvider, SimilarityProvider
from wallpapi.similarity_cache import SidecarDatabase
from wallpapi.wallhaven import Tag

TAG_SHARE = 0.6
"""How much the tags are worth when both sides have them, the baseline taking the rest. Not tuned against
anything.
"""


class TagSource(Protocol):
    """Where the provider gets tag sets from, plural so there is one call per matrix and never a loop
    (invariant 2).
    """

    def tags_for(self, wallpaper_ids: Sequence[str]) -> Mapping[str, tuple[int, ...]]:
        """The tag ids of each **Wallpaper** that has any, keyed by id; one never fetched or with no tags is
        absent.
        """
        ...


_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS fetched (
        wallpaper_id TEXT PRIMARY KEY,
        fetched_at   TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS wallpaper_tags (
        wallpaper_id TEXT    NOT NULL,
        tag_id       INTEGER NOT NULL,
        tag_name     TEXT    NOT NULL,
        PRIMARY KEY (wallpaper_id, tag_id)
    )
    """,
)


class TagCache:
    """Wallhaven's tags, kept for good in a SQLite file of the provider's own: a re-fetch costs one of 45
    calls a minute.

    `fetched` is a table of its own so a **Wallpaper** with no tags is not asked about again on every run.
    """

    def __init__(self, path: Path) -> None:
        self._db = SidecarDatabase(path, _SCHEMA)

    @property
    def path(self) -> Path:
        return self._db.path

    def size_bytes(self) -> int:
        return self._db.size_bytes()

    def tags_for(self, wallpaper_ids: Sequence[str]) -> Mapping[str, tuple[int, ...]]:
        """Every tag id held for the named **Wallpapers**, in chunked queries: SQLite's parameter limit is
        32,766.
        """
        requested = list(dict.fromkeys(wallpaper_ids))
        found: dict[str, list[int]] = {}
        connection = self._db.connect()
        for chunk in _chunked(requested, 10_000):
            placeholders = ",".join("?" * len(chunk))
            rows = connection.execute(
                f"SELECT wallpaper_id, tag_id FROM wallpaper_tags WHERE wallpaper_id IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                found.setdefault(str(row["wallpaper_id"]), []).append(int(row["tag_id"]))
        return {wallpaper_id: tuple(tags) for wallpaper_id, tags in found.items()}

    def store(self, wallpaper_id: str, tags: Iterable[Tag], *, fetched_at: dt.datetime) -> None:
        """Record one **Wallpaper**'s tags, replacing what was held; the `fetched` row is written even with no
        tags.
        """
        with self._db.write() as write:
            write.execute("DELETE FROM wallpaper_tags WHERE wallpaper_id = ?", (wallpaper_id,))
            write.executemany(
                "INSERT OR REPLACE INTO wallpaper_tags (wallpaper_id, tag_id, tag_name) VALUES (?, ?, ?)",
                [(wallpaper_id, tag.id, tag.name) for tag in tags],
            )
            write.execute(
                "INSERT OR REPLACE INTO fetched (wallpaper_id, fetched_at) VALUES (?, ?)",
                (wallpaper_id, fetched_at.isoformat()),
            )

    def fetched_ids(self) -> set[str]:
        """Every **Wallpaper** already asked about."""
        rows = self._db.connect().execute("SELECT wallpaper_id FROM fetched").fetchall()
        return {str(row["wallpaper_id"]) for row in rows}

    def names_for(self, wallpaper_id: str) -> tuple[str, ...]:
        """One **Wallpaper**'s tag names, for reading by eye rather than for the maths."""
        rows = self._db.connect().execute(
            "SELECT tag_name FROM wallpaper_tags WHERE wallpaper_id = ? ORDER BY tag_name", (wallpaper_id,)
        )
        return tuple(str(row["tag_name"]) for row in rows)


class TagSimilarityProvider:
    """Tag overlap blended with the baseline.

    The indicator matrix has columns only for the decided set's tags: a tag no decided **Wallpaper** carries
    adds nothing to an intersection, and the union needs only each side's own count. One matmul, no loop over
    pairs.
    """

    def __init__(
        self,
        tags: TagSource,
        *,
        baseline: SimilarityProvider | None = None,
        tag_share: float = TAG_SHARE,
    ) -> None:
        self._tags = tags
        self._baseline = MetadataSimilarityProvider() if baseline is None else baseline
        self._tag_share = tag_share

    def similarities(self, pool: Sequence[Wallpaper], decided: Sequence[Wallpaper]) -> NDArray[np.float32]:
        baseline = self._baseline.similarities(pool, decided)
        if not pool or not decided:
            return baseline

        held = self._tags.tags_for([w.id for w in pool] + [w.id for w in decided])
        pool_tags = [held.get(w.id, ()) for w in pool]
        decided_tags = [held.get(w.id, ()) for w in decided]

        distinct = dict.fromkeys(tag for tags in decided_tags for tag in tags)
        columns = {tag: index for index, tag in enumerate(distinct)}
        if not columns:
            return baseline

        intersection = _indicator(pool_tags, columns) @ _indicator(decided_tags, columns).T
        pool_counts = _counts(pool_tags)
        decided_counts = _counts(decided_tags)
        union = pool_counts[:, None] + decided_counts[None, :] - intersection
        # A pair with no tags has a union of zero and an undefined Jaccard; the `both` mask below decides it.
        jaccard = np.where(union > 0.0, intersection / np.where(union > 0.0, union, 1.0), 0.0)

        both = (pool_counts > 0.0)[:, None] & (decided_counts > 0.0)[None, :]
        blended = np.where(both, self._tag_share * jaccard + (1.0 - self._tag_share) * baseline, baseline)
        # Clipped for the baseline's reason.
        return np.clip(blended, 0.0, 1.0).astype(np.float32)

    def catch_up(self, thumbnails: Path, stop_event: threading.Event) -> float:
        """Nothing: filling the cache is one **API call** per **Wallpaper**, and a provider may not quietly
        spend the refill's budget.
        """
        del thumbnails, stop_event
        return NOTHING_TO_CATCH_UP

    def notice(self, pool: Sequence[Wallpaper]) -> str | None:
        """Nothing: an untagged **Wallpaper** falls back to the baseline, and nothing in the app moves a
        coverage count.
        """
        del pool
        return None

    def vectors(self, pool: Sequence[Wallpaper]) -> NDArray[np.float32] | None:
        """None: a tag set is not a position."""
        del pool
        return None


def _indicator(tag_sets: Sequence[tuple[int, ...]], columns: Mapping[int, int]) -> NDArray[np.float32]:
    """The `len(tag_sets)` x `len(columns)` matrix with 1.0 wherever a **Wallpaper** carries that tag."""
    matrix = np.zeros((len(tag_sets), len(columns)), dtype=np.float32)
    for row, tags in enumerate(tag_sets):
        for tag in tags:
            column = columns.get(tag)
            if column is not None:
                matrix[row, column] = 1.0
    return matrix


def _counts(tag_sets: Sequence[tuple[int, ...]]) -> NDArray[np.float32]:
    """How many distinct tags each **Wallpaper** carries."""
    return np.array([len(set(tags)) for tags in tag_sets], dtype=np.float32)


def _chunked(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
