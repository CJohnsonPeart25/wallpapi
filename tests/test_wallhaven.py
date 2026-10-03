"""The real Wallhaven client against recorded responses over a mock transport, and the socket guard.

`fixtures/wallhaven_search.json` is a genuine `GET /api/v1/search` response captured on 2026-09-24 with
`data` trimmed to three entries. Expected values are read off Wallhaven's own field names, so a wrong
mapping disagrees with them rather than being recomputed.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest

from wallpapi.wallhaven import RateLimited, ThumbnailUnavailable, WallhavenClient

FIXTURES = Path(__file__).parent / "fixtures"
RECORDED: dict[str, Any] = json.loads((FIXTURES / "wallhaven_search.json").read_text(encoding="utf-8"))
THUMBNAIL = "https://th.wallhaven.cc/small/ox/oxkzwm.jpg"


def answering(response: httpx2.Response) -> tuple[WallhavenClient, list[httpx2.Request]]:
    """A client whose every request gets `response`, and the requests it made."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return response

    return WallhavenClient(client=httpx2.Client(transport=httpx2.MockTransport(handler))), seen


def test_search_parses_a_recorded_wallhaven_response() -> None:
    client, seen = answering(httpx2.Response(200, json=RECORDED))

    page = client.search(sorting="random", purity="100")

    assert seen[0].url.path == "/api/v1/search"
    assert dict(seen[0].url.params) == {"sorting": "random", "purity": "100", "page": "1"}
    assert page.seed == "j1MDms"
    assert [w.id for w in page.wallpapers] == ["oxkzwm", "m3eyj9", "4vzxy5"]
    first = page.wallpapers[0]
    assert (first.width, first.height, first.ratio) == (5120, 2880, "1.78")
    assert (first.category, first.purity, first.favourites) == ("general", "sfw", 13)
    assert first.colours == ("#424153", "#996633", "#000000", "#999999", "#663300")
    assert first.thumbnail_url == "https://th.wallhaven.cc/small/ox/oxkzwm.jpg"
    assert first.full_url == "https://w.wallhaven.cc/full/ox/wallhaven-oxkzwm.jpg"
    assert first.page_url == "https://wallhaven.cc/w/oxkzwm"


def test_search_sends_the_filters_it_is_given_and_leaves_out_the_rest() -> None:
    """`atleast`, the minimum, never `resolutions`, the exact-match list. The masks are parameters: the
    policy that purity is SFW belongs with the **Filters** in the Core service. An omitted **Filter** is
    absent, not sent empty, since `atleast=` is not the same request as no `atleast`."""
    client, seen = answering(httpx2.Response(200, json=RECORDED))

    client.search(
        sorting="random",
        purity="100",
        categories="111",
        page=3,
        seed="j1MDms",
        atleast="2560x1440",
        ratios="16x9,16x10,21x9",
    )
    client.search(sorting="random", purity="100", categories="111")

    assert dict(seen[0].url.params) == {
        "sorting": "random",
        "purity": "100",
        "categories": "111",
        "page": "3",
        "seed": "j1MDms",
        "atleast": "2560x1440",
        "ratios": "16x9,16x10,21x9",
    }
    assert dict(seen[1].url.params) == {
        "sorting": "random",
        "purity": "100",
        "categories": "111",
        "page": "1",
    }


def _search(client: WallhavenClient) -> object:
    return client.search(sorting="random", purity="100")


def _thumbnail(client: WallhavenClient) -> object:
    return client.fetch_thumbnail(THUMBNAIL)


@pytest.mark.parametrize(
    ("call", "retry_after", "expected"),
    [
        pytest.param(_search, "17", 17.0, id="search"),
        # An HTTP date is not parsed: the caller's own back-off answers "Wallhaven did not say".
        pytest.param(_search, "Wed, 21 Oct 2026 07:28:00 GMT", None, id="search, a date"),
        pytest.param(_thumbnail, "90", 90.0, id="thumbnail"),
    ],
)
def test_a_429_is_raised_as_rate_limited_carrying_retry_after(
    call: Callable[[WallhavenClient], object], retry_after: str, expected: float | None
) -> None:
    """The one failure the caller treats differently, so it is typed rather than left as a status code
    for the Core service to pick apart in Wallhaven's spelling."""
    client, _ = answering(httpx2.Response(429, headers={"Retry-After": retry_after}, json={}))

    with pytest.raises(RateLimited) as raised:
        call(client)

    assert raised.value.retry_after == expected


def test_any_other_non_200_still_raises() -> None:
    client, _ = answering(httpx2.Response(503, json={}))

    with pytest.raises(httpx2.HTTPStatusError):
        client.search(sorting="random", purity="100", categories="111")


def test_a_thumbnail_the_host_refuses_is_unavailable() -> None:
    """About that one file, so the downloader skips it rather than backing off."""
    client, _ = answering(httpx2.Response(404))

    with pytest.raises(ThumbnailUnavailable) as raised:
        client.fetch_thumbnail(THUMBNAIL)

    assert raised.value.status == 404


def test_a_test_that_reaches_for_the_network_fails(no_network: list[str]) -> None:
    """The conftest guard: a raw connect, a real client and an attempt swallowed on another thread are all
    refused and recorded. Cleared at the end, or this test would fail at teardown as any other would."""

    def swallowed() -> None:
        with contextlib.suppress(OSError):
            socket.getaddrinfo("wallhaven.cc", 443)

    with pytest.raises(OSError):
        socket.create_connection(("192.0.2.1", 80), timeout=1)
    with httpx2.Client() as client, pytest.raises(httpx2.ConnectError):
        client.get("https://wallhaven.cc/api/v1/search")
    background = threading.Thread(target=swallowed)
    background.start()
    background.join()

    assert any("192.0.2.1" in attempt for attempt in no_network)
    assert sum("wallhaven.cc" in attempt for attempt in no_network) >= 2
    no_network.clear()
