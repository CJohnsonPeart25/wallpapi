"""The baseline **Similarity provider**: dominant colours and category, and nothing else.

Issue #9. Tested directly rather than through the Core service, for the reason `ratelimit.py` and
`wallhaven.py` are: this is one of the five injected dependencies — the far side of the seam, not the
inside of the Core service — and every behaviour test of **Scoring** drives it through the fake instead.
What is pinned here is the contract the protocol states and the two properties the formula promises, never
the bin arithmetic itself: nothing outside the provider may depend on that.
"""

from __future__ import annotations

import numpy as np
import pytest

from tests.fakes import wallpaper
from wallpapi.similarity import CATEGORY_SHARE, MetadataSimilarityProvider

RED = ("#ff0000", "#880000")
NEARLY_RED = ("#fa0505", "#850303")
BLUE_AND_WHITE = ("#0000ff", "#ffffff")

provider = MetadataSimilarityProvider()
"""Stateless, so one is enough. It holds nothing between calls — there is nothing here to cache."""


def _one(pool_colours: tuple[str, ...], decided_colours: tuple[str, ...], *, categories: bool) -> float:
    """The similarity between two **Wallpapers** described by their colours and whether they share a
    category."""
    matrix = provider.similarities(
        [wallpaper("aaaaaa", colours=pool_colours, category="general")],
        [wallpaper("bbbbbb", colours=decided_colours, category="general" if categories else "anime")],
    )
    return float(matrix[0, 0])


def test_a_wallpaper_against_itself_is_one() -> None:
    """The one fixed point the protocol promises, and what makes a decided **Wallpaper** in the **Pool**
    score its own value at full weight."""
    only = wallpaper("aaaaaa", colours=RED)

    assert provider.similarities([only], [only])[0, 0] == pytest.approx(1.0)


def test_two_different_wallpapers_with_the_same_palette_and_category_are_alike() -> None:
    """Similarity is a statement about what the provider can see, not about identity.

    Two **Wallpapers** it can see nothing to tell apart are as alike as a **Wallpaper** is to itself. That
    is the honest answer for a provider that only reads a palette and a category, and it is why the
    baseline is a baseline.
    """
    assert _one(RED, RED, categories=True) == pytest.approx(1.0)


def test_a_different_category_and_a_disjoint_palette_is_nothing_in_common() -> None:
    """The other end: zero, and never negative. A **Score** is built from `1 - similarity`, so a negative
    similarity would be a distance past the far end of the range."""
    assert _one(RED, BLUE_AND_WHITE, categories=False) == 0.0


def test_the_category_and_the_colours_each_carry_their_own_share() -> None:
    """The two halves of the formula, pinned one at a time.

    Matching on the category alone is worth `CATEGORY_SHARE`, matching on the palette alone the rest. The
    exact split is the provider's business — what matters outside it is that neither half can swamp the
    other, which is what a reviewer would want to check the numbers against.
    """
    assert _one(RED, BLUE_AND_WHITE, categories=True) == pytest.approx(CATEGORY_SHARE)
    assert _one(RED, RED, categories=False) == pytest.approx(1.0 - CATEGORY_SHARE)


def test_a_partly_shared_palette_lands_between_the_two_ends() -> None:
    """Half a palette in common is worth less than all of it and more than none of it.

    A cosine between counted histograms rather than a match or a miss, so "mostly the same reds, plus some
    white" is nearer than "nothing in common" without being the same **Wallpaper**.
    """
    partial = _one(RED, (RED[0], "#ffffff"), categories=True)

    assert _one(RED, BLUE_AND_WHITE, categories=True) < partial < _one(RED, RED, categories=True)


def test_colours_too_close_to_tell_apart_count_as_the_same_colour() -> None:
    """Two reds a few values apart are the same red, which is the whole reason the colours are binned.

    Wallhaven's dominant colours are quantised from the image, so demanding equal hex strings would make
    almost every pair of **Wallpapers** disjoint and almost every **Score** zero.
    """
    assert _one(RED, NEARLY_RED, categories=True) == pytest.approx(1.0)


def test_the_matrix_is_pool_by_decided_and_never_pool_by_pool() -> None:
    """Invariant 2's shape, including the degenerate column count a new install starts with.

    Three **Pool** **Wallpapers** and two decided ones is a 3x2 matrix — not 3x3, which is the shape that
    would be 800MB of float64 on a real **Pool**. Nothing decided is 3x0, which has to be an ordinary
    answer rather than an error, because it is what every database looks like before the first submission.
    """
    pool = [wallpaper(f"pool{n}", colours=RED) for n in range(3)]
    decided = [wallpaper(f"seen{n}", colours=BLUE_AND_WHITE) for n in range(2)]

    assert provider.similarities(pool, decided).shape == (3, 2)
    assert provider.similarities(pool, []).shape == (3, 0)
    assert provider.similarities([], decided).shape == (0, 2)


def test_every_similarity_is_a_float32_inside_the_unit_range() -> None:
    """What the **Score** maths is entitled to assume of any provider, real or faked."""
    pool = [wallpaper("aaaaaa", colours=RED), wallpaper("bbbbbb", colours=BLUE_AND_WHITE)]

    matrix = provider.similarities(pool, pool)

    assert matrix.dtype == np.float32
    assert np.all((matrix >= 0.0) & (matrix <= 1.0))


def test_a_wallpaper_with_no_colours_is_still_like_itself() -> None:
    """The edge an all-zero histogram would turn into a division by zero.

    Wallhaven returns colours for everything in practice, but a row written before a field existed, or a
    truncated one, must not make every **Score** on the page a NaN. A colourless **Wallpaper** is wholly
    unlike a coloured one and exactly like another colourless one.
    """
    colourless = wallpaper("aaaaaa", colours=())
    other_colourless = wallpaper("bbbbbb", colours=())

    assert provider.similarities([colourless], [colourless])[0, 0] == pytest.approx(1.0)
    assert provider.similarities([colourless], [other_colourless])[0, 0] == pytest.approx(1.0)
    assert _one((), RED, categories=True) == pytest.approx(CATEGORY_SHARE)


def test_a_colour_that_is_not_a_colour_costs_that_colour_and_nothing_else() -> None:
    """A hand-edited or truncated row is one colour lost, never an exception.

    The colours are stored as one joined string, so half of one surviving a bad write is a reachable state.
    The **Wallpaper** is then scored on the colours that did parse — here, on the red it shares.
    """
    broken = wallpaper("aaaaaa", colours=("#ff0000", "not-a-colour", "", "#gggggg"))

    assert provider.similarities([broken], [wallpaper("bbbbbb", colours=("#ff0000",))])[
        0, 0
    ] == pytest.approx(1.0)


def test_the_category_is_compared_as_wallhaven_spells_it_rather_than_exactly() -> None:
    """Category is a string off an API response, not an enum wallpapi controls, so it is trimmed and
    lowercased before it is compared."""
    matrix = provider.similarities(
        [wallpaper("aaaaaa", colours=RED, category=" General ")],
        [wallpaper("bbbbbb", colours=BLUE_AND_WHITE, category="general")],
    )

    assert float(matrix[0, 0]) == pytest.approx(CATEGORY_SHARE)
