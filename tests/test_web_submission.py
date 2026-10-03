"""Submitting a **Batch** from the page: an htmx post that answers with a banner and has the shell refetch."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import batch_id_of, make_harness
from wallpapi.model import Verdict
from wallpapi.web.app import create_app


def test_posting_the_form_records_the_batch_and_the_next_fetch_is_a_new_one(db_path: Path) -> None:
    """The acceptance criterion through the web layer: submit, and keep going without a reload."""
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        shown = batch_id_of(client.get("/batch").text)
        response = client.post("/submit", data={"batch_id": shown})
        following = client.get("/batch")

    assert response.status_code == 200
    history = harness.core.list_history()
    assert len(history) == 8
    assert all(entry.batch_id == shown for entry in history)
    assert all(entry.entry is Verdict.IGNORE for entry in history)
    assert following.text.count('data-wallpaper-id="') == 8
    assert batch_id_of(following.text) != shown


def test_a_submission_answers_with_the_banner_and_tells_the_page_to_refetch(db_path: Path) -> None:
    """No redirect and nothing stored: the banner is the response, and the grid is `/batch`'s job."""
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        shown = batch_id_of(client.get("/batch").text)
        response = client.post("/submit", data={"batch_id": shown}, follow_redirects=False)

    assert response.status_code == 200
    assert response.headers["HX-Trigger"] == "batch-submitted"
    assert "set-cookie" not in response.headers
    assert 'data-wallpaper-id="' not in response.text, "the response is the banner, not the grid"


def test_the_shell_fetches_the_batch_and_refetches_it_on_submission(db_path: Path) -> None:
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        shell = client.get("/").text

    assert 'hx-get="/batch"' in shell
    assert 'hx-trigger="load, batch-submitted from:body"' in shell
    assert 'hx-post="/submit"' in shell
    assert shell.count('class="placeholder"') == 8, "one stand-in per slot of the batch size"
    assert 'data-wallpaper-id="' not in shell, "the shell mints nothing"


def test_a_refused_submission_still_refetches_so_a_stale_tab_catches_up(db_path: Path) -> None:
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        shown = batch_id_of(client.get("/batch").text)
        client.post("/submit", data={"batch_id": shown})
        second = client.post("/submit", data={"batch_id": shown})

    assert second.headers["HX-Trigger"] == "batch-submitted"


def test_the_next_page_says_what_the_submission_recorded(db_path: Path) -> None:
    """Without this, submitting and refreshing look identical — a new grid either way.

    The count comes from the **Decision log** rather than from the form, so it says what was actually
    appended rather than what the page claimed to be showing.
    """
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        shown = batch_id_of(client.get("/batch").text)
        response = client.post("/submit", data={"batch_id": shown})

    assert "Recorded 8 ignores" in response.text


def test_posting_the_same_batch_twice_is_refused_rather_than_silently_ignored(db_path: Path) -> None:
    """Invariant 7. Two browser tabs is a real case, and the second must be told why nothing happened.

    A 200 with a fresh **Batch** would be the worst answer: the second tab would look like it had recorded
    something.
    """
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        shown = batch_id_of(client.get("/batch").text)
        client.post("/submit", data={"batch_id": shown})
        second = client.post("/submit", data={"batch_id": shown})

    assert second.status_code == 409
    assert "already" in second.text.lower()
    assert len(harness.core.list_history()) == 8
