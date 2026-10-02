"""#14's tag **Similarity provider**, and the cache behind it.

Not through the Core service, and deliberately so. A **Similarity provider** is one of the five injected
dependencies rather than a behaviour of the Core service: what a test can observe through the seam is a
**Zone**, and a **Zone** is the sign of a number this provider only contributes one term of. So these are
unit tests of the protocol's own contract — shape, range, the diagonal — and of the two pieces of arithmetic
the spike's whole comparison rests on.

No network anywhere: the provider is handed tag sets by hand, and the cache is a file under `tmp_path`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np
import pytest

from tests.fakes import wallpaper
from wallpapi.similarity import MetadataSimilarityProvider
from wallpapi.similarity_tags import TAG_SHARE, TagCache, TagSimilarityProvider
from wallpapi.wallhaven import Tag

FETCHED_AT = dt.datetime(2026, 9, 27, 12, 0, 0, tzinfo=dt.UTC)


class TagsByHand:
    """A `TagSource` over a dict, so a test says what the tags are instead of fetching them."""

    def __init__(self, tags: Mapping[str, tuple[int, ...]]) -> None:
        self._tags = dict(tags)

    def tags_for(self, wallpaper_ids: Sequence[str]) -> Mapping[str, tuple[int, ...]]:
        return {w: self._tags[w] for w in wallpaper_ids if w in self._tags}


def provider(tags: Mapping[str, tuple[int, ...]]) -> TagSimilarityProvider:
    return TagSimilarityProvider(TagsByHand(tags))


def test_the_matrix_is_pool_by_decided_and_every_value_is_a_similarity() -> None:
    pool = [wallpaper("a"), wallpaper("b"), wallpaper("c")]
    decided = [wallpaper("a"), wallpaper("d")]
    matrix = provider({"a": (1, 2), "b": (2, 3), "c": (), "d": (9,)}).similarities(pool, decided)

    assert matrix.shape == (3, 2)
    assert matrix.dtype == np.float32
    assert np.all(matrix >= 0.0) and np.all(matrix <= 1.0)


def test_a_wallpaper_against_itself_is_one() -> None:
    """The fixed point the protocol promises, and what makes a **Favourite** in the **Pool** a **Banger**.

    Held for the untagged **Wallpaper** as well as the tagged ones: the fallback has to preserve the
    diagonal or a **Wallpaper** the cache has not reached yet would stop scoring its own **Verdict** at
    full weight. `approx` because the baseline's own cosine of a unit float32 vector with itself lands a
    rounding below 1.0, which this provider inherits and must not make worse.
    """
    pool = [wallpaper("a"), wallpaper("b"), wallpaper("c")]
    matrix = provider({"a": (1, 2, 3), "b": (2,)}).similarities(pool, pool)

    assert [float(matrix[n, n]) for n in range(3)] == pytest.approx([1.0, 1.0, 1.0])
    assert np.all(matrix <= 1.0)


def test_identical_tag_sets_score_the_whole_tag_share() -> None:
    """Jaccard 1.0 on the tags; the two share a category and a palette, so the baseline term is 1.0 too."""
    pool = [wallpaper("a")]
    decided = [wallpaper("b")]
    matrix = provider({"a": (10, 20), "b": (20, 10)}).similarities(pool, decided)

    assert float(matrix[0, 0]) == 1.0


def test_tag_overlap_is_the_jaccard_index_of_the_two_sets() -> None:
    """One tag shared out of three distinct is 1/3, and it is worth `TAG_SHARE` of the answer."""
    pool = [wallpaper("a")]
    decided = [wallpaper("b")]
    baseline = MetadataSimilarityProvider().similarities(pool, decided)

    matrix = provider({"a": (1, 2), "b": (2, 3)}).similarities(pool, decided)

    expected = TAG_SHARE * (1.0 / 3.0) + (1.0 - TAG_SHARE) * float(baseline[0, 0])
    assert float(matrix[0, 0]) == pytest.approx(expected)


def test_disjoint_tag_sets_keep_only_the_baseline_share() -> None:
    pool = [wallpaper("a")]
    decided = [wallpaper("b")]
    baseline = float(MetadataSimilarityProvider().similarities(pool, decided)[0, 0])

    matrix = provider({"a": (1, 2), "b": (3, 4)}).similarities(pool, decided)

    assert float(matrix[0, 0]) == pytest.approx((1.0 - TAG_SHARE) * baseline)


def test_a_wallpaper_with_no_cached_tags_falls_back_to_the_baseline() -> None:
    """The fallback is the whole reason this is usable before the cache is full: a **Pool** fills faster
    than 45 **API calls** a minute can tag it, and a 0.0 for an untagged **Wallpaper** would say it is
    unlike every **Ban** as well as every **Favourite**."""
    pool = [wallpaper("untagged"), wallpaper("tagged")]
    decided = [wallpaper("decided")]
    baseline = MetadataSimilarityProvider().similarities(pool, decided)

    matrix = provider({"tagged": (1,), "decided": (1,)}).similarities(pool, decided)

    assert float(matrix[0, 0]) == float(baseline[0, 0])
    assert float(matrix[1, 0]) > float(baseline[1, 0])


def test_the_blend_leans_on_the_tags_rather_than_on_the_colours() -> None:
    """Two **Wallpapers** that share every tag but no colour beat two that share every colour but no tag.

    This is the whole claim of the tag provider, stated as an ordering rather than as a number: the
    baseline cannot tell a snowy peak from a white bedsheet, and the tags can.
    """
    peak = wallpaper("peak", colours=("#ffffff", "#cccccc"))
    sheet = wallpaper("sheet", colours=("#ffffff", "#cccccc"))
    other_peak = wallpaper("otherpeak", colours=("#223344", "#000000"))

    matrix = provider(
        {"peak": (1, 2), "otherpeak": (1, 2), "sheet": (99,)},
    ).similarities([other_peak, sheet], [peak])

    assert float(matrix[0, 0]) > float(matrix[1, 0])


def test_an_empty_decided_set_is_an_empty_matrix_rather_than_a_failure() -> None:
    """`classify_pool` calls the provider before it knows whether anything has been decided at all."""
    matrix = provider({"a": (1,)}).similarities([wallpaper("a")], [])

    assert matrix.shape == (1, 0)


def test_nothing_decided_carries_a_tag_still_leaves_the_baseline_answer() -> None:
    pool = [wallpaper("a")]
    decided = [wallpaper("b")]
    baseline = MetadataSimilarityProvider().similarities(pool, decided)

    matrix = provider({"a": (1, 2)}).similarities(pool, decided)

    assert float(matrix[0, 0]) == float(baseline[0, 0])


# -- the cache -----------------------------------------------------------------------------------------


def test_the_cache_hands_back_the_tags_it_was_given(tmp_path: Path) -> None:
    cache = TagCache(tmp_path / "tags.db")
    cache.store("a", [Tag(id=1, name="nature"), Tag(id=2, name="mountains")], fetched_at=FETCHED_AT)

    assert cache.tags_for(["a"]) == {"a": (1, 2)}
    assert cache.names_for("a") == ("mountains", "nature")


def test_the_cache_survives_being_reopened(tmp_path: Path) -> None:
    """Permanent is the point: a tag cost an **API call**, and paying it twice is the thing to avoid."""
    path = tmp_path / "tags.db"
    TagCache(path).store("a", [Tag(id=7, name="space")], fetched_at=FETCHED_AT)

    assert TagCache(path).tags_for(["a"]) == {"a": (7,)}


def test_a_wallpaper_with_no_tags_is_recorded_as_asked_about(tmp_path: Path) -> None:
    """Otherwise the fill step asks Wallhaven about it again on every run, for ever."""
    cache = TagCache(tmp_path / "tags.db")
    cache.store("a", [], fetched_at=FETCHED_AT)

    assert cache.tags_for(["a"]) == {}
    assert cache.fetched_ids() == {"a"}


def test_refetching_replaces_the_tags_rather_than_adding_to_them(tmp_path: Path) -> None:
    cache = TagCache(tmp_path / "tags.db")
    cache.store("a", [Tag(id=1, name="nature")], fetched_at=FETCHED_AT)
    cache.store("a", [Tag(id=2, name="anime")], fetched_at=FETCHED_AT)

    assert cache.tags_for(["a"]) == {"a": (2,)}


def test_an_unknown_wallpaper_is_simply_absent(tmp_path: Path) -> None:
    cache = TagCache(tmp_path / "tags.db")

    assert cache.tags_for(["nobody"]) == {}
    assert cache.fetched_ids() == set()


def test_the_cache_serves_the_provider_it_was_built_for(tmp_path: Path) -> None:
    """The one test that puts the two halves together, so the `TagSource` protocol is not only asserted
    against a hand-written stand-in."""
    cache = TagCache(tmp_path / "tags.db")
    cache.store("a", [Tag(id=1, name="nature")], fetched_at=FETCHED_AT)
    cache.store("b", [Tag(id=1, name="nature")], fetched_at=FETCHED_AT)

    matrix = TagSimilarityProvider(cache).similarities([wallpaper("a")], [wallpaper("b")])

    assert float(matrix[0, 0]) == 1.0


def test_it_has_no_vectors_to_cluster_on() -> None:
    """A tag set is not a position: the varied **Unknown** draw (#45) falls back to today's draw."""
    assert provider({"a": (1, 2)}).vectors([wallpaper("a")]) is None
