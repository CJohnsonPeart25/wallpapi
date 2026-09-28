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

from tests.conftest import make_harness
from tests.test_web_submission import batch_id_of
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
        batch_id = batch_id_of(client.get("/").text)

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
        batch_id = batch_id_of(client.get("/").text)
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
        batch_id = batch_id_of(client.get("/").text)
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
        batch_id = batch_id_of(client.get("/").text)

        response = client.post("/draft/all", data={"batch_id": batch_id, "verdict": "ignore"})

    assert response.status_code == 400


def test_an_unknown_bulk_verdict_is_refused(db_path: Path) -> None:
    """A **Verdict** that is not one of the four is a bad request, not a silently dropped mark."""
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        batch_id = batch_id_of(client.get("/").text)

        response = client.post("/draft/all", data={"batch_id": batch_id, "verdict": "adore"})

    assert response.status_code == 400


def test_a_bulk_mark_against_an_already_submitted_batch_is_refused(db_path: Path) -> None:
    """Invariant 7 through the bulk route, with the status the stale tab's submit would get."""
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        batch_id = batch_id_of(client.get("/").text)
        client.post("/submit", data={"batch_id": batch_id})

        response = client.post("/draft/all", data={"batch_id": batch_id, "verdict": "favourite"})

    assert response.status_code == 409
    assert "already" in response.text.lower()


def test_a_bulk_post_and_a_tile_post_cannot_be_in_flight_together(db_path: Path) -> None:
    """The one client-side hazard bulk marking introduces, closed declaratively.

    The server side is already atomic. What is not automatic is the screen: a bulk post re-renders the
    whole grid, so a single-tile post that commits after the grid was read would leave the page showing a
    mark the server no longer holds. The two kinds of control disable each other for the duration of a
    request, so only one of them can ever be in flight, and the grid the bulk post returns is the whole
    truth about the **Draft Batch**.

    Tested by proxy — there is no browser here, so what is asserted is the markup htmx acts on.
    """
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        body = client.get("/").text

    assert body.count('hx-target="#batch-grid"') == 4, "each bulk control swaps the whole grid"
    assert body.count('hx-sync="#batch-grid:replace"') == 4, "a second bulk click replaces the first"
    assert body.count('hx-disabled-elt=".verdict"') == 4, "a bulk post locks out the tile controls"
    assert body.count('hx-disabled-elt=".bulk-verdict"') == 24, "a tile post locks out the bulk controls"
    assert body.count('hx-sync="this:replace"') == 24, "invariant 6 is untouched by any of this"


def test_the_batch_carries_favourite_like_ban_and_clear_controls_for_the_whole_batch(
    db_path: Path,
) -> None:
    """Acceptance criterion: select-all and select-none controls exist, once each for the **Batch**."""
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        body = client.get("/").text

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
        body = client.get("/").text
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
        body = client.get("/").text
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


def test_the_preview_is_an_alpine_component_on_a_dialog_htmx_never_swaps(db_path: Path) -> None:
    """The preview's state lives on the `<dialog>`, which sits outside everything a swap replaces.

    Tested by proxy — there is no browser here. What is asserted is that the component hangs off the
    dialog, that it opens from a click on a thumbnail anywhere in the window rather than from a listener
    bound to one tile (which a swap would take away with the tile), and that it reads the tile's mark
    back after every htmx swap so the rail and the ring agree.
    """
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        body = client.get("/").text

    opening = body[body.index("<dialog") : body.index("<article")]
    assert "x-data=" in opening
    assert "x-on:click.window=" in opening
    assert "x-on:htmx:after-settle.camel.window=" in opening
    grid = body[body.index('<section id="batch-grid"') : body.index("</section>", body.index("batch-grid"))]
    assert "<dialog" not in grid, "the dialog must not be inside anything a bulk mark swaps"
    assert "x-data" not in grid, "no Alpine state on markup htmx replaces"


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


def test_a_tile_is_the_shape_of_the_thumbnail_in_it(db_path: Path) -> None:
    """The tile's box is 3:2, because `thumbs.small` is 300x200 whatever the **Wallpaper** is.

    A **Wallpaper**'s own `width` and `height` describe the file the preview loads, not the one on the
    tile, and rendering them as attributes on the thumbnail is a presentational hint that is simply
    wrong — it laid every tile out at the wallpaper's own height, two thousand pixels of it.

    Any box shape other than the thumbnail's own would also mean either re-cropping an image Wallhaven
    has already cropped, or ringing it with bars. Neither says anything truer about the wallpaper, and
    the wallpaper's real shape is what the preview is for.
    """
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        body = client.get("/").text
        stylesheet = client.get("/static/base.css").text

    start = body.index('<li class="tile"')
    tile = body[start : body.index("</li>", start)]
    assert 'width="' not in tile, "the wallpaper's dimensions are not the thumbnail's"
    assert 'height="' not in tile
    assert "--tile-ratio: 3 / 2" in stylesheet
    rule = next(block for block in stylesheet.split("}") if ".tile img" in block)
    assert "aspect-ratio: var(--tile-ratio)" in rule
    assert "object-fit: cover" in rule


def test_hovering_a_tile_reveals_its_verdict_rail_without_javascript(db_path: Path) -> None:
    """Hovering a **Wallpaper** brings up its controls, and nothing else on the page moves.

    This replaced hover-to-enlarge, which scaled tiles past the edge of the screen and had no answer for
    the tile in the last column. Seeing a **Wallpaper** bigger is what the preview is for; what hovering
    does now is offer the three **Verdict** controls, on a rail the grid has already reserved room for.

    Tested by proxy, and deliberately: there is no browser here, so what is asserted is that the rule is
    CSS rather than script. `:focus-within` carries the same rule so the keyboard reaches the rail too.
    """
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        stylesheet = client.get("/static/base.css").text

    rule = next(block for block in stylesheet.split("}") if ".tile:hover .verdicts" in block)
    assert ":focus-within .verdicts" in rule
    assert "opacity: 1" in rule
    # Nothing is scaled any more: a tile is exactly where the grid put it, hovered or not.
    assert "scale(" not in stylesheet
