"""#14's second candidate: a **Similarity provider** that compares the images themselves.

A CLIP image encoder turns a thumbnail into a few hundred numbers that stand for what is in the picture,
and two **Wallpapers** of the same thing land near each other whatever their palettes. That is the one
thing neither the baseline nor the tags can do: the baseline sees a palette, the tags see what somebody
typed, and this sees the image.

**ONNX Runtime, image tower only, CPU** (invariant 13). Never `torch` or `open_clip`: the image encoder
alone is a file of a few hundred megabytes against a couple of gigabytes of framework, it runs on the CPU
in tens of milliseconds, and the spike stays disposable. The text tower is not downloaded at all — nothing
here takes a text prompt, and half a CLIP is half the disk.

**Optional, and lazily imported.** `onnxruntime` and `pillow` are in the `similarity-embedding` extra
rather than in the dependencies, so the default install stays small and `numpy` stays a direct dependency
rather than arriving transitively through onnxruntime (invariant 2). Neither is imported until an
embedding is actually computed, which is what lets this module be imported — and its provider unit-tested
against vectors written by hand — with neither package installed and no model on disk.

**The formula.** Embeddings are L2 normalised when they are computed, so the cosine between two of them is
one dot product. Cosine runs `[-1, 1]` and the protocol promises `[0, 1]`, so it is mapped with
`(1 + cosine) / 2`: monotone, so it reorders nothing, and exactly 1.0 for a **Wallpaper** against itself.
A pair where either side has no cached embedding falls back to the baseline provider, for the same reason
the tag provider does — a **Pool** grows faster than it can be embedded, and a 0.0 for a **Wallpaper**
nobody has embedded yet would say it is unlike every **Ban**.

That mapping has a consequence worth stating plainly, because #14's comparison turns on it: real CLIP
cosines between natural images are not spread over `[-1, 1]` but bunched around 0.5 to 0.9, so the mapped
similarities bunch around 0.75 to 0.95 and every distance is small. The **Similarity radius** of 0.5 that
ADR 0007 calls "a starting point, not a tuned value" therefore lets every decided **Wallpaper** reach
every **Pool** member, which is a different regime from the baseline's. Rescaling the band to fill `[0, 1]`
would hide that behind a constant nobody can justify; the honest answer is the monotone mapping here and a
**Similarity radius** chosen for the provider in use.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, cast

import numpy as np
from numpy.typing import NDArray

from wallpapi.model import Wallpaper
from wallpapi.similarity import MetadataSimilarityProvider, SimilarityProvider
from wallpapi.similarity_cache import SidecarDatabase

MODEL_REPO = "Xenova/clip-vit-base-patch32"
"""The Hugging Face repository the image tower is downloaded from.

A straight ONNX export of OpenAI's `clip-vit-base-patch32` — the same weights `open_clip`'s ViT-B/32 uses,
with no fine-tuning — published as separate text and vision files, which is what makes taking the image
tower alone a download rather than a surgery.
"""

MODEL_REVISION = "d15189d7028b43f1d3e65039190477f6af591c2a"
"""Pinned to a commit rather than to `main`, so the file cannot change under the recorded checksum."""

MODEL_FILE = "onnx/vision_model_quantized.onnx"
"""The int8-quantised image tower: 85MiB against 335MiB for the float32 export of the same weights.

Quantised on purpose. It is well inside the 150MB invariant 13 budgets for this, it is the build ONNX
Runtime's CPU kernels are fastest on, and what is being asked of it is a *ranking* — which **Wallpapers**
are nearest this **Favourite** — rather than an absolute number. If the comparison had come out close, the
float32 file is the same URL with `vision_model.onnx` in place of this one and nothing else changes.
"""

MODEL_SHA256 = "583fd1110a514667812fee7d684952aaf82a99b959760c8d7dca7e0ab9839299"
"""The sha256 of that file at that revision, verified on download. Recorded so a re-download is checkable
rather than trusted, and so a reviewer can tell which weights every number in #14's comparison came from."""

MODEL_URL = f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{MODEL_FILE}"

IMAGE_SIZE = 224
"""CLIP ViT-B/32's input is 224x224. Not a choice — the export has it baked into its input shape."""

CLIP_MEAN = (0.48145466, 0.4578275, 0.40821073)
CLIP_STD = (0.26862954, 0.26130258, 0.27577711)
"""OpenAI's published CLIP preprocessing constants. These are part of the model, not a preference: an
image normalised with any other mean and standard deviation is an image the encoder was never shown."""


class EmbeddingSource(Protocol):
    """Where the provider gets vectors from.

    Plural for invariant 2's reason, exactly as the tag provider's `TagSource` is: one lookup per matrix,
    never one per **Wallpaper**.
    """

    def vectors_for(self, wallpaper_ids: Sequence[str]) -> Mapping[str, NDArray[np.float32]]:
        """The L2-normalised embedding of each **Wallpaper** that has one. The rest are simply absent."""
        ...


class Embedder(Protocol):
    """Turns thumbnail files into L2-normalised embeddings, one row each.

    A seam of its own so the provider, the cache and the fill step can all be exercised with no model on
    disk and no `onnxruntime` installed: a test injects a function that returns vectors it wrote itself.
    """

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
    """One vector per **Wallpaper**, kept for good in a SQLite file of the provider's own.

    Stored as a raw little-endian float32 blob rather than as JSON or as a column per dimension: 512 floats
    is 2KiB as bytes and about 6KiB as text, and `np.frombuffer` costs nothing to read it back.

    `dim` is stored beside the blob so a cache written by one model and read by another is caught on the
    way out rather than producing a matrix of the wrong shape — the vectors of two different encoders are
    not comparable even when they are the same length, but a length that disagrees is the one case that can
    be detected for free.
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
        """Record one **Wallpaper**'s embedding, normalised on the way in.

        Normalised here and nowhere else, so that every reader can take a dot product and call it a cosine.
        A zero vector — which a broken thumbnail could produce — is stored as it is rather than divided by
        zero; the provider's mask then treats it as the absence it is.
        """
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
    """Cosine between cached CLIP image embeddings, mapped into `[0, 1]`, with the baseline as fallback.

    Vectorised the same way everything else here is: two `(n, d)` matrices and one matmul, no Python loop
    over pairs. The only iteration is the one that gathers each **Wallpaper**'s row, which is linear in the
    **Pool**.
    """

    def __init__(self, vectors: EmbeddingSource, *, baseline: SimilarityProvider | None = None) -> None:
        self._vectors = vectors
        self._baseline = MetadataSimilarityProvider() if baseline is None else baseline

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
        # `(1 + cosine) / 2` rather than a clip at zero: a negative cosine is a real statement — these two
        # images have less in common than two unrelated ones — and clipping it would throw that away by
        # calling it the same as merely unrelated.
        mapped = (1.0 + np.clip(cosine, -1.0, 1.0)) / 2.0

        both = pool_known[:, None] & decided_known[None, :]
        blended = np.where(both, mapped, baseline)
        return np.clip(blended, 0.0, 1.0).astype(np.float32)


class OnnxClipEmbedder:
    """The real thing: a CLIP image tower run on the CPU by ONNX Runtime.

    Lazy in two ways on purpose. The session is built on the first call rather than in `__init__`, because
    loading 85MiB of weights is not something a constructor should do to a process that may never embed
    anything; and `onnxruntime` and `PIL` are imported inside the methods that need them, so that a wallpapi
    installed without the `similarity-embedding` extra can still import this module, wire up `main.py`, and
    fail with a sentence rather than an `ImportError` from the top of a file.
    """

    def __init__(self, model_path: Path, *, batch_size: int = 16) -> None:
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

    def _loaded(self) -> _OnnxSession:
        if self._session is None:
            try:
                import onnxruntime
            except ImportError as error:  # pragma: no cover - depends on the install, not on behaviour
                raise RuntimeError(
                    "the embedding **Similarity provider** needs the `similarity-embedding` extra: "
                    "uv sync --extra similarity-embedding"
                ) from error
            if not self._model_path.exists():
                raise RuntimeError(
                    f"no CLIP image tower at {self._model_path} — "
                    "run `uv run python scripts/similarity_spike.py model` to download it"
                )
            session = cast(
                "_OnnxSession",
                onnxruntime.InferenceSession(  # pyright: ignore[reportUnknownMemberType]
                    str(self._model_path), providers=["CPUExecutionProvider"]
                ),
            )
            self._session = session
            self._input_name = session.get_inputs()[0].name
            # `image_embeds` is the projected 512-vector CLIP compares images in; an export that only
            # offers `last_hidden_state` is handled by `_as_vectors` taking the class token instead.
            names = [output.name for output in session.get_outputs()]
            self._output_index = names.index("image_embeds") if "image_embeds" in names else 0
        return self._session


def download_model(destination: Path, *, url: str = MODEL_URL, expected_sha256: str = MODEL_SHA256) -> Path:
    """Fetch the image tower once, verify it, and leave it where the provider will look.

    Not a Wallhaven **API call** and nothing to do with the rate limiter — a different host entirely, asked
    once ever. Verified rather than trusted: the checksum is what ties every number in #14's comparison to
    a particular set of weights, and a truncated download would otherwise show up as a mysteriously bad
    provider rather than as an error.
    """
    import httpx2

    if destination.exists() and _sha256(destination) == expected_sha256:
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    with httpx2.stream("GET", url, follow_redirects=True, timeout=120.0) as response:
        response.raise_for_status()
        with partial.open("wb") as handle:
            for chunk in response.iter_bytes(1 << 20):
                handle.write(chunk)
    actual = _sha256(partial)
    if expected_sha256 and actual != expected_sha256:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"model checksum mismatch: expected {expected_sha256}, got {actual}")
    partial.replace(destination)
    return destination


class _OnnxNode(Protocol):
    name: str


class _OnnxSession(Protocol):
    """Just enough of `onnxruntime.InferenceSession` to be typed.

    onnxruntime ships no `py.typed`, so everything it hands back is `Unknown` under pyright's strict mode.
    Rather than scattering ignores through the embedder, the one `cast` in `_loaded` lands on this, and the
    rest of the module is checked normally.
    """

    def get_inputs(self) -> Sequence[_OnnxNode]: ...
    def get_outputs(self) -> Sequence[_OnnxNode]: ...
    def run(self, output_names: Sequence[str] | None, input_feed: Mapping[str, Any]) -> Sequence[Any]: ...


def _preprocess(path: Path) -> NDArray[np.float32]:
    """One thumbnail as CLIP's `(3, 224, 224)` input: resize the short side, centre crop, normalise.

    OpenAI's own preprocessing, step for step, because an encoder shown anything else is being asked a
    question it was not trained on. Bicubic is the resampling CLIP's reference implementation uses. The
    centre crop is what makes a 16:9 wallpaper into a square, and it does lose the sides of a wide image —
    a real limitation of embedding thumbnails, and one worth naming in the comparison rather than hiding.
    """
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
    """The model's output as one row per image.

    A projected export gives `(batch, dim)` and is already that. An export whose first output is
    `last_hidden_state` gives `(batch, tokens, dim)`, whose first token is the class token — the one the
    pooled representation is taken from — so that is what is used.
    """
    array = np.asarray(output, dtype=np.float32)
    return array[:, 0, :] if array.ndim == 3 else array


def _rows(
    wallpaper_ids: Sequence[str], held: Mapping[str, NDArray[np.float32]], width: int
) -> tuple[NDArray[np.float32], NDArray[np.bool_]]:
    """The embedding matrix for these **Wallpapers**, and a mask of which of them actually had one.

    A **Wallpaper** with no embedding gets a zero row, whose dot product with anything is zero — but the
    mask, not the zero, is what decides it falls back to the baseline. A cached vector of the wrong width
    is treated as absent for the same reason: it came from a different model and is not comparable.
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _chunked(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
