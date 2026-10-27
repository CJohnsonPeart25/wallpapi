"""The CLIP maths: preprocessing, the embed step, the mapped cosine and the near-duplicate test, as pure
functions on plain data.

No model and no file: an image is made in memory, and the session is a hand-written stand-in for an open
ONNX Runtime session, recording what it is fed. Which pairs have two **Embeddings**, and the fallback for the
rest, are wallpapi's, and `test_similarity.py` holds them through `Embeddings`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pytest
from numpy.typing import NDArray
from PIL import Image

from wallpapi.taste import clip


def unit(*values: float) -> NDArray[np.float32]:
    raw = np.array(values, dtype=np.float32)
    return (raw / np.linalg.norm(raw)).astype(np.float32)


EAST = unit(1.0, 0.0)
NORTH = unit(0.0, 1.0)
WEST = unit(-1.0, 0.0)


def rows(*vectors: NDArray[np.float32]) -> NDArray[np.float32]:
    return np.stack(vectors).astype(np.float32)


# -- the mapped cosine -------------------------------------------------------------------------------


def test_the_cosine_is_mapped_into_the_range_rather_than_clipped_into_it() -> None:
    """`(1 + cosine) / 2`: the same direction 1.0, a right angle 0.5, opposite 0.0. A clip at zero would
    make "unrelated" and "opposite" the same distance."""
    matrix = clip.similarities(rows(EAST, NORTH, WEST), rows(EAST))

    assert matrix.dtype == np.float32
    assert matrix.tolist() == [[1.0], [0.5], [0.0]]


def test_the_matrix_is_candidates_by_decided_whatever_either_side_holds() -> None:
    """Invariant 2's shape, never pairwise; an empty side is an ordinary answer."""
    candidates = rows(EAST, NORTH, WEST)
    decided = rows(NORTH, EAST)

    assert clip.similarities(candidates, decided).tolist() == [[0.5, 1.0], [1.0, 0.5], [0.5, 0.0]]
    assert clip.similarities(candidates, np.zeros((0, 2), dtype=np.float32)).shape == (3, 0)
    assert clip.similarities(np.zeros((0, 2), dtype=np.float32), decided).shape == (0, 2)


def test_a_cosine_rounded_past_one_is_held_inside_the_range() -> None:
    """float32 can put a unit vector's dot with itself a hair either side of 1."""
    long = np.array([[1.0001, 0.0]], dtype=np.float32)
    backwards = np.array([[-1.0001, 0.0]], dtype=np.float32)

    assert clip.similarities(long, long).tolist() == [[1.0]]
    assert clip.similarities(long, backwards).tolist() == [[0.0]]


# -- near-duplicates ---------------------------------------------------------------------------------


def test_a_near_duplicate_is_at_or_above_the_threshold_and_not_just_below_it() -> None:
    """ADR 0021: a repost of the same image, measured on the real **Pool** at 0.99, against the near-empty
    images at 0.984 and below."""
    matrix = np.array([[0.985, 1.0, 0.9849], [0.99, 0.984, 0.5]], dtype=np.float32)

    vetoed = clip.near_duplicates(matrix)

    assert vetoed.dtype == np.bool_
    assert vetoed.tolist() == [[True, True, False], [True, False, False]]


# -- L2 normalisation --------------------------------------------------------------------------------


def test_a_vector_is_brought_to_unit_length_in_its_own_direction() -> None:
    normal = clip.normalised(np.array([3.0, 4.0], dtype=np.float32))

    assert normal.dtype == np.float32
    assert normal.tolist() == pytest.approx([0.6, 0.8])


def test_a_vector_with_no_length_is_left_as_it_is_rather_than_divided_by_zero() -> None:
    """A row of zeros is how a store reads "no **Embedding**"; NaNs would read as one."""
    assert clip.normalised(np.zeros(3, dtype=np.float32)).tolist() == [0.0, 0.0, 0.0]


# -- the embed step ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Node:
    name: str


class Session:
    """An open session as ONNX Runtime hands one over: one input, the outputs named, `run` returning each
    output's array in that order. Records what it is fed."""

    def __init__(self, outputs: Mapping[str, Sequence[Any]], *, input_name: str = "pixel_values") -> None:
        self.outputs = {name: np.array(value, dtype=np.float32) for name, value in outputs.items()}
        self.input_name = input_name
        self.fed: list[tuple[Sequence[str] | None, dict[str, Any]]] = []

    def get_inputs(self) -> Sequence[Node]:
        return [Node(self.input_name)]

    def get_outputs(self) -> Sequence[Node]:
        return [Node(name) for name in self.outputs]

    def run(self, output_names: Sequence[str] | None, input_feed: Mapping[str, Any]) -> Sequence[Any]:
        self.fed.append((output_names, dict(input_feed)))
        return list(self.outputs.values())


PIXELS = np.zeros((2, 3, clip.IMAGE_SIZE, clip.IMAGE_SIZE), dtype=np.float32)


def test_the_pixels_go_in_under_the_session_input_and_image_embeds_come_out_at_unit_length() -> None:
    """`image_embeds` wherever the export lists it, one unit row per image, so a dot product is a cosine."""
    hidden = [[[9.0, 9.0], [9.0, 9.0]], [[9.0, 9.0], [9.0, 9.0]]]
    session = Session({"last_hidden_state": hidden, "image_embeds": [[3.0, 4.0], [0.0, 2.0]]}, input_name="x")

    vectors = clip.embed(session, PIXELS)

    assert vectors.dtype == np.float32
    assert vectors == pytest.approx(np.array([[0.6, 0.8], [0.0, 1.0]]))
    [(_, feed)] = session.fed
    assert list(feed) == ["x"]
    assert feed["x"] is PIXELS


def test_an_export_without_image_embeds_gives_its_first_output_by_the_class_token() -> None:
    """A `last_hidden_state` export is images x tokens x width: the first token of each image is its
    vector. An export whose first output is already a row per image is taken as it is."""
    tokens = [[[3.0, 4.0], [9.0, 9.0], [9.0, 9.0]], [[5.0, 0.0], [9.0, 9.0], [9.0, 9.0]]]

    by_token = clip.embed(Session({"last_hidden_state": tokens, "other": [[1.0, 1.0]] * 2}), PIXELS)
    by_row = clip.embed(Session({"pooled": [[0.0, 3.0], [2.0, 0.0]]}), PIXELS)

    assert by_token == pytest.approx(np.array([[0.6, 0.8], [1.0, 0.0]]))
    assert by_row == pytest.approx(np.array([[0.0, 1.0], [1.0, 0.0]]))


# -- preprocessing -----------------------------------------------------------------------------------

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)

AS_BLACK = (-1.7923, -1.7521, -1.4802)
AS_WHITE = (1.9303, 2.0749, 2.1459)
"""Black and white as CLIP's input sees them, each channel shifted by the mean of the images CLIP was trained
on and scaled by their spread: the values every CLIP preprocessor gives, to four places."""


def assert_all(pixels: NDArray[np.float32], channels: tuple[float, float, float]) -> None:
    """Every pixel of a `(3, height, width)` input is `channels`, to four places."""
    expected = np.broadcast_to(np.array(channels, dtype=np.float32)[:, None, None], pixels.shape)
    np.testing.assert_allclose(pixels, expected, rtol=0.0, atol=1e-4)


def banded(size: tuple[int, int], middle: tuple[int, int]) -> Image.Image:
    """A white image with a black band across the long side's span `middle`."""
    width, height = size
    image = Image.new("RGB", size, WHITE)
    start, stop = middle
    image.paste(BLACK, (start, 0, stop, height) if width > height else (0, start, width, stop))
    return image


@pytest.mark.parametrize("size", [(224, 224), (100, 50), (1000, 700), (300, 2000)])
@pytest.mark.parametrize(
    ("colour", "expected"), [(BLACK, AS_BLACK), (WHITE, AS_WHITE)], ids=["black", "white"]
)
def test_any_image_becomes_one_224_square_input_normalised_per_channel(
    size: tuple[int, int], colour: tuple[int, int, int], expected: tuple[float, float, float]
) -> None:
    pixels = clip.preprocess(Image.new("RGB", size, colour))

    assert pixels.dtype == np.float32
    assert pixels.shape == (3, 224, 224)
    assert_all(pixels, expected)


@pytest.mark.parametrize("size", [(672, 224), (224, 672)], ids=["wide", "tall"])
def test_the_long_side_is_cropped_to_its_centre(size: tuple[int, int]) -> None:
    """The middle third of a 3:1 image is all that is left of it."""
    pixels = clip.preprocess(banded(size, (224, 448)))

    assert_all(pixels, AS_BLACK)


def test_the_short_side_is_brought_to_224_keeping_the_shape_before_the_crop() -> None:
    """An 896 x 448 image halves to 448 x 224, and its middle half is the crop. Squeezed square instead, the
    white sides would be in it. The columns at the crop's edges blend with the white under the resize."""
    pixels = clip.preprocess(banded((896, 448), (224, 672)))

    assert_all(pixels[:, :, 4:220], AS_BLACK)
