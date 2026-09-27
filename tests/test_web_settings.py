"""The settings page. Issue #4.

The form posts strings and the Core service is the only thing that decides whether they are settings, so
these tests check what the page shows and what the Core service ends up holding, never a validator in
between.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import make_harness
from wallpapi.core import MAX_BATCH_SIZE
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
        shown = client.get("/").text
        assert shown.count('data-wallpaper-id="') == 8
        batch_id = shown.split('name="batch_id" value="')[1].split('"')[0]
        client.post("/settings", data={"batch_size": "2", "library_path": str(tmp_path / "Wallpapers")})
        after = client.post("/submit", data={"batch_id": batch_id})

    assert after.text.count('data-wallpaper-id="') == 2
