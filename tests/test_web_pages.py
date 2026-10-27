"""What the pages say: the few markup checks worth keeping, each once.

No browser, so neither Pico's styling nor Alpine's behaviour is exercised. What is asserted is content a
regression would drop silently: where assets come from, which controls exist, the preview's deferred image,
and the words and values each page shows. Over `make_harness`, because a page is rendered over the real
modules composed as the app composes them (ADR 0019).
"""

from __future__ import annotations

import html
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import Harness, batch_id_of, judge, live, make_harness, serving, submit_with
from tests.fakes import catalogue_of, wallpaper
from wallpapi import settings, workflows
from wallpapi.model import Verdict
from wallpapi.settings import Field, Section, Settings
from wallpapi.web.app import TEMPLATES_DIR

Web = tuple[Harness, TestClient]
ASSETS = re.compile(r'<(?:script|link)\b[^>]*\b(?:src|href)="([^"]+)"')


def test_every_page_loads_only_what_the_app_serves_and_all_of_it_answers(web: Web) -> None:
    """A personal tool that stops working when somebody else's host does is worse than a few vendored
    files, and the grid script fetches nothing of its own."""
    _, client = web

    bodies = {path: client.get(path).text for path in ("/", "/compare", "/history", "/settings")}
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
    """The star and the rail's **Like** and **Ban**. **Ignore** has no control: it is what leaving a tile
    alone says. One post per tile at a time (`closest .tile:drop`): with a sync per button, Ban clicked
    while Like is in flight lands on a detached tile and the screen shows **Like** while the server holds
    **Ban**. Bulk and tile controls, the star among them, disable each other. The **Batch** has its
    select-all and select-none controls once."""
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
        assert 'hx-disabled-elt=".verdict, .star"' in bulk.group(0)
    assert 'data-bulk-verdict="ignore"' not in body


def test_every_tile_has_one_star_beside_its_rail_that_f_presses_on_the_focused_or_hovered_tile(
    web: Web,
) -> None:
    """The star is a direct child of its tile, outside the rail, since that is how its F key finds the tile;
    F is an htmx trigger on the body beside the click, so neither `wallpapi.js` nor a new Alpine island
    decides it. Never with a modifier (Ctrl+F is the browser's find), on a held key, or in a field."""
    harness, client = web
    body = client.get("/batch").text

    tiles = re.findall(r'<li class="tile".*?</li>', body, flags=re.DOTALL)
    assert len(tiles) == len(live(harness).wallpapers)
    for tile in tiles:
        (star,) = re.findall(r'<button\b[^>]*class="star"[^>]*>', tile)
        assert 'data-verdict="favourite"' in star
        rail = tile[tile.index('<div class="verdicts">') :]
        assert 'class="star"' not in rail
        assert re.findall(r'data-verdict="(\w+)"', rail) == ["like", "ban"]
        trigger = html.unescape(re.search(r'hx-trigger="([^"]*)"', star).group(1))  # pyright: ignore[reportOptionalMemberAccess]
        assert trigger.startswith("click, keydown[")
        assert trigger.endswith("] from:body")
        for condition in ("'f'", "!repeat", "!ctrlKey", "!metaKey", "!altKey", "this.parentElement"):
            assert condition in trigger
        assert "!target.closest('input,select,textarea')" in trigger
        for state in (":focus-visible", ":hover"):
            assert state in trigger


def test_the_preview_rail_marks_through_the_tiles_own_controls_the_star_among_them(web: Web) -> None:
    """The rail posts nothing of its own: it clicks the tile's control for the same **Verdict**, which for a
    **Favourite** is the star, not a rail segment; after any swap it reads the tile's mark back."""
    _, client = web
    shell = client.get("/").text

    dialog = shell[shell.index("<dialog") : shell.index("</dialog>")]
    rail = re.findall(r'class="preview-verdict"\s+data-verdict="(\w+)"', dialog)
    assert rail == ["favourite", "like", "ban"]
    assert "querySelector('[data-verdict=' + verdict + ']')" in dialog
    assert "'.verdict[data-verdict=" not in dialog
    assert "tile.dataset.draftVerdict" in dialog
    assert 'x-on:htmx:after-settle.camel.window="sync()"' in dialog


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


def test_opening_the_preview_posts_from_the_dialog_which_no_swap_replaces(web: Web) -> None:
    """The preview-opened post is an htmx form inside the shell's `<dialog>`, carrying the **Batch** ID the
    last fetch left and swapping nothing; the **Batch** fragment htmx replaces holds none of it."""
    _, client = web
    shell = client.get("/").text
    fragment = client.get("/batch").text

    dialog = shell[shell.index("<dialog") : shell.index("</dialog>")]
    form = re.search(r'<form\b[^>]*hx-post="/draft/preview"[^>]*>', dialog)
    assert form is not None
    for attribute in ('hx-trigger="preview-opened"', 'hx-include="#batch-id"', 'hx-swap="none"'):
        assert attribute in form.group(0)
    assert 'name="wallpaper_id"' in dialog
    assert "preview-opened" in dialog[: dialog.index("<form")], "the dialog's open() must fire it"
    assert "/draft/preview" not in fragment


def test_submit_and_skip_post_the_theme_the_browser_resolved(web: Web) -> None:
    """`system` is no `data-theme` at all, so the page asks the OS the way Pico does; the value goes as an
    htmx value with the **Batch** ID."""
    _, client = web
    shell = client.get("/").text

    for path in ("/submit", "/skip"):
        form = re.search(rf'<form\b[^>]*hx-post="{path}"[^>]*>', shell)
        assert form is not None
        vals = re.search(r'hx-vals="([^"]*)"', form.group(0))
        assert vals is not None
        assert vals.group(1).lstrip().startswith("js:{theme:")
        assert "dataset.theme" in vals.group(1)
        assert "matchMedia('(prefers-color-scheme: dark)')" in vals.group(1)


@pytest.mark.parametrize("instrument", ["pick", "gauntlet"])
def test_a_lineup_tile_answers_on_a_click_or_its_number_and_none_of_these_on_a_button_or_0(
    web: Web, instrument: str
) -> None:
    """htmx attributes, no script of its own: each tile posts itself as the winner on a click or on its
    number key, the nav's "none of these" posts no winner on a click or 0, and every answer carries the
    **Line-up** ID and the theme, targets `#lineup` and is dropped while another is in flight. A key is no
    answer with a modifier held, held down, or typed into a field. A **Gauntlet** answers as a **Pick**
    does, at its own route: 0 is its "none of these" too."""
    _, client = web
    shell = client.get(f"/compare?instrument={instrument}").text
    fragment = client.get(f"/compare/lineup?instrument={instrument}").text

    tiles = re.findall(r"<button\b[^>]*data-wallpaper-id=[^>]*>", fragment)
    none = re.search(r"<form\b[^>]*data-none-of-these[^>]*>", shell)
    assert len(tiles) == 4
    assert none is not None
    answers = [*tiles, none.group(0)]
    for number, control in enumerate(answers, start=1):
        key = number % len(answers)
        trigger = re.search(r'hx-trigger="([^"]*)"', control)
        assert trigger is not None
        assert f"keydown[key=='{key}'&&" in trigger.group(1)
        assert trigger.group(1).endswith("] from:body")
        for guard in ("!ctrlKey", "!metaKey", "!altKey", "!repeat", "input,select,textarea"):
            assert guard in trigger.group(1)
        assert f'hx-post="/compare/{instrument}"' in control
        assert 'hx-target="#lineup"' in control
        assert 'hx-sync="#lineup:drop"' in control
        assert 'hx-include="#lineup-id"' in control
    for tile in tiles:
        winner = re.search(r'data-wallpaper-id="(\w+)"', tile)
        assert winner is not None
        assert f'hx-vals=\'{{"wallpaper_id": "{winner.group(1)}"}}\'' in tile
    assert re.search(r'<ul class="lineup"[^>]*hx-vals="js:\{theme:', fragment)
    assert re.search(r'hx-vals="js:\{theme:', none.group(0))
    assert "x-data" not in fragment, "no Alpine island on markup htmx replaces"


def test_an_order_tile_ranks_on_a_click_or_its_number_and_submit_is_a_button_or_enter_with_0_unbound(
    web: Web,
) -> None:
    """On an **Order**, each tile posts its rank on a click or its number key, an unranked one to be ranked
    and a ranked one, its rank on it, to be taken back; the nav's Submit posts the **Order** as it stands on
    a click or Enter, which a focused button or link keeps for itself. Every post carries the **Line-up** ID
    and the theme, targets `#lineup` and is dropped while another is in flight, under the same key guard as
    a **Pick**'s. 0 answers nothing, and the page has no "none of these"."""
    _, client = web
    shell = client.get("/compare?instrument=order").text
    fragment = client.get("/compare/lineup?instrument=order").text
    lineup_id = re.search(r'name="lineup_id" value="(\w+)"', fragment).group(1)  # pyright: ignore[reportOptionalMemberAccess]
    first = re.search(r'data-wallpaper-id="(\w+)"', fragment).group(1)  # pyright: ignore[reportOptionalMemberAccess]
    fragment = client.post(
        "/compare/order/rank", data={"lineup_id": lineup_id, "wallpaper_id": first, "ranked": "true"}
    ).text

    tiles = re.findall(r"<button\b[^>]*data-wallpaper-id=[^>]*>", fragment)
    submit = re.search(r"<form\b[^>]*data-submit-order[^>]*>", shell)
    assert len(tiles) == 4
    assert submit is not None
    for number, tile in enumerate(tiles, start=1):
        trigger = re.search(r'hx-trigger="([^"]*)"', tile)
        assert trigger is not None
        assert trigger.group(1).startswith(f"click, keydown[key=='{number}'&&")
        assert trigger.group(1).endswith("] from:body")
        for guard in ("!ctrlKey", "!metaKey", "!altKey", "!repeat", "input,select,textarea"):
            assert guard in trigger.group(1)
        wallpaper_id = re.search(r'data-wallpaper-id="(\w+)"', tile).group(1)  # pyright: ignore[reportOptionalMemberAccess]
        offered = "false" if wallpaper_id == first else "true"
        assert 'hx-post="/compare/order/rank"' in tile
        assert f'"wallpaper_id": "{wallpaper_id}", "ranked": "{offered}"' in html.unescape(tile)
        assert 'hx-target="#lineup"' in tile
        assert 'hx-sync="#lineup:drop"' in tile
        assert 'hx-include="#lineup-id"' in tile
    assert re.findall(r'<span class="rank">(\d)</span>', fragment) == ["1"]
    assert 'hx-post="/compare/order"' in submit.group(0)
    assert 'hx-target="#lineup"' in submit.group(0)
    assert 'hx-sync="#lineup:drop"' in submit.group(0)
    assert 'hx-include="#lineup-id"' in submit.group(0)
    assert re.search(r'hx-vals="js:\{theme:', submit.group(0))
    submit_trigger = html.unescape(re.search(r'hx-trigger="([^"]*)"', submit.group(0)).group(1))  # pyright: ignore[reportOptionalMemberAccess]
    assert submit_trigger.startswith("submit, keydown[key=='Enter'&&")
    assert "!target.closest('button,a')" in submit_trigger
    assert "key=='0'" not in shell + fragment
    assert "data-none-of-these" not in shell
    assert "x-data" not in fragment, "no Alpine island on markup htmx replaces"


def test_a_gauntlets_champion_wears_its_crown_on_its_own_pick_and_says_so_in_words(web: Web) -> None:
    """The crown is inside the champion's tile, so it sits on the thumbnail and is part of what a click
    picks, with its streak in the tile's name for a screen reader; no other tile has one, and no Alpine
    island is added for it."""
    _, client = web
    first = client.get("/compare/lineup?instrument=gauntlet").text
    lineup_id = re.search(r'name="lineup_id" value="(\w+)"', first).group(1)  # pyright: ignore[reportOptionalMemberAccess]
    tiles = re.findall(r'data-wallpaper-id="(\w+)"', first)

    fragment = client.post("/compare/gauntlet", data={"lineup_id": lineup_id, "wallpaper_id": tiles[3]}).text

    buttons = re.findall(r'<button\b[^>]*class="pick".*?</button>', fragment, flags=re.DOTALL)
    crowned = [button for button in buttons if 'class="crown"' in button]
    assert len(crowned) == 1
    assert f'data-wallpaper-id="{tiles[3]}"' in crowned[0]
    assert 'aria-label="Pick 4, the champion, 1 in a row"' in crowned[0]
    assert re.search(r'<span class="crown" title="Won 1 in a row">\s*♛ 1\s*</span>', crowned[0])
    assert "x-data" not in fragment


def test_every_lineup_tile_has_the_grids_star_beside_its_pick_never_inside_it(web: Web) -> None:
    """The same star as the grid's (star.html), by click or F, as a direct child of the tile so F finds it,
    and a sibling of the Pick rather than inside it, so a click on the star is never a click on the tile.
    It posts the tile's star to the live **Line-up** for the **Instrument** on screen, carrying the
    **Line-up** ID, and swaps the **Line-up** back; it is synced with the answers on `#lineup`, so a star and
    an answer are never in flight together."""
    _, client = web
    fragment = client.get("/compare/lineup?instrument=pick").text

    tiles = re.findall(r'<li class="lineup-tile">(.*?)</li>', fragment, flags=re.DOTALL)
    assert len(tiles) == 4
    for tile in tiles:
        (star,) = re.findall(r'<button\b[^>]*class="star"[^>]*>', tile)
        pick = tile[tile.index('class="pick"') : tile.index("</button>")]
        assert 'class="star"' not in pick
        assert tile.index("</button>") < tile.index('class="star"'), "the star follows the Pick, outside it"
        wallpaper_id = re.search(r'data-wallpaper-id="(\w+)"', tile).group(1)  # pyright: ignore[reportOptionalMemberAccess]
        assert 'hx-post="/compare/star?instrument=pick"' in star
        assert 'hx-target="#lineup"' in star
        assert 'hx-sync="#lineup:drop"' in star
        assert 'hx-include="#lineup-id"' in star
        assert f'"wallpaper_id": "{wallpaper_id}", "starred": "true"' in html.unescape(star)
        assert 'aria-pressed="false"' in star
        trigger = html.unescape(re.search(r'hx-trigger="([^"]*)"', star).group(1))  # pyright: ignore[reportOptionalMemberAccess]
        assert trigger.startswith("click, keydown[")
        assert "'f'" in trigger
        assert trigger.endswith("] from:body")


def test_the_lineup_size_switcher_offers_two_to_eight_and_marks_the_stored_size(db_path: Path) -> None:
    harness = make_harness(db_path)
    workflows.save_settings(harness.modules, lineup_size=6)

    with serving(harness) as client:
        shell = client.get("/compare").text

    switcher = shell[shell.index('<section id="lineup-size"') : shell.index("</section>")]
    assert re.findall(r'<option value="(\d)"', switcher) == [str(n) for n in range(2, 9)]
    assert 'value="6" selected' in switcher
    assert shell.count('<li class="placeholder">') == 6


def test_each_tile_says_its_zone_in_words(db_path: Path) -> None:
    """`data-zone` for the stylesheet, and the word itself, since states told apart only by colour are
    states nobody colour-blind can tell apart."""
    harness = make_harness(db_path)

    with serving(harness) as client:
        first = client.get("/batch").text
        batch_id = batch_id_of(first)
        loved = live(harness).wallpapers[0].id
        harness.similarity.similarity_by_pair.update({(f"wp{n:04d}", loved): 0.95 for n in range(24)})
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


def test_a_history_row_carries_the_wallpaper_its_resolved_verdict_and_its_latest_timestamp(
    db_path: Path,
) -> None:
    """The *resolved* **Verdict**, which is the difference between **History** and a print-out of the
    log, as data attributes and a machine-readable time. Read off the page, which is where the
    **Wallpaper** is joined on."""
    harness = make_harness(db_path, catalogue=(wallpaper("only"),))
    workflows.save_settings(harness.modules, batch_size=1)
    submit_with(harness, {"only": Verdict.LIKE})

    with serving(harness) as client:
        page = client.get("/history").text

    assert 'data-wallpaper-id="only"' in page
    assert 'href="https://wallhaven.cc/w/only"' in page
    assert 'data-resolved-verdict="like"' in page
    assert f'<time datetime="{harness.clock.now().isoformat()}">' in page


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


def test_the_settings_templates_spell_no_setting_and_no_mix_field() -> None:
    """Adding a setting or a **Mix** field is a change to `settings.py` alone. A setting's key is a word of
    its own; a **Mix** field is an ordinary word too ("name" is also an attribute), so for those only a
    quoted literal, an attribute lookup or a word inside a tag counts. Comments are prose, and so is the
    **Mixes** intro, which names the zones."""
    tags = re.compile(r"{{.*?}}|{%.*?%}", re.S)
    for template in ("settings.html", "settings_field.html"):
        source = re.sub(r"{#.*?#}", "", (TEMPLATES_DIR / template).read_text(encoding="utf-8"), flags=re.S)
        in_tags = " ".join(tags.findall(source))
        for key in (field.key for field in settings.FIELDS):
            assert not re.search(rf"\b{key}\b", source), f"{template} spells the setting {key}"
        for key in settings.MIX_FORM_FIELDS:
            spelled = rf"[\"']{key}[\"']|\.{key}\b"
            assert not re.search(spelled, source), f"{template} spells the Mix field {key}"
            assert not re.search(rf"\b{key}\b", in_tags), f"{template} spells the Mix field {key} in a tag"


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
    budget = ('name="api_calls_per_minute"', 'min="1"', 'max="45"', 'step="1"', 'value="35"')
    assert re.search(r"\s+".join(budget), page), "the API budget renders from its descriptor"
    assert str(chosen) in page
    assert 'action="/settings/library/download"' in page
    for name in ("explore", "refine", "duds only"):
        assert f'data-mix-row="{name}"' in page
    assert "data-mix-new" in page
    assert 'data-mix-delete="duds only"' in page
    assert 'data-mix-delete="explore"' not in page
    assert 'data-mix-delete="refine"' not in page
    assert 'data-mix-active="explore"' in page


def test_the_settings_page_counts_what_a_pool_reset_would_throw_away(web: Web) -> None:
    """The **Pool** and the live **Batch**'s **Draft Batch**, in words and in the confirm. Counting draws no
    **Batch**: the first look finds none on screen."""
    harness, client = web
    before = " ".join(client.get("/settings").text.split())
    batch = live(harness)
    for tile in batch.wallpapers[:2]:
        workflows.set_draft(harness.modules, batch.id, tile.id, Verdict.LIKE)

    after = " ".join(client.get("/settings").text.split())

    assert "The pool holds 24 wallpapers." in before
    assert "No batch is on screen." in before
    assert (
        "The batch on screen has 2 marks. A reset throws them away, as your next visit to the batches page"
        " would." in after
    )
    assert 'hx-post="/settings/pool/reset"' in after
    assert (
        'hx-confirm="Reset the pool? 24 wallpapers leave it, and the batch on screen goes with its 2 marks.'
        " Nothing is recorded." in after
    )
