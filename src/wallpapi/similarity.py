"""How alike **Wallpapers** are: thumbnails compared by a CLIP image encoder (ADR 0013). Until its model is
open, a colours-and-category baseline answers every pair where either side has no embedding yet; once it is
open, such a pair is 0.0 and the **Wallpaper** an honest **Unknown** (ADR 0024).

One matrix, **Pool** x decided, never pairwise (invariant 2): a pairwise call forces a Python loop and tempts
caching **Scores**. The CLIP maths (preprocessing, the embed step, the mapped cosine, the near-duplicate
threshold) is `taste/clip.py`'s; this fetches the model, opens it, decodes the thumbnails and keeps the
**Embeddings**. `onnxruntime`, `pillow` and the download's `httpx2` are imported inside the functions that use
them: importing onnxruntime costs most of a second, and the app and the suite run without the model.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import numpy as np
from numpy.typing import NDArray

from wallpapi import storage
from wallpapi.model import Wallpaper
from wallpapi.taste import clip

# -- the baseline ------------------------------------------------------------------------------------

HUE_BINS = 12
TONE_BINS = 2
TONE_SPLIT = 0.5
NEUTRAL_BINS = 4
SATURATION_FLOOR = 0.15
CHROMATIC_BINS = HUE_BINS * TONE_BINS

COLOURLESS_BIN = CHROMATIC_BINS + NEUTRAL_BINS
"""A bin for a **Wallpaper** with no usable colours, so its self-cosine stays 1.0."""

BIN_COUNT = COLOURLESS_BIN + 1

CATEGORY_SHARE = 0.25
"""What a matching category is worth, the colours taking the rest: weak evidence."""


def metadata_similarity(pool: Sequence[Wallpaper], decided: Sequence[Wallpaper]) -> NDArray[np.float32]:
    """The baseline: colour histograms and category off the search response. Crude, no **API call**, one
    matmul.
    """
    codes: dict[str, int] = {}
    colour = _histograms(pool) @ _histograms(decided).T
    matching = _category_codes(pool, codes)[:, None] == _category_codes(decided, codes)[None, :]
    similarity = CATEGORY_SHARE * matching + (1.0 - CATEGORY_SHARE) * colour
    # Clipped: float32 rounding can put a self-similarity above 1.0 and make a distance negative.
    return np.clip(similarity, 0.0, 1.0).astype(np.float32)


def _category_codes(wallpapers: Sequence[Wallpaper], codes: dict[str, int]) -> NDArray[np.int64]:
    """Each **Wallpaper**'s category as an integer; `codes` is shared across both sides of a matrix."""
    return np.array(
        [codes.setdefault(w.category.strip().lower(), len(codes)) for w in wallpapers], dtype=np.int64
    )


def _histograms(wallpapers: Sequence[Wallpaper]) -> NDArray[np.float32]:
    """The `len(wallpapers)` x `BIN_COUNT` matrix of unit-length colour histograms."""
    rows: list[int] = []
    packed: list[int] = []
    for index, wallpaper in enumerate(wallpapers):
        for colour in wallpaper.colours:
            rgb = _rgb(colour)
            if rgb is not None:
                rows.append(index)
                packed.append(rgb)

    counts = np.zeros((len(wallpapers), BIN_COUNT), dtype=np.float32)
    if packed:
        binned = _bins(np.array(packed, dtype=np.int64))
        flat = np.bincount(np.array(rows, dtype=np.int64) * BIN_COUNT + binned, minlength=counts.size)
        counts = flat.reshape(len(wallpapers), BIN_COUNT).astype(np.float32)

    counts[counts.sum(axis=1) == 0.0, COLOURLESS_BIN] = 1.0
    return counts / np.linalg.norm(counts, axis=1, keepdims=True)


def _bins(packed: NDArray[np.int64]) -> NDArray[np.int64]:
    """Which bin each packed 24-bit colour falls in, in one numpy pass."""
    red = ((packed >> 16) & 0xFF) / 255.0
    green = ((packed >> 8) & 0xFF) / 255.0
    blue = (packed & 0xFF) / 255.0

    value = np.maximum(np.maximum(red, green), blue)
    chroma = value - np.minimum(np.minimum(red, green), blue)
    # Divisors are guarded: a grey has no hue and black no saturation.
    saturation = np.where(value > 0.0, chroma / np.where(value > 0.0, value, 1.0), 0.0)
    safe = np.where(chroma > 0.0, chroma, 1.0)
    hue = (
        np.select(
            [chroma == 0.0, value == red, value == green],
            [0.0, ((green - blue) / safe) % 6.0, ((blue - red) / safe) + 2.0],
            default=((red - green) / safe) + 4.0,
        )
        / 6.0
    )

    hue_bin = np.minimum((hue * HUE_BINS).astype(np.int64), HUE_BINS - 1)
    tone_bin = (value >= TONE_SPLIT).astype(np.int64)
    neutral_bin = CHROMATIC_BINS + np.minimum((value * NEUTRAL_BINS).astype(np.int64), NEUTRAL_BINS - 1)
    return np.where(saturation < SATURATION_FLOOR, neutral_bin, hue_bin * TONE_BINS + tone_bin)


def _rgb(colour: str) -> int | None:
    """`"#660000"` as `0x660000`, or `None`: a hand-edited row costs one colour, not every **Score**."""
    text = colour.strip().removeprefix("#")
    if len(text) != 6:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


# -- embeddings --------------------------------------------------------------------------------------

MODEL_REPO = "Xenova/clip-vit-base-patch32"

MODEL_REVISION = "d15189d7028b43f1d3e65039190477f6af591c2a"
"""Pinned to a commit, so the file cannot change under the recorded checksum."""

MODEL_FILE = "onnx/vision_model_quantized.onnx"
"""The int8-quantised image tower, on purpose: it only has to rank."""

MODEL_SHA256 = "583fd1110a514667812fee7d684952aaf82a99b959760c8d7dca7e0ab9839299"
"""Checked on download rather than trusted."""

MODEL_URL = f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{MODEL_FILE}"

CLIP_SPACE = f"{MODEL_REPO}@{MODEL_REVISION}/{MODEL_FILE}"
"""The **Space** of the pinned CLIP image tower: its repository, revision and file, since another revision
or another file of the same model places **Wallpapers** differently."""

FALLBACK_SPACE = "colour-and-category"
"""The **Space** of the colour-and-category baseline, `metadata_similarity`."""

BLENDED_SPACE = f"{CLIP_SPACE}+{FALLBACK_SPACE}"
"""The **Space** of a matrix that held both: CLIP for the pairs with two **Embeddings** and the baseline for
the rest, as `similarities` answers while the model is not open (ADR 0024)."""

EMBED_BATCH = 16

CAUGHT_UP = 30.0

RETRY_MODEL = 3600.0
"""Seconds before a model that could not be fetched or opened is tried again."""

DOWNLOAD_CHUNK = 1 << 20
"""A MiB at a time, which is how often the download looks at its `stop_event` (invariant 12)."""

DOWNLOAD_TIMEOUT = 30.0
"""Seconds without data before the download gives up: a read timeout, not a total one."""

THREAD_NAME = "wallpapi-similarity"

JOIN_TIMEOUT = DOWNLOAD_TIMEOUT + 5.0
"""Seconds shutdown waits for the thread: greater than the model download's read timeout (invariant 12)."""

_PENDING = (
    "Image similarity is still starting up — wallpapers are being compared by colour and category until "
    "its model is ready."
)

_FAILED = (
    "Image similarity could not fetch its model ({failure}) — wallpapers are being compared by colour "
    "and category instead."
)

_COVERAGE = (
    "Image similarity covers {embedded:,} of {pool:,} Pool wallpapers so far — the rest count as Unknown "
    "until their thumbnails are embedded."
)

_UNUSABLE = (
    "Image similarity could not open its model ({failure}) — wallpapers are being compared by colour and "
    "category instead. Deleting the file under `models/` will fetch it again."
)


@dataclass(frozen=True, slots=True)
class Measured:
    """A `len(pool)` x `len(decided)` similarity matrix and the **Space** that produced it."""

    matrix: NDArray[np.float32]
    space: str


type Similarity = Callable[[Sequence[Wallpaper], Sequence[Wallpaper]], NDArray[np.float32]]
"""A `len(pool)` x `len(decided)` matrix of similarities in `[0, 1]`: what `Embeddings` falls back to."""

type Embed = Callable[[Sequence[Path]], NDArray[np.float32]]
"""Thumbnail files to one L2-normalised embedding each, a row per file.

Called with no files, it opens the model and raises if it cannot: `Embeddings` does that once, as soon as
the model is on disk, so a file that will not open is a line on the page rather than every thumbnail
failing in turn.
"""


class ModelSource(Protocol):
    """Where the CLIP image tower comes from, injected so no test needs 85MiB of weights."""

    def ensure(self, stop_event: threading.Event) -> Path:
        """The model file on disk, fetching it if absent; raises if it cannot be had. Watches `stop_event`."""
        ...


class EmbeddingStore(Protocol):
    """Where embeddings are kept: `vectors_for` is plural, so there is one lookup per matrix (invariant 2)."""

    def vectors_for(self, wallpaper_ids: Sequence[str]) -> Mapping[str, NDArray[np.float32]]:
        """The L2-normalised embedding of each **Wallpaper** that has one; the rest are absent."""
        ...

    def store(self, wallpaper_id: str, vector: NDArray[np.float32]) -> None: ...

    def embedded_ids(self) -> set[str]: ...


class Embeddings:
    """Cosine between stored CLIP embeddings, mapped into `[0, 1]`; for every pair where either side has
    none, `fallback` until the model is open and 0.0 after (ADR 0024); and the upkeep that fetches the model
    and embeds the **Thumbnail cache**.
    """

    def __init__(
        self,
        store: EmbeddingStore,
        embed: Embed,
        model: ModelSource,
        *,
        fallback: Similarity = metadata_similarity,
        batch: int = EMBED_BATCH,
    ) -> None:
        self._store = store
        self._embed = embed
        self._model = model
        self._fallback = fallback
        self._batch = batch
        self._opened = False
        self._state: str | None = _PENDING
        self._unreadable: set[str] = set()
        self._lock = threading.Lock()

    def similarities(self, pool: Sequence[Wallpaper], decided: Sequence[Wallpaper]) -> NDArray[np.float32]:
        """A `len(pool)` x `len(decided)` float32 matrix in `[0, 1]`. A pair without two **Embeddings**
        is the fallback's until the model is open and 0.0 after, beyond every radius below 1 (ADR 0024).
        """
        return self.measure(pool, decided).matrix

    def measure(self, pool: Sequence[Wallpaper], decided: Sequence[Wallpaper]) -> Measured:
        """`similarities`, with the **Space** that answered, decided in the same call so the two cannot
        disagree: `CLIP_SPACE` once the model is open, an unembedded pair's 0.0 being CLIP's own rule;
        while it is not, `BLENDED_SPACE` if any pair had two **Embeddings** and `FALLBACK_SPACE` if none
        did.
        """
        # Read once: `_open` sets it on the similarity thread, and a bool needs no lock.
        opened = self._opened
        if opened:
            unembedded = np.zeros((len(pool), len(decided)), dtype=np.float32)
        else:
            unembedded = self._fallback(pool, decided)
        embedded = self._embedded([w.id for w in pool], [w.id for w in decided])
        if embedded is None:
            return Measured(unembedded, CLIP_SPACE if opened else FALLBACK_SPACE)
        mapped, both = embedded
        blended = np.clip(np.where(both, mapped, unembedded), 0.0, 1.0).astype(np.float32)
        if opened:
            return Measured(blended, CLIP_SPACE)
        return Measured(blended, BLENDED_SPACE if bool(both.any()) else FALLBACK_SPACE)

    def stored_similarities(self, pool_ids: Sequence[str], decided_ids: Sequence[str]) -> NDArray[np.float32]:
        """`similarities` as it answers once the model is open, whatever state the model is in, by
        **Wallpaper** id: the mapped cosine where both sides have an **Embedding** and 0.0 where either has
        none (ADR 0024). Reads the store and nothing else: it never opens, fetches or embeds, and never asks
        the fallback, so a command can use it without the model.
        """
        embedded = self._embedded(pool_ids, decided_ids)
        if embedded is None:
            return np.zeros((len(pool_ids), len(decided_ids)), dtype=np.float32)
        mapped, both = embedded
        return np.clip(np.where(both, mapped, 0.0), 0.0, 1.0).astype(np.float32)

    def unembedded(self, wallpaper_ids: Sequence[str]) -> set[str]:
        """Those of `wallpaper_ids` with no **Embedding** `similarities` would use: none stored, or one of
        another width or all zeros."""
        held = self._store.vectors_for(wallpaper_ids)
        width = next(iter(held.values())).size if held else 0
        _, known = _rows(wallpaper_ids, held, width)
        return {w for w, embedded in zip(wallpaper_ids, known, strict=True) if not embedded}

    def near_duplicates(self, pool: Sequence[Wallpaper], banned: Sequence[Wallpaper]) -> NDArray[np.bool_]:
        """A `len(pool)` x `len(banned)` mask of the pairs that are one image reposted (ADR 0021): both
        sides embedded and `clip.near_duplicates`. The fallback never enters it, because flat-colour pairs
        score 1.0 there.
        """
        embedded = self._embedded([w.id for w in pool], [w.id for w in banned])
        if embedded is None:
            return np.zeros((len(pool), len(banned)), dtype=np.bool_)
        mapped, both = embedded
        return both & clip.near_duplicates(mapped)

    def _embedded(
        self, pool_ids: Sequence[str], decided_ids: Sequence[str]
    ) -> tuple[NDArray[np.float32], NDArray[np.bool_]] | None:
        """The mapped cosine of every pair and the mask of pairs where both sides have an **Embedding**, in
        one lookup; `None` when there is nothing to compare.
        """
        if not pool_ids or not decided_ids:
            return None
        held = self._store.vectors_for([*pool_ids, *decided_ids])
        if not held:
            return None
        width = next(iter(held.values())).size

        pool_rows, pool_known = _rows(pool_ids, held, width)
        decided_rows, decided_known = _rows(decided_ids, held, width)
        mapped = clip.similarities(pool_rows, decided_rows)
        return mapped, pool_known[:, None] & decided_known[None, :]

    def catch_up(self, thumbnails: Path, stop_event: threading.Event) -> float:
        """Get the model, then embed one batch of thumbnails that have none yet; seconds to the next call.

        One batch per call so `stop_event` is seen between batches (invariant 12); the **Thumbnail cache** is
        the work list. Background thread only. Never raises: a model that cannot be had is reported by
        `notice`.
        """
        if not self._opened and not self._open(stop_event):
            return RETRY_MODEL
        pending = self._pending(thumbnails)
        if not pending:
            return CAUGHT_UP
        self._embed_batch(pending[: self._batch])
        return 0.0 if len(pending) > self._batch else CAUGHT_UP

    def notice(self, pool: Sequence[Wallpaper]) -> str | None:
        """One line for the page while this is not at full strength, or `None`: a fallback **Score** looks
        like a real one.
        """
        with self._lock:
            state = self._state
            unreadable = set(self._unreadable)
        if state is not None:
            return state
        # An unreadable thumbnail is never embedded: out of the count and total, or the line never clears.
        embeddable = [w.id for w in pool if w.id not in unreadable]
        embedded = len(self._store.vectors_for(embeddable))
        if embedded >= len(embeddable):
            return None
        return _COVERAGE.format(embedded=embedded, pool=len(embeddable))

    def vectors(self, pool: Sequence[Wallpaper]) -> NDArray[np.float32]:
        """The stored rows of `pool`, in its order, for the varied **Unknown** draw (ADR 0018); a zero row
        where there is no **Embedding**.
        """
        held = self._store.vectors_for([w.id for w in pool])
        width = next(iter(held.values())).size if held else 0
        rows, _ = _rows([w.id for w in pool], held, width)
        return rows

    def _open(self, stop_event: threading.Event) -> bool:
        """Fetch and open the model. `False` means this stays on the fallback for now."""
        try:
            self._model.ensure(stop_event)
        except _Cancelled:
            return False
        except Exception as failure:  # `catch_up` must never raise
            with self._lock:
                self._state = _FAILED.format(failure=failure)
            return False
        if stop_event.is_set():
            return False
        try:
            self._embed([])
        except Exception as failure:  # `catch_up` must never raise
            with self._lock:
                self._state = _UNUSABLE.format(failure=failure)
            return False
        self._opened = True
        with self._lock:
            self._state = None
        return True

    def _pending(self, thumbnails: Path) -> list[Path]:
        """Every cached thumbnail with no embedding yet, oldest first."""
        if not thumbnails.is_dir():
            return []
        skip = self._store.embedded_ids() | self._unreadable
        files = [path for path in thumbnails.iterdir() if path.is_file() and path.stem not in skip]
        return sorted(files, key=lambda path: path.stat().st_mtime)

    def _embed_batch(self, batch: Sequence[Path]) -> None:
        try:
            vectors = self._embed(batch)
        except Exception:  # one unreadable thumbnail must not stop the rest
            for path in batch:
                self._embed_one(path)
            return
        for path, vector in zip(batch, vectors, strict=True):
            self._store.store(path.stem, vector)

    def _embed_one(self, path: Path) -> None:
        """Retry a failed batch one file at a time, so one truncated thumbnail is the only casualty."""
        try:
            vectors = self._embed([path])
        except Exception:  # a thumbnail Pillow cannot read is skipped, not fatal
            # In memory only: a restart retries it.
            with self._lock:
                self._unreadable.add(path.stem)
            return
        if len(vectors):
            self._store.store(path.stem, vectors[0])


def upkeep_loop(embeddings: Embeddings, thumbnails: Path, stop_event: threading.Event) -> None:
    """Take one step, wait as the provider asked, repeat; `catch_up` never raises."""
    while not stop_event.is_set():
        wait = embeddings.catch_up(thumbnails, stop_event)
        if wait > 0 and stop_event.wait(wait):
            return


def _rows(
    wallpaper_ids: Sequence[str], held: Mapping[str, NDArray[np.float32]], width: int
) -> tuple[NDArray[np.float32], NDArray[np.bool_]]:
    """The embedding matrix for these **Wallpapers** and a mask of which had one; the mask decides the
    fallback.
    """
    matrix = np.zeros((len(wallpaper_ids), width), dtype=np.float32)
    known = np.zeros(len(wallpaper_ids), dtype=np.bool_)
    for index, wallpaper_id in enumerate(wallpaper_ids):
        vector = held.get(wallpaper_id)
        if vector is not None and vector.size == width and float(np.abs(vector).sum()) > 0.0:
            matrix[index] = vector
            known[index] = True
    return matrix, known


# -- the production store, embed and model source ----------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    wallpaper_id TEXT PRIMARY KEY,
    dim          INTEGER NOT NULL,
    vector       BLOB    NOT NULL
)
"""


class EmbeddingCache:
    """One vector per **Wallpaper** in a SQLite file of its own beside `wallpapi.db`, regenerable, so its
    schema is applied on first use with no migration; `dim` is stored so another model's width is caught.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._connections = storage.ThreadConnections(path)
        self._created = False
        self._creation_lock = threading.Lock()

    def vectors_for(self, wallpaper_ids: Sequence[str]) -> Mapping[str, NDArray[np.float32]]:
        requested = list(dict.fromkeys(wallpaper_ids))
        found: dict[str, NDArray[np.float32]] = {}
        connection = self._connect()
        for chunk in _chunked(requested, 10_000):
            placeholders = ",".join("?" * len(chunk))
            rows = connection.execute(
                f"SELECT wallpaper_id, dim, vector FROM embeddings WHERE wallpaper_id IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                vector = np.frombuffer(bytes(row["vector"]), dtype="<f4")
                if vector.size == int(row["dim"]):
                    found[str(row["wallpaper_id"])] = vector.astype(np.float32)
        return found

    def store(self, wallpaper_id: str, vector: NDArray[np.float32]) -> None:
        """Record one **Wallpaper**'s embedding, normalised here and nowhere else."""
        flat = np.asarray(vector, dtype=np.float32).reshape(-1)
        with storage.write(self._connect()) as write:
            write.execute(
                "INSERT OR REPLACE INTO embeddings (wallpaper_id, dim, vector) VALUES (?, ?, ?)",
                (wallpaper_id, int(flat.size), clip.normalised(flat).astype("<f4").tobytes()),
            )

    def embedded_ids(self) -> set[str]:
        rows = self._connect().execute("SELECT wallpaper_id FROM embeddings").fetchall()
        return {str(row["wallpaper_id"]) for row in rows}

    def _connect(self) -> sqlite3.Connection:
        """This thread's connection, the file, WAL and the schema made once per process, under a lock."""
        if not self._created:
            with self._creation_lock:
                if not self._created:
                    self._path.parent.mkdir(parents=True, exist_ok=True)
                    connection = self._connections.get()
                    connection.execute("PRAGMA journal_mode = WAL")
                    with storage.write(connection) as write:
                        write.execute(_SCHEMA)
                    self._created = True
        return self._connections.get()


class OnnxClipEmbedder:
    """The production `Embed`: a CLIP image tower run on the CPU by ONNX Runtime, through `clip.embed`.
    Nothing is opened or imported until the first call, so the app boots and the model downloads before
    anything needs it.
    """

    def __init__(self, model_path: Path, *, batch_size: int = EMBED_BATCH) -> None:
        self._model_path = model_path
        self._batch_size = batch_size
        self._session: clip.Session | None = None

    def __call__(self, images: Sequence[Path]) -> NDArray[np.float32]:
        session = self._loaded()
        if not images:
            return np.zeros((0, 0), dtype=np.float32)
        rows: list[NDArray[np.float32]] = []
        for start in range(0, len(images), self._batch_size):
            batch = images[start : start + self._batch_size]
            rows.append(clip.embed(session, np.stack([_pixels(path) for path in batch])))
        return np.concatenate(rows, axis=0)

    def _loaded(self) -> clip.Session:
        if self._session is None:
            import onnxruntime

            if not self._model_path.exists():
                raise RuntimeError(f"no CLIP image tower at {self._model_path}")
            self._session = cast(
                "clip.Session",
                onnxruntime.InferenceSession(  # pyright: ignore[reportUnknownMemberType]
                    str(self._model_path), providers=["CPUExecutionProvider"]
                ),
            )
        return self._session


class DownloadedModel:
    """The production `ModelSource`: the image tower, fetched once from Hugging Face and verified against the
    pinned checksum on the way in.

    Written to a `.part` sibling and moved into place, so an interrupted download leaves nothing that looks
    like a model.
    """

    def __init__(
        self,
        destination: Path,
        *,
        url: str = MODEL_URL,
        expected_sha256: str = MODEL_SHA256,
    ) -> None:
        self._destination = destination
        self._url = url
        self._expected = expected_sha256

    def ensure(self, stop_event: threading.Event) -> Path:
        """The model on disk, downloading it if absent. Raises if it cannot be had."""
        import httpx2

        if self._destination.exists():
            return self._destination
        self._destination.parent.mkdir(parents=True, exist_ok=True)
        partial = self._destination.with_suffix(self._destination.suffix + ".part")
        digest = hashlib.sha256()
        try:
            with httpx2.stream("GET", self._url, follow_redirects=True, timeout=DOWNLOAD_TIMEOUT) as response:
                response.raise_for_status()
                with partial.open("wb") as handle:
                    for chunk in response.iter_bytes(DOWNLOAD_CHUNK):
                        # Checked each chunk, so shutdown waits out a megabyte, not the file (invariant 12).
                        if stop_event.is_set():
                            raise _Cancelled
                        handle.write(chunk)
                        digest.update(chunk)
            actual = digest.hexdigest()
            if self._expected and actual != self._expected:
                raise RuntimeError(f"checksum mismatch: expected {self._expected}, got {actual}")
            partial.replace(self._destination)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        return self._destination


class _Cancelled(Exception):
    """Shutdown arrived mid download."""


def _pixels(path: Path) -> NDArray[np.float32]:
    """One thumbnail file, decoded to RGB here and made CLIP's input by `clip.preprocess`."""
    from PIL import Image

    with Image.open(path) as handle:
        image = handle.convert("RGB")
    return clip.preprocess(image)


def _chunked(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
