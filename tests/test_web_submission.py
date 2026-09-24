"""Submitting a **Batch** from the page. A plain form post — htmx arrives with **Draft Batches** at #3."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import make_harness
from wallpapi.model import Verdict
from wallpapi.web.app import create_app

BATCH_ID = re.compile(r'name="batch_id" value="([0-9a-f]+)"')


def batch_id_of(body: str) -> str:
    match = BATCH_ID.search(body)
    assert match is not None, "the page must carry the Batch ID it will submit"
    return match.group(1)


def test_posting_the_form_records_the_batch_and_renders_the_next_one(db_path: Path) -> None:
    """The acceptance criterion through the web layer: submit, and keep going without a reload."""
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        shown = batch_id_of(client.get("/").text)
        response = client.post("/submit", data={"batch_id": shown})

    assert response.status_code == 200
    history = harness.core.list_history()
    assert len(history) == 8
    assert all(entry.batch_id == shown for entry in history)
    assert all(entry.verdict is Verdict.IGNORE for entry in history)
    assert response.text.count('data-wallpaper-id="') == 8
    assert batch_id_of(response.text) != shown


def test_posting_the_same_batch_twice_is_refused_rather_than_silently_ignored(db_path: Path) -> None:
    """Invariant 7. Two browser tabs is a real case, and the second must be told why nothing happened.

    A 200 with a fresh **Batch** would be the worst answer: the second tab would look like it had recorded
    something.
    """
    harness = make_harness(db_path)
    app = create_app(harness.core)

    with TestClient(app) as client:
        shown = batch_id_of(client.get("/").text)
        client.post("/submit", data={"batch_id": shown})
        second = client.post("/submit", data={"batch_id": shown})

    assert second.status_code == 409
    assert "already" in second.text.lower()
    assert len(harness.core.list_history()) == 8
