"""The route layer: status codes, redirects, `HX-Trigger`, fragment versus page, and refusals in words.

What a write means is tested through the modules elsewhere; here it is only checked that the route
reached it. `TestClient` runs the app in process over `make_harness`, the real modules composed as the app
composes them (ADR 0019) with the fakes behind them, and no background threads.
"""

from __future__ import annotations

import html
import re
import threading
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient

from tests.conftest import Harness, batch_id_of, favourite, judge, live, make_harness, serving
from tests.fakes import STUB_DIRECTION, THUMBNAIL_BYTES, catalogue_of, wallpaper
from wallpapi import batches, evidence_log, lineup, pool, settings, workflows
from wallpapi.batches import SubmissionRefused
from wallpapi.evidence_log import HISTORY_PAGE_SIZE, HistoryRefused
from wallpapi.lineup import LineupRefused
from wallpapi.model import Mix, Verdict
from wallpapi.settings import EXPLORE_MIX, MAX_BATCH_SIZE, REFINE_MIX, SettingsRefused
from wallpapi.taste.gauntlet import GAUNTLET_V1, GauntletResponse, GauntletTile
from wallpapi.taste.order import ORDER_V1, OrderTile
from wallpapi.taste.pick import PICK_V1, PickResponse, PickTile
from wallpapi.taste.verdict_grid import GridResponse, TileResponse
from wallpapi.web.app import REFUSALS

Web = tuple[Harness, TestClient]


def test_every_page_boots_on_the_shell_and_tiles_come_from_the_cache(web: Web) -> None:
    """The smoke test across all four pages: a template error in the shell breaks every one at once. A
    **Batch** and a **Line-up** of tiles are served from `/thumb`, never hotlinked (`thumbnails.py`)."""
    _, client = web

    pages = ("/", "/compare", "/compare?instrument=order", "/history", "/settings")
    responses = {path: client.get(path) for path in pages}
    batch = client.get("/batch").text
    lineup = client.get("/compare/lineup?instrument=pick").text
    ordered = client.get("/compare/lineup?instrument=order").text

    for path, response in responses.items():
        assert response.status_code == HTTPStatus.OK, path
        assert "<nav" in response.text, path
        for link in ('href="/"', 'href="/compare"', 'href="/history"', 'href="/settings"'):
            assert link in response.text, path
    assert batch.count('data-wallpaper-id="') == 8
    assert batch.count('src="/thumb/') == 8
    for fragment in (lineup, ordered):
        assert fragment.count('data-wallpaper-id="') == 4
        assert fragment.count('src="/thumb/') == 4
    assert "th.wallhaven.cc" not in batch + lineup + ordered


def test_a_thumbnail_is_fetched_once_and_then_served_off_disk(web: Web) -> None:
    """The thumbnail hosts have no published limits, so a re-fetch per page view is what the cache stops."""
    harness, client = web
    shown = live(harness).wallpapers[0]

    first = client.get(f"/thumb/{shown.id}")
    second = client.get(f"/thumb/{shown.id}")

    assert (first.status_code, second.status_code) == (HTTPStatus.OK, HTTPStatus.OK)
    assert first.content == second.content == THUMBNAIL_BYTES
    assert harness.wallhaven.thumbnail_fetches == [shown.thumbnail_url]


def test_the_shell_mints_nothing_and_fetches_a_new_batch_on_load_and_the_next_after_a_submission(
    web: Web,
) -> None:
    """A load is a visit, which draws a new **Batch**, and so is a page restored from the back-forward cache;
    the refetch after an answer asks for the next, which hands back the live one if a refusal left one (ADR
    0029). Both swap into `#batch`."""
    _, client = web

    shell = client.get("/").text

    loader = re.search(r'<div\s+id="batch"[^>]*>', shell)
    refetcher = re.search(r'<div\s+id="batch-next"[^>]*>', shell)
    assert loader is not None and refetcher is not None
    assert 'hx-get="/batch"' in loader.group(0)
    assert 'hx-trigger="load, pageshow[persisted] from:window"' in loader.group(0)
    assert 'hx-get="/batch/next"' in refetcher.group(0)
    assert 'hx-trigger="batch-submitted from:body"' in refetcher.group(0)
    assert 'hx-target="#batch"' in refetcher.group(0)
    assert 'hx-post="/skip"' in shell
    assert 'data-wallpaper-id="' not in shell


def test_the_fragments_htmx_swaps_in_are_still_fragments(web: Web) -> None:
    """The **Batch**, a tile, the grid, the **Mix** switcher and the banner never extend the shell; nor do
    the **Line-up**, the same one a star answers with, the next one a **Pick** answers with, and the
    **Line-up** size switcher."""
    harness, client = web
    fetched = client.get("/batch")
    batch_id = batch_id_of(fetched.text)
    wallpaper_id = live(harness).wallpapers[0].id
    lineup = client.get("/compare/lineup?instrument=pick")

    swaps = [
        fetched,
        client.get("/batch/next"),
        client.post("/draft", data={"batch_id": batch_id, "wallpaper_id": wallpaper_id, "verdict": "ban"}),
        client.post("/draft/all", data={"batch_id": batch_id, "verdict": "ban"}),
        client.post("/mix", data={"mix": "refine"}),
        client.post("/submit", data={"batch_id": batch_id}),
        client.post("/skip", data={"batch_id": batch_id_of(client.get("/batch").text)}),
        lineup,
        client.post(
            "/compare/star?instrument=pick",
            data={
                "lineup_id": lineup_of(lineup.text)[0],
                "wallpaper_id": lineup_of(lineup.text)[1][0],
                "starred": "true",
            },
        ),
        client.post("/compare/pick", data={"lineup_id": lineup_of(lineup.text)[0]}),
        client.post("/compare/size", data={"size": "3"}),
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
    alone, and the live **Batch** handed back shows the mark, because marks are server state."""
    harness, client = web
    batch_id = batch_id_of(client.get("/batch").text)
    marked = live(harness).wallpapers[0].id

    tile = client.post("/draft", data={"batch_id": batch_id, "wallpaper_id": marked, "verdict": "favourite"})
    held = client.get("/batch/next").text

    assert tile.status_code == HTTPStatus.OK
    assert 'data-draft-verdict="favourite"' in tile.text
    assert 'data-zone="unknown"' in tile.text
    assert '"verdict": ""' in tile.text, "the marked control must offer to clear itself"
    assert batch_id_of(held) == batch_id
    assert held.count('data-draft-verdict="favourite"') == 1
    assert held.count('data-draft-verdict=""') == 7

    cleared = client.post("/draft", data={"batch_id": batch_id, "wallpaper_id": marked, "verdict": ""})

    assert 'data-draft-verdict=""' in cleared.text
    assert live(harness).drafts == {}


def _star(tile: str) -> str:
    """The tile's one star, as its opening tag."""
    (star,) = re.findall(r'<button\b[^>]*class="star"[^>]*>', tile)
    return star


def _pressed(tile: str) -> dict[str, bool]:
    """Each of a tile's controls by the **Verdict** it marks, and whether it shows pressed."""
    return {
        verdict: 'aria-pressed="true"' in control
        for control in re.findall(r"<button\b[^>]*>", tile)
        for verdict in re.findall(r'data-verdict="(\w+)"', control)
    }


@pytest.mark.parametrize(
    ("marks", "stands"),
    [
        pytest.param(("favourite", "like"), "like", id="star then like"),
        pytest.param(("like", "favourite"), "favourite", id="like then star"),
        pytest.param(("ban", "favourite"), "favourite", id="ban then star"),
        pytest.param(("favourite", "ban"), "ban", id="star then ban"),
    ],
)
def test_the_star_and_a_like_or_ban_replace_each_other_on_the_tile_that_comes_back(
    web: Web, marks: tuple[str, str], stands: str
) -> None:
    """The star is the tile's **Favourite** control, posting to the same set-not-toggle route as the rail, so
    the tile swapped back after the second mark shows that one pressed and the first not, and the
    **Draft Batch** holds the one."""
    harness, client = web
    batch_id = batch_id_of(client.get("/batch").text)
    marked = live(harness).wallpapers[0].id

    tile = ""
    for verdict in marks:
        tile = client.post(
            "/draft", data={"batch_id": batch_id, "wallpaper_id": marked, "verdict": verdict}
        ).text

    assert 'data-verdict="favourite"' in _star(tile)
    assert _pressed(tile) == {choice: choice == stands for choice in ("favourite", "like", "ban")}
    assert live(harness).drafts == {marked: Verdict(stands)}


def test_a_starred_star_offers_to_clear_itself_and_an_unstarred_one_to_star(web: Web) -> None:
    """Clicking a starred star un-stars the tile, as clicking any marked control un-marks it."""
    harness, client = web
    batch_id = batch_id_of(client.get("/batch").text)
    marked = live(harness).wallpapers[0].id

    starred = client.post(
        "/draft", data={"batch_id": batch_id, "wallpaper_id": marked, "verdict": "favourite"}
    ).text
    cleared = client.post("/draft", data={"batch_id": batch_id, "wallpaper_id": marked, "verdict": ""}).text

    assert '"verdict": ""' in html.unescape(_star(starred))
    assert '"verdict": "favourite"' in html.unescape(_star(cleared))
    assert _pressed(cleared) == {"favourite": False, "like": False, "ban": False}


def test_a_starred_tile_submitted_is_a_favourite_and_reaches_the_library(web: Web, tmp_path: Path) -> None:
    """What the star is for: the submitted tile is a **Favourite** in the **Judgement**, and the post-commit
    reconciliation writes that **Wallpaper**, and only it, to the **Library**."""
    harness, client = web
    workflows.save_settings(harness.modules, library_path=tmp_path / "Library")
    batch_id = batch_id_of(client.get("/batch").text)
    shown = live(harness).wallpapers
    starred = shown[1].id

    client.post("/draft", data={"batch_id": batch_id, "wallpaper_id": starred, "verdict": "favourite"})
    submitted = client.post("/submit", data={"batch_id": batch_id})

    assert submitted.status_code == HTTPStatus.OK
    (judgement,) = evidence_log.judgements(harness.connect())
    assert [s.response for s in judgement.shown] == [TileResponse(favourite=w.id == starred) for w in shown]
    assert [written.wallpaper_id for written in harness.library.written] == [starred]


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
    following = client.get("/batch/next").text

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
    assert [len(j.shown) for j in evidence_log.judgements(harness.connect())] == [8]


def test_a_skip_decides_nothing_says_so_and_triggers_a_refetch(web: Web) -> None:
    """ADR 0026: the same refetch as a submission, and a new **Batch** behind it; the log holds the screen,
    passed over, and nothing in it is decided."""
    harness, client = web
    shown = batch_id_of(client.get("/batch").text)

    response = client.post("/skip", data={"batch_id": shown})
    following = client.get("/batch/next").text

    assert response.status_code == HTTPStatus.OK
    assert response.headers["HX-Trigger"] == "batch-submitted"
    assert "Skipped — nothing was decided." in response.text
    assert batch_id_of(following) != shown
    assert [j.response for j in evidence_log.judgements(harness.connect())] == [GridResponse(submitted=False)]


def test_skipping_the_same_batch_twice_is_refused_in_words_and_still_refetches(web: Web) -> None:
    """A skipped **Batch** is gone, so a second tab's skip is told so, and refetches to catch up."""
    _, client = web
    shown = batch_id_of(client.get("/batch").text)
    client.post("/skip", data={"batch_id": shown})

    again = client.post("/skip", data={"batch_id": shown})

    assert again.status_code == HTTPStatus.NOT_FOUND
    assert "That batch is no longer live here" in again.text
    assert again.headers["HX-Trigger"] == "batch-submitted"


def test_a_preview_opened_is_posted_without_a_swap_and_is_submitted_on_its_tile(web: Web) -> None:
    """What the preview posts when it opens: nothing comes back to swap, and the flag is server state, so the
    **Batch** submitted carries it, on that tile only."""
    harness, client = web
    batch_id = batch_id_of(client.get("/batch").text)
    shown = live(harness).wallpapers
    opened = shown[1].id

    posted = client.post("/draft/preview", data={"batch_id": batch_id, "wallpaper_id": opened})
    client.post("/submit", data={"batch_id": batch_id})

    assert posted.status_code == HTTPStatus.NO_CONTENT
    assert posted.text == ""
    (judgement,) = evidence_log.judgements(harness.connect())
    assert [s.response for s in judgement.shown] == [
        TileResponse(preview_opened=w.id == opened) for w in shown
    ]


@pytest.mark.parametrize("path", ["/submit", "/skip"])
@pytest.mark.parametrize(
    ("posted", "context"),
    [
        pytest.param({"theme": "dark"}, {"theme": "dark"}, id="dark"),
        pytest.param({"theme": "light"}, {"theme": "light"}, id="light"),
        pytest.param({}, {}, id="none posted"),
        pytest.param({"theme": "system"}, {}, id="system unresolved is dropped"),
        pytest.param({"theme": "sepia"}, {}, id="anything else is dropped"),
    ],
)
def test_submit_and_skip_store_the_posted_theme_and_drop_anything_but_light_or_dark(
    web: Web, path: str, posted: dict[str, str], context: dict[str, str]
) -> None:
    """The theme rides on the post as an htmx value; it is validated here and never refuses the post."""
    harness, client = web
    batch_id = batch_id_of(client.get("/batch").text)
    stored = settings.get(harness.connect())

    response = client.post(path, data={"batch_id": batch_id, **posted})

    assert response.status_code == HTTPStatus.OK
    assert [j.context for j in evidence_log.judgements(harness.connect())] == [context]
    assert settings.get(harness.connect()) == stored, "the theme is never a setting"


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
    (
        "/draft/preview",
        lambda batch: {"batch_id": batch, "wallpaper_id": "nope"},
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
    before = (
        evidence_log.judgements(harness.connect()),
        live(harness).drafts,
        settings.get(harness.connect()),
    )

    response = client.post(path, data=data(batch_id))

    assert response.status_code == status
    assert said in response.text
    assert (
        evidence_log.judgements(harness.connect()),
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
    assert "That batch is no longer live here" in unknown.text


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


# -- the compare tab ---------------------------------------------------------------------------------


LINEUP_ID = re.compile(r'name="lineup_id" value="([0-9a-f]+)"')
TILE_ID = re.compile(r'data-wallpaper-id="(\w+)"')


def lineup_of(body: str) -> tuple[str, list[str]]:
    """The **Line-up** ID a fragment carries for its answers, and its tiles' ids in order."""
    match = LINEUP_ID.search(body)
    assert match is not None, "the fragment must carry the Line-up ID it will answer"
    return match.group(1), TILE_ID.findall(body)


def test_the_compare_shell_mints_nothing_and_fetches_the_lineup_for_its_instrument(web: Web) -> None:
    """The **Instrument** is carried in the URL, **Pick** when none is named, **Order** and **Gauntlet**
    beside it; one the tab does not offer is a 404. A load, or a restore from the back-forward cache, is a
    visit (ADR 0029). The choice links each **Instrument** the tab offers and marks the one on screen."""
    _, client = web

    shell = client.get("/compare").text
    named = client.get("/compare?instrument=pick")
    order = client.get("/compare?instrument=order")
    gauntlet = client.get("/compare?instrument=gauntlet")

    assert 'hx-get="/compare/lineup?instrument=pick"' in shell
    assert 'hx-trigger="load, pageshow[persisted] from:window"' in shell
    assert 'data-wallpaper-id="' not in shell
    assert named.status_code == order.status_code == gauntlet.status_code == HTTPStatus.OK
    assert 'hx-get="/compare/lineup?instrument=order"' in order.text
    assert 'hx-get="/compare/lineup?instrument=gauntlet"' in gauntlet.text
    choice = r'<a href="/compare\?instrument=(\w+)"( aria-current="page")?>'
    assert [(name, bool(current)) for name, current in re.findall(choice, shell)] == [
        ("pick", True),
        ("order", False),
        ("gauntlet", False),
    ]
    assert [(name, bool(current)) for name, current in re.findall(choice, order.text)] == [
        ("pick", False),
        ("order", True),
        ("gauntlet", False),
    ]
    assert [current for _, current in re.findall(choice, gauntlet.text)] == ["", "", ' aria-current="page"']
    assert client.get("/compare?instrument=stream").status_code == HTTPStatus.NOT_FOUND
    assert client.get("/compare/lineup?instrument=stream").status_code == HTTPStatus.NOT_FOUND


def test_each_visit_fetches_a_new_lineup_and_records_nothing(web: Web) -> None:
    """A reload is a visit (ADR 0029): the **Line-up** before it is replaced and never recorded, and an
    answer to it is refused with words."""
    harness, client = web

    first, _ = lineup_of(client.get("/compare/lineup?instrument=pick").text)
    again, _ = lineup_of(client.get("/compare/lineup?instrument=pick").text)
    stale = client.post("/compare/pick", data={"lineup_id": first})

    assert again != first
    assert stale.status_code == HTTPStatus.CONFLICT
    assert "That line-up is no longer on screen" in " ".join(stale.text.split())
    assert lineup_of(stale.text)[0] == again
    assert evidence_log.judgements(harness.connect()) == []


def test_a_pick_records_the_winner_with_the_theme_and_answers_with_the_next_lineup(web: Web) -> None:
    """One post, one swap: the response is the next **Line-up**, not a banner and a refetch, so the next set
    comes at once. Nothing leaves the **Pool**."""
    harness, client = web
    lineup_id, tiles = lineup_of(client.get("/compare/lineup?instrument=pick").text)
    members = pool.members(harness.connect())

    response = client.post(
        "/compare/pick", data={"lineup_id": lineup_id, "wallpaper_id": tiles[2], "theme": "dark"}
    )

    assert response.status_code == HTTPStatus.OK
    assert "<html" not in response.text
    following, _ = lineup_of(response.text)
    assert following != lineup_id
    (judgement,) = evidence_log.judgements(harness.connect())
    assert (judgement.instrument, judgement.screen) == (PICK_V1, "compare")
    assert [s.response for s in judgement.shown] == [PickTile(picked=n == 2) for n in range(4)]
    assert judgement.response == PickResponse()
    assert judgement.context == {"theme": "dark"}
    assert pool.members(harness.connect()) == members


def test_none_of_these_is_a_pick_with_no_wallpaper(web: Web) -> None:
    harness, client = web
    lineup_id, _ = lineup_of(client.get("/compare/lineup?instrument=pick").text)

    response = client.post("/compare/pick", data={"lineup_id": lineup_id})

    assert response.status_code == HTTPStatus.OK
    assert lineup_of(response.text)[0] != lineup_id
    (judgement,) = evidence_log.judgements(harness.connect())
    assert judgement.response == PickResponse(none_of_these=True)


def test_answering_a_lineup_twice_is_refused_in_words_with_the_one_on_screen_now(web: Web) -> None:
    """A second tab, or a number key pressed twice: a 409 that says so and swaps in the **Line-up** now on
    screen, so the stale page catches up; only the first answer is recorded."""
    harness, client = web
    lineup_id, tiles = lineup_of(client.get("/compare/lineup?instrument=pick").text)
    first = client.post("/compare/pick", data={"lineup_id": lineup_id, "wallpaper_id": tiles[0]})
    live_now = lineup_of(first.text)

    again = client.post("/compare/pick", data={"lineup_id": lineup_id, "wallpaper_id": tiles[1]})

    assert again.status_code == HTTPStatus.CONFLICT
    assert "That line-up is no longer on screen" in " ".join(again.text.split())
    assert lineup_of(again.text) == live_now
    assert len(evidence_log.judgements(harness.connect())) == 1


def test_a_pick_of_a_wallpaper_the_lineup_does_not_show_is_a_bare_404(web: Web) -> None:
    """No control posts one, so it is a status, not a sentence."""
    harness, client = web
    lineup_id, _ = lineup_of(client.get("/compare/lineup?instrument=pick").text)

    refused = client.post("/compare/pick", data={"lineup_id": lineup_id, "wallpaper_id": "never-shown"})

    assert refused.status_code == HTTPStatus.NOT_FOUND
    assert evidence_log.judgements(harness.connect()) == []


def _ranks(body: str) -> list[int | None]:
    """The rank each **Line-up** tile shows on an **Order**, in on-screen order, `None` for unranked."""
    tiles = re.findall(r'<li class="lineup-tile".*?</li>', body, flags=re.DOTALL)
    shown = [re.search(r'<span class="rank">(\d+)</span>', tile) for tile in tiles]
    return [None if rank is None else int(rank.group(1)) for rank in shown]


def _rank(
    client: TestClient, lineup_id: str, wallpaper_id: str, *, ranked: bool = True, **form: str
) -> httpx.Response:
    """Rank one tile next on the **Order**, or take it back, as the tile's click posts it."""
    return client.post(
        "/compare/order/rank",
        data={"lineup_id": lineup_id, "wallpaper_id": wallpaper_id, "ranked": str(ranked).lower(), **form},
    )


def test_an_order_is_ranked_a_click_at_a_time_and_ranking_all_but_the_last_answers_it(web: Web) -> None:
    """Each click answers with the same **Line-up**, the ranks on its tiles and a ranked tile offering to be
    taken back, and records nothing; taking one back closes the ranks after it up. The click that leaves one
    tile unranked records one **Order** with the theme, the last tile ranked last, and answers with the next
    **Line-up**. Every tile stays in the **Pool**."""
    harness, client = web
    shown = lineup_of(client.get("/compare/lineup?instrument=order").text)
    lineup_id, (a, _, c, d) = shown
    members = pool.members(harness.connect())

    first = _rank(client, lineup_id, c)
    assert first.status_code == HTTPStatus.OK
    assert lineup_of(first.text) == shown
    assert _ranks(first.text) == [None, None, 1, None]
    tile_c = re.search(rf'<button\b[^>]*data-wallpaper-id="{c}"[^>]*>', first.text)
    assert tile_c is not None
    assert f'"wallpaper_id": "{c}", "ranked": "false"' in html.unescape(tile_c.group(0))
    assert 'hx-post="/compare/order/rank"' in first.text

    assert _ranks(_rank(client, lineup_id, a).text) == [2, None, 1, None]
    assert _ranks(_rank(client, lineup_id, c, ranked=False).text) == [1, None, None, None]
    assert _ranks(_rank(client, lineup_id, c).text) == [1, None, 2, None]
    assert evidence_log.judgements(harness.connect()) == []

    answered = _rank(client, lineup_id, d, theme="dark")

    assert answered.status_code == HTTPStatus.OK
    following, _ = lineup_of(answered.text)
    assert following != lineup_id
    assert _ranks(answered.text) == [None] * 4, "the next starts unranked"
    (judgement,) = evidence_log.judgements(harness.connect())
    assert (judgement.instrument, judgement.screen, judgement.response) == (ORDER_V1, "compare", None)
    assert [s.response for s in judgement.shown] == [OrderTile(rank=r) for r in (1, 4, 2, 3)]
    assert judgement.context == {"theme": "dark"}
    assert pool.members(harness.connect()) == members


def test_submit_records_a_partial_order_and_with_nothing_ranked_a_blank_one(web: Web) -> None:
    """Enter, or the nav's Submit: the ranks held as they stand, the rest unranked, and the next **Line-up**
    at once, as a **Pick** answers."""
    harness, client = web
    lineup_id, tiles = lineup_of(client.get("/compare/lineup?instrument=order").text)
    _rank(client, lineup_id, tiles[1])

    partial = client.post("/compare/order", data={"lineup_id": lineup_id, "theme": "light"})

    assert partial.status_code == HTTPStatus.OK
    following, _ = lineup_of(partial.text)
    assert following != lineup_id
    assert 'hx-post="/compare/order/rank"' in partial.text, "the next answers the same Instrument"

    blank = client.post("/compare/order", data={"lineup_id": following})

    assert blank.status_code == HTTPStatus.OK
    first, second = evidence_log.judgements(harness.connect())
    assert [s.response for s in first.shown] == [OrderTile(rank=1 if n == 1 else None) for n in range(4)]
    assert first.context == {"theme": "light"}
    assert [s.response for s in second.shown] == [OrderTile()] * 4
    assert second.instrument == ORDER_V1


def test_star_then_order_records_the_favourite_and_it_reaches_the_library(web: Web) -> None:
    """The star on an **Order** is the star on a **Pick**: posted for the **Instrument** on screen, it swaps
    back tiles that still rank, and the **Order** that records it puts the starred **Wallpaper** in the
    **Library** and out of the **Pool**, every other tile staying in it."""
    harness, client = web
    lineup_id, tiles = lineup_of(client.get("/compare/lineup?instrument=order").text)
    members = [w.id for w in pool.members(harness.connect())]
    _rank(client, lineup_id, tiles[0])

    starred = client.post(
        "/compare/star?instrument=order",
        data={"lineup_id": lineup_id, "wallpaper_id": tiles[3], "starred": "true"},
    )

    assert _lineup_starred(starred.text) == [n == 3 for n in range(4)]
    assert _ranks(starred.text) == [1, None, None, None], "a star keeps the ranks"
    assert 'hx-post="/compare/order/rank"' in starred.text
    client.post("/compare/order", data={"lineup_id": lineup_id})
    (judgement,) = evidence_log.judgements(harness.connect())
    assert [s.response for s in judgement.shown] == [
        OrderTile(rank=1 if n == 0 else None, favourite=n == 3) for n in range(4)
    ]
    assert [written.wallpaper_id for written in harness.library.written] == [tiles[3]]
    assert [w.id for w in pool.members(harness.connect())] == [w for w in members if w != tiles[3]]


def test_a_stale_rank_or_order_is_refused_in_words_and_a_tile_not_shown_is_a_bare_404(web: Web) -> None:
    """A second tab, or a key pressed twice: a rank or a submit on a **Line-up** already answered is a 409
    that swaps in the one on screen now, as a stale **Pick** is; a rank of a tile the **Line-up** does not
    show is a bare 404. Only the first answer is recorded."""
    harness, client = web
    lineup_id, tiles = lineup_of(client.get("/compare/lineup?instrument=order").text)
    live_now = lineup_of(client.post("/compare/order", data={"lineup_id": lineup_id}).text)

    stale_rank = _rank(client, lineup_id, tiles[0])
    stale_order = client.post("/compare/order", data={"lineup_id": lineup_id})
    unknown = _rank(client, live_now[0], "never-shown")

    for stale in (stale_rank, stale_order):
        assert stale.status_code == HTTPStatus.CONFLICT
        assert "That line-up is no longer on screen" in " ".join(stale.text.split())
        assert lineup_of(stale.text) == live_now
    assert unknown.status_code == HTTPStatus.NOT_FOUND
    assert len(evidence_log.judgements(harness.connect())) == 1


def _lineup_stars(body: str) -> list[str]:
    """Each **Line-up** tile's one star, as its opening tag, in on-screen order."""
    tiles = re.findall(r'<li class="lineup-tile".*?</li>', body, flags=re.DOTALL)
    return [_star(tile) for tile in tiles]


def _lineup_starred(body: str) -> list[bool]:
    """Whether each **Line-up** tile's star shows pressed, in on-screen order."""
    return ['aria-pressed="true"' in star for star in _lineup_stars(body)]


CROWN = re.compile(r'<span class="crown"[^>]*>\s*♛ (\d+)\s*</span>')


def _crowns(body: str) -> list[int | None]:
    """The streak on each **Line-up** tile's crown, in on-screen order, `None` for a tile without one."""
    tiles = re.findall(r'<li class="lineup-tile".*?</li>', body, flags=re.DOTALL)
    crowns = [CROWN.search(tile) for tile in tiles]
    return [None if crown is None else int(crown.group(1)) for crown in crowns]


def test_a_gauntlet_round_keeps_the_winner_in_place_under_a_crown_and_none_of_these_ends_it(web: Web) -> None:
    """Each answer is one post and one swap. The winner comes back in its place with the crown counting its
    streak, the other tiles new; "none of these" brings a new **Line-up** whole, with no crown. Each screen
    records one **Gauntlet** **Judgement** with the streak it carried and the theme. Nothing leaves the
    **Pool**."""
    harness, client = web
    visit = client.get("/compare/lineup?instrument=gauntlet").text
    first, first_tiles = lineup_of(visit)
    members = pool.members(harness.connect())
    champion = first_tiles[1]
    assert _crowns(visit) == [None] * 4, "no crown on a first screen"

    won = client.post(
        "/compare/gauntlet", data={"lineup_id": first, "wallpaper_id": champion, "theme": "dark"}
    )

    assert won.status_code == HTTPStatus.OK
    second, second_tiles = lineup_of(won.text)
    assert second != first
    assert second_tiles[1] == champion
    assert not set(second_tiles) - {champion} & set(first_tiles)
    assert _crowns(won.text) == [None, 1, None, None]
    assert 'hx-post="/compare/gauntlet"' in won.text, "the next answers the same Instrument"
    assert 'aria-label="Pick 2, the champion, 1 in a row"' in won.text

    again = client.post("/compare/gauntlet", data={"lineup_id": second, "wallpaper_id": champion})
    third, third_tiles = lineup_of(again.text)
    assert third_tiles[1] == champion
    assert _crowns(again.text) == [None, 2, None, None]

    dismissed = client.post("/compare/gauntlet", data={"lineup_id": third})

    assert dismissed.status_code == HTTPStatus.OK
    _, fourth_tiles = lineup_of(dismissed.text)
    assert not set(fourth_tiles) & set(third_tiles)
    assert _crowns(dismissed.text) == [None] * 4
    judgements = evidence_log.judgements(harness.connect())
    assert [(j.instrument, j.screen) for j in judgements] == [(GAUNTLET_V1, "compare")] * 3
    assert [j.response for j in judgements] == [
        GauntletResponse(streak=0),
        GauntletResponse(streak=1),
        GauntletResponse(streak=2, none_of_these=True),
    ]
    assert [s.response for s in judgements[1].shown] == [GauntletTile(picked=n == 1) for n in range(4)]
    assert judgements[1].shown[1].provenance["kept"] == "champion"
    assert [j.context for j in judgements] == [{"theme": "dark"}, {}, {}]
    assert pool.members(harness.connect()) == members


def test_a_reload_starts_a_new_gauntlet_and_the_crowned_screen_it_replaced_is_refused_in_words(
    web: Web,
) -> None:
    """A streak lives as long as its screen (ADR 0029): a visit, a reload included, draws a new **Line-up**
    with no crown and records nothing, and an answer to the crowned one is a 409 that shows the new one."""
    harness, client = web
    first, tiles = lineup_of(client.get("/compare/lineup?instrument=gauntlet").text)
    crowned, _ = lineup_of(
        client.post("/compare/gauntlet", data={"lineup_id": first, "wallpaper_id": tiles[0]}).text
    )

    reloaded = client.get("/compare/lineup?instrument=gauntlet").text
    stale = client.post("/compare/gauntlet", data={"lineup_id": crowned, "wallpaper_id": tiles[0]})

    after_reload, _ = lineup_of(reloaded)
    assert after_reload != crowned
    assert _crowns(reloaded) == [None] * 4
    assert stale.status_code == HTTPStatus.CONFLICT
    assert "That line-up is no longer on screen" in " ".join(stale.text.split())
    assert lineup_of(stale.text)[0] == after_reload
    assert len(evidence_log.judgements(harness.connect())) == 1


def test_a_winner_starred_on_a_gauntlet_reaches_the_library_and_its_streak_ends_there(web: Web) -> None:
    """The star on a **Gauntlet** is the star on a **Pick**: posted for the **Instrument** on screen, it
    swaps back the crowned **Line-up**, and the answer that records it puts the **Wallpaper** in the
    **Library** and out of the **Pool**, so it cannot be kept and the next comes whole."""
    harness, client = web
    first, tiles = lineup_of(client.get("/compare/lineup?instrument=gauntlet").text)
    won = client.post("/compare/gauntlet", data={"lineup_id": first, "wallpaper_id": tiles[2]}).text
    second, _ = lineup_of(won)

    starred = client.post(
        "/compare/star?instrument=gauntlet",
        data={"lineup_id": second, "wallpaper_id": tiles[2], "starred": "true"},
    )
    assert _crowns(starred.text) == [None, None, 1, None], "a star keeps the crown"
    assert 'hx-post="/compare/gauntlet"' in starred.text
    answered = client.post("/compare/gauntlet", data={"lineup_id": second, "wallpaper_id": tiles[2]})

    assert _crowns(answered.text) == [None] * 4
    assert tiles[2] not in lineup_of(answered.text)[1]
    _, judgement = evidence_log.judgements(harness.connect())
    assert [s.response for s in judgement.shown][2] == GauntletTile(picked=True, favourite=True)
    assert [written.wallpaper_id for written in harness.library.written] == [tiles[2]]
    assert tiles[2] not in [w.id for w in pool.members(harness.connect())]


def test_a_star_answers_with_the_same_lineup_starred_and_records_nothing(web: Web) -> None:
    """Starring never answers the **Line-up** or draws the next: the response is the same one, the tile's
    star now pressed and offering to come off, and nothing is recorded until the **Line-up** is answered."""
    harness, client = web
    shown = lineup_of(client.get("/compare/lineup?instrument=pick").text)
    lineup_id, tiles = shown

    starred = client.post(
        "/compare/star?instrument=pick",
        data={"lineup_id": lineup_id, "wallpaper_id": tiles[1], "starred": "true"},
    )

    assert starred.status_code == HTTPStatus.OK
    assert lineup_of(starred.text) == shown
    stars = _lineup_stars(starred.text)
    assert _lineup_starred(starred.text) == [n == 1 for n in range(4)]
    assert '"starred": "false"' in html.unescape(stars[1])
    assert '"starred": "true"' in html.unescape(stars[0])
    assert 'hx-post="/compare/pick"' in starred.text, "the tiles still answer the Instrument posted"
    assert evidence_log.judgements(harness.connect()) == []

    cleared = client.post(
        "/compare/star?instrument=pick",
        data={"lineup_id": lineup_id, "wallpaper_id": tiles[1], "starred": "false"},
    )

    assert _lineup_starred(cleared.text) == [False] * 4


def test_star_then_pick_records_one_judgement_with_both_and_the_favourite_reaches_the_library(
    web: Web,
) -> None:
    """What the star is for on the compare tab: one **Pick** **Judgement** with the **Favourite** on its tile
    beside the winner, the starred **Wallpaper** in the **Library** and out of the **Pool**, and every other
    tile still in it."""
    harness, client = web
    lineup_id, tiles = lineup_of(client.get("/compare/lineup?instrument=pick").text)
    members = [w.id for w in pool.members(harness.connect())]
    client.post(
        "/compare/star?instrument=pick",
        data={"lineup_id": lineup_id, "wallpaper_id": tiles[0], "starred": "true"},
    )

    response = client.post("/compare/pick", data={"lineup_id": lineup_id, "wallpaper_id": tiles[2]})

    assert response.status_code == HTTPStatus.OK
    (judgement,) = evidence_log.judgements(harness.connect())
    assert [s.response for s in judgement.shown] == [
        PickTile(picked=n == 2, favourite=n == 0) for n in range(4)
    ]
    assert [written.wallpaper_id for written in harness.library.written] == [tiles[0]]
    assert [w.id for w in pool.members(harness.connect())] == [w for w in members if w != tiles[0]]
    assert _lineup_starred(response.text) == [False] * 4, "the next starts unstarred"


def test_a_star_on_a_lineup_no_longer_on_screen_is_refused_in_words_with_the_one_on_screen_now(
    web: Web,
) -> None:
    """A second tab: the star lands on a **Line-up** already answered, so it is a 409 that swaps in the one on
    screen now, as a stale **Pick** is; a tile the **Line-up** does not show is a bare 404."""
    harness, client = web
    lineup_id, tiles = lineup_of(client.get("/compare/lineup?instrument=pick").text)
    live_now = lineup_of(client.post("/compare/pick", data={"lineup_id": lineup_id}).text)

    stale = client.post(
        "/compare/star?instrument=pick",
        data={"lineup_id": lineup_id, "wallpaper_id": tiles[0], "starred": "true"},
    )
    unknown = client.post(
        "/compare/star?instrument=pick",
        data={"lineup_id": live_now[0], "wallpaper_id": "never-shown", "starred": "true"},
    )

    assert stale.status_code == HTTPStatus.CONFLICT
    assert "That line-up is no longer on screen" in " ".join(stale.text.split())
    assert lineup_of(stale.text) == live_now
    assert unknown.status_code == HTTPStatus.NOT_FOUND
    assert _lineup_starred(client.get("/compare/lineup").text) == [False] * 4
    assert harness.connect().execute("SELECT COUNT(*) FROM lineup_stars").fetchone()[0] == 0


def test_switching_the_lineup_size_answers_with_the_switcher_alone_and_reaches_the_next_lineup(
    web: Web,
) -> None:
    harness, client = web
    lineup_id, _ = lineup_of(client.get("/compare/lineup?instrument=pick").text)

    switched = client.post("/compare/size", data={"size": "6"})
    still = lineup.live(harness.connect())
    following = client.post("/compare/pick", data={"lineup_id": lineup_id}).text
    visited = client.get("/compare/lineup?instrument=pick").text

    assert switched.status_code == HTTPStatus.OK
    assert 'id="lineup-size"' in switched.text
    assert 'value="6" selected' in switched.text
    assert "data-wallpaper-id" not in switched.text
    assert still is not None
    assert (still.id, len(still.wallpapers)) == (lineup_id, 4), "the Line-up on screen keeps its size"
    assert len(lineup_of(following)[1]) == 6
    assert len(lineup_of(visited)[1]) == 6
    assert settings.get(harness.connect()).lineup_size == 6


@pytest.mark.parametrize("posted", ["1", "9", "four", ""])
def test_a_lineup_size_outside_two_to_eight_is_a_400_and_changes_nothing(web: Web, posted: str) -> None:
    """Refused at the edge as the settings module refuses it: the switcher comes back as stored."""
    harness, client = web

    refused = client.post("/compare/size", data={"size": posted})

    assert refused.status_code == HTTPStatus.BAD_REQUEST
    assert 'value="4" selected' in refused.text
    assert settings.get(harness.connect()).lineup_size == 4


@pytest.mark.parametrize(
    ("arrange", "words"),
    [
        pytest.param({"fill_pool": 0}, ["fewer than two"], id="nothing arrived yet"),
        pytest.param({"fail_from_call": 1}, ["cannot reach Wallhaven", "set up to fail"], id="unreachable"),
    ],
)
def test_an_unavailable_lineup_is_a_page_in_words_never_a_500(
    db_path: Path, arrange: dict[str, int], words: list[str]
) -> None:
    harness = make_harness(db_path, **arrange)  # pyright: ignore[reportArgumentType]

    with serving(harness) as client:
        response = client.get("/compare/lineup?instrument=pick")

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    for word in words:
        assert word in " ".join(response.text.split())
    assert "too_few" not in response.text


# -- each visit draws a new screen (ADR 0029) ------------------------------------------------------------


def _counts(harness: Harness) -> tuple[int, ...]:
    """The rows a live screen holds: unsubmitted **Batches**, **Draft Batch** marks and previews, **Line-ups**
    and their stars."""
    connection = harness.connect()
    return tuple(
        int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("batches", "draft_batch", "draft_preview", "lineups", "lineup_stars")
    )


def test_two_visits_to_the_batches_page_show_two_batches_and_the_first_is_never_recorded(web: Web) -> None:
    """A reload is a visit: the first **Batch** is replaced, nothing is recorded, and its tiles stay in the
    **Pool**."""
    harness, client = web
    members = pool.members(harness.connect())

    first = client.get("/batch").text
    second = client.get("/batch").text

    assert batch_id_of(first) != batch_id_of(second)
    assert TILE_ID.findall(first) != TILE_ID.findall(second)
    assert evidence_log.judgements(harness.connect()) == []
    assert pool.members(harness.connect()) == members
    assert live(harness).id == batch_id_of(second)


def test_marks_and_stars_are_gone_after_a_reload_and_nothing_is_recorded(web: Web) -> None:
    """Every mark a screen holds goes with it: the **Draft Batch**'s marks and opened previews on the
    **Batches** page, the stars on the compare tab. The new screens come up unmarked."""
    harness, client = web
    batch_id = batch_id_of(client.get("/batch").text)
    client.post("/draft/all", data={"batch_id": batch_id, "verdict": "favourite"})
    client.post("/draft/preview", data={"batch_id": batch_id, "wallpaper_id": live(harness).wallpapers[0].id})
    lineup_id, tiles = lineup_of(client.get("/compare/lineup?instrument=pick").text)
    client.post(
        "/compare/star?instrument=pick",
        data={"lineup_id": lineup_id, "wallpaper_id": tiles[0], "starred": "true"},
    )
    assert _counts(harness) == (1, 8, 1, 1, 1)

    batch = client.get("/batch").text
    compare = client.get("/compare/lineup?instrument=pick").text

    assert batch.count('data-draft-verdict="favourite"') == 0
    assert _lineup_starred(compare) == [False] * 4
    assert _counts(harness) == (1, 0, 0, 1, 0)
    assert evidence_log.judgements(harness.connect()) == []
    assert harness.library.written == []


@pytest.mark.parametrize("path", ["/submit", "/skip"])
def test_answering_a_batch_a_later_visit_replaced_is_refused_in_words_and_refetches_the_live_one(
    web: Web, path: str
) -> None:
    """Two tabs on the **Batches** page: the second visit replaced the first tab's **Batch**, so its
    submission or **Skip** is refused with a sentence, and its refetch shows the one live now rather than
    replacing it in turn."""
    harness, client = web
    stale = batch_id_of(client.get("/batch").text)
    now_live = batch_id_of(client.get("/batch").text)

    refused = client.post(path, data={"batch_id": stale})
    refetched = client.get("/batch/next").text

    assert refused.status_code == HTTPStatus.NOT_FOUND
    assert "That batch is no longer live here" in refused.text
    assert "replaced when the page was opened again in another tab" in " ".join(refused.text.split())
    assert refused.headers["HX-Trigger"] == "batch-submitted"
    assert batch_id_of(refetched) == now_live
    assert evidence_log.judgements(harness.connect()) == []


def test_a_mark_never_redraws_the_batch(web: Web) -> None:
    """Only a visit draws: marking a tile, marking them all, opening a preview and switching the **Mix** leave
    the **Batch** on screen live, and an answer to it is recorded."""
    harness, client = web
    batch_id = batch_id_of(client.get("/batch").text)
    shown = live(harness).wallpapers[0].id

    client.post("/draft", data={"batch_id": batch_id, "wallpaper_id": shown, "verdict": "like"})
    client.post("/draft/all", data={"batch_id": batch_id, "verdict": "ban"})
    client.post("/draft/preview", data={"batch_id": batch_id, "wallpaper_id": shown})
    client.post("/mix", data={"mix": "refine"})
    submitted = client.post("/submit", data={"batch_id": batch_id})

    assert submitted.status_code == HTTPStatus.OK
    assert len(evidence_log.judgements(harness.connect())) == 1


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


def test_a_batch_minted_with_no_unknowns_says_to_lower_the_similarity_radius(web: Web) -> None:
    """Every **Pool** member within the radius of a **Ban**: the next **Batch** is all **Duds**, and the page
    says why as each is minted (ADR 0023), which a visit does (ADR 0029)."""
    harness, client = web
    shown = live(harness)
    banned = shown.wallpapers[0].id
    harness.similarity.similarity_by_pair.update({(w.id, banned): 1.0 for w in catalogue_of(24)})
    workflows.set_draft(harness.modules, shown.id, banned, Verdict.BAN)
    client.post("/submit", data={"batch_id": shown.id})

    minted = client.get("/batch/next").text
    handed_back = client.get("/batch/next").text
    visited = client.get("/batch").text

    words = " ".join(html.unescape(re.sub(r"<[^>]+>", "", minted)).split())
    assert "data-unknown-short" in minted
    assert "Only 0 unknown wallpapers in the pool, fewer than the explore mix's 6 unknown slots" in words
    assert "Lower the similarity radius in Settings" in words
    assert "data-unknown-short" not in handed_back, "the live Batch read back has nothing to say"
    assert "data-unknown-short" in visited, "a visit mints, so it says it again"


def test_a_batch_with_unknowns_to_spare_adds_no_unknown_line(web: Web) -> None:
    _, client = web

    assert "data-unknown-short" not in client.get("/batch").text


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
    """A **Pool** growing only at random means there are no **Favourites** or **Likes** yet: something to
    act on."""
    harness = make_harness(
        db_path, catalogue=catalogue_of(2), like_results={"wp0000": catalogue_of(4, prefix="lk")}
    )
    if favourited:
        workflows.save_settings(harness.modules, pool_target_size=1000)
        favourite(harness, "wp0000")
        harness.fill_pool(1)

    with serving(harness) as client:
        text = client.get("/batch").text

    assert ("searching for lookalikes of a favourite or like" in text) is favourited
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
    for n in range(count):
        harness.clock.advance(1)
        judge(harness, **{f"wp{n:04d}": Verdict.LIKE})

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
    assert evidence_log.resolve(harness.connect(), ["judged"])["judged"].verdict is verdict


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
            "api_calls_per_minute": "20",
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
    assert stored.api_calls_per_minute == 20


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


def test_resetting_the_pool_answers_with_the_section_and_what_it_removed(web: Web) -> None:
    """In place, not a redirect: the section swaps itself, now counting an empty **Pool**. What the reset
    does to the modules is `test_pool.py`'s; this is that the route reached it."""
    harness, client = web
    live(harness)
    client.get("/compare/lineup?instrument=pick")

    response = client.post("/settings/pool/reset", follow_redirects=False)

    said = " ".join(response.text.split())
    assert response.status_code == HTTPStatus.OK
    assert (
        "Removed 24 pool members, discarded the batch and the line-up on screen and evicted 0 thumbnails."
        in said
    )
    assert "The pool holds 0 wallpapers." in said
    assert "<html" not in response.text
    assert pool.members(harness.connect()) == []
    assert batches.live(harness.connect()) is None


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
    reasons = [*SubmissionRefused.Reason, *HistoryRefused.Reason, *LineupRefused.Reason]

    assert len(REFUSALS) == len(reasons)
    assert all(REFUSALS.keys() & {reason} for reason in reasons)
