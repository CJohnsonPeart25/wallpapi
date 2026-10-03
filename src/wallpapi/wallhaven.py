"""The Wallhaven client seam and the shape of a search result.

The only module that knows Wallhaven's URLs, field names and parameters, and its one failure of its own, a 429
(`RateLimited`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx2
from pydantic import BaseModel

from wallpapi.model import Wallpaper

API_SEARCH_URL = "https://wallhaven.cc/api/v1/search"
"""The 45-calls-per-minute limit applies to `wallhaven.cc/api`: this URL and the next."""

API_WALLPAPER_URL = "https://wallhaven.cc/api/v1/w/{wallpaper_id}"
"""The single-**Wallpaper** endpoint, the only place Wallhaven returns tags."""

REQUEST_TIMEOUT = 10.0
"""Seconds. Must stay below the shutdown join timeout (invariant 12)."""

TOO_MANY_REQUESTS = 429


class RateLimited(Exception):
    """Wallhaven answered 429, the only failure told apart from the rest.

    `retry_after` is `None` when there is no usable header; `Retry-After` is parsed as seconds only, and the
    caller's back-off covers an HTTP date.
    """

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("wallhaven answered 429 too many requests")
        self.retry_after = retry_after


class ThumbnailUnavailable(Exception):
    """The thumbnail host refused this one file (a 404, a 403, a 5xx).

    Told apart so the downloader skips the file, where `RateLimited` or a transport failure makes it back off
    a minute.
    """

    def __init__(self, status: int) -> None:
        super().__init__(f"the thumbnail host answered {status}")
        self.status = status


def _retry_after_seconds(value: str | None) -> float | None:
    """The `Retry-After` header as seconds, or `None` if absent or an HTTP date."""
    if value is None:
        return None
    try:
        return float(value.strip())
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class SearchPage:
    """One page of a Wallhaven search: 24 **Wallpapers**, and the `meta.seed` that stops a walk repeating
    itself.
    """

    wallpapers: tuple[Wallpaper, ...]
    seed: str | None = None


@dataclass(frozen=True, slots=True)
class Tag:
    """One of Wallhaven's labels on a **Wallpaper**. The id is what overlap is computed on; the name is for
    humans.
    """

    id: int
    name: str


class Wallhaven(Protocol):
    """What the Core service needs of Wallhaven."""

    def search(
        self,
        *,
        sorting: str,
        purity: str,
        categories: str | None = None,
        query: str | None = None,
        page: int = 1,
        seed: str | None = None,
        atleast: str | None = None,
        ratios: str | None = None,
    ) -> SearchPage:
        """One page of results. `purity` is Wallhaven's three-bit mask, so SFW-only is `"100"`.

        `seed` carries a previous page's `meta.seed`. `query` is Wallhaven's `q`, a search expression such as
        `like:<id>`; this seam only carries it. `None` parameters are omitted from the request. Raises
        `RateLimited` on a 429.
        """
        ...

    def fetch_thumbnail(self, url: str) -> bytes:
        """The bytes of one thumbnail. Not an **API call**.

        Raises `RateLimited` on a 429 and `ThumbnailUnavailable` on any other refusal.
        """
        ...


class _Thumbs(BaseModel):
    small: str


class _SearchItem(BaseModel):
    """One `data` entry, in Wallhaven's spelling."""

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


class _TagItem(BaseModel):
    id: int
    name: str


class _WallpaperDetail(BaseModel):
    """The single-**Wallpaper** payload, narrowed to its tags."""

    tags: list[_TagItem] = []


class _WallpaperResponse(BaseModel):
    data: _WallpaperDetail


def _to_wallpaper(item: _SearchItem) -> Wallpaper:
    """Wallhaven's field names onto the glossary's."""
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
    """The only module that knows Wallhaven. Thin: it forwards the parameters it is given and maps the
    response.

    The minimum **Favourites** **Filter** never arrives: Wallhaven has no parameter for it. No API key, so
    purity is SFW.
    """

    def __init__(self, client: httpx2.Client | None = None) -> None:
        self._client = httpx2.Client(timeout=REQUEST_TIMEOUT) if client is None else client

    def search(
        self,
        *,
        sorting: str,
        purity: str,
        categories: str | None = None,
        query: str | None = None,
        page: int = 1,
        seed: str | None = None,
        atleast: str | None = None,
        ratios: str | None = None,
    ) -> SearchPage:
        """One page of results. An **API call**; `None` parameters are omitted from the query."""
        parameters: dict[str, str | int] = {"sorting": sorting, "purity": purity, "page": page}
        for name, value in (
            ("categories", categories),
            ("q", query),
            ("seed", seed),
            ("atleast", atleast),
            ("ratios", ratios),
        ):
            if value is not None:
                parameters[name] = value
        response = self._client.get(API_SEARCH_URL, params=parameters)
        if response.status_code == TOO_MANY_REQUESTS:
            raise RateLimited(_retry_after_seconds(response.headers.get("Retry-After")))
        response.raise_for_status()
        payload = _SearchResponse.model_validate(response.json())
        return SearchPage(
            wallpapers=tuple(_to_wallpaper(item) for item in payload.data), seed=payload.meta.seed
        )

    def fetch_tags(self, wallpaper_id: str) -> tuple[Tag, ...]:
        """One **Wallpaper**'s tags: one **API call** each, so it is not on the `Wallhaven` protocol.

        The refill must not call it; the tag **Similarity provider** holds the real client and paces itself. A
        429 becomes `RateLimited`.
        """
        response = self._client.get(API_WALLPAPER_URL.format(wallpaper_id=wallpaper_id))
        if response.status_code == TOO_MANY_REQUESTS:
            raise RateLimited(_retry_after_seconds(response.headers.get("Retry-After")))
        response.raise_for_status()
        payload = _WallpaperResponse.model_validate(response.json())
        return tuple(Tag(id=item.id, name=item.name) for item in payload.data.tags)

    def fetch_thumbnail(self, url: str) -> bytes:
        """The bytes behind a `thumbs.small` URL, from `th.wallhaven.cc`: not an **API call**, so not counted
        against the 45 a minute (ADR 0017).

        A 429 becomes `RateLimited`, any other non-2xx `ThumbnailUnavailable`.
        """
        response = self._client.get(url)
        if response.status_code == TOO_MANY_REQUESTS:
            raise RateLimited(_retry_after_seconds(response.headers.get("Retry-After")))
        if not response.is_success:
            raise ThumbnailUnavailable(response.status_code)
        return response.content

    def close(self) -> None:
        self._client.close()
