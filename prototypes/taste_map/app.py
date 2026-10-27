"""PROTOTYPE, throwaway: the taste map (wallpapi-5tu), a look at how real **Verdicts** group into a
**Distillation**'s prototypes as the merge distance changes (method: wallpapi-wa5.10).

Reads only the snapshot copies `snapshot.py` makes; thumbnails are served read-only from the live install.
Every merge distance on the slider is grouped and laid out once at startup, and the page gets them all in
one JSON, so the slider never waits on the network.

    uv run python prototypes/taste_map/snapshot.py
    uv run uvicorn prototypes.taste_map.app:app --host 0.0.0.0 --port 8765
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from numpy.typing import NDArray

from wallpapi import decisions, pool, storage
from wallpapi.model import Wallpaper
from wallpapi.similarity import EmbeddingCache, Embeddings

HERE = Path(__file__).resolve().parent
SNAPSHOT = Path(os.environ.get("TASTE_MAP_SNAPSHOT", HERE / "snapshot"))
THUMBNAILS = Path(os.environ.get("TASTE_MAP_THUMBNAILS", "/mnt/c/Users/Cameron/.wallpapi/thumbnails"))

STEP = 0.005
LEVELS = [round(i * STEP, 3) for i in range(41)]  # 0 to 0.20
DEFAULT_MERGE_DISTANCE = 0.05
NEIGHBOURS = 2  # web edges: each prototype to its nearest same-sign prototypes


@dataclass
class Decided:
    """The embedded decided **Wallpapers** in log order, their values and their distance matrix."""

    wallpapers: list[Wallpaper]
    verdicts: list[str]
    values: NDArray[np.float64]
    distance: NDArray[np.float64]
    plane: NDArray[np.float64]  # 2D PCA of the embeddings
    decided_total: int
    no_embedding: int
    cleared: int


class _NoModel:
    """Nothing here embeds: the snapshot's stored vectors are all there is."""

    def ensure(self, stop_event: object) -> Path:
        raise RuntimeError("the taste map never fetches the model")


def _no_embed(images: Sequence[Path]) -> NDArray[np.float32]:
    raise RuntimeError("the taste map never embeds")


def _no_fallback(a: Sequence[Wallpaper], b: Sequence[Wallpaper]) -> NDArray[np.float32]:
    return np.zeros((len(a), len(b)), dtype=np.float32)


def load() -> Decided:
    connection = storage.connect(SNAPSHOT / "wallpapi.db")
    # Log order: each Wallpaper at the sequence of its deciding (latest) entry, as History orders.
    latest: dict[str, int] = {}
    for entry in decisions.entries(connection):
        latest[entry.wallpaper_id] = entry.seq
    ordered = sorted(latest, key=latest.__getitem__)
    resolved = decisions.resolve(connection, ordered)
    standing = [wid for wid in ordered if resolved[wid].verdict is not None]
    cache = EmbeddingCache(SNAPSHOT / "embeddings.db")
    vectors = cache.vectors_for(standing)
    embedded = [wid for wid in standing if wid in vectors]
    by_id = pool.wallpapers(connection, embedded)
    wallpapers = [by_id[wid] for wid in embedded]
    # The same mapped similarity the Score uses, (1 + cos) / 2, so distance = 1 - mapped = (1 - cos) / 2.
    # Decided x decided is fine for a look at ~800; the real Distillation walks n x K.
    similarity = Embeddings(cache, _no_embed, _NoModel(), fallback=_no_fallback)
    distance = 1.0 - similarity.similarities(wallpapers, wallpapers).astype(np.float64)
    np.fill_diagonal(distance, 0.0)
    rows = np.stack([vectors[wid] for wid in embedded]).astype(np.float64)
    centred = rows - rows.mean(axis=0)
    _, _, axes = np.linalg.svd(centred, full_matrices=False)
    plane = centred @ axes[:2].T
    verdicts = [str(resolved[wid].verdict) for wid in embedded]
    return Decided(
        wallpapers=wallpapers,
        verdicts=verdicts,
        values=np.array([resolved[wid].value for wid in embedded], dtype=np.float64),
        distance=distance,
        plane=plane,
        decided_total=len(standing),
        no_embedding=len(standing) - len(embedded),
        cleared=len(ordered) - len(standing),
    )


def group(decided: Decided, merge: float) -> list[list[int]]:
    """wa5.10: in log order, join the nearest same-sign group whose first member is within `merge`, else
    start one. Merge distance 0 merges nothing, so every decided Wallpaper is its own prototype.
    """
    groups: list[list[int]] = []
    firsts: list[int] = []
    signs: list[bool] = []
    for i in range(len(decided.wallpapers)):
        positive = bool(decided.values[i] > 0)
        best: int | None = None
        if merge > 0 and groups:
            d = decided.distance[i, firsts]
            eligible = (np.array(signs) == positive) & (d <= merge)
            if eligible.any():
                best = int(np.flatnonzero(eligible)[np.argmin(d[eligible])])
        if best is None:
            groups.append([i])
            firsts.append(i)
            signs.append(positive)
        else:
            groups[best].append(i)
    return groups


def medoid(decided: Decided, members: list[int]) -> int:
    within = decided.distance[np.ix_(members, members)]
    return members[int(np.argmin((within**2).sum(axis=1)))]


def radius(weight: float) -> float:
    return min(12.0 + 20.0 * float(np.sqrt(abs(weight) / 100.0)), 110.0)


def layout(anchors: NDArray[np.float64], radii: NDArray[np.float64]) -> NDArray[np.float64]:
    """Start at the PCA anchors, scaled to hold the nodes' area, then push overlaps apart."""
    count = len(radii)
    span = float(np.sqrt((np.pi * radii**2).sum())) * 1.5
    low, high = np.percentile(anchors, 2, axis=0), np.percentile(anchors, 98, axis=0)
    home = (anchors - (low + high) / 2) / np.maximum(high - low, 1e-9) * span
    position = home.copy()
    if count < 2:
        return position
    rng = np.random.default_rng(0)
    position += rng.normal(0, 0.5, position.shape)
    gap = radii[:, None] + radii[None, :] + 3.0
    for _ in range(60):
        delta = position[:, None, :] - position[None, :, :]
        apart = np.sqrt((delta**2).sum(axis=2))
        np.fill_diagonal(apart, np.inf)
        overlap = np.clip(gap - apart, 0.0, None)
        if not overlap.any():
            break
        push = (overlap / np.maximum(apart, 1e-6) / 2)[:, :, None] * delta
        position += push.sum(axis=1) * 0.5
        position += (home - position) * 0.02
    return position


def level(decided: Decided, merge: float) -> dict[str, Any]:
    groups = group(decided, merge)
    protos = [medoid(decided, members) for members in groups]
    weights = np.array([decided.values[members].sum() for members in groups])
    radii = np.array([radius(w) for w in weights])
    position = layout(decided.plane[protos], radii)
    assign = [0] * len(decided.wallpapers)
    to_proto = [0.0] * len(decided.wallpapers)
    for g, members in enumerate(groups):
        for i in members:
            assign[i] = g
            to_proto[i] = round(float(decided.distance[i, protos[g]]), 4)
    between = decided.distance[np.ix_(protos, protos)].copy()
    np.fill_diagonal(between, np.inf)
    sign = weights > 0
    between[sign[:, None] != sign[None, :]] = np.inf
    edges: set[tuple[int, int]] = set()
    for g in range(len(groups)):
        for h in np.argsort(between[g])[:NEIGHBOURS]:
            if np.isfinite(between[g, h]):
                edges.add((min(g, int(h)), max(g, int(h))))
    return {
        "d": merge,
        "assign": assign,
        "toProto": to_proto,
        "proto": protos,
        "weight": [int(w) for w in weights],
        "r": [round(float(r), 1) for r in radii],
        "x": [round(float(p[0]), 1) for p in position],
        "y": [round(float(p[1]), 1) for p in position],
        "edges": sorted(edges),
    }


def build() -> dict[str, Any]:
    decided = load()
    return {
        "meta": {
            "decidedTotal": decided.decided_total,
            "embedded": len(decided.wallpapers),
            "noEmbedding": decided.no_embedding,
            "cleared": decided.cleared,
            "defaultD": DEFAULT_MERGE_DISTANCE,
        },
        "items": [
            {"id": w.id, "v": v, "val": int(val), "url": w.page_url, "thumb": w.thumbnail_url}
            for w, v, val in zip(decided.wallpapers, decided.verdicts, decided.values, strict=True)
        ],
        "levels": levels(decided),
    }


def levels(decided: Decided) -> list[dict[str, Any]]:
    """One level per slider step; a step that groups exactly as the last one reuses its layout."""
    out: list[dict[str, Any]] = []
    for merge in LEVELS:
        if out and group(decided, merge) == group(decided, out[-1]["d"]):
            out.append({**out[-1], "d": merge})
        else:
            out.append(level(decided, merge))
    return out


def cached() -> dict[str, Any]:
    """`build()` takes ~20 s, so its result is kept beside the snapshot until the snapshot changes."""
    store = SNAPSHOT / "map.json"
    sources = [SNAPSHOT / "wallpapi.db", SNAPSHOT / "embeddings.db", HERE / "app.py"]
    if store.exists() and store.stat().st_mtime > max(path.stat().st_mtime for path in sources):
        return json.loads(store.read_text(encoding="utf-8"))
    data = build()
    store.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    return data


app = FastAPI()
app.add_middleware(GZipMiddleware, minimum_size=1000)
DATA = cached()
THUMBS = {path.stem: path for path in THUMBNAILS.iterdir() if path.is_file()} if THUMBNAILS.is_dir() else {}


@app.get("/", response_class=HTMLResponse)
def page() -> str:
    return (HERE / "index.html").read_text(encoding="utf-8")


@app.get("/api/map")
def taste_map() -> dict[str, Any]:
    return DATA


@app.get("/thumb/{wallpaper_id}")
def thumb(wallpaper_id: str) -> FileResponse:
    path = THUMBS.get(wallpaper_id)
    if path is None:
        raise HTTPException(status_code=404)
    return FileResponse(path, headers={"Cache-Control": "max-age=86400"})
