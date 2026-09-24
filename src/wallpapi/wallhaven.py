"""The Wallhaven client seam and the shape of a search result.

`meta.seed` is carried here because Wallhaven returns one on `sorting=random` and it is what keeps a walk
across pages from repeating itself. Nothing at #2 pages — a **Batch** is one live search — so it is recorded
and not yet used.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from wallpapi.model import Wallpaper


@dataclass(frozen=True, slots=True)
class SearchPage:
    """One page of a Wallhaven search. Listings return 24 **Wallpapers** per page."""

    wallpapers: tuple[Wallpaper, ...]
    seed: str | None = None


class WallhavenSearcher(Protocol):
    """What the Core service needs of Wallhaven."""

    def search(self, *, sorting: str, purity: str, page: int = 1) -> SearchPage:
        """One page of results. `purity` is Wallhaven's three-bit mask, so SFW-only is `"100"`."""
        ...
