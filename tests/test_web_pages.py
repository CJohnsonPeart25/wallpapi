"""What the pages say: the few markup checks worth keeping, each once.

No browser, so neither Pico's styling nor Alpine's behaviour is exercised. What is asserted is content a
regression would drop silently: where assets come from, which controls exist, the preview's deferred image,
and the words and values each page shows.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import Harness, batch_id_of, judge, live, make_harness, serving
from tests.fakes import catalogue_of, wallpaper
from wallpapi import settings, workflows
from wallpapi.model import Verdict
from wallpapi.settings import Field, Section, Settings

Web = tuple[Harness, TestClient]
ASSETS = re.compile(r'<(?:script|link)\b[^>]*\b(?:src|href)="([^"]+)"')


def test_every_page_loads_only_what_the_app_serves_and_all_of_it_answers(web: Web) -> None:
    """A personal tool that stops working when somebody else's host does is worse than a few vendored
    files, and the grid script fetches nothing of its own."""
    _, client = web

    bodies = {path: client.get(path).text for path in ("/", "/history", "/settings")}
    loaded = {asset for body in bodies.values() for asset in ASSETS.findall(body)}
    answers = {asset: client.get(asset) for asset in loaded}

    for path, body in bodies.items():
        for asset in ASSETS.findall(body):
            assert asset.startswith("/static/"), f"{path} loads {asset}, which the app does not serve"
        for base in ("pico.indigo.min.css", "base.css", "htmx.min.js", "alpine.min.js"):
            assert f'"/static/{base}"' in body, f"{path} is not on the shell: no {base}"
    assert "/static/wallpapi.js" in loaded
    assert all(response.status_code == 200 for response in answers.values())
    assert "http" not in answers["/static/wallpapi.js"].text


def test_every_tile_has_its_three_controls_and_each_posts_with_hx_sync(web: Web) -> None:
    """**Ignore** has no control: it is what leaving a tile alone says. One post per tile at a time
    (`closest .tile:drop`): with a sync per button, Ban clicked while Like is in flight lands on a detached
    tile and the screen shows **Like** while the server holds **Ban**. Bulk and tile controls disable each
    other. The **Batch** has its select-all and select-none controls once."""
    harness, client = web
    body = client.get("/batch").text
    shown = live(harness).wallpapers[0].id

    tile = client.post(
        "/draft", data={"batch_id": batch_id_of(body), "wallpaper_id": shown, "verdict": "like"}
    ).text

    controls = re.findall(r"<button\b[^>]*data-verdict=[^>]*>", tile)
    assert {re.search(r'data-verdict="(\w*)"', c).group(1) for c in controls} >= {"favourite", "like", "ban"}  # pyright: ignore[reportOptionalMemberAccess]
    for control in controls + re.findall(r"<button\b[^>]*data-verdict=[^>]*>", body):
        assert 'hx-sync="closest .tile:drop"' in control
        assert "this:replace" not in control
        assert 'hx-disabled-elt=".bulk-verdict"' in control
    assert 'data-verdict="ignore"' not in body
    for choice in ("favourite", "like", "ban", ""):
        bulk = re.search(rf'<button\b[^>]*data-bulk-verdict="{choice}"[^>]*>', body)
        assert bulk is not None
        assert 'hx-sync="#batch-grid:replace"' in bulk.group(0)
        assert 'hx-disabled-elt=".verdict"' in bulk.group(0)
    assert 'data-bulk-verdict="ignore"' not in body


def test_the_preview_fetches_nothing_until_it_is_opened(web: Web) -> None:
    """A native `<dialog>` whose image has no `src` and no binding: nothing full-size is fetched until it
    is asked for (a deferred decision in `AGENTS.md`). Each tile carries its full-size and Wallhaven URLs,
    and the one link out is the preview's, opened without a handle back or a referrer."""
    harness, client = web
    body = client.get("/").text + client.get("/batch").text

    dialog = body[body.index("<dialog") : body.index("</dialog>")]
    assert "<img" in dialog
    assert "src=" not in dialog
    assert ":src" not in dialog
    assert 'rel="noopener noreferrer"' in dialog
    for shown in live(harness).wallpapers:
        assert f'data-full-url="{shown.full_url}"' in body
        assert f'data-page-url="{shown.page_url}"' in body
        assert f'href="{shown.page_url}"' not in body, "the tile renders no link of its own"


def test_each_tile_says_its_zone_in_words(db_path: Path) -> None:
    """`data-zone` for the stylesheet, and the word itself, since states told apart only by colour are
    states nobody colour-blind can tell apart."""
    harness = make_harness(db_path)
    loved = live(harness).wallpapers[0].id
    harness.similarity.similarity_by_pair.update({(f"wp{n:04d}", loved): 0.95 for n in range(24)})

    with serving(harness) as client:
        first = client.get("/batch").text
        batch_id = batch_id_of(first)
        client.post("/draft", data={"batch_id": batch_id, "wallpaper_id": loved, "verdict": "favourite"})
        client.post("/submit", data={"batch_id": batch_id})
        second = client.get("/batch").text

    assert 'data-zone="unknown"' in first
    assert 'data-zone="banger"' in second
    assert ">banger<" in second


def test_a_history_row_shows_its_thumbnail_verdict_time_and_link(db_path: Path) -> None:
    """The thumbnail off the cache with no `width`/`height`, which describe the full-size file; the
    resolved **Verdict** by name and as the pressed control, **Ignore** included; and the link shown."""
    harness = make_harness(
        db_path, catalogue=(wallpaper("judged"), wallpaper("ignored"), wallpaper("untouched"))
    )
    judge(harness, judged=Verdict.FAVOURITE, ignored=Verdict.IGNORE)

    with serving(harness) as client:
        body = client.get("/history").text

    def row(wallpaper_id: str) -> str:
        start = body.rindex("<tr", 0, body.index(f'id="history-{wallpaper_id}"'))
        return body[start : body.index("</tr>", start)]

    judged = row("judged")
    thumbnail = re.search(r"<img\b[^>]*>", judged)
    assert thumbnail is not None
    assert 'src="/thumb/judged"' in thumbnail.group(0)
    assert "width=" not in thumbnail.group(0)
    assert "height=" not in thumbnail.group(0)
    assert ">favourite<" in judged
    assert "2026-09-24 11:30 UTC" in judged
    assert 'href="https://wallhaven.cc/w/judged"' in judged
    controls = re.findall(r"<button\b[^>]*>", judged)
    assert len(controls) == 4
    for control in controls:
        assert 'hx-sync="closest tr:drop"' in control, "one post per row, or a late entry lands unseen"
        assert "this:replace" not in control
    pressed = [b for b in re.findall(r"<button\b[^>]*>", row("ignored")) if 'aria-pressed="true"' in b]
    assert len(pressed) == 1
    assert 'data-verdict="ignore"' in pressed[0]
    assert "untouched" not in body


def test_an_empty_history_says_so_and_so_does_an_empty_filter(db_path: Path) -> None:
    harness = make_harness(db_path, catalogue=catalogue_of(2))

    with serving(harness) as client:
        empty = client.get("/history").text
        judge(harness, wp0000=Verdict.LIKE)
        filtered = client.get("/history?verdict=ban").text

    assert "Nothing judged yet" in empty
    assert "Nothing resolves to ban yet" in filtered


def test_the_switcher_offers_every_mix_with_its_percentages_and_marks_the_active_one(db_path: Path) -> None:
    """The names say nothing about which way round they are, so the options carry the numbers; a **Mix**
    made on the settings page is selectable where it is used."""
    harness = make_harness(db_path)
    workflows.save_mix(harness.modules, "duds only", unknown=0, banger=0, dud=100)

    with serving(harness) as client:
        page = client.get("/").text

    for name, shares in (("explore", "75/20/5"), ("refine", "25/70/5"), ("duds only", "0/0/100")):
        assert f'value="{name}"' in page
        assert shares in page
    assert 'data-mix-active="explore"' in page


def test_a_setting_declared_in_a_new_section_shows_on_the_page_with_no_template_edit(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The form is the table: one descriptor gives the page its section, label, input and hint. It has a
    minimum and no maximum, so the input says the one and not the other."""
    throwaway = Section(heading="Throwaway", intro="Only here for the test.")
    extra = Field(
        default=3,
        parse=int,
        message="Never shown.",
        minimum=2,
        section=throwaway,
        label="Extra knob",
        help="From {minimum} up.",
        step="1",
    )
    extra.__set_name__(Settings, "extra_knob")
    monkeypatch.setattr(Settings, "extra_knob", extra, raising=False)
    monkeypatch.setattr(settings, "FIELDS", (*settings.FIELDS, extra))
    monkeypatch.setattr(settings, "FORM_FIELDS", (*settings.FORM_FIELDS, extra))
    harness = make_harness(db_path)

    with serving(harness) as client:
        page = client.get("/settings").text

    article = page.split("<h2>Throwaway</h2>", 1)[1].split("</article>", 1)[0]
    assert "<p><small>Only here for the test.</small></p>" in article
    assert '<label for="extra_knob">Extra knob</label>' in article
    attributes = ('type="number"', 'id="extra_knob"', 'name="extra_knob"', 'min="2"', 'step="1"', 'value="3"')
    assert re.search(r"<input\s+" + r"\s+".join(attributes) + r"\s+/>", article)
    assert "<small>From 2 up.</small>" in article


def test_the_settings_page_shows_what_is_stored_and_offers_what_can_be_done(
    db_path: Path, tmp_path: Path
) -> None:
    """The form is an edit, not a blank slate. Every **Mix** has a row and there is one for adding; only
    a custom **Mix** offers delete, and the active one is marked so the missing button explains itself."""
    harness = make_harness(db_path)
    chosen = tmp_path / "Wallpapers"
    workflows.save_settings(harness.modules, batch_size=12, library_path=chosen)
    workflows.save_mix(harness.modules, "duds only", unknown=0, banger=0, dud=100)

    with serving(harness) as client:
        page = client.get("/settings").text

    for field, value in (("batch_size", "12"), ("min_width", "2560"), ("allowed_ratios", "16x9,16x10,21x9")):
        assert f'name="{field}"' in page
        assert f'value="{value}"' in page
    for field in ("min_height", "min_favourites", "pool_target_size"):
        assert f'name="{field}"' in page
    assert str(chosen) in page
    assert 'action="/settings/library/download"' in page
    for name in ("explore", "refine", "duds only"):
        assert f'data-mix-row="{name}"' in page
    assert "data-mix-new" in page
    assert 'data-mix-delete="duds only"' in page
    assert 'data-mix-delete="explore"' not in page
    assert 'data-mix-delete="refine"' not in page
    assert 'data-mix-active="explore"' in page
