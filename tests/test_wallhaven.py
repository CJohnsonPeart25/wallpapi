"""The real Wallhaven client, driven against a recorded response rather than the network.

`tests/fixtures/wallhaven_search.json` is a genuine `GET /api/v1/search?sorting=random&purity=100` response,
captured on 2026-09-24 with `data` trimmed to three entries for legibility and `meta` left verbatim. Every
expected value below is read from Wallhaven's own field names — `favorites`, `colors`, `dimension_x` — so the
test disagrees with the mapping if the mapping is wrong, rather than recomputing it.

No network: `httpx2.MockTransport` answers from the fixture.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from wallpapi.wallhaven import RateLimited, WallhavenClient

FIXTURE = Path(__file__).parent / "fixtures" / "wallhaven_search.json"
RECORDED: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))

WALLPAPER_FIXTURE = Path(__file__).parent / "fixtures" / "wallhaven_wallpaper.json"
WALLPAPER_RECORDED: dict[str, Any] = json.loads(WALLPAPER_FIXTURE.read_text(encoding="utf-8"))


def test_search_parses_a_recorded_wallhaven_response() -> None:
    """The client maps Wallhaven's spelling onto the domain's, and carries `meta.seed` through."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=RECORDED)

    client = WallhavenClient(client=httpx2.Client(transport=httpx2.MockTransport(handler)))

    page = client.search(sorting="random", purity="100")

    request = seen[0]
    assert request.url.path == "/api/v1/search"
    assert dict(request.url.params) == {"sorting": "random", "purity": "100", "page": "1"}

    assert page.seed == "j1MDms"
    assert [w.id for w in page.wallpapers] == ["oxkzwm", "m3eyj9", "4vzxy5"]

    first = page.wallpapers[0]
    assert first.width == 5120
    assert first.height == 2880
    assert first.ratio == "1.78"
    assert first.category == "general"
    assert first.purity == "sfw"
    assert first.favourites == 13
    assert first.colours == ("#424153", "#996633", "#000000", "#999999", "#663300")
    assert first.thumbnail_url == "https://th.wallhaven.cc/small/ox/oxkzwm.jpg"
    assert first.full_url == "https://w.wallhaven.cc/full/ox/wallhaven-oxkzwm.jpg"
    assert first.page_url == "https://wallhaven.cc/w/oxkzwm"


def test_search_sends_the_filters_it_is_given() -> None:
    """Acceptance criteria: minimum resolution and allowed ratios go into the query, purity is SFW.

    `atleast` and never `resolutions`: `atleast` is a minimum, `resolutions` is an exact-match list, and the
    **Filters** call for a minimum. `ratios` does take a comma-separated list.

    The masks arrive as parameters rather than being decided here. The client is thin on purpose — the
    policy that purity is always SFW and every category is on belongs with the **Filters**, in the Core
    service, where a test can reach it.
    """
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=RECORDED)

    client = WallhavenClient(client=httpx2.Client(transport=httpx2.MockTransport(handler)))

    client.search(
        sorting="random",
        purity="100",
        categories="111",
        page=3,
        seed="j1MDms",
        atleast="2560x1440",
        ratios="16x9,16x10,21x9",
    )

    assert dict(seen[0].url.params) == {
        "sorting": "random",
        "purity": "100",
        "categories": "111",
        "page": "3",
        "seed": "j1MDms",
        "atleast": "2560x1440",
        "ratios": "16x9,16x10,21x9",
    }


def test_filters_that_were_not_asked_for_are_left_out_of_the_query() -> None:
    """An omitted **Filter** must be absent, not sent empty.

    `atleast=` with no value is not the same request as no `atleast` at all, and guessing which way
    Wallhaven reads it is not a bet worth taking.
    """
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=RECORDED)

    client = WallhavenClient(client=httpx2.Client(transport=httpx2.MockTransport(handler)))

    client.search(sorting="random", purity="100", categories="111")

    assert "atleast" not in dict(seen[0].url.params)
    assert "ratios" not in dict(seen[0].url.params)
    assert "seed" not in dict(seen[0].url.params)


def test_a_429_is_raised_as_rate_limited_carrying_retry_after() -> None:
    """Acceptance criterion: the client recovers from a 429 — which starts with recognising one.

    A typed exception rather than a bare `HTTPStatusError`, because the caller has to tell "wait the number
    of seconds Wallhaven named" apart from every other failure, and picking that apart from a status code
    on the far side of the seam would put Wallhaven's spelling in the Core service.
    """

    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(429, headers={"Retry-After": "17"}, json={"error": "too many requests"})

    client = WallhavenClient(client=httpx2.Client(transport=httpx2.MockTransport(handler)))

    with pytest.raises(RateLimited) as raised:
        client.search(sorting="random", purity="100", categories="111")

    assert raised.value.retry_after == 17.0


def test_a_429_without_a_usable_retry_after_carries_none() -> None:
    """The header is optional, and may be an HTTP date rather than a count of seconds.

    `None` rather than a guess: the caller's own back-off is the answer to "Wallhaven did not say", and
    parsing a date format to save it a constant would be the client knowing more than it needs to.
    """

    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, json={})

    client = WallhavenClient(client=httpx2.Client(transport=httpx2.MockTransport(handler)))

    with pytest.raises(RateLimited) as raised:
        client.search(sorting="random", purity="100", categories="111")

    assert raised.value.retry_after is None


def test_any_other_non_200_still_raises() -> None:
    """A 500 from Wallhaven is a failure, not a rate limit, and must not be mistaken for one."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(503, json={})

    client = WallhavenClient(client=httpx2.Client(transport=httpx2.MockTransport(handler)))

    with pytest.raises(httpx2.HTTPStatusError):
        client.search(sorting="random", purity="100", categories="111")


def test_fetch_tags_reads_the_single_wallpaper_endpoint() -> None:
    """#14: tags come only from `GET /api/v1/w/{id}`, one **API call** per **Wallpaper**.

    `tests/fixtures/wallhaven_wallpaper.json` is a genuine response for `oxkzwm` — the same **Wallpaper**
    the search fixture's first entry is — captured on 2026-09-27 with `tags` trimmed to three entries. The
    ids and names below are read off Wallhaven's own payload, so the test disagrees with the mapping if
    the mapping is wrong rather than recomputing it.
    """
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=WALLPAPER_RECORDED)

    client = WallhavenClient(client=httpx2.Client(transport=httpx2.MockTransport(handler)))

    tags = client.fetch_tags("oxkzwm")

    assert seen[0].url.path == "/api/v1/w/oxkzwm"
    assert [(tag.id, tag.name) for tag in tags] == [
        (267, "Ford"),
        (27713, "Ford Mustang Mach 1"),
        (314, "car"),
    ]


def test_fetch_tags_tolerates_a_wallpaper_with_no_tags() -> None:
    """Wallhaven has plenty. An empty tuple rather than a failure, because the cache's whole job is to be
    able to say "asked, and there were none" (#14)."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(200, json={"data": {"id": "oxkzwm", "tags": []}})

    client = WallhavenClient(client=httpx2.Client(transport=httpx2.MockTransport(handler)))

    assert client.fetch_tags("oxkzwm") == ()


def test_fetch_tags_raises_rate_limited_on_a_429() -> None:
    """The tags endpoint is on `wallhaven.cc/api` and so is inside the same 45-a-minute budget: a fill
    step that ignored a 429 here would spend the **Pool** refill's allowance for it."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(429, headers={"Retry-After": "9"}, json={})

    client = WallhavenClient(client=httpx2.Client(transport=httpx2.MockTransport(handler)))

    with pytest.raises(RateLimited) as raised:
        client.fetch_tags("oxkzwm")

    assert raised.value.retry_after == 9.0
