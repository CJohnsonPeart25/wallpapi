"""#14's first candidate: a **Similarity provider** that reads Wallhaven's tags.

A tag is Wallhaven's own label on a **Wallpaper** — `landscape`, `mountains`, `long exposure` — applied by
its users and identified by an integer. Two **Wallpapers** that share most of their tags are about the same
thing, which is exactly what the baseline provider's colour histogram cannot see: a snowy peak and a
bedsheet share a palette and nothing else.

**What it costs.** Tags come only from the single-**Wallpaper** endpoint, `GET /api/v1/w/{id}`, one call
per **Wallpaper**, and that endpoint is on `wallhaven.cc/api` — the 45-per-minute budget the **Pool**
refill is already spending. So a **Pool** of 2,000 **Wallpapers** is 2,000 **API calls**, about 45 minutes
of the budget at full tilt, and a **Pool** of 10,000 is nearly four hours. That is the single fact the
spike turns on, and it is why fetching is a step somebody runs on purpose rather than something wired into
the refill thread, where it would compete with the searches that stock the **Pool**.

**The formula.** Tag similarity is the Jaccard index of the two tag-id sets — shared tags over total
distinct tags — blended with the baseline's colour-and-category number:

    similarity = TAG_SHARE * jaccard + (1 - TAG_SHARE) * baseline

for a pair where both sides have tags, and the baseline alone for a pair where either side does not. The
fallback is the whole reason this stays usable: a **Pool** fills faster than 45 calls a minute can tag it,
so at any moment most of it is untagged, and a provider that answered 0.0 for an untagged **Wallpaper**
would call it unlike everything — including unlike the **Bans** — and quietly promote it.

Jaccard rather than cosine over the same sets. Cosine divides by the geometric mean of the two set sizes
and so barely notices that one **Wallpaper** has four tags and the other forty; Jaccard divides by the
union and does. A **Wallpaper** tagged `nature` alone is not 70% the same as one tagged `nature` plus
nineteen other things, and Jaccard is the one that says so.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from wallpapi.model import Wallpaper
from wallpapi.similarity import MetadataSimilarityProvider, SimilarityProvider
from wallpapi.similarity_cache import SidecarDatabase
from wallpapi.wallhaven import Tag

TAG_SHARE = 0.6
"""How much of the similarity the tags are worth when both sides have them, the baseline taking the rest.

Six-tenths rather than all of it. The tags are the better evidence — they are about subject, which is what
the baseline is blind to — but they are sparse, crowd-sourced and uneven: a **Wallpaper** can carry two
tags or thirty, and two photographs of the same mountain can share none of them if different people tagged
them. Keeping four-tenths on colour and category means a pair the tags cannot tell apart still gets an
answer from the evidence that is always present, and it keeps the number continuous rather than collapsing
to a handful of Jaccard values.

Not tuned against anything. Like the **Similarity radius** and **Similarity decay** in ADR 0007, this is a
starting point, and #14's comparison says what it is worth.
"""


class TagSource(Protocol):
    """Where the provider gets tag sets from.

    Plural, for invariant 2's reason: one call per matrix, never one per **Wallpaper**. A per-**Wallpaper**
    lookup here would be a Python loop over the whole **Pool** on a path that already has to be one pass.
    """

    def tags_for(self, wallpaper_ids: Sequence[str]) -> Mapping[str, tuple[int, ...]]:
        """The tag ids of each **Wallpaper** that has any, keyed by **Wallpaper** id.

        A **Wallpaper** that has never been fetched and one Wallhaven returned no tags for are both simply
        absent: the provider treats them the same way, by falling back to the baseline.
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
    """Wallhaven's tags for a **Wallpaper**, kept for good in a SQLite file of the provider's own.

    Permanent because a **Wallpaper**'s tags barely change and a re-fetch costs one of 45 calls a minute.
    Regenerable because every one of them can be asked for again — which is what makes it a cache rather
    than a second **Decision log**, and what lets it live outside `wallpapi.db` with no migration.

    `fetched` is a separate table from `wallpaper_tags` on purpose. Without it, a **Wallpaper** Wallhaven
    returned no tags for is indistinguishable from one nobody has asked about, and the fill step would ask
    about it again on every run for ever.
    """

    def __init__(self, path: Path) -> None:
        self._db = SidecarDatabase(path, _SCHEMA)

    @property
    def path(self) -> Path:
        return self._db.path

    def size_bytes(self) -> int:
        return self._db.size_bytes()

    def tags_for(self, wallpaper_ids: Sequence[str]) -> Mapping[str, tuple[int, ...]]:
        """Every tag id held for the named **Wallpapers**, in one query rather than one per **Wallpaper**.

        Chunked because SQLite's parameter limit is 32,766 and a **Pool** plus its decided set can pass it:
        20,000 **Pool** members is already most of the way there.
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
        """Record one **Wallpaper**'s tags, replacing whatever was held for it.

        Replacing rather than adding, so a re-fetch of a **Wallpaper** whose tags were edited on Wallhaven
        leaves the cache agreeing with Wallhaven rather than with the union of both readings. The
        `fetched` row is written whether or not there were any tags — that is the point of it.
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
        """Every **Wallpaper** already asked about, so a fill step costs nothing for the second run."""
        rows = self._db.connect().execute("SELECT wallpaper_id FROM fetched").fetchall()
        return {str(row["wallpaper_id"]) for row in rows}

    def names_for(self, wallpaper_id: str) -> tuple[str, ...]:
        """One **Wallpaper**'s tag names, for reading a spike result rather than for the maths."""
        rows = self._db.connect().execute(
            "SELECT tag_name FROM wallpaper_tags WHERE wallpaper_id = ? ORDER BY tag_name", (wallpaper_id,)
        )
        return tuple(str(row["tag_name"]) for row in rows)


class TagSimilarityProvider:
    """Tag overlap blended with the baseline, behind the same protocol as everything else.

    **Vectorised, and only over the columns that can matter.** The obvious spelling is an indicator matrix
    over every distinct tag in the **Pool** and the decided set together, which for 20,000 **Wallpapers**
    and 20,000 distinct tags is 1.6GB of float32 for a matrix that is almost entirely zeros. It is not
    needed: a tag no decided **Wallpaper** carries contributes nothing to any intersection, and the union
    needs only each side's own tag count, which is one number per **Wallpaper**. So the columns are the
    tags of the decided set alone — a few hundred for a realistic **Decision log** — and the intersection
    is one matmul. The only Python iteration is over each **Wallpaper**'s own tag list while the indicators
    are built, which is linear in the **Pool** and not quadratic in it, exactly as the baseline's
    histograms are.
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
        # Guarded rather than patched up afterwards: a pair with no tags on either side has a union of
        # zero, and its Jaccard index is not 0.0 but undefined — which is why the `both` mask below, and
        # not this division, is what decides that such a pair falls back to the baseline.
        jaccard = np.where(union > 0.0, intersection / np.where(union > 0.0, union, 1.0), 0.0)

        both = (pool_counts > 0.0)[:, None] & (decided_counts > 0.0)[None, :]
        blended = np.where(both, self._tag_share * jaccard + (1.0 - self._tag_share) * baseline, baseline)
        # Clipped for the baseline's reason: a **Wallpaper** against itself must come out at 1.0 and never
        # a rounding above it, or the **Score** maths' `1 - similarity` distance goes negative.
        return np.clip(blended, 0.0, 1.0).astype(np.float32)


def _indicator(tag_sets: Sequence[tuple[int, ...]], columns: Mapping[int, int]) -> NDArray[np.float32]:
    """The `len(tag_sets)` x `len(columns)` matrix with a 1.0 wherever a **Wallpaper** carries that tag."""
    matrix = np.zeros((len(tag_sets), len(columns)), dtype=np.float32)
    for row, tags in enumerate(tag_sets):
        for tag in tags:
            column = columns.get(tag)
            if column is not None:
                matrix[row, column] = 1.0
    return matrix


def _counts(tag_sets: Sequence[tuple[int, ...]]) -> NDArray[np.float32]:
    """How many distinct tags each **Wallpaper** carries — the union's other half."""
    return np.array([len(set(tags)) for tags in tag_sets], dtype=np.float32)


def _chunked(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
