"""The **Explore** / **Refine** switcher on the **Batch** page. Issue #10.

FastAPI's `TestClient` against `create_app(core)` with the fakes behind it, like every other web test
here. Nothing touches the network, and what is asserted is what the page says and what the Core service
holds afterwards — never the markup for its own sake.
"""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import make_harness
from tests.zoned import zoned_pool
from wallpapi.core import REFINE_MIX, Batch
from wallpapi.model import Verdict
from wallpapi.web.app import create_app


def test_the_batch_page_names_the_active_mix_and_offers_the_other(db_path: Path) -> None:
    """The acceptance criterion as the user meets it: which **Mix** is in force, and a way to change it.

    A dropdown, whose options carry the percentages — "explore" and "refine" are names for 75/20/5 and
    25/70/5, and the names on their own say nothing about which way round they are. The one in force is
    the selected option, which is what a `<select>` shows when it is closed.
    """
    harness = make_harness(db_path)

    with TestClient(create_app(harness.core)) as client:
        page = client.get("/")

    assert 'value="explore"' in page.text
    assert 'value="refine"' in page.text
    assert "75/20/5" in page.text
    assert "25/70/5" in page.text
    # Which one is in force, said in the markup rather than left to whichever option happens to be
    # first: it is the selected option, and the attribute is what a test or a script can read it off.
    assert 'data-mix-active="explore"' in page.text
    assert page.text.count("selected") == 1


def test_switching_persists_and_the_swapped_section_shows_the_new_mix(db_path: Path) -> None:
    """The route the switcher posts to: stored through the Core service, and re-rendered from what is
    stored rather than from what was posted."""
    harness = make_harness(db_path)

    with TestClient(create_app(harness.core)) as client:
        switched = client.post("/mix", data={"mix": "refine"})
        reloaded = client.get("/")

    assert switched.status_code == HTTPStatus.OK
    assert 'id="mix-switcher"' in switched.text
    assert harness.core.active_mix() == REFINE_MIX
    assert 'data-mix-active="refine"' in reloaded.text


def test_switching_does_not_disturb_the_batch_on_screen(db_path: Path) -> None:
    """The same rule the batch size has had since #4: the **Mix** is read when a **Batch** is minted.

    The response is the switcher alone, so the grid is not swapped and the **Draft Batch** the user is
    part way through is untouched — which is checked here by the mark surviving the switch, not merely by
    the **Batch** ID.
    """
    pool = zoned_pool(db_path, bangers=10, duds=10, unknowns=20)
    pool.harness.core.update_settings(batch_size=8)

    with TestClient(create_app(pool.harness.core)) as client:
        client.get("/")
        live = pool.harness.core.get_next_batch()
        assert isinstance(live, Batch)
        marked = live.wallpapers[0].id
        client.post("/draft", data={"batch_id": live.id, "wallpaper_id": marked, "verdict": "like"})
        switched = client.post("/mix", data={"mix": "refine"})

    assert "batch-grid" not in switched.text
    after = pool.harness.core.get_next_batch()
    assert isinstance(after, Batch)
    assert after.id == live.id
    assert after.wallpapers == live.wallpapers
    assert after.drafts == {marked: Verdict.LIKE}


def test_a_mix_nobody_has_heard_of_is_a_bad_request_and_changes_nothing(db_path: Path) -> None:
    """No control can post this — it answers a hand-made post, and the same 400 is what a **Mix** deleted
    in another tab at #12 would give. The switcher comes back unchanged rather than blank."""
    harness = make_harness(db_path)

    with TestClient(create_app(harness.core)) as client:
        refused = client.post("/mix", data={"mix": "nope"})

    assert refused.status_code == HTTPStatus.BAD_REQUEST
    assert 'data-mix-active="explore"' in refused.text
    assert harness.core.get_settings().active_mix == "explore"


def test_there_is_no_switcher_on_a_page_with_no_batch_to_switch_for(db_path: Path) -> None:
    """A **Batch** page with no **Batch** is the one place the switcher is deliberately *not* rendered.

    A **Mix** with no **Pool** to apply it to is a control that cannot do anything and an extra thing to
    explain on the page whose whole job is explaining why there is nothing. The refill indicator is what
    answers that page's question. The shell is drawn before it knows whether there is a **Batch**, so the
    switcher is in its nav and the stylesheet hides it until a fetch leaves a `#batch-id` behind.
    """
    harness = make_harness(db_path, fill_pool=0)

    with TestClient(create_app(harness.core)) as client:
        fetched = client.get("/batch")
        stylesheet = client.get("/static/base.css").text

    assert fetched.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert 'id="batch-id"' not in fetched.text
    assert "Pool 0 of" in fetched.text
    rule = next(block for block in stylesheet.split("}") if "body:not(:has(#batch-id))" in block)
    assert "#mix-switcher" in rule
    assert "visibility: hidden" in rule
