"""The three **Similarity providers** and the two caches behind them, tested directly.

A provider is one of the five injected dependencies, so these are unit tests of the protocol's contract and
of each provider's arithmetic; every **Scoring** test drives the fake instead. No model, no `onnxruntime`
and no network: vectors and tag sets are written by hand, and the caches are files under `tmp_path`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from tests.fakes import wallpaper
from wallpapi.similarity import CATEGORY_SHARE, MetadataSimilarityProvider, SimilarityProvider
from wallpapi.similarity_embedding import EmbeddingCache, EmbeddingSimilarityProvider
from wallpapi.similarity_tags import TAG_SHARE, TagCache, TagSimilarityProvider
from wallpapi.wallhaven import Tag

RED = ("#ff0000", "#880000")
NEARLY_RED = ("#fa0505", "#850303")
BLUE_AND_WHITE = ("#0000ff", "#ffffff")
FETCHED_AT = dt.datetime(2026, 9, 27, 12, 0, 0, tzinfo=dt.UTC)

baseline = MetadataSimilarityProvider()


def vector(*values: float) -> NDArray[np.float32]:
    """A unit-length vector in the direction given, since the provider assumes normalised rows."""
    raw = np.array(values, dtype=np.float32)
    return (raw / np.linalg.norm(raw)).astype(np.float32)


EAST = vector(1.0, 0.0)
NORTH = vector(0.0, 1.0)
NORTH_EAST = vector(1.0, 1.0)
WEST = vector(-1.0, 0.0)
DIRECTIONS = (EAST, NORTH, NORTH_EAST, WEST)


class VectorsByHand:
    def __init__(self, vectors: Mapping[str, NDArray[np.float32]]) -> None:
        self._vectors = dict(vectors)

    def vectors_for(self, wallpaper_ids: Sequence[str]) -> Mapping[str, NDArray[np.float32]]:
        return {w: self._vectors[w] for w in wallpaper_ids if w in self._vectors}


class TagsByHand:
    def __init__(self, tags: Mapping[str, tuple[int, ...]]) -> None:
        self._tags = dict(tags)

    def tags_for(self, wallpaper_ids: Sequence[str]) -> Mapping[str, tuple[int, ...]]:
        return {w: self._tags[w] for w in wallpaper_ids if w in self._tags}


def embedding(vectors: Mapping[str, NDArray[np.float32]]) -> EmbeddingSimilarityProvider:
    return EmbeddingSimilarityProvider(VectorsByHand(vectors))


def tagged(tags: Mapping[str, tuple[int, ...]]) -> TagSimilarityProvider:
    return TagSimilarityProvider(TagsByHand(tags))


def _metadata_knowing(ids: Sequence[str]) -> SimilarityProvider:
    del ids
    return MetadataSimilarityProvider()


def _embedding_knowing(ids: Sequence[str]) -> SimilarityProvider:
    return embedding({w: DIRECTIONS[n % len(DIRECTIONS)] for n, w in enumerate(ids)})


def _tags_knowing(ids: Sequence[str]) -> SimilarityProvider:
    return tagged({w: (n, n + 1) for n, w in enumerate(ids)})


PROVIDERS = {"metadata": _metadata_knowing, "embedding": _embedding_knowing, "tags": _tags_knowing}
"""Each provider, given data (vectors, tags) for the IDs named and none for the rest."""


# -- the contract every provider keeps ---------------------------------------------------------------


@pytest.mark.parametrize("name", PROVIDERS)
def test_every_provider_keeps_the_protocol_contract(name: str) -> None:
    """Invariant 2's shape (**Pool** x decided, never **Pool** x **Pool**, empty either side an ordinary
    answer), float32 inside [0, 1], and 1.0 on the diagonal, including for a **Wallpaper** the provider has
    no data for: the fallback must not cost a decided **Wallpaper** its own weight."""
    pool = [wallpaper("a", colours=RED), wallpaper("b", colours=BLUE_AND_WHITE), wallpaper("nodata")]
    decided = [wallpaper("c", colours=RED), wallpaper("d", colours=BLUE_AND_WHITE)]
    provider = PROVIDERS[name](["a", "b", "c", "d"])

    matrix = provider.similarities(pool, decided)
    assert matrix.shape == (3, 2)
    assert matrix.dtype == np.float32
    assert np.all((matrix >= 0.0) & (matrix <= 1.0))
    assert provider.similarities(pool, []).shape == (3, 0)
    assert provider.similarities([], decided).shape == (0, 2)

    itself = provider.similarities(pool, pool)
    assert [float(itself[n, n]) for n in range(3)] == pytest.approx([1.0, 1.0, 1.0])
    assert np.all(itself <= 1.0)


@pytest.mark.parametrize("name", ["metadata", "tags"])
def test_a_provider_with_no_positions_has_no_vectors(name: str) -> None:
    """The varied **Unknown** draw then falls back to the seeded shuffle."""
    assert PROVIDERS[name](["a"]).vectors([wallpaper("a"), wallpaper("b")]) is None


@pytest.mark.parametrize("name", ["embedding", "tags"])
@pytest.mark.parametrize("missing", ["pool", "decided"])
def test_a_side_with_no_data_falls_back_to_the_baseline(name: str, missing: str) -> None:
    """A **Pool** fills faster than it can be embedded or tagged. A 0.0 there would say the **Wallpaper**
    is unlike every **Ban** as well as every **Favourite**."""
    pool = [wallpaper("p")]
    decided = [wallpaper("d")]
    provider = PROVIDERS[name](["d"] if missing == "pool" else ["p"])

    assert provider.similarities(pool, decided) == pytest.approx(baseline.similarities(pool, decided))


# -- metadata ----------------------------------------------------------------------------------------


def _one(pool_colours: tuple[str, ...], decided_colours: tuple[str, ...], *, categories: bool) -> float:
    matrix = baseline.similarities(
        [wallpaper("aaaaaa", colours=pool_colours, category="general")],
        [wallpaper("bbbbbb", colours=decided_colours, category="general" if categories else "anime")],
    )
    return float(matrix[0, 0])


def test_the_baseline_sees_only_palette_and_category() -> None:
    """Two **Wallpapers** it cannot tell apart are as alike as one is to itself; nothing in common is zero,
    never negative, since a **Score** is built from `1 - similarity`."""
    assert _one(RED, RED, categories=True) == pytest.approx(1.0)
    assert _one(RED, BLUE_AND_WHITE, categories=False) == 0.0


def test_the_category_and_the_colours_each_carry_their_own_share() -> None:
    assert _one(RED, BLUE_AND_WHITE, categories=True) == pytest.approx(CATEGORY_SHARE)
    assert _one(RED, RED, categories=False) == pytest.approx(1.0 - CATEGORY_SHARE)


def test_a_partly_shared_palette_lands_between_the_two_ends() -> None:
    partial = _one(RED, (RED[0], "#ffffff"), categories=True)

    assert _one(RED, BLUE_AND_WHITE, categories=True) < partial < _one(RED, RED, categories=True)


def test_colours_too_close_to_tell_apart_count_as_the_same_colour() -> None:
    """Wallhaven quantises its dominant colours, so equal hex strings would make almost every pair
    disjoint."""
    assert _one(RED, NEARLY_RED, categories=True) == pytest.approx(1.0)


def test_a_wallpaper_with_no_colours_is_still_like_itself() -> None:
    """The edge an all-zero histogram would turn into a division by zero and a page of NaN **Scores**."""
    colourless = wallpaper("aaaaaa", colours=())
    other_colourless = wallpaper("bbbbbb", colours=())

    assert baseline.similarities([colourless], [colourless])[0, 0] == pytest.approx(1.0)
    assert baseline.similarities([colourless], [other_colourless])[0, 0] == pytest.approx(1.0)
    assert _one((), RED, categories=True) == pytest.approx(CATEGORY_SHARE)


def test_a_colour_that_is_not_a_colour_costs_that_colour_and_nothing_else() -> None:
    """Colours are stored as one joined string, so half of one surviving a bad write is reachable."""
    broken = wallpaper("aaaaaa", colours=("#ff0000", "not-a-colour", "", "#gggggg"))

    matrix = baseline.similarities([broken], [wallpaper("bbbbbb", colours=("#ff0000",))])

    assert matrix[0, 0] == pytest.approx(1.0)


def test_the_category_is_compared_as_wallhaven_spells_it_rather_than_exactly() -> None:
    matrix = baseline.similarities(
        [wallpaper("aaaaaa", colours=RED, category=" General ")],
        [wallpaper("bbbbbb", colours=BLUE_AND_WHITE, category="general")],
    )

    assert float(matrix[0, 0]) == pytest.approx(CATEGORY_SHARE)


# -- embedding ---------------------------------------------------------------------------------------


def test_the_cosine_is_mapped_into_the_range_rather_than_clipped_into_it() -> None:
    """`(1 + cosine) / 2`. A clip at zero would make "unrelated" and "opposite" the same distance."""
    pool = [wallpaper("same"), wallpaper("right_angle"), wallpaper("opposite")]

    matrix = embedding({"subject": EAST, "same": EAST, "right_angle": NORTH, "opposite": WEST}).similarities(
        pool, [wallpaper("subject")]
    )

    assert [float(matrix[n, 0]) for n in range(3)] == pytest.approx([1.0, 0.5, 0.0])


def test_a_nearer_direction_scores_higher_than_a_further_one() -> None:
    pool = [wallpaper("near"), wallpaper("far")]

    matrix = embedding({"subject": EAST, "near": NORTH_EAST, "far": NORTH}).similarities(
        pool, [wallpaper("subject")]
    )

    assert float(matrix[0, 0]) > float(matrix[1, 0])


def test_a_vector_of_the_wrong_width_is_treated_as_absent() -> None:
    """A cache written by one model and read by another: the one mismatch that can be caught for nothing."""
    pool = [wallpaper("three_dimensional"), wallpaper("two_dimensional")]
    decided = [wallpaper("subject")]

    matrix = embedding(
        {"subject": EAST, "two_dimensional": EAST, "three_dimensional": vector(1.0, 0.0, 0.0)}
    ).similarities(pool, decided)

    assert float(matrix[0, 0]) == float(baseline.similarities(pool, decided)[0, 0])
    assert float(matrix[1, 0]) == pytest.approx(1.0)


def test_the_vectors_are_its_rows_in_pool_order_with_zeros_for_no_embedding() -> None:
    """`len(pool)` x d, and a zero row for none cached or one of a width this cache does not use."""
    pool = [wallpaper("b"), wallpaper("unembedded"), wallpaper("a"), wallpaper("three_dimensional")]

    rows = embedding({"a": EAST, "b": NORTH, "three_dimensional": vector(1.0, 0.0, 0.0)}).vectors(pool)
    nothing = embedding({}).vectors([wallpaper("a"), wallpaper("b")])

    assert rows is not None
    assert rows.tolist() == [NORTH.tolist(), [0.0, 0.0], EAST.tolist(), [0.0, 0.0]]
    assert nothing is not None
    assert len(nothing) == 2
    assert not np.any(nothing)


# -- tags --------------------------------------------------------------------------------------------


def test_identical_tag_sets_score_the_whole_tag_share() -> None:
    matrix = tagged({"a": (10, 20), "b": (20, 10)}).similarities([wallpaper("a")], [wallpaper("b")])

    assert float(matrix[0, 0]) == 1.0


def test_tag_overlap_is_the_jaccard_index_of_the_two_sets() -> None:
    pool = [wallpaper("a")]
    decided = [wallpaper("b")]
    base = float(baseline.similarities(pool, decided)[0, 0])

    overlapping = tagged({"a": (1, 2), "b": (2, 3)}).similarities(pool, decided)
    disjoint = tagged({"a": (1, 2), "b": (3, 4)}).similarities(pool, decided)

    assert float(overlapping[0, 0]) == pytest.approx(TAG_SHARE / 3.0 + (1.0 - TAG_SHARE) * base)
    assert float(disjoint[0, 0]) == pytest.approx((1.0 - TAG_SHARE) * base)


def test_the_blend_leans_on_the_tags_rather_than_on_the_colours() -> None:
    """The tag provider's whole claim: the baseline cannot tell a snowy peak from a white bedsheet."""
    peak = wallpaper("peak", colours=("#ffffff", "#cccccc"))
    sheet = wallpaper("sheet", colours=("#ffffff", "#cccccc"))
    other_peak = wallpaper("otherpeak", colours=("#223344", "#000000"))

    matrix = tagged({"peak": (1, 2), "otherpeak": (1, 2), "sheet": (99,)}).similarities(
        [other_peak, sheet], [peak]
    )

    assert float(matrix[0, 0]) > float(matrix[1, 0])


# -- the caches --------------------------------------------------------------------------------------


class _Embeddings:
    def __init__(self, path: Path) -> None:
        self.cache = EmbeddingCache(path)

    def store(self, wallpaper_id: str, n: int) -> None:
        self.cache.store(wallpaper_id, DIRECTIONS[n])

    def read(self, wallpaper_id: str) -> object:
        found = self.cache.vectors_for([wallpaper_id]).get(wallpaper_id)
        return None if found is None else np.round(found, 5).tolist()

    @staticmethod
    def value(n: int) -> object:
        return np.round(DIRECTIONS[n], 5).tolist()

    def known(self) -> set[str]:
        return self.cache.embedded_ids()

    def provider(self) -> SimilarityProvider:
        return EmbeddingSimilarityProvider(self.cache)


class _Tags:
    def __init__(self, path: Path) -> None:
        self.cache = TagCache(path)

    def store(self, wallpaper_id: str, n: int) -> None:
        self.cache.store(wallpaper_id, [Tag(id=n, name=f"tag{n}")], fetched_at=FETCHED_AT)

    def read(self, wallpaper_id: str) -> object:
        return self.cache.tags_for([wallpaper_id]).get(wallpaper_id)

    @staticmethod
    def value(n: int) -> object:
        return (n,)

    def known(self) -> set[str]:
        return self.cache.fetched_ids()

    def provider(self) -> SimilarityProvider:
        return TagSimilarityProvider(self.cache)


@pytest.mark.parametrize("open_cache", [_Embeddings, _Tags])
def test_a_cache_keeps_the_latest_value_across_a_reopen_and_serves_its_provider(
    open_cache: Callable[[Path], _Embeddings | _Tags], tmp_path: Path
) -> None:
    """Permanent is the point: an entry cost a model run or an **API call**. Re-storing replaces rather
    than adds, and an unknown **Wallpaper** is simply absent."""
    path = tmp_path / "cache.db"
    first = open_cache(path)
    assert first.read("nobody") is None
    assert first.known() == set()
    first.store("a", 0)
    first.store("a", 1)
    first.store("b", 1)

    reopened = open_cache(path)

    assert reopened.read("a") == reopened.value(1)
    assert reopened.known() == {"a", "b"}
    matrix = reopened.provider().similarities([wallpaper("a")], [wallpaper("b")])
    assert float(matrix[0, 0]) == pytest.approx(1.0)


def test_the_embedding_cache_hands_back_a_unit_vector_it_can_dot(tmp_path: Path) -> None:
    """Normalising on the way in is what lets every reader take a dot product and call it a cosine."""
    cache = EmbeddingCache(tmp_path / "embeddings.db")
    cache.store("a", np.array([3.0, 4.0], dtype=np.float32))

    assert cache.vectors_for(["a"])["a"] == pytest.approx(np.array([0.6, 0.8], dtype=np.float32))


def test_the_tag_cache_keeps_names_and_records_an_untagged_wallpaper_as_asked_about(tmp_path: Path) -> None:
    """Without the empty record the fill step would ask Wallhaven about it again on every run, for ever."""
    cache = TagCache(tmp_path / "tags.db")
    cache.store("a", [Tag(id=1, name="nature"), Tag(id=2, name="mountains")], fetched_at=FETCHED_AT)
    cache.store("untagged", [], fetched_at=FETCHED_AT)

    assert cache.tags_for(["a", "untagged"]) == {"a": (1, 2)}
    assert cache.names_for("a") == ("mountains", "nature")
    assert cache.fetched_ids() == {"a", "untagged"}
