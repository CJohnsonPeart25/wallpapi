"""The one line that decides which **Similarity provider** the real app runs with (ADR 0013).

Tested because all three look identical from the page: a typo that silently wired the baseline back in
would be a wallpapi scoring by colour with nothing anywhere saying so. Nothing here boots the app, fetches
anything or loads a model — `build_similarity` only constructs, the caches open lazily on first use, and
the model is not touched until the background thread asks for it.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from wallpapi.main import build_similarity
from wallpapi.similarity import NOTHING_TO_CATCH_UP, MetadataSimilarityProvider
from wallpapi.similarity_embedding import EmbeddingSimilarityProvider
from wallpapi.similarity_tags import TagSimilarityProvider


def test_the_default_is_the_embedding_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset means image embeddings — the provider #14 measured and the maintainer chose."""
    monkeypatch.delenv("WALLPAPI_SIMILARITY", raising=False)

    assert isinstance(build_similarity(tmp_path), EmbeddingSimilarityProvider)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("metadata", MetadataSimilarityProvider),
        ("tags", TagSimilarityProvider),
        ("embedding", EmbeddingSimilarityProvider),
    ],
)
def test_each_name_selects_its_provider(
    name: str, expected: type[object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All three stay reachable. The two that lost #14 are kept because the comparison that chose between
    them ran on a synthetic **Decision log**, and nobody has yet seen them on a real one."""
    monkeypatch.setenv("WALLPAPI_SIMILARITY", name)

    assert isinstance(build_similarity(tmp_path), expected)


def test_an_unrecognised_name_refuses_rather_than_falling_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo must not quietly run a different provider from the one that was asked for."""
    monkeypatch.setenv("WALLPAPI_SIMILARITY", "clip")

    with pytest.raises(ValueError, match="metadata, tags or embedding"):
        build_similarity(tmp_path)


def test_selecting_a_provider_touches_no_disk_until_it_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Constructing a provider must create neither its cache file nor its model directory: every boot
    would otherwise leave empty databases behind it, and the default one would be reaching for 85MiB
    before the app had finished starting."""
    monkeypatch.setenv("WALLPAPI_SIMILARITY", "embedding")
    build_similarity(tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_the_tag_provider_is_wired_to_nothing_that_spends_api_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Selecting it must not start it fetching tags in the background.

    One **API call** per **Wallpaper** out of Wallhaven's 45 a minute is the same budget the refill spends
    keeping the **Pool** stocked — about 2,000 of them for a **Pool** at its default target — and taking
    half of it is a decision for the person running wallpapi rather than for the provider. So its
    `catch_up` does nothing at all, selected or not, which is what this pins.
    """
    monkeypatch.setenv("WALLPAPI_SIMILARITY", "tags")
    provider = build_similarity(tmp_path)

    assert provider.catch_up(tmp_path, threading.Event()) == NOTHING_TO_CATCH_UP
    assert provider.notice() is None
