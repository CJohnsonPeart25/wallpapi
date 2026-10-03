"""The **Similarity provider** wallpapi runs with: thumbnails compared by a CLIP image encoder (ADR 0013).

`onnxruntime` and `pillow` are imported inside the methods that use them because importing onnxruntime costs
most of a second. Cosine is mapped by `(1 + cosine) / 2` into `[0, 1]`; a pair with no cached embedding falls
back to the baseline.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, cast

import numpy as np
from numpy.typing import NDArray

from wallpapi.model import Wallpaper
from wallpapi.similarity import NOTHING_TO_CATCH_UP, MetadataSimilarityProvider, SimilarityProvider
from wallpapi.similarity_cache import SidecarDatabase

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


class ModelSource(Protocol):
    """Where the CLIP image tower comes from, injected so no test needs 85MiB of weights."""

    def ensure(self, stop_event: threading.Event) -> Path:
        """The model file on disk, fetching it if absent; raises if it cannot be had. Watches `stop_event`."""
        ...


class EmbeddingWriter(Protocol):
    def store(self, wallpaper_id: str, vector: NDArray[np.float32]) -> None: ...

    def embedded_ids(self) -> set[str]: ...


class EmbeddingSource(Protocol):
    """Where the provider gets vectors from, plural so there is one lookup per matrix (invariant 2)."""

    def vectors_for(self, wallpaper_ids: Sequence[str]) -> Mapping[str, NDArray[np.float32]]:
        """The L2-normalised embedding of each **Wallpaper** that has one; the rest are absent."""
        ...


class Embedder(Protocol):
    """Turns thumbnail files into L2-normalised embeddings; injected so no test needs a model."""

    def __call__(self, images: Sequence[Path]) -> NDArray[np.float32]: ...


_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS embeddings (
        wallpaper_id TEXT PRIMARY KEY,
        dim          INTEGER NOT NULL,
        vector       BLOB    NOT NULL
    )
    """,
)


class EmbeddingCache:
    """One vector per **Wallpaper** in a SQLite file of the provider's own; `dim` is stored so another model's
    width is caught.
    """

    def __init__(self, path: Path) -> None:
        self._db = SidecarDatabase(path, _SCHEMA)

    @property
    def path(self) -> Path:
        return self._db.path

    def size_bytes(self) -> int:
        return self._db.size_bytes()

    def vectors_for(self, wallpaper_ids: Sequence[str]) -> Mapping[str, NDArray[np.float32]]:
        requested = list(dict.fromkeys(wallpaper_ids))
        found: dict[str, NDArray[np.float32]] = {}
        connection = self._db.connect()
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
        with self._db.write() as write:
            write.execute(
                "INSERT OR REPLACE INTO embeddings (wallpaper_id, dim, vector) VALUES (?, ?, ?)",
                (wallpaper_id, int(flat.size), _normalised(flat).astype("<f4").tobytes()),
            )

    def embedded_ids(self) -> set[str]:
        rows = self._db.connect().execute("SELECT wallpaper_id FROM embeddings").fetchall()
        return {str(row["wallpaper_id"]) for row in rows}


class EmbeddingSimilarityProvider:
    """Cosine between cached CLIP embeddings, mapped into `[0, 1]`, with the baseline as the per-pair
    fallback.

    `notice(pool)` says when it is on the fallback. `model=None` means it manages no model and reports
    nothing.
    """

    def __init__(
        self,
        vectors: EmbeddingSource,
        *,
        baseline: SimilarityProvider | None = None,
        model: ModelSource | None = None,
        embedder: Embedder | None = None,
        cache: EmbeddingWriter | None = None,
        batch: int = EMBED_BATCH,
    ) -> None:
        self._vectors = vectors
        self._baseline = MetadataSimilarityProvider() if baseline is None else baseline
        self._model = model
        self._embedder = embedder
        self._cache = cache if cache is not None else vectors if isinstance(vectors, EmbeddingCache) else None
        self._batch = batch
        # Pending: a model source and nothing to run it with.
        self._state: str | None = _PENDING if model is not None and embedder is None else None
        self._unreadable: set[str] = set()
        self._lock = threading.Lock()

    def similarities(self, pool: Sequence[Wallpaper], decided: Sequence[Wallpaper]) -> NDArray[np.float32]:
        baseline = self._baseline.similarities(pool, decided)
        if not pool or not decided:
            return baseline

        held = self._vectors.vectors_for([w.id for w in pool] + [w.id for w in decided])
        if not held:
            return baseline
        width = next(iter(held.values())).size

        pool_rows, pool_known = _rows([w.id for w in pool], held, width)
        decided_rows, decided_known = _rows([w.id for w in decided], held, width)
        cosine = pool_rows @ decided_rows.T
        # Not clipped at zero: a negative cosine means less in common than unrelated.
        mapped = (1.0 + np.clip(cosine, -1.0, 1.0)) / 2.0

        both = pool_known[:, None] & decided_known[None, :]
        blended = np.where(both, mapped, baseline)
        return np.clip(blended, 0.0, 1.0).astype(np.float32)

    def catch_up(self, thumbnails: Path, stop_event: threading.Event) -> float:
        """Get the model, then embed one batch of thumbnails that have none yet.

        One batch per call so `stop_event` is seen between batches (invariant 12); the **Thumbnail cache** is
        the work list. Never raises: a failed download is reported by `notice`.
        """
        if self._model is None or self._cache is None:
            return NOTHING_TO_CATCH_UP
        if self._embedder is None and not self._ready(stop_event):
            return NOTHING_TO_CATCH_UP

        pending = self._pending(thumbnails)
        if not pending:
            return CAUGHT_UP
        self._embed(pending[: self._batch])
        return 0.0 if len(pending) > self._batch else CAUGHT_UP

    def notice(self, pool: Sequence[Wallpaper]) -> str | None:
        """What the page says while this provider is not yet itself, or `None`: a fallback **Score** looks
        like a real one.
        """
        with self._lock:
            state = self._state
            unreadable = set(self._unreadable)
        if state is not None or self._model is None:
            return state
        # An unreadable thumbnail is never embedded: out of the count and total, or the line never clears.
        embeddable = [w.id for w in pool if w.id not in unreadable]
        embedded = len(self._vectors.vectors_for(embeddable))
        if embedded >= len(embeddable):
            return None
        return _COVERAGE.format(embedded=embedded, pool=len(embeddable))

    def vectors(self, pool: Sequence[Wallpaper]) -> NDArray[np.float32] | None:
        """The cached CLIP rows of `pool`, in its order; a zero row where there is no **Embedding**."""
        held = self._vectors.vectors_for([w.id for w in pool])
        width = next(iter(held.values())).size if held else 0
        rows, _ = _rows([w.id for w in pool], held, width)
        return rows

    def _ready(self, stop_event: threading.Event) -> bool:
        """Fetch and open the model, once. `False` means this stays on the baseline for now."""
        if self._model is None:
            return False
        try:
            path = self._model.ensure(stop_event)
        except _Cancelled:
            return False
        except Exception as failure:  # `catch_up` must never raise
            with self._lock:
                self._state = _FAILED.format(failure=failure)
            return False
        if stop_event.is_set():
            return False

        embedder = OnnxClipEmbedder(path)
        try:
            # Opened here so an unopenable file is a line on the page, not every embedding failing silently.
            embedder.warm()
        except Exception as failure:  # `catch_up` must never raise
            with self._lock:
                self._state = _UNUSABLE.format(failure=failure)
            return False

        self._embedder = embedder
        with self._lock:
            self._state = None
        return True

    def _pending(self, thumbnails: Path) -> list[Path]:
        """Every cached thumbnail with no embedding yet, oldest first."""
        if self._cache is None or not thumbnails.is_dir():
            return []
        skip = self._cache.embedded_ids() | self._unreadable
        files = [path for path in thumbnails.iterdir() if path.is_file() and path.stem not in skip]
        return sorted(files, key=lambda path: path.stat().st_mtime)

    def _embed(self, batch: Sequence[Path]) -> None:
        """One batch through the model and into the cache."""
        if self._embedder is None or self._cache is None:
            return
        try:
            vectors = self._embedder(batch)
        except Exception:  # one unreadable thumbnail must not stop the rest
            for path in batch:
                self._embed_one(path)
            return
        for path, vector in zip(batch, vectors, strict=True):
            self._cache.store(path.stem, vector)

    def _embed_one(self, path: Path) -> None:
        """Retry a failed batch one file at a time, so one truncated thumbnail is the only casualty."""
        if self._embedder is None or self._cache is None:
            return
        try:
            vectors = self._embedder([path])
        except Exception:  # a thumbnail Pillow cannot read is skipped, not fatal
            # In memory only: a restart retries it.
            with self._lock:
                self._unreadable.add(path.stem)
            return
        if len(vectors):
            self._cache.store(path.stem, vectors[0])


class OnnxClipEmbedder:
    """A CLIP image tower run on the CPU by ONNX Runtime, its session built on first call."""

    def __init__(self, model_path: Path, *, batch_size: int = EMBED_BATCH) -> None:
        self._model_path = model_path
        self._batch_size = batch_size
        self._session: _OnnxSession | None = None
        self._input_name: str = ""
        self._output_index: int = 0

    def __call__(self, images: Sequence[Path]) -> NDArray[np.float32]:
        if not images:
            return np.zeros((0, 0), dtype=np.float32)
        session = self._loaded()
        rows: list[NDArray[np.float32]] = []
        for start in range(0, len(images), self._batch_size):
            batch = images[start : start + self._batch_size]
            pixels = np.stack([_preprocess(path) for path in batch])
            outputs = session.run(None, {self._input_name: pixels})
            rows.append(_as_vectors(outputs[self._output_index]))
        stacked = np.concatenate(rows, axis=0)
        return np.stack([_normalised(row) for row in stacked])

    def warm(self) -> None:
        """Open the session now, so a model that will not open is reported once."""
        self._loaded()

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
    """The image tower, fetched once from Hugging Face and verified against the pinned checksum on the way in.

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

    @property
    def path(self) -> Path:
        return self._destination

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


def _normalised(vector: NDArray[np.float32]) -> NDArray[np.float32]:
    """Unit length, or unchanged if there is no length to divide by."""
    norm = float(np.linalg.norm(vector))
    return vector if norm == 0.0 else (vector / norm).astype(np.float32)


def _chunked(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
