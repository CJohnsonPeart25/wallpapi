"""The **Similarity provider** wallpapi runs with: the images themselves, compared by a CLIP encoder.

A CLIP image encoder turns a thumbnail into a few hundred numbers that stand for what is in the picture,
and two **Wallpapers** of the same thing land near each other whatever their palettes. That is the one
thing neither the baseline nor the tags can do: the baseline sees a palette, the tags see what somebody
typed, and this sees the image. #14 measured all three and this one won — see
`docs/adr/0013-image-embeddings-are-the-similarity-provider.md` and the spike note it links to.

**ONNX Runtime, image tower only, CPU** (invariant 13). Never `torch` or `open_clip`: the image encoder
alone is 85MiB against a couple of gigabytes of framework, and it runs on the CPU in about 7ms a
**Wallpaper**. The text tower is not downloaded at all — nothing here takes a text prompt, and half a CLIP
is half the disk. `onnxruntime` and `pillow` are ordinary dependencies, but both are imported inside the
methods that use them: importing onnxruntime costs the best part of a second, and the app starts, the page
renders and the whole test suite runs without ever needing it. `numpy` stays a *direct* dependency rather
than being left to arrive through onnxruntime (invariant 2).

**Nothing here is on the path of a page load.** The model is fetched and the thumbnails are embedded by
`catch_up`, which only the background thread calls. All a request touches is the cache and one matmul.

**The formula.** Embeddings are L2 normalised when they are computed, so the cosine between two of them is
one dot product. Cosine runs `[-1, 1]` and the protocol promises `[0, 1]`, so it is mapped with
`(1 + cosine) / 2`: monotone, so it reorders nothing, and exactly 1.0 for a **Wallpaper** against itself.
A pair where either side has no cached embedding falls back to the baseline provider — a **Pool** grows
faster than it can be embedded, and a 0.0 for a **Wallpaper** nobody has embedded yet would say it is
unlike every **Ban**.

That mapping has a consequence the **Similarity radius** default depends on: real CLIP cosines between
natural images are not spread over `[-1, 1]` but bunched, so the mapped similarities span about 0.59 to
1.0 and every distance is under 0.41. Rescaling the band to fill `[0, 1]` would hide that behind a
constant nobody can justify. The honest answer is the monotone mapping here and a **Similarity radius**
chosen for the provider in use, which is why migration 9 moves the default from 0.5 to 0.15 (ADR 0013).
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

EMBED_BATCH = 16
"""Thumbnails per model run, and so how often `catch_up` looks at its `stop_event`.

Sixteen 224x224 images is a few megabytes of float32 and about a tenth of a second of CPU, which is short
enough that shutdown never waits on it and long enough that the per-call overhead is not the cost.
"""

CAUGHT_UP = 30.0
"""Seconds to wait once every cached thumbnail has an embedding.

Not `NOTHING_TO_CATCH_UP`: the thumbnail downloader is adding to the **Thumbnail cache** while this runs,
so "caught up" is only ever "caught up for now", and half a minute is soon enough that a **Wallpaper** is
embedded long before anybody has judged enough for its **Score** to matter.
"""

DOWNLOAD_CHUNK = 1 << 20
"""A MiB at a time, which is also how often the download looks at its `stop_event` (invariant 12)."""

DOWNLOAD_TIMEOUT = 30.0
"""Seconds without data before the download gives up — a read timeout, not a total one: the whole file is
85MiB and how long that takes is the user's connection's business, but a stalled socket is not."""

_PENDING = (
    "Image similarity is still starting up — wallpapers are being compared by colour and category until "
    "its model is ready."
)
"""What the page says before the provider can embed anything.

"Starting up" rather than "downloading", because it covers both states it is actually true in: a first
boot that is fetching 85MiB, and the moment after any boot before the background thread has opened a model
that was already there. No progress and no percentage — this is a line on a page nobody is watching, and
the honest summary is that the **Scores** are the baseline's for now.
"""

_FAILED = (
    "Image similarity could not fetch its model ({failure}) — wallpapers are being compared by colour "
    "and category instead."
)
"""And after the fetch failed. The failure is quoted rather than summarised, because the two causes worth
telling apart — no network, and a checksum that did not match — read completely differently."""

_COVERAGE = (
    "Image similarity covers {embedded:,} of {pool:,} Pool wallpapers so far — the rest are compared by "
    "colour and category until their thumbnails are embedded."
)
"""And once the model is working, while part of the **Pool** has no **Embedding** yet (#44).

A count rather than a percentage, because the two numbers are what a person can check against the
indicator beside it. It goes away by itself: the downloader fetches every **Pool** member's thumbnail and
`catch_up` embeds each one it finds.
"""

_UNUSABLE = (
    "Image similarity could not open its model ({failure}) — wallpapers are being compared by colour and "
    "category instead. Deleting the file under `models/` will fetch it again."
)
"""And when the file is there but ONNX Runtime will not open it — a different problem with a different
answer, so a different sentence. Reachable only if somebody replaced the file: the download verifies its
checksum before moving anything into place."""


class ModelSource(Protocol):
    """Where the CLIP image tower comes from, injected so no test needs 85MiB of weights."""

    def ensure(self, stop_event: threading.Event) -> Path:
        """The model file on disk, fetching it if it is not there yet. Raises if it cannot be had.

        Called from the background thread only, and expected to look at `stop_event` while it works: the
        download is 85MiB and shutdown must not have to outlast it (invariant 12).
        """
        ...


class EmbeddingWriter(Protocol):
    """The write side of the cache, kept apart from `EmbeddingSource` so a test can read from a dict."""

    def store(self, wallpaper_id: str, vector: NDArray[np.float32]) -> None: ...

    def embedded_ids(self) -> set[str]: ...


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

    **It degrades to the baseline rather than to nothing, and that is what makes it safe to ship as the
    default.** A **Wallpaper** with no cached embedding falls back per pair, so a fresh install with no
    model downloaded yet behaves exactly as the baseline did, a half-filled cache behaves as a mixture,
    and nothing ever has to wait for a model to render a page. `notice(pool)` is what stops that being
    invisible.

    `model` and `embedder` are both injected so that the whole of this class can be tested with no model
    on disk and nothing downloaded: pass an `EmbeddingSource` of hand-written vectors for the maths, and a
    fake `ModelSource` plus a fake `Embedder` for the upkeep. `model=None` means this provider manages no
    model at all — it serves whatever its cache holds, keeps nothing up and reports nothing, which is the
    right shape for a unit test and for anything driven from a cache filled elsewhere.
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
        # The cache it *writes* to, which is the same object it reads from whenever that object can be
        # written to — an `EmbeddingCache` is both. Separate parameters because the read side is a
        # protocol a test satisfies with a dict, and a dict cannot be written back to.
        self._cache = cache if cache is not None else vectors if isinstance(vectors, EmbeddingCache) else None
        self._batch = batch
        # "Pending" means there is no way to embed anything yet — a model source and nothing to run
        # it with. An injected embedder is already a way, so a provider given one is at full strength
        # from the start and has nothing to tell the page.
        self._state: str | None = _PENDING if model is not None and embedder is None else None
        self._unreadable: set[str] = set()
        # The state is read on request threads and written on the background one, so it is behind a lock
        # for the same reason the refill's own status is.
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
        # `(1 + cosine) / 2` rather than a clip at zero: a negative cosine is a real statement — these two
        # images have less in common than two unrelated ones — and clipping it would throw that away by
        # calling it the same as merely unrelated.
        mapped = (1.0 + np.clip(cosine, -1.0, 1.0)) / 2.0

        both = pool_known[:, None] & decided_known[None, :]
        blended = np.where(both, mapped, baseline)
        return np.clip(blended, 0.0, 1.0).astype(np.float32)

    def catch_up(self, thumbnails: Path, stop_event: threading.Event) -> float:
        """Get the model, then embed one batch of thumbnails that have none yet.

        One batch per call rather than all of them, so the thread's `stop_event` is looked at between
        batches and shutdown never has to outlast a **Pool**-sized backlog (invariant 12). Returns 0.0
        while there is more to do, so the loop comes straight back, and `CAUGHT_UP` once the
        **Thumbnail cache** holds nothing this provider has not seen — the refill keeps adding to it, so
        "done" is only ever "done for now".

        **The Thumbnail cache is the work list**, and that is the whole answer to what happens to a
        **Wallpaper** with no thumbnail: it is not a file in that directory, so it never gets embedded,
        so every pair it appears in falls back to the baseline term — until the thumbnail downloader (#44)
        or a tile render fetches its thumbnail, after which this picks it up on the next pass. Nothing has
        to tell this provider what is in the **Pool**, which is what keeps it out of the database it knows
        nothing about.

        Never raises. A failed download is recorded and reported through `notice(pool)`; the loop that calls
        this has nothing to catch, exactly as `refill_loop` has nothing to catch.
        """
        if self._model is None or self._cache is None:
            return NOTHING_TO_CATCH_UP
        if self._embedder is None and not self._ready(stop_event):
            # A failed download is not retried every few seconds for ever: the common cause is being
            # offline, and the next restart is soon enough.
            return NOTHING_TO_CATCH_UP

        pending = self._pending(thumbnails)
        if not pending:
            return CAUGHT_UP
        self._embed(pending[: self._batch])
        return 0.0 if len(pending) > self._batch else CAUGHT_UP

    def notice(self, pool: Sequence[Wallpaper]) -> str | None:
        """What the page says while this provider is not yet itself, or `None` once it is.

        **Scores** computed from the fallback are indistinguishable from **Scores** computed properly, so
        a provider quietly running on colours alone would be a page quietly telling the user something
        else than it appears to. One line, in the words of somebody who did not read this module.

        A model that is pending, failed or unusable is the whole story, and says so. Once the model is
        working, the line is how much of `pool` has an **Embedding** (#44), until all of it has. A
        provider with no model to manage reports nothing, as it always has: nothing it does will move
        the count.
        """
        with self._lock:
            state = self._state
        if state is not None or self._model is None:
            return state
        embedded = len(self._vectors.vectors_for([w.id for w in pool]))
        if embedded >= len(pool):
            return None
        return _COVERAGE.format(embedded=embedded, pool=len(pool))

    def _ready(self, stop_event: threading.Event) -> bool:
        """Fetch and open the model, once. `False` means this stays on the baseline for now."""
        if self._model is None:
            return False
        try:
            path = self._model.ensure(stop_event)
        except _Cancelled:
            # Shutdown, not a failure. Saying so on a page nobody will load again would be wrong.
            return False
        except Exception as failure:  # see the docstring: this must never raise
            with self._lock:
                self._state = _FAILED.format(failure=failure)
            return False
        if stop_event.is_set():
            return False

        embedder = OnnxClipEmbedder(path)
        try:
            # Opened here rather than on the first thumbnail, so that a model file which is present but
            # not openable — replaced by hand, or a `.part` somebody renamed — is a sentence on the page
            # instead of every embedding silently failing behind a notice saying all is well. The cost is
            # loading 85MiB of weights on the background thread, which is where it belongs.
            embedder.warm()
        except Exception as failure:  # see the docstring: this must never raise
            with self._lock:
                self._state = _UNUSABLE.format(failure=failure)
            return False

        self._embedder = embedder
        with self._lock:
            self._state = None
        return True

    def _pending(self, thumbnails: Path) -> list[Path]:
        """Every cached thumbnail with no embedding yet, oldest first.

        Oldest first because the **Thumbnail cache** grows as the user works: taking the backlog in the
        order it arrived means a restart resumes rather than starts again somewhere else.
        """
        if self._cache is None or not thumbnails.is_dir():
            return []
        skip = self._cache.embedded_ids() | self._unreadable
        files = [path for path in thumbnails.iterdir() if path.is_file() and path.stem not in skip]
        return sorted(files, key=lambda path: path.stat().st_mtime)

    def _embed(self, batch: Sequence[Path]) -> None:
        """One batch through the model and into the cache. A file that will not open costs itself only."""
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
        """Retry a failed batch one file at a time, so a single bad thumbnail is the only casualty.

        A truncated or half-written thumbnail is a real case — the **Thumbnail cache** is written
        atomically, but a file can still be replaced by hand — and one of them must not be able to stop
        the **Pool** from ever being embedded.
        """
        if self._embedder is None or self._cache is None:
            return
        try:
            vectors = self._embedder([path])
        except Exception:  # a thumbnail Pillow cannot read is skipped, not fatal
            # Remembered so the pass that follows does not pick it up again and spin. In memory only: a
            # restart retries it, which is the right answer for a file that was being written as it was
            # read.
            self._unreadable.add(path.stem)
            return
        if len(vectors):
            self._cache.store(path.stem, vectors[0])


class OnnxClipEmbedder:
    """The real thing: a CLIP image tower run on the CPU by ONNX Runtime.

    Lazy in two ways on purpose. The session is built on the first call rather than in `__init__`, because
    loading 85MiB of weights is not something a constructor should do to a process that may never embed
    anything; and `onnxruntime` and `PIL` are imported inside the methods that need them rather than at the
    top of the file, because importing onnxruntime costs the best part of a second and the app starts, the
    page renders and the whole test suite runs without ever needing it.
    """

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
        """Open the session now rather than on the first thumbnail, and raise here if it will not open.

        Called once, from the background thread. Everything else about this class is lazy; this is the one
        place that deliberately is not, so that "the model is unusable" is discovered somewhere it can be
        reported rather than on every embedding for ever.
        """
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
            # `image_embeds` is the projected 512-vector CLIP compares images in; an export that only
            # offers `last_hidden_state` is handled by `_as_vectors` taking the class token instead.
            names = [output.name for output in session.get_outputs()]
            self._output_index = names.index("image_embeds") if "image_embeds" in names else 0
        return self._session


class DownloadedModel:
    """The image tower, fetched once from Hugging Face and verified against the pinned checksum.

    Not a Wallhaven **API call** and nothing to do with the rate limiter — a different host entirely,
    asked once ever, and never again once the file is on disk.

    **Verified rather than trusted, and only ever verified on the way in.** A download that came out
    wrong is deleted and raised about, so it shows up as a sentence on the page rather than as a
    mysteriously bad provider. The file already on disk is *not* re-hashed on every boot: that is 85MiB of
    reading to answer a question that only changes if somebody edits the file, and the cost would be paid
    at every start for ever. A corrupt file that got past the checksum fails when ONNX Runtime opens it,
    which is the same sentence on the same line.

    Written to a `.part` beside the destination and moved into place, so an interrupted download leaves
    nothing that looks like a model. A sibling, not `%TEMP%`, for invariant 10's reason: `os.replace` is
    only atomic within one filesystem.
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
        """The model on disk, downloading it if it is not there. Raises if it cannot be had."""
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
                        # Checked between chunks rather than only at the end, so a shutdown arriving
                        # during an 85MiB download is waited out for a megabyte and not for the file
                        # (invariant 12). A stop leaves the `.part` behind and the next start begins
                        # again, which costs a download nobody was going to use anyway.
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
    """Shutdown arrived mid download. Not a failure worth putting on the page — nobody will read it."""


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


def _chunked(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
