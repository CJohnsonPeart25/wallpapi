"""The one line that decides which **Similarity provider** the real app runs with (#14).

Tested because it is the line every number in the spike's comparison depends on: a typo that silently
wired the baseline back in would make the comparison a lie and nothing would say so. Nothing here boots
the app, fetches anything or loads a model — `build_similarity` only constructs, and both candidates open
their cache lazily on first use.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wallpapi.main import build_similarity
from wallpapi.similarity import MetadataSimilarityProvider
from wallpapi.similarity_embedding import EmbeddingSimilarityProvider
from wallpapi.similarity_tags import TagSimilarityProvider


def test_the_default_is_the_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset means the provider #9 shipped. #14 is a spike, and a spike does not change what runs."""
    monkeypatch.delenv("WALLPAPI_SIMILARITY", raising=False)

    assert isinstance(build_similarity(tmp_path), MetadataSimilarityProvider)


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
    monkeypatch.setenv("WALLPAPI_SIMILARITY", name)

    assert isinstance(build_similarity(tmp_path), expected)


def test_an_unrecognised_name_refuses_rather_than_falling_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo must not quietly run the provider the other two are being compared against."""
    monkeypatch.setenv("WALLPAPI_SIMILARITY", "clip")

    with pytest.raises(ValueError, match="metadata, tags or embedding"):
        build_similarity(tmp_path)


def test_selecting_a_candidate_touches_no_disk_until_it_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Constructing a provider must not create its cache file, or every boot of the baseline app would
    leave two empty databases behind it."""
    monkeypatch.setenv("WALLPAPI_SIMILARITY", "tags")
    build_similarity(tmp_path)

    assert list(tmp_path.iterdir()) == []
