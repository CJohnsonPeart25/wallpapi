"""The **Similarity provider**: the baseline, `Embeddings` over a dict store with a stub `embed` and model
source, and the SQLite store under `tmp_path`. No model, no `onnxruntime` and no network.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import tomllib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from tests.conftest import SOURCE, make_harness
from tests.fakes import (
    STUB_DIRECTION,
    MemoryStore,
    ModelOnDisk,
    ModelThatFails,
    StubEmbed,
    catalogue_of,
    wallpaper,
)
from wallpapi.main import build_similarity
from wallpapi.similarity import (
    CATEGORY_SHARE,
    CAUGHT_UP,
    RETRY_MODEL,
    EmbeddingCache,
    Embeddings,
    ModelSource,
    Similarity,
    metadata_similarity,
)

RED = ("#ff0000", "#880000")
NEARLY_RED = ("#fa0505", "#850303")
BLUE_AND_WHITE = ("#0000ff", "#ffffff")


def vector(*values: float) -> NDArray[np.float32]:
    """A unit-length vector in the direction given, since the store holds normalised rows."""
    raw = np.array(values, dtype=np.float32)
    return (raw / np.linalg.norm(raw)).astype(np.float32)


EAST = vector(1.0, 0.0)
NORTH = vector(0.0, 1.0)
NORTH_EAST = vector(1.0, 1.0)
WEST = vector(-1.0, 0.0)
DIRECTIONS = (EAST, NORTH, NORTH_EAST, WEST)


def embeddings(
    vectors: Mapping[str, NDArray[np.float32]] | None = None,
    *,
    embed: StubEmbed | None = None,
    model: ModelSource | None = None,
    batch: int = 16,
) -> Embeddings:
    return Embeddings(
        MemoryStore(vectors),
        StubEmbed() if embed is None else embed,
        ModelOnDisk() if model is None else model,
        batch=batch,
    )


def thumbnails(directory: Path, *names: str) -> Path:
    """A **Thumbnail cache** directory holding a file per name, oldest first in the order given."""
    directory.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(names):
        path = directory / f"{name}.jpg"
        path.write_bytes(b"\xff\xd8\xff\xe0 fake thumbnail")
        # Distinct mtimes, so "oldest first" does not depend on the filesystem's clock resolution.
        os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
    return directory


def _baseline(ids: Sequence[str]) -> Similarity:
    del ids
    return metadata_similarity


def _all_embedded(ids: Sequence[str]) -> Similarity:
    return embeddings({w: DIRECTIONS[n % len(DIRECTIONS)] for n, w in enumerate(ids)}).similarities


def _none_embedded(ids: Sequence[str]) -> Similarity:
    del ids
    return embeddings().similarities


PROVIDERS: dict[str, Callable[[Sequence[str]], Similarity]] = {
    "baseline": _baseline,
    "embedded": _all_embedded,
    "unembedded": _none_embedded,
}
"""The matrix as each source answers it, given **Embeddings** for the IDs named and none for the rest."""


# -- the contract ------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", PROVIDERS)
def test_the_matrix_is_pool_by_decided_in_range_and_one_on_the_diagonal(name: str) -> None:
    """Invariant 2's shape: **Pool** x decided, never **Pool** x **Pool**, empty either side an ordinary
    answer. float32 inside [0, 1], and 1.0 on the diagonal, including for a **Wallpaper** with no
    **Embedding**: the fallback must not cost a decided **Wallpaper** its own weight."""
    pool = [wallpaper("a", colours=RED), wallpaper("b", colours=BLUE_AND_WHITE), wallpaper("nodata")]
    decided = [wallpaper("c", colours=RED), wallpaper("d", colours=BLUE_AND_WHITE)]
    similarities = PROVIDERS[name](["a", "b", "c", "d"])

    matrix = similarities(pool, decided)
    assert matrix.shape == (3, 2)
    assert matrix.dtype == np.float32
    assert np.all((matrix >= 0.0) & (matrix <= 1.0))
    assert similarities(pool, []).shape == (3, 0)
    assert similarities([], decided).shape == (0, 2)

    itself = similarities(pool, pool)
    assert [float(itself[n, n]) for n in range(3)] == pytest.approx([1.0, 1.0, 1.0])
    assert np.all(itself <= 1.0)


@pytest.mark.parametrize("missing", ["pool", "decided"])
def test_a_side_with_no_embedding_falls_back_to_the_baseline(missing: str) -> None:
    """A **Pool** fills faster than it can be embedded. A 0.0 there would say the **Wallpaper** is unlike
    every **Ban** as well as every **Favourite**."""
    pool = [wallpaper("p")]
    decided = [wallpaper("d")]
    similarities = _all_embedded(["d"] if missing == "pool" else ["p"])

    assert similarities(pool, decided) == pytest.approx(metadata_similarity(pool, decided))


def test_numpy_is_a_direct_dependency() -> None:
    """numpy is a direct dependency, never left to arrive through onnxruntime: the **Score** maths and the
    baseline need it whether or not the model ever loads."""
    project = tomllib.loads((SOURCE.parent.parent / "pyproject.toml").read_text(encoding="utf-8"))

    assert any(dependency.startswith("numpy") for dependency in project["project"]["dependencies"])


def test_the_module_works_with_onnxruntime_and_pillow_blocked(tmp_path: Path) -> None:
    """Both are imported inside the functions that use them: the app boots, the composition root builds
    the provider and the fallback answers without either, and the embed only fails when called."""
    script = """
import sys
from pathlib import Path

sys.modules["onnxruntime"] = None
sys.modules["PIL"] = None

from wallpapi.main import build_similarity
from wallpapi.similarity import OnnxClipEmbedder

similarity = build_similarity(Path(sys.argv[1]))
assert similarity.similarities([], []).shape == (0, 0)
assert similarity.vectors([]).shape[0] == 0
try:
    OnnxClipEmbedder(Path(sys.argv[1]) / "absent.onnx")([])
except ImportError:
    print("blocked")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=60, check=False
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "blocked"


def test_building_the_provider_touches_no_disk_until_it_is_used(tmp_path: Path) -> None:
    """The model is fetched and opened on the similarity thread, never at boot."""
    build_similarity(tmp_path)

    assert list(tmp_path.iterdir()) == []


# -- the baseline ------------------------------------------------------------------------------------


def _one(pool_colours: tuple[str, ...], decided_colours: tuple[str, ...], *, categories: bool) -> float:
    matrix = metadata_similarity(
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

    assert metadata_similarity([colourless], [colourless])[0, 0] == pytest.approx(1.0)
    assert metadata_similarity([colourless], [other_colourless])[0, 0] == pytest.approx(1.0)
    assert _one((), RED, categories=True) == pytest.approx(CATEGORY_SHARE)


def test_a_colour_that_is_not_a_colour_costs_that_colour_and_nothing_else() -> None:
    """Colours are stored as one joined string, so half of one surviving a bad write is reachable."""
    broken = wallpaper("aaaaaa", colours=("#ff0000", "not-a-colour", "", "#gggggg"))

    matrix = metadata_similarity([broken], [wallpaper("bbbbbb", colours=("#ff0000",))])

    assert matrix[0, 0] == pytest.approx(1.0)


def test_the_category_is_compared_as_wallhaven_spells_it_rather_than_exactly() -> None:
    matrix = metadata_similarity(
        [wallpaper("aaaaaa", colours=RED, category=" General ")],
        [wallpaper("bbbbbb", colours=BLUE_AND_WHITE, category="general")],
    )

    assert float(matrix[0, 0]) == pytest.approx(CATEGORY_SHARE)


# -- embeddings --------------------------------------------------------------------------------------


def test_the_cosine_is_mapped_into_the_range_rather_than_clipped_into_it() -> None:
    """`(1 + cosine) / 2`. A clip at zero would make "unrelated" and "opposite" the same distance."""
    pool = [wallpaper("same"), wallpaper("right_angle"), wallpaper("opposite")]

    matrix = embeddings({"subject": EAST, "same": EAST, "right_angle": NORTH, "opposite": WEST}).similarities(
        pool, [wallpaper("subject")]
    )

    assert [float(matrix[n, 0]) for n in range(3)] == pytest.approx([1.0, 0.5, 0.0])


def test_a_nearer_direction_scores_higher_than_a_further_one() -> None:
    pool = [wallpaper("near"), wallpaper("far")]

    matrix = embeddings({"subject": EAST, "near": NORTH_EAST, "far": NORTH}).similarities(
        pool, [wallpaper("subject")]
    )

    assert float(matrix[0, 0]) > float(matrix[1, 0])


def test_a_vector_of_the_wrong_width_is_treated_as_absent() -> None:
    """A store written by one model and read by another: the one mismatch that can be caught for nothing."""
    pool = [wallpaper("three_dimensional"), wallpaper("two_dimensional")]
    decided = [wallpaper("subject")]

    matrix = embeddings(
        {"subject": EAST, "two_dimensional": EAST, "three_dimensional": vector(1.0, 0.0, 0.0)}
    ).similarities(pool, decided)

    assert float(matrix[0, 0]) == float(metadata_similarity(pool, decided)[0, 0])
    assert float(matrix[1, 0]) == pytest.approx(1.0)


def test_the_vectors_are_its_rows_in_pool_order_with_zeros_for_no_embedding() -> None:
    """`len(pool)` x d, and a zero row for none stored or one of a width this store does not use; with
    nothing stored at all, a row of no width each, which the varied draw reads as "no **Embedding**"."""
    pool = [wallpaper("b"), wallpaper("unembedded"), wallpaper("a"), wallpaper("three_dimensional")]

    rows = embeddings({"a": EAST, "b": NORTH, "three_dimensional": vector(1.0, 0.0, 0.0)}).vectors(pool)
    nothing = embeddings().vectors([wallpaper("a"), wallpaper("b")])

    assert rows.tolist() == [NORTH.tolist(), [0.0, 0.0], EAST.tolist(), [0.0, 0.0]]
    assert nothing.shape == (2, 0)


# -- upkeep and the notice ---------------------------------------------------------------------------


def test_before_the_model_arrives_the_page_is_told_and_coverage_waits() -> None:
    """A **Score** from the fallback looks exactly like one from the model, so the difference is said out
    loud. Coverage is how far the model has got, so it says nothing before there is a model."""
    similarity = embeddings()

    empty = similarity.notice([])
    notice = similarity.notice([wallpaper("a")])

    assert empty is not None
    assert "colour and category" in empty
    assert notice is not None
    assert "still starting up" in notice
    assert "Pool wallpapers" not in notice


def test_catching_up_embeds_what_is_in_the_thumbnail_cache_and_later_arrivals(tmp_path: Path) -> None:
    """A **Wallpaper** with no thumbnail is not in the work list, so it falls back to the baseline until
    its thumbnail arrives, and the next pass picks it up. The model is fetched once."""
    embed = StubEmbed()
    model = ModelOnDisk()
    store = MemoryStore()
    similarity = Embeddings(store, embed, model)
    directory = thumbnails(tmp_path / "thumbs", "one", "two")

    similarity.catch_up(directory, threading.Event())
    assert embed.seen == ["one", "two"]

    thumbnails(directory, "arrived_later")
    similarity.catch_up(directory, threading.Event())

    assert store.embedded_ids() == {"one", "two", "arrived_later"}
    assert model.calls == 1


def test_catching_up_takes_one_batch_at_a_time_and_asks_to_come_straight_back(tmp_path: Path) -> None:
    """One batch per call is how the thread's `stop_event` gets looked at during a backlog (invariant
    12); the 0.0 is how the loop knows not to sleep on the way through it."""
    embed = StubEmbed()
    similarity = embeddings(embed=embed, batch=2)
    directory = thumbnails(tmp_path / "thumbs", "a", "b", "c")

    assert similarity.catch_up(directory, threading.Event()) == 0.0
    assert similarity.catch_up(directory, threading.Event()) == CAUGHT_UP
    assert embed.batches == [2, 1]


def test_an_empty_thumbnail_cache_is_caught_up_rather_than_an_error(tmp_path: Path) -> None:
    assert embeddings().catch_up(tmp_path / "never_created", threading.Event()) == CAUGHT_UP


def test_a_model_file_that_will_not_open_says_so_rather_than_failing_silently(tmp_path: Path) -> None:
    """`embed` called with no files opens the model, as soon as it is on disk. Only reachable if somebody
    replaced the file after its checksum was verified, but otherwise every embedding fails behind a notice
    that says all is well. It outranks coverage."""
    embed = StubEmbed(will_not_open=True)
    similarity = embeddings(embed=embed)

    wait = similarity.catch_up(thumbnails(tmp_path / "thumbs", "one"), threading.Event())

    assert wait == RETRY_MODEL
    notice = similarity.notice([wallpaper("a")])
    assert notice is not None
    assert "could not open its model (INVALID_PROTOBUF : Load model failed)" in notice
    assert "Deleting the file under `models/` will fetch it again." in notice
    assert embed.seen == []


def test_a_failed_download_says_why_and_keeps_the_baseline(tmp_path: Path) -> None:
    """ "Could not reach it" and "the file was not what it should be" are different problems, so the line
    names the reason. Tried again in an hour: the usual cause is being offline."""
    embed = StubEmbed()
    similarity = embeddings(embed=embed, model=ModelThatFails("connection refused"))

    wait = similarity.catch_up(thumbnails(tmp_path / "thumbs", "one"), threading.Event())

    assert wait == RETRY_MODEL
    notice = similarity.notice([wallpaper("a")])
    assert notice is not None
    assert "could not fetch its model (connection refused)" in notice
    assert "Pool wallpapers" not in notice
    assert embed.seen == []
    assert float(similarity.similarities([wallpaper("a")], [wallpaper("a")])[0, 0]) == pytest.approx(1.0)


def test_a_thumbnail_that_will_not_open_costs_only_itself(tmp_path: Path) -> None:
    """It must not stop the rest being embedded, be picked up again every pass, or hold the coverage line
    on the page for ever: it is out of the count and out of the total."""
    store = MemoryStore()
    similarity = Embeddings(store, StubEmbed(unreadable=["broken"]), ModelOnDisk())
    directory = thumbnails(tmp_path / "thumbs", "broken", "fine")

    similarity.catch_up(directory, threading.Event())

    assert store.embedded_ids() == {"fine"}
    assert similarity.catch_up(directory, threading.Event()) == CAUGHT_UP
    assert similarity.notice([wallpaper("broken"), wallpaper("fine")]) is None
    notice = similarity.notice([wallpaper("broken"), wallpaper("fine"), wallpaper("waiting")])
    assert notice is not None
    assert "1 of 2 Pool wallpapers" in notice


def test_an_embedded_wallpaper_stops_using_the_baseline(tmp_path: Path) -> None:
    """Same category and palette, so the baseline would call these identical; their vectors are at a right
    angle, which maps to 0.5."""
    similarity = embeddings({"b": NORTH})
    similarity.catch_up(thumbnails(tmp_path / "thumbs", "a"), threading.Event())

    matrix = similarity.similarities([wallpaper("a")], [wallpaper("b")])

    assert float(matrix[0, 0]) == pytest.approx(0.5)


def test_the_page_is_told_how_much_of_the_pool_is_embedded(tmp_path: Path) -> None:
    """Counted against the **Pool** only, so a retired **Wallpaper** with an **Embedding** does not count,
    and in thousands the way a person reads them. An empty or wholly embedded **Pool** adds no line."""
    similarity = embeddings({"a": EAST, "b": NORTH, "retired": EAST})
    similarity.catch_up(tmp_path / "never_created", threading.Event())

    partial = similarity.notice([wallpaper("a"), wallpaper("b"), wallpaper("c")])
    large = similarity.notice([wallpaper(f"w{n:04d}") for n in range(1200)])

    assert partial == (
        "Image similarity covers 2 of 3 Pool wallpapers so far — the rest are compared by colour and "
        "category until their thumbnails are embedded."
    )
    assert large is not None
    assert "0 of 1,200 Pool wallpapers" in large
    assert similarity.notice([wallpaper("a"), wallpaper("b")]) is None
    assert similarity.notice([]) is None


def test_the_core_service_asks_about_the_whole_pool(db_path: Path) -> None:
    harness = make_harness(db_path, catalogue=catalogue_of(24))
    harness.store.vector_by_id["wp0000"] = STUB_DIRECTION
    harness.modules.similarity.catch_up(harness.modules.thumbnails.directory, threading.Event())

    notice = harness.modules.similarity.notice(harness.modules.thumbnails.obtainable(harness.connect()))

    assert notice is not None
    assert "1 of 24 Pool wallpapers" in notice


# -- the SQLite store --------------------------------------------------------------------------------


def test_the_store_keeps_the_latest_vector_across_a_reopen(tmp_path: Path) -> None:
    """Permanent is the point: an entry cost a model run. Re-storing replaces rather than adds, and an
    unknown **Wallpaper** is simply absent."""
    path = tmp_path / "embeddings.db"
    first = EmbeddingCache(path)
    assert first.vectors_for(["nobody"]) == {}
    assert first.embedded_ids() == set()
    first.store("a", EAST)
    first.store("a", NORTH)
    first.store("b", NORTH)

    reopened = EmbeddingCache(path)

    assert reopened.vectors_for(["a"])["a"] == pytest.approx(NORTH)
    assert reopened.embedded_ids() == {"a", "b"}
    matrix = Embeddings(reopened, StubEmbed(), ModelOnDisk()).similarities([wallpaper("a")], [wallpaper("b")])
    assert float(matrix[0, 0]) == pytest.approx(1.0)


def test_the_store_hands_back_a_unit_vector_it_can_dot(tmp_path: Path) -> None:
    """Normalising on the way in is what lets every reader take a dot product and call it a cosine."""
    cache = EmbeddingCache(tmp_path / "embeddings.db")
    cache.store("a", np.array([3.0, 4.0], dtype=np.float32))

    assert cache.vectors_for(["a"])["a"] == pytest.approx(np.array([0.6, 0.8], dtype=np.float32))
