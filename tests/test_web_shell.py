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


def test_the_fragments_htmx_swaps_in_are_still_fragments(db_path: Path) -> None:
    """The **Batch**, a tile, the grid, the **Mix** switcher and the submission banner never extend the
    shell: a swap is a fragment, not a page."""
    harness = make_harness(db_path)

    with TestClient(create_app(harness.core)) as client:
        fetched = client.get("/batch")
        batch_id = batch_id_of(fetched.text)
        wallpaper_id = re.findall(r'data-wallpaper-id="([^"]+)"', fetched.text)[0]
        swaps = [
            fetched,
            client.post(
                "/draft", data={"batch_id": batch_id, "wallpaper_id": wallpaper_id, "verdict": "ban"}
            ),
            client.post("/draft/all", data={"batch_id": batch_id, "verdict": "ban"}),
            client.post("/mix", data={"mix": "refine"}),
            client.post("/submit", data={"batch_id": batch_id}),
        ]

    for swap in swaps:
        assert swap.status_code == 200
        assert "<html" not in swap.text
        assert "<nav" not in swap.text
        assert "/static/" not in swap.text
