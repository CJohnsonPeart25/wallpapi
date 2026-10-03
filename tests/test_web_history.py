"""The **History** page. Issue #7's web half.

The page is a view over the **Decision log**: what it shows is read back through the Core service on every
request, and every edit it posts is an appended entry. These tests check what the page renders and what
the Core service ends up holding, never anything in between.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import judge, make_harness
from tests.fakes import catalogue_of, wallpaper
from wallpapi.core import HISTORY_PAGE_SIZE, Batch
from wallpapi.model import Verdict
from wallpapi.web.app import create_app


def test_the_page_lists_a_row_per_judged_wallpaper_with_its_thumbnail_and_verdict(db_path: Path) -> None:
    """Acceptance criterion: **History** lists **Verdicts** with thumbnail, **Verdict** and timestamp.

    The thumbnail comes off the **Thumbnail cache** at `/thumb/{id}` and is never hotlinked (invariant 8),
    and the Wallhaven link opens in a new tab so working through **History** is not interrupted.
    """
    harness = make_harness(db_path, catalogue=(wallpaper("judged"), wallpaper("untouched")))
    judge(harness, judged=Verdict.LIKE)
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.get("/history")

    assert response.status_code == 200
    assert response.text.count('class="history-row"') == 1
    assert 'data-wallpaper-id="judged"' in response.text
    assert 'src="/thumb/judged"' in response.text
    assert 'data-resolved-verdict="like"' in response.text
    assert "2026-09-24 11:30 UTC" in response.text
    assert 'href="https://wallhaven.cc/w/judged"' in response.text
    assert "untouched" not in response.text


def test_an_ignored_row_shows_ignore_as_its_standing_verdict(db_path: Path) -> None:
    """An **Ignored** **Wallpaper** has its **Ignore** filled, as any standing **Verdict** is."""
    harness = make_harness(db_path, catalogue=(wallpaper("ignored"),))
    harness.core.update_settings(batch_size=1)
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    harness.core.submit_batch(batch.id)
    app = create_app(harness.core)

    with TestClient(app) as client:
        row = _row(client.get("/history").text, "ignored")

    marked = [b for b in re.findall(r"<button\b[^>]*>", row) if 'aria-pressed="true"' in b]
    assert len(marked) == 1
    assert 'data-verdict="ignore"' in marked[0]


def test_the_filter_narrows_the_listing(db_path: Path) -> None:
    """Filtering by resolved **Verdict**, which is what keeps thousands of **Ignores** off the page."""
    harness = make_harness(db_path, catalogue=catalogue_of(3))
    judge(harness, wp0000=Verdict.LIKE, wp0001=Verdict.BAN, wp0002=Verdict.LIKE)
    app = create_app(harness.core)

    with TestClient(app) as client:
        liked = client.get("/history?verdict=like")
        unknown = client.get("/history?verdict=sideways")

    assert liked.status_code == 200
    assert liked.text.count('class="history-row"') == 2
    assert 'data-wallpaper-id="wp0001"' not in liked.text
    assert unknown.status_code == 400


def test_the_page_is_paged_and_the_links_carry_the_filter(db_path: Path) -> None:
    """A hundred rows a page, with a next link that keeps the filter the user is looking through."""
    count = HISTORY_PAGE_SIZE + 1
    harness = make_harness(db_path, catalogue=catalogue_of(count), page_size=count)
    judge(harness, **{f"wp{n:04d}": Verdict.LIKE for n in range(count)})
    app = create_app(harness.core)

    with TestClient(app) as client:
        first = client.get("/history?verdict=like")
        second = client.get("/history?page=2&verdict=like")

    assert first.text.count('class="history-row"') == HISTORY_PAGE_SIZE
    assert "/history?page=2&verdict=like" in first.text
    assert "previous" not in first.text
    assert second.text.count('class="history-row"') == 1
    assert 'data-wallpaper-id="wp0000"' in second.text


def test_posting_an_edit_swaps_the_row_back_showing_the_new_verdict(db_path: Path) -> None:
    """The row and not the page: an edit changes one **Wallpaper**, and re-rendering a hundred rows
    would fight with whatever else the user has clicked since."""
    harness = make_harness(db_path, catalogue=(wallpaper("judged"),))
    judge(harness, judged=Verdict.LIKE)
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.post("/history/verdict", data={"wallpaper_id": "judged", "verdict": "ban"})

    assert response.status_code == 200
    assert response.text.count('class="history-row"') == 1
    assert 'data-resolved-verdict="ban"' in response.text
    assert "<html" not in response.text, "the swap is the row, not the page"
    assert harness.core.resolve_verdicts(["judged"])["judged"].verdict is Verdict.BAN


def test_posting_an_ignore_overturns_the_verdict_and_swaps_the_row_back(db_path: Path) -> None:
    """How **History** withdraws a **Verdict** since #37: the same **Ignore** unmarking a tile writes,
    appended with no **Batch**, and the latest entry decides — so the **Favourite** stands no longer."""
    harness = make_harness(db_path, catalogue=(wallpaper("judged"),))
    judge(harness, judged=Verdict.FAVOURITE)
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.post("/history/verdict", data={"wallpaper_id": "judged", "verdict": "ignore"})

    assert response.status_code == 200
    assert 'data-resolved-verdict="ignore"' in response.text
    latest = harness.core.list_history(wallpaper_id="judged")[-1]
    assert (latest.entry, latest.batch_id) == (Verdict.IGNORE, None)


def test_a_blank_or_unknown_post_is_refused(db_path: Path) -> None:
    """No control posts either. This guards the post a person could still make by hand, and nothing is
    appended when they do."""
    harness = make_harness(db_path, catalogue=(wallpaper("judged"),))
    judge(harness, judged=Verdict.LIKE)
    app = create_app(harness.core)

    with TestClient(app) as client:
        blank = client.post("/history/verdict", data={"wallpaper_id": "judged", "verdict": ""})
        sideways = client.post("/history/verdict", data={"wallpaper_id": "judged", "verdict": "sideways"})
        unknown = client.post("/history/verdict", data={"wallpaper_id": "nope", "verdict": "like"})

    assert blank.status_code == 400
    assert sideways.status_code == 400
    assert unknown.status_code == 404
    assert len(harness.core.list_history()) == 1


def test_the_empty_page_says_so_rather_than_rendering_nothing(db_path: Path) -> None:
    """A **History** with no rows is a state worth explaining, and a filter with no matches is a
    different one — the way out of the second is a link back to all of them."""
    harness = make_harness(db_path, catalogue=catalogue_of(2))
    app = create_app(harness.core)

    with TestClient(app) as client:
        empty = client.get("/history")
        judge(harness, wp0000=Verdict.LIKE)
        filtered = client.get("/history?verdict=ban")

    assert "Nothing judged yet" in empty.text
    assert "Nothing resolves to ban yet" in filtered.text


# -- On the shared shell (#41) ---------------------------------------------------------------------------

CLASS_ATTRIBUTE = re.compile(r'class="([^"]*)"')


def _row(body: str, wallpaper_id: str) -> str:
    """The markup of one row, from its opening `<tr` to its `</tr>`."""
    start = body.rindex("<tr", 0, body.index(f'id="history-{wallpaper_id}"'))
    return body[start : body.index("</tr>", start) + len("</tr>")]


def test_the_listing_is_a_striped_table_with_a_row_per_wallpaper(db_path: Path) -> None:
    """Pico's own table, and the row is one `<tr>` whether it is rendered in the page or swapped alone.

    The swap target is the row's `<tr>`: a `<li>` or a `<div>` coming back into a `<tbody>` would be
    dropped by the parser, and the edit would appear to do nothing.
    """
    harness = make_harness(db_path, catalogue=(wallpaper("judged"),))
    judge(harness, judged=Verdict.LIKE)
    app = create_app(harness.core)

    with TestClient(app) as client:
        body = client.get("/history").text
        swapped = client.post("/history/verdict", data={"wallpaper_id": "judged", "verdict": "ban"}).text

    table = body[body.index('<table class="striped">') : body.index("</table>")]
    assert table.count('class="history-row"') == 1
    assert body.count('hx-target="closest tr"') == 4, "four verdicts, each swapping its row"
    assert re.sub(r"\{#.*?#\}", "", swapped, flags=re.S).lstrip().startswith("<tr")
    assert swapped.rstrip().endswith("</tr>")


def test_a_row_shows_its_thumbnail_verdict_time_controls_and_link(db_path: Path) -> None:
    """The five columns: the thumbnail off the cache, the resolved **Verdict** by name, the time, the
    controls, and the Wallhaven link shown rather than hidden as it was under the tile's rules.

    The thumbnail has no `width`/`height` attributes, for the reason the tile has none: they describe the
    full-resolution file, not `thumbs.small`, and laid the image out at the wallpaper's size.
    """
    harness = make_harness(db_path, catalogue=(wallpaper("judged"),))
    judge(harness, judged=Verdict.FAVOURITE)
    app = create_app(harness.core)

    with TestClient(app) as client:
        row = _row(client.get("/history").text, "judged")

    assert row.count("<td") == 5
    thumbnail = re.search(r"<img\b[^>]*>", row)
    assert thumbnail
    assert 'src="/thumb/judged"' in thumbnail.group(0)
    assert "width=" not in thumbnail.group(0)
    assert "height=" not in thumbnail.group(0)
    assert ">favourite<" in row
    assert "<time" in row
    assert 'href="https://wallhaven.cc/w/judged"' in row
    assert "links" not in CLASS_ATTRIBUTE.findall(row), "the tile's rule hid anything classed links"
