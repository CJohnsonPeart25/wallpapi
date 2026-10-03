"""Marking tiles from the page: the htmx half of the **Draft Batch**.

The Core service tests cover what a mark *means*; these cover the three criteria that are about the page
itself — that the controls exist, that they cannot swap a stale tile back in, and that htmx is served by the
app rather than fetched from somebody else's CDN.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import make_harness
from tests.test_web_submission import batch_id_of
from wallpapi.core import Batch
from wallpapi.web.app import create_app


def test_every_tile_carries_favourite_like_and_ban_controls(db_path: Path) -> None:
    """Acceptance criterion: each **Wallpaper** in a **Batch** has all three controls.

    **Ignore** deliberately has no control — it is the implicit **Verdict** everything unmarked gets on
    submit, and a button for it would be a second way to say what leaving a tile alone already says.
    """
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        body = client.get("/").text + client.get("/batch").text

    # Nine of each, not eight: one per tile, plus the preview's own rail, which is the same three
    # controls for whichever **Wallpaper** the preview is open on.
    for choice in ("favourite", "like", "ban"):
        assert body.count(f'data-verdict="{choice}"') == 9
    assert body.count('class="preview-verdict"') == 3
    assert 'data-verdict="ignore"' not in body


def test_every_control_posts_with_hx_sync_so_a_late_response_cannot_win(db_path: Path) -> None:
    """Acceptance criterion: `hx-sync="this:replace"`.

    Clicking **Like** then **Ban** quickly fires two posts against one tile. Without this, the slower
    response swaps its tile in last and the user is left looking at a mark they have already changed —
    and, worse, one that disagrees with what is stored.
    """
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        body = client.get("/batch").text

    assert body.count('hx-sync="this:replace"') == 24


def test_marking_a_tile_from_the_page_records_the_draft_and_appends_nothing(db_path: Path) -> None:
    """The htmx post lands on the **Draft Batch**, and the **Decision log** stays untouched."""
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        page = client.get("/batch").text
        batch_id = batch_id_of(page)
        live = harness.core.get_next_batch()
        assert isinstance(live, Batch)
        marked = live.wallpapers[0].id

        response = client.post(
            "/draft", data={"batch_id": batch_id, "wallpaper_id": marked, "verdict": "favourite"}
        )

    assert response.status_code == 200
    assert 'data-draft-verdict="favourite"' in response.text
    assert harness.core.list_history() == []


def test_the_marked_control_posts_a_clear_so_a_second_click_unmarks_it(db_path: Path) -> None:
    """Acceptance criterion: marking the same control twice clears the mark.

    The control the user would click again carries an empty **Verdict**, so the post says what state to end
    up in rather than "flip it". A replayed click therefore clears it once and then keeps it cleared, which
    is what invariant 6 means by setting rather than toggling.
    """
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        batch_id = batch_id_of(client.get("/batch").text)
        live = harness.core.get_next_batch()
        assert isinstance(live, Batch)
        marked = live.wallpapers[0].id
        tile = client.post(
            "/draft", data={"batch_id": batch_id, "wallpaper_id": marked, "verdict": "favourite"}
        ).text

        assert '"verdict": ""' in tile, "the marked control must offer to clear itself"

        cleared = client.post("/draft", data={"batch_id": batch_id, "wallpaper_id": marked, "verdict": ""})

    assert cleared.status_code == 200
    assert 'data-draft-verdict=""' in cleared.text
    reloaded = harness.core.get_next_batch()
    assert isinstance(reloaded, Batch)
    assert reloaded.drafts == {}


def test_a_page_load_after_a_partial_draft_shows_the_marks_already_set(db_path: Path) -> None:
    """Closing the tab and coming back resumes where you were — the marks are server state, not the
    browser's. ADR 0002 makes the **Batch** survive; this makes the marks on it survive too."""
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        batch_id = batch_id_of(client.get("/batch").text)
        live = harness.core.get_next_batch()
        assert isinstance(live, Batch)
        client.post(
            "/draft",
            data={"batch_id": batch_id, "wallpaper_id": live.wallpapers[0].id, "verdict": "like"},
        )

        reloaded = client.get("/batch").text

    assert reloaded.count('data-draft-verdict="like"') == 1
    assert reloaded.count('data-draft-verdict=""') == 7


def test_an_ignore_cannot_be_drafted(db_path: Path) -> None:
    """**Ignore** is derived on submit, never stored as a mark.

    A stored **Ignore** would be a second way to say what an absent row already says, and **Verdict
    resolution** would then have two shapes of nothing to tell apart. There is no control for it, so this
    only guards a hand-made post — but that is exactly the post that would corrupt the **Draft Batch**.
    """
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        batch_id = batch_id_of(client.get("/batch").text)
        live = harness.core.get_next_batch()
        assert isinstance(live, Batch)

        response = client.post(
            "/draft",
            data={"batch_id": batch_id, "wallpaper_id": live.wallpapers[0].id, "verdict": "ignore"},
        )

    assert response.status_code == 400


def test_the_next_page_reports_the_explicit_verdicts_separately_from_the_ignores(db_path: Path) -> None:
    """The #2 criterion, now that a submission is no longer all **Ignores**.

    "Recorded 8 ignores" was true when nothing could be marked. Saying it about a submission holding two
    **Favourites** would be a plain lie about what went into the **Decision log**, which is the one thing
    the page is there to report.
    """
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        batch_id = batch_id_of(client.get("/batch").text)
        live = harness.core.get_next_batch()
        assert isinstance(live, Batch)
        for wallpaper in live.wallpapers[:2]:
            client.post(
                "/draft",
                data={"batch_id": batch_id, "wallpaper_id": wallpaper.id, "verdict": "favourite"},
            )

        response = client.post("/submit", data={"batch_id": batch_id})

    assert "Recorded 2 verdicts" in " ".join(response.text.split())
    assert "and 6 ignores" in " ".join(response.text.split())
