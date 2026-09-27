"""The **History** page. Issue #7's web half.

The page is a view over the **Decision log**: what it shows is read back through the Core service on every
request, and every edit it posts is an appended entry. These tests check what the page renders and what
the Core service ends up holding, never anything in between.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import Harness, make_harness
from tests.fakes import catalogue_of, wallpaper
from wallpapi.core import HISTORY_PAGE_SIZE, Batch
from wallpapi.model import Clearance, Verdict
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


def test_every_row_carries_the_edit_and_clear_controls(db_path: Path) -> None:
    """Any **Verdict** can be changed from **History**, and an **Explicit Verdict** can be cleared.

    Each control posts the **Verdict** the row should end up with, and carries `hx-sync="this:replace"`
    exactly as a tile does, so a late response cannot swap a stale row back over a newer one.
    """
    harness = make_harness(db_path, catalogue=(wallpaper("judged"),))
    judge(harness, judged=Verdict.LIKE)
    app = create_app(harness.core)

    with TestClient(app) as client:
        body = client.get("/history").text

    for choice in ("favourite", "like", "ban"):
        assert f'"verdict": "{choice}"' in body
    assert 'hx-post="/history/clear"' in body
    assert body.count('hx-sync="this:replace"') == 4, "three verdicts and a clear"


def test_a_row_with_nothing_standing_has_no_clear_control(db_path: Path) -> None:
    """An **Ignored** **Wallpaper** has no **Explicit Verdict** to withdraw, so no button offers to.

    `clear_verdict` refuses one anyway; a control whose answer is always "nothing happened" should not be
    on the page in the first place.
    """
    harness = make_harness(db_path, catalogue=(wallpaper("ignored"),))
    harness.core.update_settings(batch_size=1)
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    harness.core.submit_batch(batch.id)
    app = create_app(harness.core)

    with TestClient(app) as client:
        body = client.get("/history").text

    assert 'data-resolved-verdict="ignore"' in body
    assert 'hx-post="/history/clear"' not in body


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


def test_posting_a_clear_swaps_the_row_back_with_nothing_standing(db_path: Path) -> None:
    """Its own route, because a **Clearance** is an entry rather than the absence of one — and the row
    that comes back shows the **Ignores** stacking again."""
    harness = make_harness(db_path, catalogue=(wallpaper("judged"),))
    harness.core.update_settings(batch_size=1)
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    harness.core.submit_batch(batch.id)
    judge(harness, judged=Verdict.FAVOURITE)
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.post("/history/clear", data={"wallpaper_id": "judged"})

    assert response.status_code == 200
    assert 'data-resolved-verdict="ignore"' in response.text
    assert [e.entry for e in harness.core.list_history(wallpaper_id="judged")][-1] is Clearance.CLEARED


def test_a_hand_made_ignore_post_is_refused(db_path: Path) -> None:
    """No control posts an **Ignore** — it is derived at submit, never chosen. This guards the post a
    person could still make by hand, and nothing is appended when they do."""
    harness = make_harness(db_path, catalogue=(wallpaper("judged"),))
    judge(harness, judged=Verdict.LIKE)
    app = create_app(harness.core)

    with TestClient(app) as client:
        ignored = client.post("/history/verdict", data={"wallpaper_id": "judged", "verdict": "ignore"})
        blank = client.post("/history/verdict", data={"wallpaper_id": "judged", "verdict": ""})
        unknown = client.post("/history/verdict", data={"wallpaper_id": "nope", "verdict": "like"})

    assert ignored.status_code == 400
    assert blank.status_code == 400
    assert unknown.status_code == 404
    assert len(harness.core.list_history()) == 1


def test_clearing_a_row_with_nothing_standing_is_refused(db_path: Path) -> None:
    """Two tabs: the row was cleared in one, and the other still shows the control. The second post is
    told why nothing happened rather than being absorbed — and htmx leaves the stale row alone."""
    harness = make_harness(db_path, catalogue=(wallpaper("judged"),))
    judge(harness, judged=Verdict.LIKE)
    app = create_app(harness.core)

    with TestClient(app) as client:
        first = client.post("/history/clear", data={"wallpaper_id": "judged"})
        second = client.post("/history/clear", data={"wallpaper_id": "judged"})

    assert first.status_code == 200
    assert second.status_code == 409
    assert len(harness.core.list_history(wallpaper_id="judged")) == 2


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
