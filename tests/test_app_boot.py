"""The smoke test the definition of done requires: the app boots and serves a page.

This is the one test that does not enter through the Core service, because what it checks is that the web
layer is wired to it at all. It still touches no network — the Wallhaven client is the fake.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import make_harness
from wallpapi.web.app import create_app


def test_app_boots_and_serves_the_batch_page(db_path: Path) -> None:
    """GET / renders a Batch of 8 tiles.

    The app is built around an already-constructed Core service so the smoke test can inject the fake
    Wallhaven client. That the Core service is constructible from settings alone is a separate concern and
    belongs to whatever wires up the real entry point.
    """
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    body = response.text
    assert body.count('data-wallpaper-id="') == 8
    assert "th.wallhaven.cc" not in body, "tiles must be served from the Thumbnail cache, not hotlinked"


def test_the_page_does_not_reach_wallhaven_for_thumbnails(db_path: Path) -> None:
    """Invariant: thumbnails are served by wallpapi, so a Batch of 32 is not 32 hotlinks.

    #2 ships the serving seam only — a files-on-disk Thumbnail cache with no eviction. Verdict-aware
    eviction and the size cap arrive at #7, where History creates the actual requirement.
    """
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        body = client.get("/").text

    assert body.count('src="/thumb/') == 8
