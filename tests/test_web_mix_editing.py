"""The **Mixes** section of the settings page. Issue #12.

FastAPI's `TestClient` against `create_app(core)` with the fakes behind it, like every other web test
here. What is asserted is what the page offers and what the Core service holds afterwards — never the
markup for its own sake.
"""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import make_harness
from wallpapi.core import EXPLORE_MIX, REFINE_MIX
from wallpapi.model import Mix
from wallpapi.web.app import create_app


def test_the_settings_page_lists_every_mix_with_its_percentages(db_path: Path) -> None:
    """The section the user edits from: a row per **Mix**, with the three numbers as fields rather than
    as text, and a row for adding one."""
    harness = make_harness(db_path)
    harness.core.save_mix("duds only", unknown=0, banger=0, dud=100)

    with TestClient(create_app(harness.core)) as client:
        page = client.get("/settings")

    assert page.status_code == HTTPStatus.OK
    assert 'data-mix-row="explore"' in page.text
    assert 'data-mix-row="refine"' in page.text
    assert 'data-mix-row="duds only"' in page.text
    assert "data-mix-new" in page.text


def test_explore_and_refine_have_no_delete_control_and_a_custom_mix_does(db_path: Path) -> None:
    """The page offers no control that cannot work. **Explore** and **Refine** are editable and
    permanent, so the only delete buttons are on the **Mixes** the user made."""
    harness = make_harness(db_path)
    harness.core.save_mix("duds only", unknown=0, banger=0, dud=100)

    with TestClient(create_app(harness.core)) as client:
        page = client.get("/settings")

    assert 'data-mix-delete="duds only"' in page.text
    assert 'data-mix-delete="explore"' not in page.text
    assert 'data-mix-delete="refine"' not in page.text


def test_saving_a_mix_persists_it_and_redirects(db_path: Path) -> None:
    """Post-redirect-get on success, the same as saving any other setting: re-saving a **Mix** writes the
    same three numbers, so a refresh may as well be a page load."""
    harness = make_harness(db_path)

    with TestClient(create_app(harness.core)) as client:
        saved = client.post(
            "/settings/mixes",
            data={"name": "explore", "unknown": "50", "banger": "45", "dud": "5"},
            follow_redirects=False,
        )

    assert saved.status_code == HTTPStatus.SEE_OTHER
    assert saved.headers["location"].startswith("/settings")
    assert harness.core.active_mix() == Mix(name="explore", unknown=50, banger=45, dud=5)


def test_adding_a_custom_mix_puts_it_in_the_list(db_path: Path) -> None:
    """The add row and the edit rows post to the same route, because creating and editing are the same
    write: a **Mix** is saved under its name, and whether one was already there is not the user's
    question."""
    harness = make_harness(db_path)

    with TestClient(create_app(harness.core)) as client:
        client.post(
            "/settings/mixes", data={"name": "  Night  ", "unknown": "10", "banger": "80", "dud": "10"}
        )
        page = client.get("/settings")

    assert harness.core.list_mixes() == (
        Mix(name="Night", unknown=10, banger=80, dud=10),
        EXPLORE_MIX,
        REFINE_MIX,
    )
    assert 'data-mix-row="Night"' in page.text


def test_an_invalid_mix_shows_the_reason_and_persists_nothing(db_path: Path) -> None:
    """One error branch, as every other settings refusal has: the Core service's reason rendered in
    words, and what was typed still in the fields so that fixing one number is not retyping three."""
    harness = make_harness(db_path)

    with TestClient(create_app(harness.core)) as client:
        refused = client.post(
            "/settings/mixes", data={"name": "explore", "unknown": "30", "banger": "30", "dud": "30"}
        )

    assert refused.status_code == HTTPStatus.BAD_REQUEST
    assert "add up to 100" in refused.text
    assert harness.core.active_mix() == EXPLORE_MIX
    # The refused numbers are back in **Explore**'s own row, not the stored 75/20/5 and not the add row.
    row = refused.text.split('data-mix-row="explore"', 1)[1].split("</form>", 1)[0]
    assert row.count('value="30"') == 3


def test_a_nameless_mix_is_refused_with_the_name_reason(db_path: Path) -> None:
    """The other half of the validator, reached from the add row, where the name is the field the user
    fills in."""
    harness = make_harness(db_path)

    with TestClient(create_app(harness.core)) as client:
        refused = client.post(
            "/settings/mixes", data={"name": "  ", "unknown": "50", "banger": "45", "dud": "5"}
        )

    assert refused.status_code == HTTPStatus.BAD_REQUEST
    assert "A mix needs a name" in refused.text
    assert harness.core.list_mixes() == (EXPLORE_MIX, REFINE_MIX)


def test_deleting_a_custom_mix_removes_it_and_redirects(db_path: Path) -> None:
    harness = make_harness(db_path)
    harness.core.save_mix("duds only", unknown=0, banger=0, dud=100)

    with TestClient(create_app(harness.core)) as client:
        deleted = client.post("/settings/mixes/delete", data={"name": "duds only"}, follow_redirects=False)

    assert deleted.status_code == HTTPStatus.SEE_OTHER
    assert harness.core.list_mixes() == (EXPLORE_MIX, REFINE_MIX)


def test_deleting_the_active_mix_shows_the_reason_and_keeps_it(db_path: Path) -> None:
    """The page renders no delete control on the active **Mix**, so this answers the second tab — and it
    says which **Mix** is in the way rather than refusing silently."""
    harness = make_harness(db_path)
    harness.core.save_mix("duds only", unknown=0, banger=0, dud=100)
    harness.core.update_settings(active_mix="duds only")

    with TestClient(create_app(harness.core)) as client:
        refused = client.post("/settings/mixes/delete", data={"name": "duds only"})

    assert refused.status_code == HTTPStatus.BAD_REQUEST
    assert "the active mix" in refused.text
    assert harness.core.active_mix() == Mix(name="duds only", unknown=0, banger=0, dud=100)


def test_deleting_explore_is_refused_in_words(db_path: Path) -> None:
    harness = make_harness(db_path)

    with TestClient(create_app(harness.core)) as client:
        refused = client.post("/settings/mixes/delete", data={"name": "explore"})

    assert refused.status_code == HTTPStatus.BAD_REQUEST
    assert "cannot be deleted" in refused.text
    assert harness.core.list_mixes() == (EXPLORE_MIX, REFINE_MIX)


def test_the_batch_page_switcher_offers_a_custom_mix(db_path: Path) -> None:
    """The switcher (#10) lists every **Mix** there is, so a **Mix** made on the settings page is
    selectable where it is about to be used — and selecting it is the ordinary `/mix` post, which refused
    every name but the seeded two before this ticket."""
    harness = make_harness(db_path)
    harness.core.save_mix("duds only", unknown=0, banger=0, dud=100)

    with TestClient(create_app(harness.core)) as client:
        page = client.get("/")
        switched = client.post("/mix", data={"mix": "duds only"})

    assert 'data-mix="duds only"' in page.text
    assert "0/0/100" in page.text
    assert switched.status_code == HTTPStatus.OK
    assert harness.core.active_mix() == Mix(name="duds only", unknown=0, banger=0, dud=100)


def test_the_settings_page_says_which_mix_is_active(db_path: Path) -> None:
    """Why a **Mix** has no delete control has to be readable off the page, or the missing button is just
    a missing button."""
    harness = make_harness(db_path)

    with TestClient(create_app(harness.core)) as client:
        page = client.get("/settings")

    assert 'data-mix-active="explore"' in page.text
