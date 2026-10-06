"""The route layer: status codes, redirects, `HX-Trigger`, fragment versus page, and refusals in words.

What a write means is tested through the workflows elsewhere; here it is only checked that the route
reached it. `TestClient` runs the app in process over the fakes, with no background threads.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import Harness, batch_id_of, favourite, judge, live, make_harness, serving
from tests.fakes import STUB_DIRECTION, THUMBNAIL_BYTES, catalogue_of, wallpaper
from wallpapi import decisions, settings, workflows
from wallpapi.batches import SubmissionRefused
from wallpapi.decisions import HISTORY_PAGE_SIZE, HistoryRefused
from wallpapi.model import Mix, Verdict
from wallpapi.settings import EXPLORE_MIX, MAX_BATCH_SIZE, REFINE_MIX, SettingsRefused
from wallpapi.web.app import REFUSALS

Web = tuple[Harness, TestClient]


def test_every_page_boots_on_the_shell_and_tiles_come_from_the_cache(web: Web) -> None:
    """The smoke test across all three pages: a template error in the shell breaks every one at once. A
    **Batch** of tiles is served from `/thumb`, never hotlinked (`thumbnails.py`)."""
    _, client = web

    responses = {path: client.get(path) for path in ("/", "/history", "/settings")}
    batch = client.get("/batch").text

    for path, response in responses.items():
        assert response.status_code == HTTPStatus.OK, path
        assert "<nav" in response.text, path
        for link in ('href="/"', 'href="/history"', 'href="/settings"'):
            assert link in response.text, path
    assert batch.count('data-wallpaper-id="') == 8
    assert batch.count('src="/thumb/') == 8
    assert "th.wallhaven.cc" not in batch


def test_a_thumbnail_is_fetched_once_and_then_served_off_disk(web: Web) -> None:
    """The thumbnail hosts have no published limits, so a re-fetch per page view is what the cache stops."""
    harness, client = web
    shown = live(harness).wallpapers[0]

    first = client.get(f"/thumb/{shown.id}")
    second = client.get(f"/thumb/{shown.id}")

    assert (first.status_code, second.status_code) == (HTTPStatus.OK, HTTPStatus.OK)
    assert first.content == second.content == THUMBNAIL_BYTES
    assert harness.wallhaven.thumbnail_fetches == [shown.thumbnail_url]


def test_the_shell_mints_nothing_and_fetches_the_batch_on_load_and_after_a_submission(web: Web) -> None:
    _, client = web

    shell = client.get("/").text

    assert 'hx-get="/batch"' in shell
    assert 'hx-trigger="load, batch-submitted from:body"' in shell
    assert 'data-wallpaper-id="' not in shell


def test_the_fragments_htmx_swaps_in_are_still_fragments(web: Web) -> None:
    """The **Batch**, a tile, the grid, the **Mix** switcher and the banner never extend the shell."""
    harness, client = web
    fetched = client.get("/batch")
    batch_id = batch_id_of(fetched.text)
    wallpaper_id = live(harness).wallpapers[0].id

    swaps = [
        fetched,
        client.post("/draft", data={"batch_id": batch_id, "wallpaper_id": wallpaper_id, "verdict": "ban"}),
        client.post("/draft/all", data={"batch_id": batch_id, "verdict": "ban"}),
        client.post("/mix", data={"mix": "refine"}),
        client.post("/submit", data={"batch_id": batch_id}),
    ]

    for swap in swaps:
        assert swap.status_code == HTTPStatus.OK
        assert "<html" not in swap.text
        assert "<nav" not in swap.text
        assert "/static/" not in swap.text


# -- marking and submitting --------------------------------------------------------------------------


def test_a_tile_post_sets_the_mark_and_answers_with_the_tile(web: Web) -> None:
    """The marked control posts an empty **Verdict**, so a second click says "end up clear" rather than
    "flip it" (`set_draft` sets rather than toggles). The tile keeps its **Zone** label when swapped
    alone, and a reload shows the mark, because marks are server state."""
    harness, client = web
    batch_id = batch_id_of(client.get("/batch").text)
    marked = live(harness).wallpapers[0].id

    tile = client.post("/draft", data={"batch_id": batch_id, "wallpaper_id": marked, "verdict": "favourite"})
    reloaded = client.get("/batch").text

    assert tile.status_code == HTTPStatus.OK
    assert 'data-draft-verdict="favourite"' in tile.text
    assert 'data-zone="unknown"' in tile.text
    assert '"verdict": ""' in tile.text, "the marked control must offer to clear itself"
    assert reloaded.count('data-draft-verdict="favourite"') == 1
    assert reloaded.count('data-draft-verdict=""') == 7

    cleared = client.post("/draft", data={"batch_id": batch_id, "wallpaper_id": marked, "verdict": ""})

    assert 'data-draft-verdict=""' in cleared.text
    assert live(harness).drafts == {}


def test_a_bulk_post_answers_with_the_whole_grid(web: Web) -> None:
    """One post for the **Batch**, and the grid comes back carrying what it just did."""
    harness, client = web
    batch_id = batch_id_of(client.get("/batch").text)

    marked = client.post("/draft/all", data={"batch_id": batch_id, "verdict": "favourite"})
    cleared = client.post("/draft/all", data={"batch_id": batch_id, "verdict": ""})

    assert marked.status_code == HTTPStatus.OK
    assert marked.text.count('data-draft-verdict="favourite"') == 8
    assert cleared.text.count('data-draft-verdict=""') == 8
    assert live(harness).drafts == {}


@pytest.mark.parametrize(
    ("marks", "said"),
    [
        pytest.param(0, "Recorded 8 ignores", id="nothing marked"),
        pytest.param(2, "Recorded 2 verdicts", id="two"),
    ],
)
def test_a_submission_answers_with_what_it_recorded_and_triggers_a_refetch(
    web: Web, marks: int, said: str
) -> None:
    """No redirect and nothing stored on the client: the banner is the response, and the next `/batch` is
    a new **Batch**. The count comes from the **Decision log**, not from the form."""
    harness, client = web
    shown = batch_id_of(client.get("/batch").text)
    for tile in live(harness).wallpapers[:marks]:
        client.post("/draft", data={"batch_id": shown, "wallpaper_id": tile.id, "verdict": "favourite"})

    response = client.post("/submit", data={"batch_id": shown}, follow_redirects=False)
    following = client.get("/batch").text

    assert response.status_code == HTTPStatus.OK
    assert response.headers["HX-Trigger"] == "batch-submitted"
    assert "set-cookie" not in response.headers
    assert said in " ".join(response.text.split())
    assert 'data-wallpaper-id="' not in response.text, "the response is the banner, not the grid"
    assert batch_id_of(following) != shown
    assert following.count('data-wallpaper-id="') == 8


def test_posting_the_same_batch_twice_is_refused_and_still_refetches(web: Web) -> None:
    """Invariant 7: a 200 with a fresh **Batch** would make the second tab look as if it had recorded
    something. It still refetches, so the stale tab catches up."""
    harness, client = web
    shown = batch_id_of(client.get("/batch").text)
    client.post("/submit", data={"batch_id": shown})

    second = client.post("/submit", data={"batch_id": shown})

    assert second.status_code == HTTPStatus.CONFLICT
    assert "already" in second.text.lower()
    assert second.headers["HX-Trigger"] == "batch-submitted"
    assert len(decisions.entries(harness.connect())) == 8


HAND_MADE_POSTS: list[tuple[str, Callable[[str], dict[str, str]], HTTPStatus, str]] = [
    # Ignore is derived on submit and never stored as a mark: a stored one would be a second shape of
    # nothing for Verdict resolution to tell apart from an absent row.
    (
        "/draft",
        lambda batch: {"batch_id": batch, "wallpaper_id": "wp0001", "verdict": "ignore"},
        HTTPStatus.BAD_REQUEST,
        "an ignore is derived, not drafted",
    ),
    (
        "/draft",
        lambda batch: {"batch_id": batch, "wallpaper_id": "nope", "verdict": "like"},
        HTTPStatus.NOT_FOUND,
        "unknown wallpaper",
    ),
    (
        "/draft/all",
        lambda batch: {"batch_id": batch, "verdict": "ignore"},
        HTTPStatus.BAD_REQUEST,
        "an ignore is derived, not drafted",
    ),
    (
        "/draft/all",
        lambda batch: {"batch_id": batch, "verdict": "adore"},
        HTTPStatus.BAD_REQUEST,
        "unknown verdict",
    ),
    (
        "/history/verdict",
        lambda _: {"wallpaper_id": "wp0000", "verdict": ""},
        HTTPStatus.BAD_REQUEST,
        "a verdict is required",
    ),
    (
        "/history/verdict",
        lambda _: {"wallpaper_id": "wp0000", "verdict": "sideways"},
        HTTPStatus.BAD_REQUEST,
        "unknown verdict",
    ),
    (
        "/history/verdict",
        lambda _: {"wallpaper_id": "nope", "verdict": "like"},
        HTTPStatus.NOT_FOUND,
        "unknown wallpaper",
    ),
    # A Mix deleted in another tab gives the same answer: the switcher, showing what is stored.
    ("/mix", lambda _: {"mix": "nope"}, HTTPStatus.BAD_REQUEST, 'id="mix-switcher"'),
]


@pytest.mark.parametrize(("path", "data", "status", "said"), HAND_MADE_POSTS)
def test_a_post_no_control_sends_is_refused_and_changes_nothing(
    web: Web, path: str, data: Callable[[str], dict[str, str]], status: HTTPStatus, said: str
) -> None:
    harness, client = web
    judge(harness, wp0000=Verdict.LIKE)
    batch_id = batch_id_of(client.get("/batch").text)
    before = (decisions.entries(harness.connect()), live(harness).drafts, settings.get(harness.connect()))

    response = client.post(path, data=data(batch_id))

    assert response.status_code == status
    assert said in response.text
    assert (
        decisions.entries(harness.connect()),
        live(harness).drafts,
        settings.get(harness.connect()),
    ) == before


def test_a_stale_tab_is_told_in_words_which_batch_it_is_on(web: Web) -> None:
    """The banner a second tab gets, unlike the bare statuses above: a real case, so a sentence."""
    _, client = web
    shown = batch_id_of(client.get("/batch").text)
    client.post("/submit", data={"batch_id": shown})

    again = client.post("/submit", data={"batch_id": shown})
    unknown = client.post("/draft/all", data={"batch_id": "nope", "verdict": "like"})

    assert again.status_code == HTTPStatus.CONFLICT
    assert "That batch has already been submitted" in again.text
    assert unknown.status_code == HTTPStatus.NOT_FOUND
    assert "That batch is not one this instance knows about." in unknown.text


def test_switching_the_mix_answers_with_the_switcher_alone(web: Web) -> None:
    """Re-rendered from what is stored, and without the grid, so the **Batch** on screen is untouched."""
    harness, client = web

    switched = client.post("/mix", data={"mix": "refine"})
    reloaded = client.get("/").text

    assert switched.status_code == HTTPStatus.OK
    assert 'id="mix-switcher"' in switched.text
    assert "batch-grid" not in switched.text
    assert settings.active_mix(harness.connect()) == REFINE_MIX
    assert 'data-mix-active="refine"' in reloaded


def test_switching_to_a_mix_nobody_has_heard_of_is_a_400_and_changes_nothing(web: Web) -> None:
    """Only a second tab that deleted the **Mix**, or a hand-made post, can send one."""
    harness, client = web

    refused = client.post("/mix", data={"mix": "nope"})

    assert refused.status_code == HTTPStatus.BAD_REQUEST
    assert 'data-mix-active="explore"' in refused.text
    assert settings.active_mix(harness.connect()) == EXPLORE_MIX


# -- when there is nothing to show -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arrange", "words"),
    [
        pytest.param({"fill_pool": 0}, ["pool is empty"], id="nothing arrived yet"),
        pytest.param(
            {"fail_from_call": 1}, ["cannot reach Wallhaven", "set up to fail", "11:30"], id="unreachable"
        ),
        pytest.param({"rate_limited_calls": 1}, ["cannot reach Wallhaven"], id="a 429"),
    ],
)
def test_an_unavailable_batch_is_a_page_in_words_never_a_500(
    db_path: Path, arrange: dict[str, int], words: list[str]
) -> None:
    """A status code and some words, including when the last attempt failed, and never the reason's enum
    value. Before the **Pool**, a failed first call was a traceback."""
    harness = make_harness(db_path, **arrange)  # pyright: ignore[reportArgumentType]

    with serving(harness) as client:
        response = client.get("/batch")

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert "text/html" in response.headers["content-type"]
    for word in words:
        assert word in response.text
    assert "wallhaven_unreachable" not in response.text


@pytest.mark.parametrize("fill_pool", [1, 0], ids=["a batch", "unavailable"])
def test_the_refill_indicator_and_the_provider_notice_are_on_every_state_of_the_page(
    db_path: Path, fill_pool: int
) -> None:
    """Most needed on an empty page, where an empty **Pool** and a dead refill look alike. A **Score** from
    the fallback looks like one from the model, so the provider's notice goes beside it."""
    harness = make_harness(db_path, fill_pool=fill_pool)
    status = harness.modules.refill.status()

    with serving(harness) as client:
        text = client.get("/batch").text

    assert f"Pool {status.pool_size} of {status.target_size}" in text
    assert "Refill not running" in text, "nothing started the thread in this app"
    assert "Image similarity is still starting up" in text


def test_a_provider_at_full_strength_adds_no_line(web: Web) -> None:
    """The model open and every **Pool** **Wallpaper** embedded."""
    harness, client = web
    harness.store.vector_by_id.update({w.id: STUB_DIRECTION for w in catalogue_of(24)})
    harness.modules.similarity.catch_up(harness.modules.thumbnails.directory, threading.Event())

    assert "similarity-notice" not in client.get("/batch").text


def test_the_indicator_shows_the_last_refill_error_on_a_stocked_page(db_path: Path) -> None:
    harness = make_harness(db_path)
    harness.wallhaven.fail_from_call = 2
    harness.fill_pool(1)

    with serving(harness) as client:
        response = client.get("/batch")

    assert response.status_code == HTTPStatus.OK
    assert "Last error" in response.text
    assert "set up to fail" in response.text


@pytest.mark.parametrize("favourited", [False, True], ids=["random", "lookalikes"])
def test_the_indicator_names_the_strategy_the_last_refill_step_used(db_path: Path, favourited: bool) -> None:
    """A **Pool** growing only at random means there are no **Favourites** yet: something to act on."""
    harness = make_harness(
        db_path, catalogue=catalogue_of(2), like_results={"wp0000": catalogue_of(4, prefix="lk")}
    )
    if favourited:
        workflows.save_settings(harness.modules, pool_target_size=1000)
        favourite(harness, "wp0000")
        harness.fill_pool(1)

    with serving(harness) as client:
        text = client.get("/batch").text

    assert ("searching for lookalikes of a favourite" in text) is favourited
    assert ("searching at random" in text) is not favourited


@pytest.mark.parametrize(
    ("arrange", "lookalikes"),
    [
        pytest.param("empty", None, id="an empty pool says nothing of lookalikes"),
        pytest.param("random", 0, id="none yet is said as a zero"),
        pytest.param("favourited", 4, id="lookalikes counted"),
    ],
)
def test_the_indicator_says_how_much_of_the_pool_is_lookalikes(
    db_path: Path, arrange: str, lookalikes: int | None
) -> None:
    """Whether the like: search is feeding the **Pool** once there are **Favourites**. A zero on a stocked
    **Pool** is the point: lookalikes are not arriving."""
    harness = make_harness(
        db_path,
        catalogue=catalogue_of(2),
        like_results={"wp0000": catalogue_of(4, prefix="lk")},
        fill_pool=0 if arrange == "empty" else 1,
    )
    if arrange == "favourited":
        workflows.save_settings(harness.modules, pool_target_size=1000)
        favourite(harness, "wp0000")
        harness.fill_pool(1)
    status = harness.modules.refill.status()

    with serving(harness) as client:
        text = client.get("/batch").text

    if lookalikes is None:
        assert f"Pool 0 of {status.target_size}." in text
        assert "of them lookalikes" not in text
    else:
        assert f"Pool {status.pool_size} of {status.target_size}, {lookalikes} of them lookalikes." in text


# -- History -----------------------------------------------------------------------------------------


def test_the_history_filter_narrows_the_listing_and_an_unknown_one_is_refused(db_path: Path) -> None:
    harness = make_harness(db_path, catalogue=catalogue_of(3))
    judge(harness, wp0000=Verdict.LIKE, wp0001=Verdict.BAN, wp0002=Verdict.LIKE)

    with serving(harness) as client:
        liked = client.get("/history?verdict=like")
        unknown = client.get("/history?verdict=sideways")

    assert liked.text.count('class="history-row"') == 2
    assert 'data-wallpaper-id="wp0001"' not in liked.text
    assert unknown.status_code == HTTPStatus.BAD_REQUEST


def test_history_is_paged_and_the_links_carry_the_filter(db_path: Path) -> None:
    count = HISTORY_PAGE_SIZE + 1
    harness = make_harness(db_path, catalogue=catalogue_of(count), page_size=count)
    judge(harness, **{f"wp{n:04d}": Verdict.LIKE for n in range(count)})

    with serving(harness) as client:
        first = client.get("/history?verdict=like").text
        second = client.get("/history?page=2&verdict=like").text

    assert first.count('class="history-row"') == HISTORY_PAGE_SIZE
    assert "/history?page=2&verdict=like" in first
    assert "previous" not in first
    assert second.count('class="history-row"') == 1
    assert 'data-wallpaper-id="wp0000"' in second


@pytest.mark.parametrize("verdict", [Verdict.BAN, Verdict.IGNORE])
def test_a_history_edit_answers_with_its_row_alone(db_path: Path, verdict: Verdict) -> None:
    """A `<tr>`, because a `<div>` coming back into a `<tbody>` is dropped by the parser and the edit would
    seem to do nothing; and the row, because re-rendering a hundred would fight the user's other clicks."""
    harness = make_harness(db_path, catalogue=(wallpaper("judged"),))
    judge(harness, judged=Verdict.FAVOURITE)

    with serving(harness) as client:
        response = client.post("/history/verdict", data={"wallpaper_id": "judged", "verdict": verdict.value})

    assert response.status_code == HTTPStatus.OK
    assert response.text.count('class="history-row"') == 1
    assert f'data-resolved-verdict="{verdict.value}"' in response.text
    assert re.sub(r"\{#.*?#\}", "", response.text, flags=re.S).lstrip().startswith("<tr")
    assert response.text.rstrip().endswith("</tr>")
    assert decisions.resolve(harness.connect(), ["judged"])["judged"].verdict is verdict


# -- settings ----------------------------------------------------------------------------------------


def test_saving_settings_redirects_and_a_partial_post_leaves_the_rest(web: Web, tmp_path: Path) -> None:
    """Post-redirect-get, unlike submitting: saving the same settings twice is the same settings. A field
    a post does not carry keeps its value (ADR 0004)."""
    harness, client = web
    chosen = tmp_path / "Wallpapers"

    saved = client.post(
        "/settings",
        data={
            "batch_size": "2",
            "library_path": str(chosen),
            "min_width": "1920",
            "min_height": "1080",
            "allowed_ratios": "21x9, 32x9",
            "min_favourites": "40",
            "pool_target_size": "150",
        },
        follow_redirects=False,
    )
    client.post("/settings", data={"batch_size": "4"}, follow_redirects=False)

    assert saved.status_code == HTTPStatus.SEE_OTHER
    assert saved.headers["location"].startswith("/settings")
    assert "Settings saved." in client.get(saved.headers["location"]).text
    stored = settings.get(harness.connect())
    assert (stored.batch_size, stored.library_path, stored.pool_target_size) == (4, chosen, 150)
    assert (stored.min_width, stored.min_height, stored.min_favourites) == (1920, 1080, 40)
    assert stored.allowed_ratios == ("21x9", "32x9")


def test_a_field_cleared_and_saved_keeps_its_stored_value(web: Web, tmp_path: Path) -> None:
    """An empty field is a field not posted, as it always was on this page: clearing one and saving leaves it
    alone rather than refusing it as blank."""
    harness, client = web
    chosen = tmp_path / "Wallpapers"
    workflows.save_settings(harness.modules, batch_size=5, library_path=chosen)

    saved = client.post(
        "/settings", data={"batch_size": "", "library_path": "", "min_width": "1920"}, follow_redirects=False
    )

    assert saved.status_code == HTTPStatus.SEE_OTHER
    stored = settings.get(harness.connect())
    assert (stored.batch_size, stored.library_path, stored.min_width) == (5, chosen, 1920)


@pytest.mark.parametrize(
    ("posted", "words"),
    [
        pytest.param(
            {"batch_size": "0", "library_path": "Elsewhere"}, [str(MAX_BATCH_SIZE)], id="batch size"
        ),
        # "eight" must come back as the page, not FastAPI's JSON 422: why the fields arrive as strings.
        pytest.param({"batch_size": "eight"}, [], id="not a number"),
        pytest.param({"library_path": "Wallpapers"}, ["absolute"], id="a relative library path"),
        pytest.param({"allowed_ratios": "16:9"}, ["16x9"], id="an unknown ratio names the ones that work"),
        pytest.param(
            {"min_width": "9999", "allowed_ratios": "nope"}, ['value="1440"'], id="untouched fields shown"
        ),
    ],
)
def test_a_refused_settings_post_is_the_page_with_the_reason_and_what_was_typed(
    web: Web, posted: dict[str, str], words: list[str]
) -> None:
    """One error branch, the refusal's reason in words and never its enum value; and what was typed,
    so correcting one field is not retyping the rest."""
    harness, client = web
    before = settings.get(harness.connect())

    response = client.post("/settings", data=posted)

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert "text/html" in response.headers["content-type"]
    assert "Nothing was changed." in response.text
    assert not [reason for reason in SettingsRefused.Reason if reason.value in response.text]
    for typed in posted.values():
        assert f'value="{typed}"' in response.text
    for word in words:
        assert word.lower() in response.text.lower()
    assert settings.get(harness.connect()) == before


def test_a_batch_size_saved_from_the_page_reaches_the_next_batch_and_the_shell(
    web: Web, tmp_path: Path
) -> None:
    """The live **Batch** is submitted in between: the size applies to the next one minted."""
    _, client = web
    batch_id = batch_id_of(client.get("/batch").text)

    client.post("/settings", data={"batch_size": "2", "library_path": str(tmp_path / "Wallpapers")})
    client.post("/submit", data={"batch_id": batch_id})

    assert client.get("/batch").text.count('data-wallpaper-id="') == 2
    assert client.get("/").text.count('class="placeholder"') == 2, "the shell's stand-ins follow it too"


def test_downloading_the_favourites_redirects_and_says_what_happened(db_path: Path, tmp_path: Path) -> None:
    """Written, skipped and failed, in words. Post-redirect-get, since the download is a folder full of
    network calls a refresh should not repeat. The fake's paths are not on this disk, which is the
    state the button exists for."""
    harness = make_harness(db_path, catalogue=catalogue_of(2))
    workflows.save_settings(harness.modules, batch_size=2, library_path=tmp_path / "Library")
    stubborn, _ = (w.id for w in live(harness).wallpapers)
    workflows.set_all_drafts(harness.modules, live(harness).id, Verdict.FAVOURITE)
    workflows.submit(harness.modules, live(harness).id)
    harness.library.fail_for.add(stubborn)

    with serving(harness) as client:
        redirected = client.post("/settings/library/download", follow_redirects=False)
        page = client.get(redirected.headers["location"]).text

    assert redirected.status_code == HTTPStatus.SEE_OTHER
    assert redirected.headers["location"].startswith("/settings?")
    for said in ("1 downloaded", "0 already there", "1 failed"):
        assert said in page


# -- Mixes on the settings page ----------------------------------------------------------------------


def test_saving_a_mix_redirects_and_one_route_both_adds_and_edits(web: Web) -> None:
    harness, client = web

    edited = client.post(
        "/settings/mixes",
        data={"name": "explore", "unknown": "50", "banger": "45", "dud": "5"},
        follow_redirects=False,
    )
    client.post("/settings/mixes", data={"name": "  Night  ", "unknown": "10", "banger": "80", "dud": "10"})

    assert edited.status_code == HTTPStatus.SEE_OTHER
    assert edited.headers["location"].startswith("/settings")
    assert tuple(listed.mix for listed in settings.list_mixes(harness.connect())) == (
        Mix(name="Night", unknown=10, banger=80, dud=10),
        Mix(name="explore", unknown=50, banger=45, dud=5),
        REFINE_MIX,
    )


@pytest.mark.parametrize(
    ("posted", "words"),
    [
        pytest.param(
            {"name": "explore", "unknown": "30", "banger": "30", "dud": "30"}, "add up to 100", id="sum"
        ),
        pytest.param(
            {"name": "  ", "unknown": "50", "banger": "45", "dud": "5"}, "A mix needs a name", id="name"
        ),
    ],
)
def test_an_invalid_mix_shows_the_reason_and_what_was_typed(
    web: Web, posted: dict[str, str], words: str
) -> None:
    """The refused numbers come back in the row they were typed in, so fixing one is not retyping three."""
    harness, client = web

    refused = client.post("/settings/mixes", data=posted)

    assert refused.status_code == HTTPStatus.BAD_REQUEST
    assert words in refused.text
    assert tuple(listed.mix for listed in settings.list_mixes(harness.connect())) == (EXPLORE_MIX, REFINE_MIX)
    if posted["name"] == "explore":
        row = refused.text.split('data-mix-row="explore"', 1)[1].split("</tr>", 1)[0]
        assert row.count('value="30"') == 3


def test_a_mix_post_missing_a_field_is_refused_in_words_and_never_a_500(web: Web) -> None:
    """Only a hand-made post can leave one out; it reads as empty, so the settings module says why."""
    harness, client = web

    refused = client.post("/settings/mixes", data={"name": "night", "unknown": "50", "banger": "50"})

    assert refused.status_code == HTTPStatus.BAD_REQUEST
    assert "add up to 100" in refused.text
    assert 'value="night"' in refused.text.split("data-mix-new", 1)[1]
    assert tuple(listed.mix for listed in settings.list_mixes(harness.connect())) == (EXPLORE_MIX, REFINE_MIX)


@pytest.mark.parametrize(
    ("deleted", "active", "status", "words"),
    [
        pytest.param("duds only", "explore", HTTPStatus.SEE_OTHER, "", id="a custom mix"),
        # No control is rendered for these two; this answers the second tab, and says what is in the way.
        pytest.param("duds only", "duds only", HTTPStatus.BAD_REQUEST, "the active mix", id="the active mix"),
        pytest.param("explore", "refine", HTTPStatus.BAD_REQUEST, "cannot be deleted", id="explore"),
    ],
)
def test_deleting_a_mix_redirects_or_says_why_not(
    db_path: Path, deleted: str, active: str, status: HTTPStatus, words: str
) -> None:
    harness = make_harness(db_path)
    workflows.save_mix(harness.modules, "duds only", unknown=0, banger=0, dud=100)
    workflows.save_settings(harness.modules, active_mix=active)

    with serving(harness) as client:
        response = client.post("/settings/mixes/delete", data={"name": deleted}, follow_redirects=False)

    assert response.status_code == status
    assert words in response.text
    assert (
        deleted
        in {mix.name for mix in tuple(listed.mix for listed in settings.list_mixes(harness.connect()))}
    ) is (status != HTTPStatus.SEE_OTHER)


def test_every_refusal_reason_has_its_own_entry() -> None:
    """`REFUSALS` is keyed by `StrEnum` members from two modules, which hash as their string values: a new
    reason reusing another's value would silently take over that entry's response."""
    reasons = [*SubmissionRefused.Reason, *HistoryRefused.Reason]

    assert len(REFUSALS) == len(reasons)
    assert all(REFUSALS.keys() & {reason} for reason in reasons)
