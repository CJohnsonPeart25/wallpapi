"""How alike **Wallpapers** are: thumbnails compared by a CLIP image encoder (ADR 0013), and a
colours-and-category baseline wherever either side has no embedding yet.

One matrix, **Pool** x decided, never pairwise (invariant 2): a pairwise call forces a Python loop and tempts
caching **Scores**. Cosine is mapped by `(1 + cosine) / 2` into `[0, 1]`. `onnxruntime`, `pillow` and the
download's `httpx2` are imported inside the functions that use them: importing onnxruntime costs most of a
second, and the app and the suite run without the model.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, cast

import numpy as np
from numpy.typing import NDArray

from wallpapi import storage
from wallpapi.model import Wallpaper

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

IMAGE_SIZE = 224

CLIP_MEAN = (0.48145466, 0.4578275, 0.40821073)
CLIP_STD = (0.26862954, 0.26130258, 0.27577711)

EMBED_BATCH = 16

CAUGHT_UP = 30.0

RETRY_MODEL = 3600.0
"""Seconds before a model that could not be fetched or opened is tried again."""

DOWNLOAD_CHUNK = 1 << 20
"""A MiB at a time, which is how often the download looks at its `stop_event` (invariant 12)."""

DOWNLOAD_TIMEOUT = 30.0
"""Seconds without data before the download gives up: a read timeout, not a total one."""

_PENDING = (
    "Image similarity is still starting up — wallpapers are being compared by colour and category until "
    "its model is ready."
)

_FAILED = (
    "Image similarity could not fetch its model ({failure}) — wallpapers are being compared by colour "
    "and category instead."
)

_COVERAGE = (
    "Image similarity covers {embedded:,} of {pool:,} Pool wallpapers so far — the rest are compared by "
    "colour and category until their thumbnails are embedded."
)

_UNUSABLE = (
    "Image similarity could not open its model ({failure}) — wallpapers are being compared by colour and "
    "category instead. Deleting the file under `models/` will fetch it again."
)

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
    """Cosine between stored CLIP embeddings, mapped into `[0, 1]`, with `fallback` for every pair where
    either side has none; and the upkeep that fetches the model and embeds the **Thumbnail cache**.
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
        """A `len(pool)` x `len(decided)` float32 matrix in `[0, 1]`."""
        fallback = self._fallback(pool, decided)
        if not pool or not decided:
            return fallback

        held = self._store.vectors_for([w.id for w in pool] + [w.id for w in decided])
        if not held:
            return fallback
        width = next(iter(held.values())).size

        pool_rows, pool_known = _rows([w.id for w in pool], held, width)
        decided_rows, decided_known = _rows([w.id for w in decided], held, width)
        cosine = pool_rows @ decided_rows.T
        # Not clipped at zero: a negative cosine means less in common than unrelated.
        mapped = (1.0 + np.clip(cosine, -1.0, 1.0)) / 2.0

        both = pool_known[:, None] & decided_known[None, :]
        blended = np.where(both, mapped, fallback)
        return np.clip(blended, 0.0, 1.0).astype(np.float32)

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
                (wallpaper_id, int(flat.size), _normalised(flat).astype("<f4").tobytes()),
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
    """The production `Embed`: a CLIP image tower run on the CPU by ONNX Runtime. Nothing is opened or
    imported until the first call, so the app boots and the model downloads before anything needs it.
    """

    def __init__(self, model_path: Path, *, batch_size: int = EMBED_BATCH) -> None:
        self._model_path = model_path
        self._batch_size = batch_size
        self._session: _OnnxSession | None = None
        self._input_name: str = ""
        self._output_index: int = 0

    def __call__(self, images: Sequence[Path]) -> NDArray[np.float32]:
        session = self._loaded()
        if not images:
            return np.zeros((0, 0), dtype=np.float32)
        rows: list[NDArray[np.float32]] = []
        for start in range(0, len(images), self._batch_size):
            batch = images[start : start + self._batch_size]
            pixels = np.stack([_preprocess(path) for path in batch])
            outputs = session.run(None, {self._input_name: pixels})
            rows.append(_as_vectors(outputs[self._output_index]))
        stacked = np.concatenate(rows, axis=0)
        return np.stack([_normalised(row) for row in stacked])

    def _loaded(self) -> _OnnxSession:
        if self._session is None:
            import onnxruntime

            if not self._model_path.exists():
                raise RuntimeError(f"no CLIP image tower at {self._model_path}")
            session = cast(
                "_OnnxSession",
                onnxruntime.InferenceSession(  # pyright: ignore[reportUnknownMemberType]
                    str(self._model_path), providers=["CPUExecutionProvider"]
                ),
            )
            self._session = session
            self._input_name = session.get_inputs()[0].name

            names = [output.name for output in session.get_outputs()]
            self._output_index = names.index("image_embeds") if "image_embeds" in names else 0
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


class _OnnxNode(Protocol):
    name: str


class _OnnxSession(Protocol):
    """Just enough of `onnxruntime.InferenceSession` to type: it ships no `py.typed`."""

    def get_inputs(self) -> Sequence[_OnnxNode]: ...
    def get_outputs(self) -> Sequence[_OnnxNode]: ...
    def run(self, output_names: Sequence[str] | None, input_feed: Mapping[str, Any]) -> Sequence[Any]: ...


def _preprocess(path: Path) -> NDArray[np.float32]:
    """One thumbnail as CLIP's `(3, 224, 224)` input: resize the short side, centre crop, normalise."""
    from PIL import Image

    with Image.open(path) as handle:
        image = handle.convert("RGB")
        width, height = image.size
        scale = IMAGE_SIZE / min(width, height)
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        resized = image.resize(size, Image.Resampling.BICUBIC)
    left = (resized.width - IMAGE_SIZE) // 2
    top = (resized.height - IMAGE_SIZE) // 2
    cropped = resized.crop((left, top, left + IMAGE_SIZE, top + IMAGE_SIZE))

    pixels = np.asarray(cropped, dtype=np.float32) / 255.0
    normalised = (pixels - np.array(CLIP_MEAN, dtype=np.float32)) / np.array(CLIP_STD, dtype=np.float32)
    return np.transpose(normalised, (2, 0, 1)).astype(np.float32)


def _as_vectors(output: Any) -> NDArray[np.float32]:
    """The model's output as one row per image: the class token if the export gives `last_hidden_state`."""
    array = np.asarray(output, dtype=np.float32)
    return array[:, 0, :] if array.ndim == 3 else array


def _normalised(vector: NDArray[np.float32]) -> NDArray[np.float32]:
    """Unit length, or unchanged if there is no length to divide by."""
    norm = float(np.linalg.norm(vector))
    return vector if norm == 0.0 else (vector / norm).astype(np.float32)


def _chunked(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
