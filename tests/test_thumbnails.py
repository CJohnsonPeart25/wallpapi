"""Serving tiles from the **Thumbnail cache**. Invariant 8: a **Batch** of 32 is not 32 hotlinks.

#2 ships the serving seam and an unevicted directory. Verdict-aware eviction and the size cap are #7, where
**History** creates the actual requirement.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import make_harness
from tests.fakes import THUMBNAIL_BYTES
from wallpapi.core import Batch
from wallpapi.web.app import create_app


def test_the_first_request_fetches_a_thumbnail_and_the_second_comes_off_disk(db_path: Path) -> None:
    """Fetch once, serve for ever. A second request must not go back to Wallhaven.

    The thumbnail hosts sit behind DDoS protection with no published limits, so re-fetching a tile on every
    page view is the thing the cache exists to prevent.
    """
    harness = make_harness(db_path)
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    shown = batch.wallpapers[0]
    app = create_app(harness.core)

    with TestClient(app) as client:
        first = client.get(f"/thumb/{shown.id}")
        second = client.get(f"/thumb/{shown.id}")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.content == THUMBNAIL_BYTES
    assert second.content == THUMBNAIL_BYTES
    assert harness.wallhaven.thumbnail_fetches == [shown.thumbnail_url]
    assert [p.name for p in harness.core.thumbnail_dir.iterdir()] == [f"{shown.id}.jpg"]
