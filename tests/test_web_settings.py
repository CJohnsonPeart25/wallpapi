"""The settings page. Issue #4.

The form posts strings and the Core service is the only thing that decides whether they are settings, so
these tests check what the page shows and what the Core service ends up holding, never a validator in
between.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import make_harness
from tests.fakes import catalogue_of
from wallpapi.core import MAX_BATCH_SIZE, Batch
from wallpapi.model import Verdict
from wallpapi.web.app import create_app


def test_the_page_renders_the_current_settings(db_path: Path, tmp_path: Path) -> None:
    """GET /settings shows what is stored, so the form is an edit rather than a blank slate."""
    harness = make_harness(db_path)
    chosen = tmp_path / "Wallpapers"
    harness.core.update_settings(batch_size=12, library_path=chosen)
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.get("/settings")

    assert response.status_code == 200
    assert 'name="batch_size"' in response.text
    assert 'value="12"' in response.text
    assert str(chosen) in response.text


def test_posting_valid_settings_persists_them(db_path: Path, tmp_path: Path) -> None:
    """Acceptance criteria 1 and 2 through the web layer."""
    harness = make_harness(db_path)
    chosen = tmp_path / "Wallpapers"
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.post("/settings", data={"batch_size": "2", "library_path": str(chosen)})

    assert response.status_code == 200
    settings = harness.core.get_settings()
    assert settings.batch_size == 2
    assert settings.library_path == chosen


def test_saving_redirects_so_a_refresh_does_not_repost(db_path: Path, tmp_path: Path) -> None:
    """Post-redirect-get, unlike submitting a **Batch**.

    Submitting is refused on a refresh because appending to the **Decision log** twice is a real mistake
    worth shouting about. Saving the same settings twice is the same settings, so the kinder answer is for
    the refresh to be a plain page load.
    """
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.post(
            "/settings",
            data={"batch_size": "4", "library_path": str(tmp_path / "Wallpapers")},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/settings")


def test_posting_an_invalid_batch_size_shows_the_reason_and_persists_nothing(
    db_path: Path, tmp_path: Path
) -> None:
    """One error branch: the Core service's refusal reason is what the page renders."""
    harness = make_harness(db_path)
    chosen = tmp_path / "Wallpapers"
    harness.core.update_settings(batch_size=8, library_path=chosen)
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.post(
            "/settings", data={"batch_size": "0", "library_path": str(tmp_path / "Elsewhere")}
        )

    assert response.status_code == 400
    assert str(MAX_BATCH_SIZE) in response.text, "the page should say what the accepted range is"
    settings = harness.core.get_settings()
    assert settings.batch_size == 8
    assert settings.library_path == chosen


def test_a_non_numeric_batch_size_is_a_rendered_refusal_rather_than_a_validation_error(
    db_path: Path, tmp_path: Path
) -> None:
    """Typing "eight" must come back as the settings page, not FastAPI's JSON 422.

    This is why the form fields are taken as strings and coerced in the Core service: a typed `int` at the
    edge would answer with a payload the browser renders as a wall of JSON.
    """
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.post(
            "/settings", data={"batch_size": "eight", "library_path": str(tmp_path / "Wallpapers")}
        )

    assert response.status_code == 400
    assert "text/html" in response.headers["content-type"]
    assert harness.core.get_settings().batch_size == 8


def test_posting_a_relative_library_path_shows_the_reason_and_persists_nothing(db_path: Path) -> None:
    """Invariant 9: an absolute path or nothing."""
    harness = make_harness(db_path)
    before = harness.core.get_settings().library_path
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.post("/settings", data={"batch_size": "8", "library_path": "Wallpapers"})

    assert response.status_code == 400
    assert "absolute" in response.text.lower()
    assert harness.core.get_settings().library_path == before


def test_a_refused_post_keeps_what_was_typed(db_path: Path) -> None:
    """Re-rendering with the stored values would silently throw away the rest of the form."""
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.post("/settings", data={"batch_size": "0", "library_path": "Wallpapers"})

    assert 'value="0"' in response.text
    assert 'value="Wallpapers"' in response.text


def test_the_batch_page_links_to_the_settings_page(db_path: Path) -> None:
    """Otherwise the page exists and nothing reaches it."""
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.get("/")

    assert 'href="/settings"' in response.text


def test_the_settings_page_links_back_to_the_batch_page(db_path: Path) -> None:
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.get("/settings")

    assert 'href="/"' in response.text


def test_a_batch_size_saved_from_the_page_reaches_the_next_batch(db_path: Path, tmp_path: Path) -> None:
    """End to end: the acceptance criterion as a user would meet it.

    The live **Batch** is submitted in between, because the new size applies to the next **Batch** minted
    rather than to the one already on screen.
    """
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        shown = client.get("/batch").text
        assert shown.count('data-wallpaper-id="') == 8
        batch_id = shown.split('name="batch_id" value="')[1].split('"')[0]
        client.post("/settings", data={"batch_size": "2", "library_path": str(tmp_path / "Wallpapers")})
        client.post("/submit", data={"batch_id": batch_id})
        after = client.get("/batch")
        shell = client.get("/")

    assert after.text.count('data-wallpaper-id="') == 2
    assert shell.text.count('class="placeholder"') == 2, "the shell's stand-ins follow the setting too"


def test_the_page_renders_the_filters_and_the_pool_target(db_path: Path) -> None:
    """Issue #6: the **Filters** and the **Pool** target size are editable from the settings page."""
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.get("/settings")

    body = response.text
    assert response.status_code == 200
    for field in ("min_width", "min_height", "allowed_ratios", "min_favourites", "pool_target_size"):
        assert f'name="{field}"' in body
    assert 'value="2560"' in body
    assert 'value="16x9,16x10,21x9"' in body


def test_saving_the_filters_from_the_page_persists_them(db_path: Path) -> None:
    """Acceptance criterion, through the web layer: the **Filters** are configurable and persisted."""
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.post(
            "/settings",
            data={
                "min_width": "1920",
                "min_height": "1080",
                "allowed_ratios": "21x9, 32x9",
                "min_favourites": "40",
                "pool_target_size": "150",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    settings = harness.core.get_settings()
    assert (settings.min_width, settings.min_height) == (1920, 1080)
    assert settings.allowed_ratios == ("21x9", "32x9")
    assert settings.min_favourites == 40
    assert settings.pool_target_size == 150


def test_an_unknown_ratio_is_a_rendered_refusal_naming_the_ones_that_work(db_path: Path) -> None:
    """A reason with no words on the page renders as the fallback, so every new reason gets a branch."""
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.post("/settings", data={"allowed_ratios": "16:9"})

    assert response.status_code == 400
    assert "16x9" in response.text
    assert "allowed_ratios_invalid" not in response.text, "the reason needs words, not its enum value"
    assert harness.core.get_settings().allowed_ratios == ("16x9", "16x10", "21x9")


def test_a_post_that_names_one_field_leaves_the_others_alone(db_path: Path) -> None:
    """ADR 0004's convention at the edge: a field the post did not carry keeps the value it had.

    The real form always posts every field. This is what stops a partial post — a script, a page rendered
    before a section existed — from silently resetting the settings it never knew about.
    """
    harness = make_harness(db_path)
    harness.core.update_settings(min_favourites=77)
    app = create_app(harness.core)

    with TestClient(app) as client:
        client.post("/settings", data={"batch_size": "4"}, follow_redirects=False)

    settings = harness.core.get_settings()
    assert settings.batch_size == 4
    assert settings.min_favourites == 77


def test_a_refused_filter_post_keeps_what_was_typed_beside_what_is_stored(db_path: Path) -> None:
    """Correcting one field must not mean retyping the rest — including the fields the post never carried."""
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.post("/settings", data={"min_width": "9999", "allowed_ratios": "nope"})

    assert response.status_code == 400
    assert 'value="9999"' in response.text
    assert 'value="nope"' in response.text
    assert 'value="1440"' in response.text, "the untouched minimum height should still be shown"


# -- Download all Favourites (#14) -----------------------------------------------------------------------


def test_the_page_offers_a_download_for_every_favourite(db_path: Path) -> None:
    """The one-way sync has to be somewhere the user can press it, and settings is where the folder is."""
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.get("/settings")

    assert 'action="/settings/library/download"' in response.text


def test_downloading_the_favourites_writes_them_and_says_how_many(db_path: Path, tmp_path: Path) -> None:
    """The acceptance criterion through the page: written, skipped and failed, in words the user reads.

    The fake writer records paths that are not on this disk, which is exactly the state the button exists
    for — a database carried to a machine whose **Library** folder is empty.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(2))
    harness.core.update_settings(batch_size=2, library_path=tmp_path / "Library")
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    harness.core.set_all_draft_verdicts(batch.id, Verdict.FAVOURITE)
    harness.core.submit_batch(batch.id)
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.post("/settings/library/download")

    assert response.status_code == 200
    assert "2 downloaded" in response.text
    assert "0 already there" in response.text
    assert "0 failed" in response.text
    assert len(harness.library.written) == 4


def test_downloading_the_favourites_redirects_so_a_refresh_does_not_repost(
    db_path: Path, tmp_path: Path
) -> None:
    """Post-redirect-get, as every other write on this page is.

    The operation is idempotent, so a reposted download would be harmless — but it is also a folder full
    of network calls, and a refresh should not be one.
    """
    harness = make_harness(db_path)
    harness.core.update_settings(library_path=tmp_path / "Library")
    app = create_app(harness.core)

    with TestClient(app, follow_redirects=False) as client:
        response = client.post("/settings/library/download")

    assert response.status_code == 303
    assert response.headers["location"].startswith("/settings?")


def test_a_download_with_nothing_to_do_says_so_rather_than_nothing(db_path: Path, tmp_path: Path) -> None:
    """Zero of everything is an answer. A page that changed in no visible way would look broken."""
    harness = make_harness(db_path)
    harness.core.update_settings(library_path=tmp_path / "Library")
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.post("/settings/library/download")

    assert "0 downloaded" in response.text
    assert harness.library.written == []


def test_a_failed_download_is_reported_on_the_page(db_path: Path, tmp_path: Path) -> None:
    """Failures are counted, not raised: the **Favourite** stands and the next press is the whole retry."""
    harness = make_harness(db_path, catalogue=catalogue_of(1))
    harness.core.update_settings(batch_size=1, library_path=tmp_path / "Library")
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    harness.core.set_all_draft_verdicts(batch.id, Verdict.FAVOURITE)
    harness.core.submit_batch(batch.id)
    harness.library.fail_for.add(batch.wallpapers[0].id)
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.post("/settings/library/download")

    assert "1 failed" in response.text
    assert "0 downloaded" in response.text


# -- On the shared shell (#41) ---------------------------------------------------------------------------


def _inside_an_article(body: str, index: int) -> bool:
    before = body[:index]
    return before.rfind("<article") > before.rfind("</article>")


def test_the_saved_and_refused_messages_are_plain_paragraphs(db_path: Path) -> None:
    """A refusal sits in an `<article>` so it stands out from the page; a saved note does not need to."""
    harness = make_harness(db_path)

    with TestClient(create_app(harness.core)) as client:
        saved = client.get("/settings?saved=1").text
        refused = client.post("/settings", data={"batch_size": "0"}).text

    assert "<p>Settings saved.</p>" in saved
    reason = refused.index("Batch size must be between")
    assert _inside_an_article(refused, reason)
    assert refused.rfind("<p>", 0, reason) > refused.rfind("<article>", 0, reason)
    assert "Nothing was changed." in refused
