"""The **History** page. Issue #7's web half.

The page is a view over the **Decision log**: what it shows is read back through the Core service on every
request, and every edit it posts is an appended entry. These tests check what the page renders and what
the Core service ends up holding, never anything in between.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import Harness, make_harness
from tests.fakes import catalogue_of, wallpaper
from wallpapi.core import HISTORY_PAGE_SIZE, Batch
from wallpapi.model import Verdict
from wallpapi.web.app import create_app


def judge(harness: Harness, **marks: Verdict) -> None:
    """Give each named **Wallpaper** a **Verdict**, in the order given."""
    for wallpaper_id, verdict in marks.items():
        assert harness.core.edit_verdict(wallpaper_id, verdict) is None


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


def test_every_row_carries_the_four_verdict_controls(db_path: Path) -> None:
    """Any **Verdict** can be changed to any other from **History**, **Ignore** included (#37).

    Each control posts the **Verdict** the row should end up with, and carries `hx-sync="this:replace"`
    exactly as a tile does, so a late response cannot swap a stale row back over a newer one. There is no
    separate clear: withdrawing a **Verdict** is posting the **Ignore**.
    """
    harness = make_harness(db_path, catalogue=(wallpaper("judged"),))
    judge(harness, judged=Verdict.LIKE)
    app = create_app(harness.core)

    with TestClient(app) as client:
        body = client.get("/history").text

    for choice in ("favourite", "like", "ban", "ignore"):
        assert f'"verdict": "{choice}"' in body
    assert "/history/clear" not in body
    assert body.count('hx-sync="this:replace"') == 4, "four verdicts"


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


def test_every_page_links_to_the_other_two(db_path: Path) -> None:
    """One nav, three pages. **History** is reachable without typing a URL, and so is the way back."""
    harness = make_harness(db_path, catalogue=catalogue_of(1))
    app = create_app(harness.core)

    with TestClient(app) as client:
        pages = [client.get(path).text for path in ("/", "/history", "/settings")]

    for body in pages:
        assert 'href="/history"' in body
        assert 'href="/settings"' in body
        assert 'href="/"' in body


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


def test_the_row_controls_are_pico_buttons_and_not_the_tile_rail(db_path: Path) -> None:
    """The tile rail is absolutely positioned, hidden until hover and has its words at `font-size: 0`.
    A row carrying its class would inherit all of that the moment a selector stopped being scoped."""
    harness = make_harness(db_path, catalogue=(wallpaper("judged"),))
    judge(harness, judged=Verdict.LIKE)
    app = create_app(harness.core)

    with TestClient(app) as client:
        row = _row(client.get("/history").text, "judged")

    tokens = {token for value in CLASS_ATTRIBUTE.findall(row) for token in value.split()}
    assert "verdicts" not in tokens
    assert "verdict" not in tokens
    assert "history-verdicts" in tokens
    buttons = re.findall(r"<button\b[^>]*>", row)
    assert len(buttons) == 4, "four verdicts"
    for button in buttons:
        assert 'class="outline secondary"' in button or 'class="secondary"' in button
    marked = [button for button in buttons if 'aria-pressed="true"' in button]
    assert len(marked) == 1
    assert 'data-verdict="like"' in marked[0]
    assert 'class="secondary"' in marked[0], "the standing verdict is the filled one"


def test_the_filter_is_a_group_of_buttons_with_the_current_one_marked_and_not_a_link(db_path: Path) -> None:
    """A link to the page you are on is a click that does nothing, so the current filter is marked
    with `aria-current` and has no `href` — as it was a bare span before Pico."""
    harness = make_harness(db_path, catalogue=catalogue_of(2))
    judge(harness, wp0000=Verdict.LIKE, wp0001=Verdict.BAN)
    app = create_app(harness.core)

    with TestClient(app) as client:
        everything = client.get("/history").text
        liked = client.get("/history?verdict=like").text

    for body, current in ((everything, "all"), (liked, "like")):
        group = body[body.index('<div role="group"') : body.index("</div>", body.index('<div role="group"'))]
        assert group.count('aria-current="true"') == 1
        assert f'aria-current="true">{current}</span>' in group
        assert 'class="outline secondary"' in group
        assert group.count('role="button"') == 5, "all, and the four verdicts"
    assert 'href="/history?verdict=like"' in everything
    assert 'href="/history?verdict=like"' not in liked
    assert 'href="/history?verdict=ban"' in liked


def test_the_count_is_small_print_and_paging_is_a_group_of_button_links(db_path: Path) -> None:
    count = HISTORY_PAGE_SIZE + 1
    harness = make_harness(db_path, catalogue=catalogue_of(count), page_size=count)
    judge(harness, **{f"wp{n:04d}": Verdict.LIKE for n in range(count)})
    app = create_app(harness.core)

    with TestClient(app) as client:
        first = client.get("/history").text
        second = client.get("/history?page=2").text

    assert re.search(r"<small>\s*101 wallpapers,\s*page 1 of 2\.\s*</small>", first)
    button = '<a role="button" class="outline secondary"'
    assert f'{button} href="/history?page=2">next</a>' in first
    assert f'{button} href="/history?page=1">previous</a>' in second
    assert "next</a>" not in second


def test_the_stylesheet_rings_a_history_thumbnail_as_it_rings_a_tile(db_path: Path) -> None:
    """The same outline in the same colour for the resolved **Verdict** as for a marked tile, so a
    **Favourite** reads the same on both pages; and the row's buttons take the rail's glyphs."""
    harness = make_harness(db_path)

    with TestClient(create_app(harness.core)) as client:
        stylesheet = client.get("/static/base.css").text

    for verdict in ("favourite", "like", "ban"):
        resolved = f'.history-row[data-resolved-verdict="{verdict}"]'
        ring = next(block for block in stylesheet.split("}") if resolved in block)
        assert f'.tile[data-draft-verdict="{verdict}"]' in ring, "one rule for both, so they cannot drift"
        assert f"var(--{verdict})" in ring
        glyph = next(
            block
            for block in stylesheet.split("}")
            if f'[data-verdict="{verdict}"]::before' in block and "content:" in block
        )
        assert ".history-verdicts" in glyph


def test_the_ignore_control_has_a_glyph_of_its_own(db_path: Path) -> None:
    """The row's words are at `font-size: 0`, so a control with no glyph would be an empty button. The
    tile rail has no **Ignore** to borrow one from — a tile un-marks — so **History** has its own."""
    harness = make_harness(db_path)

    with TestClient(create_app(harness.core)) as client:
        stylesheet = client.get("/static/base.css").text

    assert any(
        '[data-verdict="ignore"]::before' in block and "content:" in block and ".history-verdicts" in block
        for block in stylesheet.split("}")
    )
