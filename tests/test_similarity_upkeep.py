"""Which provider the app runs, and what the embedding provider does on the background thread.

No model and no network: the `ModelSource` is a stand-in that hands back a path or raises, and the
`Embedder` returns vectors written here. The real `DownloadedModel` and `OnnxClipEmbedder` are the two
pieces left uncovered. One test imports `onnxruntime` on purpose, handing it bytes that are not a graph:
the failure it pins would otherwise be invisible.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from tests.conftest import make_harness
from tests.fakes import catalogue_of, wallpaper
from wallpapi.main import build_similarity
from wallpapi.similarity import NOTHING_TO_CATCH_UP, MetadataSimilarityProvider
from wallpapi.similarity_embedding import CAUGHT_UP, EmbeddingCache, EmbeddingSimilarityProvider

EAST = np.array([1.0, 0.0], dtype=np.float32)
NORTH = np.array([0.0, 1.0], dtype=np.float32)


# -- selection ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (None, EmbeddingSimilarityProvider),
        ("metadata", MetadataSimilarityProvider),
        ("embedding", EmbeddingSimilarityProvider),
    ],
)
def test_each_name_selects_its_provider_and_unset_is_embedding(
    name: str | None, expected: type[object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both look identical from the page, so a typo wiring the baseline back in would be silent."""
    if name is None:
        monkeypatch.delenv("WALLPAPI_SIMILARITY", raising=False)
    else:
        monkeypatch.setenv("WALLPAPI_SIMILARITY", name)

    assert isinstance(build_similarity(tmp_path), expected)


def test_an_unrecognised_name_refuses_rather_than_falling_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WALLPAPI_SIMILARITY", "clip")

    with pytest.raises(ValueError, match="metadata or embedding"):
        build_similarity(tmp_path)


def test_selecting_a_provider_touches_no_disk_until_it_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise every boot leaves empty databases behind and reaches for 85MiB before it has started."""
    monkeypatch.setenv("WALLPAPI_SIMILARITY", "embedding")
    build_similarity(tmp_path)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", ["metadata"])
def test_the_providers_that_read_no_thumbnails_keep_nothing_up_and_say_nothing(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WALLPAPI_SIMILARITY", name)
    provider = build_similarity(tmp_path)

    assert provider.catch_up(tmp_path, threading.Event()) == NOTHING_TO_CATCH_UP
    assert provider.notice([wallpaper("a"), wallpaper("b")]) is None


def test_the_provider_is_asked_about_the_whole_pool(db_path: Path) -> None:
    """`notice` takes the **Pool**, the shape `similarities` takes, so the Core service need not know
    which provider it holds."""
    harness = make_harness(db_path, catalogue=catalogue_of(24))

    harness.core.similarity_notice()

    assert harness.similarity.notice_pools == [tuple(w.id for w in catalogue_of(24))]


# -- the embedding provider's upkeep -----------------------------------------------------------------


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
    """A `ModelSource` that cannot be had: no network, or a checksum that did not match."""

    def __init__(self, message: str = "connection refused") -> None:
        self.message = message

    def ensure(self, stop_event: threading.Event) -> Path:
        del stop_event
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
        # Distinct mtimes, so "oldest first" does not depend on the filesystem's clock resolution.
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


def not_a_model(tmp_path: Path) -> ModelOnDisk:
    path = tmp_path / "m.onnx"
    path.write_bytes(b"this is not an onnx graph")
    return ModelOnDisk(path)


def test_a_provider_with_no_model_source_keeps_nothing_up() -> None:
    """What the unit tests and a cache filled elsewhere both want: serve what is there, do nothing."""
    bare = EmbeddingSimilarityProvider(EmbeddingCache(Path("unused")))

    assert bare.catch_up(Path("unused"), threading.Event()) == NOTHING_TO_CATCH_UP
    assert bare.notice([]) is None


def test_before_the_model_arrives_the_page_is_told_and_coverage_waits(tmp_path: Path) -> None:
    """A **Score** from the fallback looks exactly like one from the model, so the difference is said out
    loud. Coverage is how far the model has got, so it says nothing before there is a model."""
    embedding, _ = provider(tmp_path, model=ModelOnDisk(tmp_path / "model.onnx"))

    empty = embedding.notice([])
    notice = embedding.notice([wallpaper("a")])

    assert empty is not None
    assert "colour and category" in empty
    assert notice is not None
    assert "still starting up" in notice
    assert "Pool wallpapers" not in notice


def test_catching_up_embeds_what_is_in_the_thumbnail_cache_and_later_arrivals(tmp_path: Path) -> None:
    """A **Wallpaper** with no thumbnail is not in the work list, so it falls back to the baseline until
    its thumbnail arrives, and the next pass picks it up."""
    embedder = CountingEmbedder()
    embedding, cache = provider(tmp_path, model=ModelOnDisk(tmp_path / "m.onnx"), embedder=embedder)
    directory = thumbnails(tmp_path / "thumbs", "one", "two")

    embedding.catch_up(directory, threading.Event())
    assert embedder.seen == ["one", "two"]
    assert cache.embedded_ids() == {"one", "two"}

    thumbnails(directory, "arrived_later")
    embedding.catch_up(directory, threading.Event())

    assert cache.embedded_ids() == {"one", "two", "arrived_later"}


def test_catching_up_takes_one_batch_at_a_time_and_asks_to_come_straight_back(tmp_path: Path) -> None:
    """One batch per call is how the thread's `stop_event` gets looked at during a backlog (invariant
    12); the 0.0 is how the loop knows not to sleep on the way through it."""
    embedder = CountingEmbedder()
    embedding, cache = provider(tmp_path, model=ModelOnDisk(tmp_path / "m.onnx"), embedder=embedder, batch=2)
    directory = thumbnails(tmp_path / "thumbs", "a", "b", "c")

    assert embedding.catch_up(directory, threading.Event()) == 0.0
    assert cache.embedded_ids() == {"a", "b"}

    assert embedding.catch_up(directory, threading.Event()) == CAUGHT_UP
    assert cache.embedded_ids() == {"a", "b", "c"}
    assert embedder.batches == [2, 1]


def test_an_empty_thumbnail_cache_is_caught_up_rather_than_an_error(tmp_path: Path) -> None:
    embedding, _ = provider(tmp_path, model=ModelOnDisk(tmp_path / "m.onnx"), embedder=CountingEmbedder())

    assert embedding.catch_up(tmp_path / "never_created", threading.Event()) == CAUGHT_UP


def test_a_model_file_that_will_not_open_says_so_rather_than_failing_silently(tmp_path: Path) -> None:
    """Only reachable if somebody replaced the file after its checksum was verified, but otherwise every
    embedding fails behind a notice that says all is well. It outranks coverage."""
    embedding, cache = provider(tmp_path, model=not_a_model(tmp_path))

    embedding.catch_up(thumbnails(tmp_path / "thumbs", "one"), threading.Event())

    notice = embedding.notice([wallpaper("a")])
    assert notice is not None
    assert "could not open its model" in notice
    assert "Pool wallpapers" not in notice
    assert cache.embedded_ids() == set()


def test_a_failed_download_says_why_keeps_the_baseline_and_is_not_retried(tmp_path: Path) -> None:
    """ "Could not reach it" and "the file was not what it should be" are different problems, so the line
    names the reason. Not retried: the usual cause is being offline, and the next restart is soon enough."""
    embedding, cache = provider(tmp_path, model=ModelThatFails("connection refused"))

    wait = embedding.catch_up(thumbnails(tmp_path / "thumbs", "one"), threading.Event())

    assert wait == NOTHING_TO_CATCH_UP
    notice = embedding.notice([wallpaper("a")])
    assert notice is not None
    assert "connection refused" in notice
    assert "Pool wallpapers" not in notice
    assert cache.embedded_ids() == set()
    assert float(embedding.similarities([wallpaper("a")], [wallpaper("a")])[0, 0]) == pytest.approx(1.0)


def test_an_injected_embedder_means_the_model_is_never_asked_for(tmp_path: Path) -> None:
    model = ModelOnDisk(tmp_path / "m.onnx")
    embedding, _ = provider(tmp_path, model=model, embedder=CountingEmbedder())
    directory = thumbnails(tmp_path / "thumbs", "one")

    embedding.catch_up(directory, threading.Event())
    embedding.catch_up(directory, threading.Event())

    assert model.calls == 0


def test_a_thumbnail_that_will_not_open_costs_only_itself(tmp_path: Path) -> None:
    """It must not stop the rest being embedded, be picked up again every pass, or hold the coverage line
    on the page for ever: it is out of the count and out of the total."""
    embedder = CountingEmbedder(unreadable=["broken"])
    embedding, cache = provider(tmp_path, model=ModelOnDisk(tmp_path / "m.onnx"), embedder=embedder)
    directory = thumbnails(tmp_path / "thumbs", "broken", "fine")

    embedding.catch_up(directory, threading.Event())

    assert cache.embedded_ids() == {"fine"}
    assert embedding.catch_up(directory, threading.Event()) == CAUGHT_UP
    assert embedding.notice([wallpaper("broken"), wallpaper("fine")]) is None
    notice = embedding.notice([wallpaper("broken"), wallpaper("fine"), wallpaper("waiting")])
    assert notice is not None
    assert "1 of 2 Pool wallpapers" in notice


def test_an_embedded_wallpaper_stops_using_the_baseline(tmp_path: Path) -> None:
    """Same category and palette, so the baseline would call these identical; their vectors are at a right
    angle, which maps to 0.5."""
    embedding, cache = provider(tmp_path, model=ModelOnDisk(tmp_path / "m.onnx"), embedder=CountingEmbedder())
    embedding.catch_up(thumbnails(tmp_path / "thumbs", "a"), threading.Event())
    cache.store("b", NORTH)

    matrix = embedding.similarities([wallpaper("a")], [wallpaper("b")])

    assert float(matrix[0, 0]) == pytest.approx(0.5)


def test_the_page_is_told_how_much_of_the_pool_is_embedded(tmp_path: Path) -> None:
    """Counted against the **Pool** only, so a retired **Wallpaper** with an **Embedding** does not count,
    and in thousands the way a person reads them."""
    embedding, cache = provider(tmp_path, model=ModelOnDisk(tmp_path / "m.onnx"), embedder=CountingEmbedder())
    cache.store("a", EAST)
    cache.store("b", NORTH)
    cache.store("retired", EAST)

    partial = embedding.notice([wallpaper("a"), wallpaper("b"), wallpaper("c")])
    large = embedding.notice([wallpaper(f"w{n:04d}") for n in range(1200)])

    assert partial is not None
    assert "2 of 3 Pool wallpapers" in partial
    assert large is not None
    assert "0 of 1,200 Pool wallpapers" in large


def test_a_wholly_embedded_pool_adds_no_line(tmp_path: Path) -> None:
    """Nothing extra once coverage is complete, and an empty **Pool** is not short of anything."""
    embedding, cache = provider(tmp_path, model=ModelOnDisk(tmp_path / "m.onnx"), embedder=CountingEmbedder())
    cache.store("a", EAST)
    cache.store("b", NORTH)

    assert embedding.notice([wallpaper("a"), wallpaper("b")]) is None
    assert embedding.notice([]) is None
