"""The Wallhaven client seam and the shape of a search result.

`meta.seed` is carried here because Wallhaven returns one on `sorting=random` and it is what keeps a walk
across pages from repeating itself. Nothing at #2 pages — a **Batch** is one live search — so it is recorded
and not yet used.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx2
from pydantic import BaseModel

from wallpapi.model import Wallpaper

API_SEARCH_URL = "https://wallhaven.cc/api/v1/search"
"""The documented 45-calls-per-minute limit applies to `wallhaven.cc/api` — this URL and no other."""

REQUEST_TIMEOUT = 10.0
"""Seconds. Must stay below the shutdown join timeout, so shutdown cannot hang mid-request (invariant 12)."""


@dataclass(frozen=True, slots=True)
class SearchPage:
    """One page of a Wallhaven search. Listings return 24 **Wallpapers** per page."""

    wallpapers: tuple[Wallpaper, ...]
    seed: str | None = None


class Wallhaven(Protocol):
    """What the Core service needs of Wallhaven."""

    def search(self, *, sorting: str, purity: str, page: int = 1) -> SearchPage:
        """One page of results. `purity` is Wallhaven's three-bit mask, so SFW-only is `"100"`."""
        ...

    def fetch_thumbnail(self, url: str) -> bytes:
        """The bytes of one thumbnail. Not an **API call** — see `WallhavenClient.fetch_thumbnail`."""
        ...


class _Thumbs(BaseModel):
    small: str


class _SearchItem(BaseModel):
    """One `data` entry. Wallhaven's spelling, not the domain's — the mapping happens in one place below."""

    id: str
    url: str
    purity: str
    category: str
    dimension_x: int
    dimension_y: int
    ratio: str
    favorites: int
    colors: list[str]
    path: str
    thumbs: _Thumbs


class _SearchMeta(BaseModel):
    seed: str | None = None


class _SearchResponse(BaseModel):
    data: list[_SearchItem]
    meta: _SearchMeta


def _to_wallpaper(item: _SearchItem) -> Wallpaper:
    """Wallhaven's field names onto the glossary's. `thumbs.small` is the tile size the page uses."""
    return Wallpaper(
        id=item.id,
        width=item.dimension_x,
        height=item.dimension_y,
        ratio=item.ratio,
        category=item.category,
        purity=item.purity,
        favourites=item.favorites,
        colours=tuple(item.colors),
        thumbnail_url=item.thumbs.small,
        full_url=item.path,
        page_url=item.url,
    )


class WallhavenClient:
    """The only module that knows Wallhaven.

    Thin on purpose: #2 needs one random SFW search and the thumbnail bytes behind it. **Filters** —
    `atleast`, `ratios`, minimum **Favourites** — and paging on `meta.seed` arrive with the **Pool** at #6.

    No API key: NSFW is what requires one, and purity is fixed to SFW.
    """

    def __init__(self, client: httpx2.Client | None = None) -> None:
        self._client = httpx2.Client(timeout=REQUEST_TIMEOUT) if client is None else client

    def search(self, *, sorting: str, purity: str, page: int = 1) -> SearchPage:
        """One page of results — 24 **Wallpapers**, per Wallhaven's listing size.

        This is an **API call**, and the only method here that is. Whatever enforces the documented
        45-per-minute limit at #6 wraps this one and not `fetch_thumbnail`.
        """
        response = self._client.get(
            API_SEARCH_URL, params={"sorting": sorting, "purity": purity, "page": page}
        )
        response.raise_for_status()
        payload = _SearchResponse.model_validate(response.json())
        return SearchPage(
            wallpapers=tuple(_to_wallpaper(item) for item in payload.data), seed=payload.meta.seed
        )

    def fetch_thumbnail(self, url: str) -> bytes:
        """The bytes behind a `thumbs.small` URL.

        Not an **API call**: thumbnails come from `th.wallhaven.cc`, a separate host from `wallhaven.cc/api`,
        so these must not be counted against the documented 45-per-minute limit. They do want throttling of
        their own — those hosts sit behind DDoS protection with no published limits — but that belongs with
        the **Pool** refill at #6, and the **Thumbnail cache** already means each tile is fetched once.
        """
        response = self._client.get(url)
        response.raise_for_status()
        return response.content

    def close(self) -> None:
        self._client.close()
