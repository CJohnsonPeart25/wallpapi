"""The **Batch** page when there is nothing to show, and the refill indicator. Issues #6 and #15.

The whole point of #15 is a status code and some words, so these tests are about exactly that: never a 500,
and never a bare reason code.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import make_harness
from tests.fakes import catalogue_of
from tests.test_refill_like import favourite
from wallpapi.web.app import create_app


def test_an_empty_pool_renders_a_page_rather_than_a_500(db_path: Path) -> None:
    """Acceptance criterion: the **Batch unavailable** page is rendered, never an unhandled exception."""
    harness = make_harness(db_path, fill_pool=0)
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.get("/batch")

    assert response.status_code == 503
    assert "text/html" in response.headers["content-type"]
    assert "pool is empty" in response.text


def test_an_unreachable_wallhaven_renders_the_error_and_when_it_happened(db_path: Path) -> None:
    """#15 as the user meets it: the page says Wallhaven is the problem and when it last failed.

    Before the **Pool**, this exact situation — the first **API call** of a page load failing — propagated
    out of the Core service and the browser got a 500 with a traceback behind it.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(24), fail_from_call=1, fill_pool=1)
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.get("/batch")

    assert response.status_code == 503
    body = response.text
    assert "cannot reach Wallhaven" in body
    assert "set up to fail" in body
    assert "11:30" in body, "the page should say when the last attempt failed"
    assert "wallhaven_unreachable" not in body, "the reason needs words, not its enum value"


def test_a_non_200_from_wallhaven_reads_the_same_way_as_a_transport_failure(db_path: Path) -> None:
    """Acceptance criterion: a non-200 is caught too, and never propagates.

    A 429 is the non-200 the client names; the page has one branch for "the last call failed" and does not
    care which kind of failure it was.
    """
    harness = make_harness(db_path, rate_limited_calls=1, fill_pool=1)
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.get("/batch")

    assert response.status_code == 503
    assert "cannot reach Wallhaven" in response.text


def test_the_batch_page_shows_the_refill_indicator(db_path: Path) -> None:
    """Acceptance criterion: the refill is visible. One line, on every state of the page."""
    harness = make_harness(db_path, catalogue=catalogue_of(24))
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.get("/batch")

    assert "Pool 24 of 500" in response.text
    assert "Refill not running" in response.text, "nothing started the thread in this app"


def test_the_indicator_is_on_the_unavailable_page_too(db_path: Path) -> None:
    """Where it matters most: an empty page and a dead refill thread look exactly alike without it."""
    harness = make_harness(db_path, fill_pool=0)
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.get("/batch")

    assert "Pool 0 of 500" in response.text
    assert "Refill not running" in response.text


def test_the_indicator_shows_the_last_refill_error(db_path: Path) -> None:
    """A **Pool** that has some **Wallpapers** in it still needs to say the refill has stopped working."""
    harness = make_harness(db_path, catalogue=catalogue_of(24), fill_pool=1)
    harness.wallhaven.fail_from_call = 2
    harness.fill_pool(1)
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.get("/batch")

    assert response.status_code == 200, "a stocked pool still shows a Batch"
    assert "Last error" in response.text
    assert "set up to fail" in response.text


def test_the_indicator_names_the_strategy_the_last_refill_step_used(db_path: Path) -> None:
    """#13: "refill fetching" says nothing about *what*, and the two strategies say different things.

    A **Pool** growing only at random means there are no **Favourites** in the **Decision log** yet, which
    is something the person reading the line can act on. Still one line.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(24), fill_pool=1)
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.get("/batch")

    assert "searching at random" in response.text
    assert "lookalikes" not in response.text, "no Favourites yet, so nothing to look like"


def test_the_indicator_says_when_the_refill_is_searching_for_lookalikes(db_path: Path) -> None:
    """The other half of the same line, once there is a **Favourite** for a like: search to be about."""
    harness = make_harness(
        db_path,
        catalogue=catalogue_of(2),
        like_results={"wp0000": catalogue_of(4, prefix="lk")},
        fill_pool=1,
    )
    harness.core.update_settings(pool_target_size=1000)
    favourite(harness, "wp0000")
    harness.fill_pool(1)
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.get("/batch")

    assert "searching for lookalikes of a favourite" in response.text


def test_the_batch_page_says_when_the_similarity_provider_is_not_at_full_strength(db_path: Path) -> None:
    """#14: a **Score** from the fallback looks exactly like a **Score** from the model.

    So a wallpapi that had never managed to fetch its model would be indistinguishable from one working
    perfectly, for as long as nobody noticed the **Bangers** were only the right colour. The provider
    supplies the sentence; the page's job is putting it where it will be read, beside the refill line that
    answers the same kind of question.
    """
    harness = make_harness(
        db_path, catalogue=catalogue_of(24), similarity_notice="Image similarity is still fetching its model."
    )
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.get("/batch")

    assert "Image similarity is still fetching its model." in response.text


def test_the_notice_is_on_the_unavailable_page_too(db_path: Path) -> None:
    """The same reasoning as the refill indicator's: the states that explain a thin page are exactly the
    ones worth showing on it."""
    harness = make_harness(db_path, fill_pool=0, similarity_notice="Still fetching its model.")
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.get("/batch")

    assert response.status_code == 503
    assert "Still fetching its model." in response.text


def test_a_provider_at_full_strength_adds_no_line(db_path: Path) -> None:
    """The usual case, and the one that must not leave an empty paragraph behind it."""
    harness = make_harness(db_path, catalogue=catalogue_of(24))
    app = create_app(harness.core)

    with TestClient(app) as client:
        response = client.get("/batch")

    assert "similarity-notice" not in response.text


def test_the_provider_is_asked_about_the_whole_pool(db_path: Path) -> None:
    """#44: `notice` takes the **Pool**, the same shape `similarities` already does, so a provider can say
    how much of it is covered without the Core service learning which provider it holds."""
    harness = make_harness(db_path, catalogue=catalogue_of(24))

    harness.core.similarity_notice()

    assert harness.similarity.notice_pools == [tuple(w.id for w in catalogue_of(24))]
