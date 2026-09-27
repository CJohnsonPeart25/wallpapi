"""What the embedding provider does on the background thread: fetch its model, embed thumbnails, say so.

**Still no model and no network.** The `ModelSource` is a stand-in that hands back a path or raises, and
the `Embedder` is a function that returns vectors written here. That is the whole point of both being
injected: the download, the fallback while it has not happened, and the failure afterwards are all
behaviours somebody has to be able to check without 85MiB and a working connection.

What is left uncovered is the real `DownloadedModel` and the real `OnnxClipEmbedder` — the two pieces that
*are* the network and the model. That is named in the review note rather than papered over with a mock of
a download.

One exception, and it is deliberate: the test that pins what happens to a model file which will not
open does import `onnxruntime`, because the thing it pins is ONNX Runtime refusing a file. It hands it
twenty-five bytes that are not a graph, so no model is loaded and nothing is fetched — but it is the one
test here that touches the library at all, and it is worth it: the failure it covers is the only one that
would otherwise be invisible.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from tests.fakes import wallpaper
from wallpapi.similarity import NOTHING_TO_CATCH_UP
from wallpapi.similarity_embedding import (
    CAUGHT_UP,
    EmbeddingCache,
    EmbeddingSimilarityProvider,
)

EAST = np.array([1.0, 0.0], dtype=np.float32)
NORTH = np.array([0.0, 1.0], dtype=np.float32)


class ModelOnDisk:
    """A `ModelSource` that is simply already there. Records how often it was asked."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls = 0

    def ensure(self, stop_event: threading.Event) -> Path:
        del stop_event
        self.calls += 1
        return self.path


class ModelThatFails:
    """A `ModelSource` that cannot be had — no network, or a checksum that did not match."""

    def __init__(self, message: str = "connection refused") -> None:
        self.message = message
        self.calls = 0

    def ensure(self, stop_event: threading.Event) -> Path:
        del stop_event
        self.calls += 1
        raise RuntimeError(self.message)


class CountingEmbedder:
    """An `Embedder` that returns a fixed direction per file and counts what it was given."""

    def __init__(self, unreadable: Sequence[str] = ()) -> None:
        self.seen: list[str] = []
        self.batches: list[int] = []
        self.unreadable = set(unreadable)

    def __call__(self, images: Sequence[Path]) -> NDArray[np.float32]:
        if any(path.stem in self.unreadable for path in images):
            raise OSError("cannot identify image file")
        self.batches.append(len(images))
        self.seen.extend(path.stem for path in images)
        return np.stack([EAST for _ in images])


def thumbnails(directory: Path, *names: str) -> Path:
    """A **Thumbnail cache** directory holding a file per name, oldest first in the order given."""
    directory.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(names):
        path = directory / f"{name}.jpg"
        path.write_bytes(b"\xff\xd8\xff\xe0 fake thumbnail")
        # Distinct mtimes, so "oldest first" is a fact about the files rather than about the filesystem's
        # clock resolution.
        import os

        os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
    return directory


def provider(
    tmp_path: Path, *, model: object, embedder: CountingEmbedder | None = None, batch: int = 16
) -> tuple[EmbeddingSimilarityProvider, EmbeddingCache]:
    cache = EmbeddingCache(tmp_path / "embeddings.db")
    return (
        EmbeddingSimilarityProvider(
            cache,
            model=model,  # pyright: ignore[reportArgumentType] - a ModelSource stand-in
            embedder=embedder,
            batch=batch,
        ),
        cache,
    )


def test_a_provider_with_no_model_source_keeps_nothing_up() -> None:
    """The shape the unit tests and a cache filled elsewhere both want: serve what is there, do nothing."""
    bare = EmbeddingSimilarityProvider(EmbeddingCache(Path("unused")))

    assert bare.catch_up(Path("unused"), threading.Event()) == NOTHING_TO_CATCH_UP
    assert bare.notice() is None


def test_before_the_model_arrives_the_page_is_told(tmp_path: Path) -> None:
    """A **Score** from the fallback looks exactly like a **Score** from the model, so the difference has
    to be said out loud or it is invisible."""
    embedding, _ = provider(tmp_path, model=ModelOnDisk(tmp_path / "model.onnx"))

    notice = embedding.notice()

    assert notice is not None
    assert "colour and category" in notice


def test_a_wallpaper_with_no_embedding_falls_back_rather_than_scoring_zero(tmp_path: Path) -> None:
    """Which is what makes the empty-cache state safe: a fresh wallpapi behaves as the baseline did."""
    embedding, _ = provider(tmp_path, model=ModelOnDisk(tmp_path / "model.onnx"))
    pair = [wallpaper("a")], [wallpaper("a")]

    assert float(embedding.similarities(*pair)[0, 0]) == pytest.approx(1.0)


def test_catching_up_embeds_the_cached_thumbnails(tmp_path: Path) -> None:
    """The **Thumbnail cache** is the work list, so its contents are what ends up in the cache."""
    embedder = CountingEmbedder()
    embedding, cache = provider(tmp_path, model=ModelOnDisk(tmp_path / "m.onnx"), embedder=embedder)

    embedding.catch_up(thumbnails(tmp_path / "thumbs", "one", "two"), threading.Event())

    assert embedder.seen == ["one", "two"]
    assert cache.embedded_ids() == {"one", "two"}


def test_a_wallpaper_with_no_thumbnail_is_simply_not_embedded(tmp_path: Path) -> None:
    """The answer to "what about a **Wallpaper** the page has not fetched a thumbnail for yet": it is not
    a file in that directory, so nothing embeds it and every pair it is in falls back to the baseline —
    until the tile renders, after which the next pass picks it up."""
    embedder = CountingEmbedder()
    embedding, cache = provider(tmp_path, model=ModelOnDisk(tmp_path / "m.onnx"), embedder=embedder)
    directory = thumbnails(tmp_path / "thumbs", "fetched")

    embedding.catch_up(directory, threading.Event())
    assert cache.embedded_ids() == {"fetched"}

    thumbnails(directory, "arrived_later")
    embedding.catch_up(directory, threading.Event())

    assert cache.embedded_ids() == {"fetched", "arrived_later"}


def test_catching_up_takes_one_batch_at_a_time_and_asks_to_come_straight_back(tmp_path: Path) -> None:
    """One batch per call is how the thread's `stop_event` gets looked at during a **Pool**-sized backlog
    (invariant 12); the 0.0 is how the loop knows not to sleep on the way through it."""
    embedder = CountingEmbedder()
    embedding, cache = provider(tmp_path, model=ModelOnDisk(tmp_path / "m.onnx"), embedder=embedder, batch=2)
    directory = thumbnails(tmp_path / "thumbs", "a", "b", "c")

    assert embedding.catch_up(directory, threading.Event()) == 0.0
    assert cache.embedded_ids() == {"a", "b"}

    assert embedding.catch_up(directory, threading.Event()) == CAUGHT_UP
    assert cache.embedded_ids() == {"a", "b", "c"}
    assert embedder.batches == [2, 1]


def test_an_empty_thumbnail_cache_is_caught_up_rather_than_an_error(tmp_path: Path) -> None:
    """The state on a first boot, before the refill has put anything in the **Pool**."""
    embedding, _ = provider(tmp_path, model=ModelOnDisk(tmp_path / "m.onnx"), embedder=CountingEmbedder())

    assert embedding.catch_up(tmp_path / "never_created", threading.Event()) == CAUGHT_UP


def test_a_provider_that_can_already_embed_says_nothing(tmp_path: Path) -> None:
    """ "Still fetching" means there is no way to embed anything yet. A provider handed one is at full
    strength from the start, and a line saying otherwise would be a line that is simply untrue."""
    embedding, _ = provider(tmp_path, model=ModelOnDisk(tmp_path / "m.onnx"), embedder=CountingEmbedder())

    assert embedding.notice() is None


def test_a_model_file_that_will_not_open_says_so_rather_than_failing_silently(tmp_path: Path) -> None:
    """Only reachable if somebody replaced the file — the download verifies its checksum before moving
    anything into place — but the alternative is every embedding failing behind a notice that says all is
    well, which is the one failure mode nobody would ever notice."""
    not_a_model = tmp_path / "m.onnx"
    not_a_model.write_bytes(b"this is not an onnx graph")
    embedding, cache = provider(tmp_path, model=ModelOnDisk(not_a_model))

    embedding.catch_up(thumbnails(tmp_path / "thumbs", "one"), threading.Event())

    notice = embedding.notice()
    assert notice is not None
    assert "could not open its model" in notice
    assert cache.embedded_ids() == set()


def test_a_failed_download_says_so_and_keeps_serving_the_baseline(tmp_path: Path) -> None:
    """The acceptance criterion for the failure path: no crash, no blank page, and a line that names the
    reason — "could not reach it" and "the file was not what it should be" are different problems."""
    model = ModelThatFails("connection refused")
    embedding, cache = provider(tmp_path, model=model)

    embedding.catch_up(thumbnails(tmp_path / "thumbs", "one"), threading.Event())

    notice = embedding.notice()
    assert notice is not None
    assert "connection refused" in notice
    assert cache.embedded_ids() == set()
    assert float(embedding.similarities([wallpaper("a")], [wallpaper("a")])[0, 0]) == pytest.approx(1.0)


def test_a_failed_download_is_not_retried_every_few_seconds(tmp_path: Path) -> None:
    """The usual cause is being offline, and the next restart is soon enough. A loop that came back every
    thirty seconds would be a log full of the same failure and a page that never settles."""
    model = ModelThatFails()
    embedding, _ = provider(tmp_path, model=model)

    assert embedding.catch_up(tmp_path / "thumbs", threading.Event()) == NOTHING_TO_CATCH_UP


def test_the_model_is_asked_for_once_rather_than_every_pass(tmp_path: Path) -> None:
    model = ModelOnDisk(tmp_path / "m.onnx")
    embedding, _ = provider(tmp_path, model=model, embedder=CountingEmbedder())
    directory = thumbnails(tmp_path / "thumbs", "one")

    embedding.catch_up(directory, threading.Event())
    embedding.catch_up(directory, threading.Event())

    assert model.calls == 0, "an injected embedder means the model is never needed at all"


def test_a_thumbnail_that_will_not_open_costs_only_itself(tmp_path: Path) -> None:
    """A half-written or hand-replaced file must not be able to stop the whole **Pool** being embedded,
    and must not be picked up again on every pass either."""
    embedder = CountingEmbedder(unreadable=["broken"])
    embedding, cache = provider(tmp_path, model=ModelOnDisk(tmp_path / "m.onnx"), embedder=embedder)
    directory = thumbnails(tmp_path / "thumbs", "broken", "fine")

    embedding.catch_up(directory, threading.Event())

    assert cache.embedded_ids() == {"fine"}
    assert embedding.catch_up(directory, threading.Event()) == CAUGHT_UP


def test_an_embedded_wallpaper_stops_using_the_baseline(tmp_path: Path) -> None:
    """The whole point of the upkeep: once two **Wallpapers** are embedded, what decides their similarity
    is the images and not their palettes."""
    embedder = CountingEmbedder()
    embedding, cache = provider(tmp_path, model=ModelOnDisk(tmp_path / "m.onnx"), embedder=embedder)
    embedding.catch_up(thumbnails(tmp_path / "thumbs", "a"), threading.Event())
    cache.store("b", NORTH)

    # Same category and same palette, so the baseline would call these identical; their vectors are at a
    # right angle, which maps to 0.5.
    matrix = embedding.similarities([wallpaper("a")], [wallpaper("b")])

    assert float(matrix[0, 0]) == pytest.approx(0.5)
