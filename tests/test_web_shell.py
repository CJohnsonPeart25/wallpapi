"""The shell every full page is rendered in: the vendored assets, the nav, and the theme (#40, ADR 0014).

No browser here, so none of Pico's styling or Alpine's behaviour is exercised. What is asserted is the part a
regression would break silently: which files each page loads and where from, that the vendored files are
the published ones byte for byte, that the theme is applied by an inline script before anything else, and
that a fragment htmx swaps in is still a fragment and not a page.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_harness
from tests.fakes import catalogue_of
from tests.test_web_submission import batch_id_of
from wallpapi.web.app import STATIC_DIR, create_app

PAGES = ("/", "/history", "/settings")
ASSETS = re.compile(r'<(?:script|link)\b[^>]*\b(?:src|href)="([^"]+)"')

VENDORED = {
    "pico.indigo.min.css": (83_336, "3ff75cde84c76491549e1a7c64294c2c83cb2f92231ea44692f9ffc69897a811"),
    "alpine.min.js": (55_891, "232519394c6c8fdba6f362b1d9da16106db513cdbf899011f00daab4051df31c"),
}
"""Size and SHA-256 of each release file as fetched, recorded in ADR 0014.

Pinned so that a line-ending conversion on checkout, a formatter run over `static/`, or a hand edit all
show up here rather than as a vendored file that is quietly no longer the one its version number claims.
"""


def _pages(db_path: Path) -> dict[str, str]:
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    with TestClient(create_app(harness.core)) as client:
        return {path: client.get(path).text for path in PAGES}


def _head(body: str) -> str:
    return body[body.index("<head>") : body.index("</head>")]


@pytest.mark.parametrize("name", sorted(VENDORED))
def test_the_vendored_files_are_the_release_files_byte_for_byte(name: str) -> None:
    """Acceptance criterion: byte-identical to the release files, licence headers kept."""
    size, digest = VENDORED[name]
    data = (STATIC_DIR / name).read_bytes()

    assert len(data) == size
    assert hashlib.sha256(data).hexdigest() == digest
    assert b"MIT" in data, "the licence header is part of the file"


def test_every_page_loads_only_what_the_app_serves_and_all_of_it_answers(db_path: Path) -> None:
    """Every `<script src>` and `<link href>` on all three pages is under `/static/`, and is there."""
    harness = make_harness(db_path, catalogue=catalogue_of(8))

    with TestClient(create_app(harness.core)) as client:
        bodies = {path: client.get(path).text for path in PAGES}
        loaded = {asset for body in bodies.values() for asset in ASSETS.findall(body)}
        answers = {asset: client.get(asset).status_code for asset in loaded}
        gone = client.get("/static/wallpapi.css").status_code

    for path, body in bodies.items():
        for asset in ASSETS.findall(body):
            assert asset.startswith("/static/"), f"{path} loads {asset}, which the app does not serve"
        for base in ("pico.indigo.min.css", "base.css", "htmx.min.js", "alpine.min.js"):
            assert f'"/static/{base}"' in body, f"{path} is not on the shell: no {base}"
    assert loaded >= {f"/static/{name}" for name in ("wallpapi.js", *VENDORED)}
    assert all(status == 200 for status in answers.values()), answers
    assert gone == 404, "wallpapi.css is replaced by Pico and base.css, not kept beside them"


def test_alpine_is_deferred_and_the_grid_script_is_only_where_there_is_a_grid(db_path: Path) -> None:
    """Alpine starts after the document is parsed, so every `x-data` in it exists when it looks.

    wallpapi.js sizes the **Batch** grid and does nothing else, so it is loaded by the page with a grid.
    """
    pages = _pages(db_path)

    for body in pages.values():
        assert '<script src="/static/alpine.min.js" defer></script>' in body
    assert "/static/wallpapi.js" in pages["/"]
    assert "/static/wallpapi.js" not in pages["/history"]
    assert "/static/wallpapi.js" not in pages["/settings"]


def test_the_theme_is_applied_by_an_inline_script_before_anything_is_painted(db_path: Path) -> None:
    """Inline, and ahead of the stylesheets, so a stored light or dark is on `<html>` for the first frame.

    Inline is also what keeps it out of the "every asset from /static" rule: it is not an asset. A test
    because moving it into a file looks like a tidy-up and costs a flash of the wrong theme on every load.
    """
    for path, body in _pages(db_path).items():
        head = _head(body)
        inline = re.search(r"<script>(.*?)</script>", head, re.S)
        assert inline, f"{path} has no inline theme script"
        assert "localStorage" in inline.group(1)
        assert "data-theme" in inline.group(1)
        assert head.index("<script>") < head.index("/static/pico.indigo.min.css"), "before first paint"


def test_every_page_has_the_theme_button_last_in_the_nav(db_path: Path) -> None:
    """One button, cycling system, light and dark, and the last thing in the nav on every page."""
    for path, body in _pages(db_path).items():
        nav = body[body.index("<nav") : body.index("</nav>")]
        assert nav.count("data-theme-toggle") == 1, path
        assert "x-data=" in nav
        last = nav[nav.rindex("<li>") :]
        assert "data-theme-toggle" in last, f"the theme button is not last in {path}'s nav"


def test_each_page_marks_itself_current_in_the_nav(db_path: Path) -> None:
    """The shell's nav is one template, so which page is current has to come from the page."""
    pages = _pages(db_path)

    assert '<a href="/" aria-current="page">' in pages["/"]
    assert '<a href="/history" aria-current="page">' in pages["/history"]
    assert '<a href="/settings" aria-current="page">' in pages["/settings"]
    for body in pages.values():
        assert body.count('aria-current="page"') == 1
        assert '<main class="container-fluid">' in body


def test_submit_and_the_mix_are_in_the_nav_and_nowhere_near_the_tiles(db_path: Path) -> None:
    """The dock is the nav's right-hand list: Submit as a plain primary button, the **Mix** as a select."""
    body = _pages(db_path)["/"]
    nav = body[body.index("<nav") : body.index("</nav>")]

    assert 'action="/submit"' in nav
    assert '<button type="submit">Submit batch</button>' in nav
    assert '<section id="mix-switcher"' in nav
    assert "<select" in nav
    assert 'action="/submit"' not in body[body.index("<main") :], "Submit is not under the grid"


def test_the_stylesheet_carries_what_alpine_needs_and_pico_cannot_say(db_path: Path) -> None:
    """`x-cloak` is Alpine's to use and not Alpine's to ship; the root size is pinned against Pico's."""
    harness = make_harness(db_path)

    with TestClient(create_app(harness.core)) as client:
        stylesheet = client.get("/static/base.css").text

    rule = next(block for block in stylesheet.split("}") if "[x-cloak]" in block)
    assert "display: none !important" in rule
    assert "--pico-font-size: 100%" in stylesheet


def test_the_fragments_htmx_swaps_in_are_still_fragments(db_path: Path) -> None:
    """A tile, the grid and the **Mix** switcher never extend the shell: a swap is a fragment, not a page."""
    harness = make_harness(db_path)

    with TestClient(create_app(harness.core)) as client:
        batch_id = batch_id_of(client.get("/").text)
        live = client.get("/").text
        wallpaper_id = re.findall(r'data-wallpaper-id="([^"]+)"', live)[0]
        swaps = [
            client.post(
                "/draft", data={"batch_id": batch_id, "wallpaper_id": wallpaper_id, "verdict": "ban"}
            ),
            client.post("/draft/all", data={"batch_id": batch_id, "verdict": "ban"}),
            client.post("/mix", data={"mix": "refine"}),
        ]

    for swap in swaps:
        assert swap.status_code == 200
        assert "<html" not in swap.text
        assert "<nav" not in swap.text
        assert "/static/" not in swap.text
