"""The CLIP maths: an image as the model's input, the embed step over an open session, how alike two
**Embeddings** are, and when that is one image reposted (ADR 0013).

wallpapi fetches and checks the model, opens the session and decodes each thumbnail; this takes what those
hand it. Rows are L2-normalised **Embeddings**, so a dot product is a cosine, and cosine is mapped by
`(1 + cosine) / 2` into `[0, 1]`, the range a **Score**'s `1 - similarity` reads as a distance.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np
from numpy.typing import NDArray

if TYPE_CHECKING:
    from PIL import Image

IMAGE_SIZE = 224

CLIP_MEAN = (0.48145466, 0.4578275, 0.40821073)
CLIP_STD = (0.26862954, 0.26130258, 0.27577711)
"""The input OpenAI's CLIP image towers were trained on, the ViT-B/32 wallpapi fetches among them. The model's
id is wallpapi's, beside the file and checksum it pins: this is handed a session and cannot tell which."""

NEAR_DUPLICATE_SIMILARITY = 0.985
"""The mapped similarity at which two **Embeddings** are one image reposted (ADR 0021): measured, not a
setting. The identical pairs on the real **Pool** sat at 0.99 and the near-empty images at 0.984 and below.
"""

_IMAGE_EMBEDS = "image_embeds"
"""The output an image-tower export names for its projected **Embeddings**."""


class Node(Protocol):
    """An input or output of the session, as far as this reads one."""

    @property
    def name(self) -> str: ...


class Session(Protocol):
    """Just enough of an open `onnxruntime.InferenceSession` to type: it ships no `py.typed`, and this never
    imports it."""

    def get_inputs(self) -> Sequence[Node]: ...
    def get_outputs(self) -> Sequence[Node]: ...
    def run(self, output_names: Sequence[str] | None, input_feed: Mapping[str, Any]) -> Sequence[Any]: ...


def preprocess(image: Image.Image) -> NDArray[np.float32]:
    """One decoded RGB image as CLIP's `(3, 224, 224)` input: resize the short side, centre crop, normalise.

    Pillow is imported here rather than at the top: the app and the suite run without it.
    """
    from PIL.Image import Resampling

    width, height = image.size
    scale = IMAGE_SIZE / min(width, height)
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    resized = image.resize(size, Resampling.BICUBIC)
    left = (resized.width - IMAGE_SIZE) // 2
    top = (resized.height - IMAGE_SIZE) // 2
    cropped = resized.crop((left, top, left + IMAGE_SIZE, top + IMAGE_SIZE))

    pixels = np.asarray(cropped, dtype=np.float32) / 255.0
    normalised = (pixels - np.array(CLIP_MEAN, dtype=np.float32)) / np.array(CLIP_STD, dtype=np.float32)
    return np.transpose(normalised, (2, 0, 1)).astype(np.float32)


def embed(session: Session, pixels: NDArray[np.float32]) -> NDArray[np.float32]:
    """One L2-normalised **Embedding** per image in `pixels`, a stack of one or more model inputs, from one
    run of the session.

    The output is `image_embeds` if the export has it, else its first; a `last_hidden_state` (images x tokens
    x width) gives each image its class token.
    """
    names = [output.name for output in session.get_outputs()]
    chosen = names.index(_IMAGE_EMBEDS) if _IMAGE_EMBEDS in names else 0
    outputs = session.run(None, {session.get_inputs()[0].name: pixels})
    array = np.asarray(outputs[chosen], dtype=np.float32)
    vectors = array[:, 0, :] if array.ndim == 3 else array
    return np.stack([normalised(row) for row in vectors])


def normalised(vector: NDArray[np.float32]) -> NDArray[np.float32]:
    """Unit length, or unchanged if there is no length to divide by."""
    norm = float(np.linalg.norm(vector))
    return vector if norm == 0.0 else (vector / norm).astype(np.float32)


def similarities(candidates: NDArray[np.float32], decided: NDArray[np.float32]) -> NDArray[np.float32]:
    """The `len(candidates)` x `len(decided)` matrix of mapped cosines between two stacks of unit rows, in one
    matmul (Invariant 2)."""
    cosine = candidates @ decided.T
    # Not clipped at zero: a negative cosine means less in common than unrelated.
    return (1.0 + np.clip(cosine, -1.0, 1.0)) / 2.0


def near_duplicates(similarities: NDArray[np.float32]) -> NDArray[np.bool_]:
    """Which pairs of a mapped similarity matrix are one image reposted (ADR 0021). Only for pairs that both
    have an **Embedding**: which those are is the caller's to know."""
    return similarities >= NEAR_DUPLICATE_SIMILARITY
