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

from wallpapi.wallhaven import WallhavenClient

FIXTURE = Path(__file__).parent / "fixtures" / "wallhaven_search.json"
RECORDED: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))


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
