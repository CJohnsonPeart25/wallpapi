"""The **Batch** viewing UX: bulk marking, the fullscreen preview, and the link out to Wallhaven.

The Core service tests cover what a bulk mark *means*. These cover the page: that one click posts once and
comes back with the whole grid re-rendered, that a bulk post and a single-tile post cannot overlap, and
that every asset the page loads is served by the app.

The JavaScript itself is not exercised here — there is no browser. What is asserted is the markup it hangs
off, which is the half a regression would break silently.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import batch_id_of, make_harness
from wallpapi.core import Batch
from wallpapi.model import Verdict
from wallpapi.web.app import create_app

EXTERNAL_ASSETS = re.compile(r'<(?:script|link)\b[^>]*\b(?:src|href)="([^"]+)"')
"""Every asset the page pulls in. A CDN reference would show up here as an absolute URL."""


def test_selecting_all_marks_every_tile_in_one_post(db_path: Path) -> None:
    """Acceptance criterion: select-all applies a chosen **Verdict** across the **Batch**.

    One post, and the response is the whole grid carrying the marked state — the page must not have to
    reload to see what it just did, and the **Decision log** must still be untouched.
    """
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        batch_id = batch_id_of(client.get("/batch").text)

        response = client.post("/draft/all", data={"batch_id": batch_id, "verdict": "favourite"})

    assert response.status_code == 200
    assert response.text.count('data-draft-verdict="favourite"') == 8
    live = harness.core.get_next_batch()
    assert isinstance(live, Batch)
    assert live.drafts == {wallpaper.id: Verdict.FAVOURITE for wallpaper in live.wallpapers}
    assert harness.core.list_history() == []


def test_clearing_all_removes_every_mark_in_one_post(db_path: Path) -> None:
    """Acceptance criterion: select-none. A delete of every row, not a write of eight explicit nothings."""
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        batch_id = batch_id_of(client.get("/batch").text)
        client.post("/draft/all", data={"batch_id": batch_id, "verdict": "ban"})

        response = client.post("/draft/all", data={"batch_id": batch_id, "verdict": ""})

    assert response.status_code == 200
    assert response.text.count('data-draft-verdict=""') == 8
    live = harness.core.get_next_batch()
    assert isinstance(live, Batch)
    assert live.drafts == {}


def test_a_bulk_mark_keeps_the_single_tile_controls_working(db_path: Path) -> None:
    """Bulk first, then correct the one tile you disagree with.

    The grid that comes back has to be a working grid, not a rendering of one: the per-tile controls in it
    still post to `/draft`, and the tile that was corrected is the only one that changed.
    """
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        batch_id = batch_id_of(client.get("/batch").text)
        client.post("/draft/all", data={"batch_id": batch_id, "verdict": "like"})
        live = harness.core.get_next_batch()
        assert isinstance(live, Batch)
        changed = live.wallpapers[3].id

        tile = client.post("/draft", data={"batch_id": batch_id, "wallpaper_id": changed, "verdict": "ban"})

    assert tile.status_code == 200
    assert 'data-draft-verdict="ban"' in tile.text
    reloaded = harness.core.get_next_batch()
    assert isinstance(reloaded, Batch)
    assert reloaded.drafts == {
        wallpaper.id: (Verdict.BAN if wallpaper.id == changed else Verdict.LIKE)
        for wallpaper in reloaded.wallpapers
    }


def test_an_ignore_cannot_be_bulk_drafted(db_path: Path) -> None:
    """**Ignore** is derived on submit, never stored as a mark — the same guard the single-tile route has.

    No control posts it. This only guards a hand-made post, but a bulk one would write an explicit
    **Ignore** against every tile at once, which is exactly the shape **Verdict resolution** must never
    have to tell apart from an absent row.
    """
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        batch_id = batch_id_of(client.get("/batch").text)

        response = client.post("/draft/all", data={"batch_id": batch_id, "verdict": "ignore"})

    assert response.status_code == 400


def test_an_unknown_bulk_verdict_is_refused(db_path: Path) -> None:
    """A **Verdict** that is not one of the four is a bad request, not a silently dropped mark."""
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        batch_id = batch_id_of(client.get("/batch").text)

        response = client.post("/draft/all", data={"batch_id": batch_id, "verdict": "adore"})

    assert response.status_code == 400


def test_the_batch_carries_favourite_like_ban_and_clear_controls_for_the_whole_batch(
    db_path: Path,
) -> None:
    """Acceptance criterion: select-all and select-none controls exist, once each for the **Batch**."""
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        body = client.get("/batch").text

    for choice in ("favourite", "like", "ban"):
        assert body.count(f'data-bulk-verdict="{choice}"') == 1
    assert body.count('data-bulk-verdict=""') == 1, "select-none is the fourth control"
    assert 'data-bulk-verdict="ignore"' not in body


def test_every_tile_links_to_its_wallhaven_page(db_path: Path) -> None:
    """Acceptance criterion: each **Wallpaper** links to its Wallhaven page.

    Opened in a new tab so judging a **Batch** is not interrupted, and with `rel="noopener noreferrer"`
    so the page it opens gets neither a handle back nor a referrer.

    One link on the page, not nine (#40). The tile renders none — a hidden anchor per tile was markup
    kept only for a script to read — and carries the URL as `data-page-url` instead, which the preview's
    one link takes when it opens on that tile.
    """
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        body = client.get("/").text + client.get("/batch").text
        live = harness.core.get_next_batch()
        assert isinstance(live, Batch)

    for wallpaper in live.wallpapers:
        assert f'data-page-url="{wallpaper.page_url}"' in body
        assert f'href="{wallpaper.page_url}"' not in body, "the tile renders no link of its own"
    assert body.count('rel="noopener noreferrer"') == 1
    assert body.count('target="_blank"') == 1
    dialog = body[body.index("<dialog") : body.index("</dialog>")]
    assert 'rel="noopener noreferrer"' in dialog, "the one link is the preview's"


def test_the_page_carries_a_fullscreen_preview_dialog_and_fetches_nothing_for_it_up_front(
    db_path: Path,
) -> None:
    """Acceptance criterion: any **Wallpaper** can be opened fullscreen at full resolution.

    A native `<dialog>` and one full-resolution source per tile. The dialog's image carries no `src` in the
    markup: invariant 8 forbids hotlinking a **Batch** of thumbnails, and the deal that makes one
    user-asked-for full-size image acceptable is that nothing is fetched until it is asked for.
    """
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        body = client.get("/").text + client.get("/batch").text
        live = harness.core.get_next_batch()
        assert isinstance(live, Batch)

    assert "<dialog" in body
    assert body.count("data-full-url=") == 8
    for wallpaper in live.wallpapers:
        assert f'data-full-url="{wallpaper.full_url}"' in body
    dialog = body[body.index("<dialog") : body.index("</dialog>")]
    assert "<img" in dialog
    assert "src=" not in dialog, "the full-resolution image is fetched when the dialog opens, not before"
    # Never bound: `x-bind:src` or `:src` would carry `src=` in the markup, and a binding would also
    # re-fetch on every change of state rather than only when the preview opens.
    assert ":src" not in dialog
    assert "setAttribute('src'" in dialog, "the source is set by hand when the preview opens"
    assert "removeAttribute('src')" in dialog, "and dropped again when it closes"


def test_no_script_or_stylesheet_the_page_loads_comes_from_anywhere_but_the_app(db_path: Path) -> None:
    """Every asset is vendored and served by the app — htmx, Alpine, Pico, the grid script and base.css.

    A personal tool that stops working when somebody else's host is unreachable is worse than one with a
    few small files in the repo, and a third-party script on a page rendering your own **Decision log** is
    a dependency on somebody else's integrity as well as their uptime.
    """
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        body = client.get("/").text
        script = client.get("/static/wallpapi.js")
        stylesheet = client.get("/static/base.css")

    assets = EXTERNAL_ASSETS.findall(body)
    assert assets, "the page loads at least htmx"
    for asset in assets:
        assert asset.startswith("/static/"), f"{asset} is not served by the app"
    assert script.status_code == 200
    assert stylesheet.status_code == 200
    assert "http" not in script.text, "the grid script must not fetch anything of its own"
