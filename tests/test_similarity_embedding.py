"""#14's embedding **Similarity provider**, and the cache behind it.

**No model and no `onnxruntime` anywhere in here.** Every vector below is written by hand, which is the
whole point of the `EmbeddingSource` seam: the arithmetic the comparison rests on — cosine, its mapping
into `[0, 1]`, the fallback, the diagonal — is checked in a millisecond and with nothing downloaded, and
what is left untested is the ONNX session and the preprocessing, which no test could check without the
85MiB file. That gap is named in the review note rather than papered over with a mock of a model.

Tested directly rather than through the Core service, for `test_similarity.py`'s reason: a **Similarity
provider** is one of the five injected dependencies, and every behaviour test of **Scoring** drives the
fake instead.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from tests.fakes import wallpaper
from wallpapi.similarity import MetadataSimilarityProvider
from wallpapi.similarity_embedding import EmbeddingCache, EmbeddingSimilarityProvider


def vector(*values: float) -> NDArray[np.float32]:
    """A unit-length vector in the direction given, since the provider assumes normalised rows."""
    raw = np.array(values, dtype=np.float32)
    return (raw / np.linalg.norm(raw)).astype(np.float32)


EAST = vector(1.0, 0.0)
NORTH = vector(0.0, 1.0)
NORTH_EAST = vector(1.0, 1.0)
WEST = vector(-1.0, 0.0)


class VectorsByHand:
    """An `EmbeddingSource` over a dict, so a test states the geometry instead of running a model."""

    def __init__(self, vectors: Mapping[str, NDArray[np.float32]]) -> None:
        self._vectors = dict(vectors)

    def vectors_for(self, wallpaper_ids: Sequence[str]) -> Mapping[str, NDArray[np.float32]]:
        return {w: self._vectors[w] for w in wallpaper_ids if w in self._vectors}


def provider(vectors: Mapping[str, NDArray[np.float32]]) -> EmbeddingSimilarityProvider:
    return EmbeddingSimilarityProvider(VectorsByHand(vectors))


def test_the_matrix_is_pool_by_decided_and_every_value_is_a_similarity() -> None:
    pool = [wallpaper("a"), wallpaper("b"), wallpaper("c")]
    decided = [wallpaper("a"), wallpaper("d")]
    matrix = provider({"a": EAST, "b": NORTH, "c": WEST, "d": NORTH_EAST}).similarities(pool, decided)

    assert matrix.shape == (3, 2)
    assert matrix.dtype == np.float32
    assert np.all(matrix >= 0.0) and np.all(matrix <= 1.0)


def test_a_wallpaper_against_itself_is_one() -> None:
    """The fixed point the protocol promises. A cosine of 1.0 maps to 1.0, which is what makes a
    **Favourite** still in the **Pool** score its own +100 at full weight."""
    pool = [wallpaper("a"), wallpaper("b"), wallpaper("unembedded")]
    matrix = provider({"a": EAST, "b": NORTH}).similarities(pool, pool)

    assert [float(matrix[n, n]) for n in range(3)] == pytest.approx([1.0, 1.0, 1.0])
    assert np.all(matrix <= 1.0)


def test_the_cosine_is_mapped_into_the_range_rather_than_clipped_into_it() -> None:
    """`(1 + cosine) / 2`, over the three cosines whose value is obvious.

    Monotone, so it changes no ordering, and it keeps the difference between "unrelated" and "opposite"
    instead of flattening both to zero — which a clip at zero would do, and which would tell the **Score**
    maths that an image with nothing in common and an image that is the reverse of this one are the same
    distance away.
    """
    pool = [wallpaper("same"), wallpaper("right_angle"), wallpaper("opposite")]
    decided = [wallpaper("subject")]

    matrix = provider({"subject": EAST, "same": EAST, "right_angle": NORTH, "opposite": WEST}).similarities(
        pool, decided
    )

    assert [float(matrix[n, 0]) for n in range(3)] == pytest.approx([1.0, 0.5, 0.0])


def test_a_nearer_direction_scores_higher_than_a_further_one() -> None:
    """The only property the **Score** maths actually depends on: the ordering."""
    pool = [wallpaper("near"), wallpaper("far")]
    decided = [wallpaper("subject")]

    matrix = provider({"subject": EAST, "near": NORTH_EAST, "far": NORTH}).similarities(pool, decided)

    assert float(matrix[0, 0]) > float(matrix[1, 0])


def test_a_wallpaper_with_no_cached_embedding_falls_back_to_the_baseline() -> None:
    """A **Pool** grows faster than it can be embedded, so most of it has no vector for a while. A 0.0
    there would say the **Wallpaper** is unlike every **Ban** as well as every **Favourite**."""
    pool = [wallpaper("unembedded"), wallpaper("embedded")]
    decided = [wallpaper("subject")]
    baseline = MetadataSimilarityProvider().similarities(pool, decided)

    matrix = provider({"embedded": WEST, "subject": EAST}).similarities(pool, decided)

    assert float(matrix[0, 0]) == float(baseline[0, 0])
    assert float(matrix[1, 0]) == 0.0


def test_nothing_embedded_at_all_is_the_baseline_matrix() -> None:
    pool = [wallpaper("a")]
    decided = [wallpaper("b")]
    baseline = MetadataSimilarityProvider().similarities(pool, decided)

    assert provider({}).similarities(pool, decided) == pytest.approx(baseline)


def test_an_empty_decided_set_is_an_empty_matrix_rather_than_a_failure() -> None:
    assert provider({"a": EAST}).similarities([wallpaper("a")], []).shape == (1, 0)


def test_a_vector_of_the_wrong_width_is_treated_as_absent() -> None:
    """A cache written by one model and read by another. The vectors are not comparable, and a length that
    disagrees is the one case that can be caught for nothing."""
    pool = [wallpaper("three_dimensional"), wallpaper("two_dimensional")]
    decided = [wallpaper("subject")]
    baseline = MetadataSimilarityProvider().similarities(pool, decided)

    matrix = provider(
        {"subject": EAST, "two_dimensional": EAST, "three_dimensional": vector(1.0, 0.0, 0.0)}
    ).similarities(pool, decided)

    assert float(matrix[0, 0]) == float(baseline[0, 0])
    assert float(matrix[1, 0]) == pytest.approx(1.0)


def test_the_vectors_are_its_rows_in_pool_order_with_zeros_for_no_embedding() -> None:
    """What the varied **Unknown** draw clusters on (#45): `len(pool)` x d, never **Pool** x **Pool**, and a
    row of zeros where there is no **Embedding** — none cached, or one of a width this cache does not use."""
    pool = [wallpaper("b"), wallpaper("unembedded"), wallpaper("a"), wallpaper("three_dimensional")]

    rows = provider({"a": EAST, "b": NORTH, "three_dimensional": vector(1.0, 0.0, 0.0)}).vectors(pool)

    assert rows is not None
    assert rows.shape == (4, 2)
    assert rows.tolist() == [NORTH.tolist(), [0.0, 0.0], EAST.tolist(), [0.0, 0.0]]


def test_nothing_embedded_is_every_row_without_an_embedding() -> None:
    rows = provider({}).vectors([wallpaper("a"), wallpaper("b")])

    assert rows is not None
    assert len(rows) == 2
    assert not np.any(rows)


# -- the cache -----------------------------------------------------------------------------------------


def test_the_cache_hands_back_a_unit_vector_it_can_dot(tmp_path: Path) -> None:
    """Normalising on the way in is what lets every reader take a dot product and call it a cosine."""
    cache = EmbeddingCache(tmp_path / "embeddings.db")
    cache.store("a", np.array([3.0, 4.0], dtype=np.float32))

    stored = cache.vectors_for(["a"])["a"]

    assert stored == pytest.approx(np.array([0.6, 0.8], dtype=np.float32))
    assert float(np.linalg.norm(stored)) == pytest.approx(1.0)


def test_the_cache_survives_being_reopened(tmp_path: Path) -> None:
    """Permanent is the point: an embedding cost a model run, and the cache is what stops it costing two."""
    path = tmp_path / "embeddings.db"
    EmbeddingCache(path).store("a", EAST)

    assert EmbeddingCache(path).vectors_for(["a"])["a"] == pytest.approx(EAST)


def test_re_embedding_replaces_the_vector(tmp_path: Path) -> None:
    cache = EmbeddingCache(tmp_path / "embeddings.db")
    cache.store("a", EAST)
    cache.store("a", NORTH)

    assert cache.vectors_for(["a"])["a"] == pytest.approx(NORTH)


def test_an_unknown_wallpaper_is_simply_absent(tmp_path: Path) -> None:
    cache = EmbeddingCache(tmp_path / "embeddings.db")

    assert cache.vectors_for(["nobody"]) == {}
    assert cache.embedded_ids() == set()


def test_the_cache_serves_the_provider_it_was_built_for(tmp_path: Path) -> None:
    """The one test that puts the halves together, so `EmbeddingSource` is not only asserted against a
    hand-written stand-in."""
    cache = EmbeddingCache(tmp_path / "embeddings.db")
    cache.store("a", EAST)
    cache.store("b", NORTH)

    matrix = EmbeddingSimilarityProvider(cache).similarities([wallpaper("a")], [wallpaper("b")])

    assert float(matrix[0, 0]) == pytest.approx(0.5)
