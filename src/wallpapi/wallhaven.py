"""The Wallhaven client seam and the shape of a search result.

`meta.seed` is carried here because Wallhaven returns one on `sorting=random` and it is what keeps a walk
across pages from repeating itself. The **Pool** refill walks pages, so the seed is passed back in rather
than only recorded.

This module is the only one that knows Wallhaven: its URLs, its field names, its query parameters, and the
one failure of its own it has — a 429. `RateLimited` is defined here for that reason, and the Core service
imports it rather than picking a status code apart on the far side of the seam.
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

TOO_MANY_REQUESTS = 429


class RateLimited(Exception):
    """Wallhaven answered 429. The one failure mode the caller can do something specific about.

    A type of its own rather than the `HTTPStatusError` underneath it, because the **Pool** refill has to
    tell "wait the number of seconds Wallhaven named" apart from every other transport failure — and doing
    that by reading a status code would put Wallhaven's spelling inside the Core service.

    `retry_after` is `None` when Wallhaven sent no usable header. The caller's own back-off is the answer to
    that; parsing the HTTP-date form of the header to save it a constant would be this module knowing more
    than it needs to.
    """

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("wallhaven answered 429 too many requests")
        self.retry_after = retry_after


def _retry_after_seconds(value: str | None) -> float | None:
    """The `Retry-After` header as seconds, or `None` if it is absent or given as an HTTP date."""
    if value is None:
        return None
    try:
        return float(value.strip())
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class SearchPage:
    """One page of a Wallhaven search. Listings return 24 **Wallpapers** per page."""

    wallpapers: tuple[Wallpaper, ...]
    seed: str | None = None


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

        `seed` carries a previous page's `meta.seed` so a walk across pages does not repeat itself.

        `query` is Wallhaven's `q`, which takes a search *expression* rather than only words: the refill
        sends `like:<wallhaven id>` through it to ask for one **Wallpaper**'s lookalikes (#13). What the
        expression means is Wallhaven's business and which one to send is the Core service's decision; this
        seam only carries it.

        `atleast` is a minimum resolution as `WxH` and `ratios` a comma-separated list of Wallhaven's named
        ratios — the **Filters**, in Wallhaven's spelling. Every optional parameter is omitted from the
        query when it is `None`, because an empty parameter is not the same request as an absent one.

        Raises `RateLimited` on a 429.
        """
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

    Thin on purpose: it forwards the parameters it is given and maps the response onto the glossary. What
    those parameters should be — SFW purity, every category, the **Filters** the user configured — is the
    Core service's decision, taken where a behaviour test can reach it. The minimum **Favourites**
    **Filter** never arrives here at all: Wallhaven's search has no parameter for it, so it is applied
    locally as **Wallpapers** enter the **Pool**.

    No API key: NSFW is what requires one, and purity is fixed to SFW.
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
        """One page of results — 24 **Wallpapers**, per Wallhaven's listing size.

        This is an **API call**, and the only method here that is. The 45-per-minute limiter counts this and
        not `fetch_thumbnail`.

        Every optional parameter is omitted from the query when it is `None`, so the first call of a walk
        asks Wallhaven for a fresh seed and later calls carry back the one it returned.

        A 429 becomes `RateLimited`; every other non-200 raises as it always did.
        """
        parameters: dict[str, str | int] = {"sorting": sorting, "purity": purity, "page": page}
        for name, value in (
            ("categories", categories),
            # Wallhaven spells it `q`, and this module is the only place that spelling belongs.
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

    def fetch_thumbnail(self, url: str) -> bytes:
        """The bytes behind a `thumbs.small` URL.

        Not an **API call**: thumbnails come from `th.wallhaven.cc`, a separate host from `wallhaven.cc/api`,
        so these must not be counted against the documented 45-per-minute limit. Throttling them is still
        deferred — the **Thumbnail cache** means each tile is fetched once ever, and this runs on a request
        thread with no `stop_event` to wait on cancellably (invariant 12). See `AGENTS.md`.
        """
        response = self._client.get(url)
        response.raise_for_status()
        return response.content

    def close(self) -> None:
        self._client.close()
