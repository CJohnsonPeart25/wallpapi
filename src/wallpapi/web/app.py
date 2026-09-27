"""The web layer: Jinja templates over an already-constructed Core service.

Endpoints are plain `def`, so FastAPI runs them in its threadpool and the synchronous core never blocks the
event loop (ADR 0001). The app is handed a Core service rather than building one, which is what lets the
smoke test inject the fake Wallhaven client.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http import HTTPStatus
from mimetypes import guess_type
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.types import Lifespan

from wallpapi.core import (
    MAX_BATCH_SIZE,
    MAX_FILTER_PIXELS,
    MAX_MIX_NAME_LENGTH,
    MAX_POOL_TARGET_SIZE,
    MAX_SIMILARITY_DECAY,
    MIN_BATCH_SIZE,
    MIN_POOL_TARGET_SIZE,
    MIX_TOTAL,
    UNDELETABLE_MIXES,
    WALLHAVEN_RATIOS,
    Batch,
    BatchUnavailable,
    CoreService,
    HistoryRefused,
    SettingsRefused,
    SubmissionRefused,
)
from wallpapi.model import Mix, Verdict
from wallpapi.refill import RefillThread


@dataclass(frozen=True, slots=True)
class _DownloadCounts:
    """What the last "download all **Favourites**" did, as the settings page renders it (#14).

    A value rather than three loose template variables, so the template can ask whether there is anything
    to say at all — `downloaded` is what tells a page load after a download from a plain page load, and
    three zeroes are a real answer rather than an absent one.
    """

    downloaded: bool
    written: int
    skipped: int
    failed: int


TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
"""Vendored htmx, stylesheet and preview script, served by the app. No third-party asset in any template."""


def _drafted_verdict(posted: str) -> Verdict | None:
    """The **Verdict** a tile control posted, or `None` where it posted a clear.

    **Ignore** is refused rather than accepted: it is derived for everything unmarked at submit, so a
    stored one would be a second way to say what an absent row already says, and **Verdict resolution**
    would have two shapes of nothing to tell apart. No control posts it — this guards a hand-made post.
    """
    if not posted:
        return None
    try:
        chosen = Verdict(posted)
    except ValueError:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail="unknown verdict") from None
    if chosen is Verdict.IGNORE:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail="an ignore is derived, not drafted")
    return chosen


def _chosen_verdict(posted: str) -> Verdict:
    """The **Verdict** a **History** control posted. Required, and never an **Ignore**.

    The tile version has a "no mark" to post; **History** has a clear, which is its own route because a
    **Clearance** is an entry rather than the absence of one. So an empty **Verdict** here is a malformed
    post rather than a meaning, and it is refused with the same 400 an unknown one gets.
    """
    chosen = _drafted_verdict(posted)
    if chosen is None:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail="a verdict is required")
    return chosen


def _history_filter(posted: str) -> Verdict | None:
    """The resolved **Verdict** the **History** listing is narrowed to, or `None` for all of them.

    **Ignore** *is* accepted here, unlike everywhere a **Verdict** is chosen: it is a resolved value like
    any other, and "show me only what I scrolled past" is the filter the page most needs.
    """
    if not posted:
        return None
    try:
        return Verdict(posted)
    except ValueError:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail="unknown verdict") from None


_HISTORY_REFUSAL_STATUS = {
    HistoryRefused.Reason.UNKNOWN_WALLPAPER: HTTPStatus.NOT_FOUND,
    HistoryRefused.Reason.IGNORE_NOT_CHOOSABLE: HTTPStatus.BAD_REQUEST,
    HistoryRefused.Reason.NOTHING_TO_CLEAR: HTTPStatus.CONFLICT,
}
"""What each **History** refusal is over HTTP.

Every one of them is a hand-made post or a second tab — the page renders no control that can produce one,
and no clear control at all on a row with nothing to clear. So they are status codes rather than a rendered
branch: htmx does not swap a 4xx, which is exactly right when the answer is "that row did not change".
"""

_FILTERABLE_VERDICTS = (Verdict.FAVOURITE, Verdict.LIKE, Verdict.BAN, Verdict.IGNORE)
"""The **History** filter's choices, strongest positive to the thing that is barely a judgement at all.

Written out rather than `tuple(Verdict)`, which would put **Ignore** in the middle: **Ignore** is last
because it is the overwhelming majority of the rows and the least interesting of them.
"""


def _lifespan(core: CoreService) -> Lifespan[FastAPI]:
    """Start the **Pool** refill with the app and join it on the way out.

    The thread is started here rather than in the Core service because the Core service is also what a
    test constructs, and constructing one must not start a thread that talks to Wallhaven.

    Joined with a timeout greater than the client's request timeout (invariant 12), so a stop arriving mid
    request still lands: the request cannot outlast its own timeout, and every wait inside the loop is a
    `stop_event.wait`.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        del app
        thread = RefillThread(core)
        thread.start()
        try:
            yield
        finally:
            thread.stop()

    return lifespan


def _stored_fields(core: CoreService) -> dict[str, str]:
    """Every settings form field as text, read from the Core service.

    One place that knows the form's field names, so adding a setting is a field on `Settings`, a keyword on
    `update_settings`, an entry here and an input in the template — and nothing else.
    """
    current = core.get_settings()
    return {
        "batch_size": str(current.batch_size),
        "library_path": str(current.library_path),
        "pool_target_size": str(current.pool_target_size),
        "min_width": str(current.min_width),
        "min_height": str(current.min_height),
        "allowed_ratios": current.ratios,
        "min_favourites": str(current.min_favourites),
        "similarity_radius": str(current.similarity_radius),
        "similarity_decay": str(current.similarity_decay),
        "thumbnail_cache_max_mb": str(current.thumbnail_cache_max_mb),
        "revisit_weight": str(current.revisit_weight),
    }


def _mix_context(core: CoreService) -> dict[str, object]:
    """What the switcher needs: every **Mix** there is, and which one the next **Batch** will use.

    One function, because the switcher is rendered from two places — inside the **Batch** page and on its
    own as the htmx swap — and the two must not be able to disagree about what "active" means.

    `active_mix` is the stored *name* rather than `core.active_mix().name`, so that a name whose **Mix**
    has been deleted still shows as the thing the user chose. The draw falls back; the page should not
    quietly claim the fallback was the choice.
    """
    return {"mixes": core.list_mixes(), "active_mix": core.get_settings().active_mix}


def _mix_section(core: CoreService, posted: Mapping[str, str] | None = None) -> dict[str, object]:
    """The **Mixes** section of the settings page: a row per **Mix**, plus the row that adds one.

    Built here rather than in the template because three facts have to be combined per row — what is
    stored, what the user has just typed, and whether the row may be deleted — and a template deciding
    any of them would be a second place that knows the rules.

    `posted` is the save that was refused, put back over the row it came from so that fixing one number
    is not retyping three. A posted name that matches no stored **Mix** belongs to the add row, which is
    where it was typed; that includes the empty name, which is the only way `MIX_NAME_INVALID` is
    reached.

    A row is deletable when it is neither **Explore** nor **Refine** (`UNDELETABLE_MIXES`) nor the active
    **Mix**. Both are refusals the Core service makes anyway; not rendering the control is the page
    declining to offer a button that cannot work, the same way **History** renders no clear control on a
    row with nothing to clear.
    """
    typed = dict(posted or {})
    typed_name = typed.get("name", "").strip()
    stored = core.list_mixes()
    active = core.get_settings().active_mix

    def fields(mix: Mix) -> dict[str, object]:
        edited = typed if typed_name == mix.name else {}
        return {
            "name": mix.name,
            "unknown": edited.get("unknown", str(mix.unknown)),
            "banger": edited.get("banger", str(mix.banger)),
            "dud": edited.get("dud", str(mix.dud)),
            "active": mix.name == active,
            "deletable": mix.name not in UNDELETABLE_MIXES and mix.name != active,
        }

    added = typed if typed_name not in {mix.name for mix in stored} else {}
    return {
        "mix_rows": [fields(mix) for mix in stored],
        "new_mix": {field: added.get(field, "") for field in ("name", "unknown", "banger", "dud")},
        "mix_total": MIX_TOTAL,
        "max_mix_name_length": MAX_MIX_NAME_LENGTH,
    }


def create_app(core: CoreService, *, refill: bool = False) -> FastAPI:
    """The app over an already-constructed Core service.

    `refill` starts the background **Pool** refill in the lifespan and joins it on shutdown. Off by
    default, and `main.py` is the one caller that turns it on: a test that started a thread would be a test
    with a race in it, and every behaviour test here drives `refill_step` by hand instead. The one test
    that does start it is `test_refill_thread.py`, and what it checks is the thread itself.
    """
    app = FastAPI(title="wallpapi", lifespan=_lifespan(core) if refill else None)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def render(
        request: Request,
        result: Batch | BatchUnavailable | SubmissionRefused,
        *,
        recorded: int | None = None,
        ignored: int | None = None,
    ) -> HTMLResponse:
        """One template and one status code per outcome, so every route answers the same way.

        Every outcome carries the refill status, because the indicator is on the page in all of them —
        most of all on the one that says there is nothing to show, where what the refill is doing is the
        answer to "why".
        """
        context: dict[str, object] = {"status": core.refill_status(), **_mix_context(core)}
        if isinstance(result, Batch):
            return templates.TemplateResponse(
                request,
                "batch.html",
                {**context, "batch": result, "recorded": recorded, "ignored": ignored},
            )
        if isinstance(result, SubmissionRefused):
            already = result.reason is SubmissionRefused.Reason.ALREADY_SUBMITTED
            return templates.TemplateResponse(
                request,
                "batch.html",
                {**context, "refused": result},
                status_code=HTTPStatus.CONFLICT if already else HTTPStatus.NOT_FOUND,
            )
        # 503 and never a 500 (#15). Nothing here failed: the **Pool** is empty, which is a state the page
        # can explain and which the refill may well fix by itself.
        return templates.TemplateResponse(
            request,
            "batch.html",
            {**context, "unavailable": result},
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        )

    @app.get("/", response_class=HTMLResponse)
    def batch_page(request: Request) -> HTMLResponse:
        """The **Batch** page. Tiles are served from the **Thumbnail cache**, never hotlinked."""
        return render(request, core.get_next_batch())

    @app.post("/submit", response_class=HTMLResponse)
    def submit(request: Request, batch_id: Annotated[str, Form()]) -> HTMLResponse:
        """Submit the **Batch** and render the next one.

        A plain form post: #2 has no **Draft Batch**, so there is nothing for htmx to swap in and out yet.
        No post-redirect-get either — a refresh that resubmits is refused outright rather than silently
        absorbed, which is the better answer to the two-tabs case.
        """
        result = core.submit_batch(batch_id)
        if not isinstance(result, Batch):
            return render(request, result)
        # Counted from the Decision log, not from the form: the page reports what was appended rather
        # than what the browser claimed to be showing.
        appended = core.list_history(batch_id=batch_id)
        ignored = sum(1 for entry in appended if entry.entry is Verdict.IGNORE)
        return render(request, result, recorded=len(appended), ignored=ignored)

    @app.post("/draft", response_class=HTMLResponse)
    def draft(
        request: Request,
        batch_id: Annotated[str, Form()],
        wallpaper_id: Annotated[str, Form()],
        verdict: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        """Mark one tile, or clear it, and swap that tile back.

        The response is the tile alone rather than the page: a mark changes one tile and nothing else, and
        re-rendering the grid would fight with any other tile the user has clicked since. `hx-sync` on the
        control is what stops a late response swapping a stale tile back in.
        """
        refused = core.set_draft_verdict(batch_id, wallpaper_id, _drafted_verdict(verdict))
        if refused is not None:
            return render(request, refused)

        live = core.get_next_batch()
        if not isinstance(live, Batch):
            return render(request, live)
        marked = next((w for w in live.wallpapers if w.id == wallpaper_id), None)
        if marked is None:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="unknown wallpaper")
        return templates.TemplateResponse(
            request,
            "tile.html",
            {
                "batch": live,
                "wallpaper": marked,
                "draft": live.drafts.get(wallpaper_id),
                "zone": live.zones.get(wallpaper_id),
            },
        )

    @app.post("/draft/all", response_class=HTMLResponse)
    def draft_all(
        request: Request,
        batch_id: Annotated[str, Form()],
        verdict: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        """Mark the whole **Batch**, or clear it, and swap the whole grid back.

        One post and one transaction for one click, never one per tile (invariant 6). The response is the
        grid rather than the tile, because a bulk mark changes every tile — the **Draft Batch** is read
        back after the write, so what the user is left looking at is what the server actually holds.

        The grid and the tiles disable each other's controls while a request is in flight
        (`hx-disabled-elt`), so a bulk post and a single-tile post can never overlap. Without that, a tile
        post committing after this route read the **Draft Batch** would leave the swapped-in grid showing
        a mark the server no longer holds — the server side is atomic, but the screen would not be.

        An **Ignore** is refused here exactly as it is for a single mark: it is derived at submit, and a
        bulk one would write an explicit nothing against every tile at once.
        """
        refused = core.set_all_draft_verdicts(batch_id, _drafted_verdict(verdict))
        if refused is not None:
            return render(request, refused)

        live = core.get_next_batch()
        if not isinstance(live, Batch):
            return render(request, live)
        return templates.TemplateResponse(request, "grid.html", {"batch": live})

    def render_history_row(request: Request, wallpaper_id: str) -> HTMLResponse:
        """The one row an edit or a **Clearance** changed, swapped back into the listing.

        The row and not the page, for the reason a tile is swapped rather than the grid: an edit changes
        one **Wallpaper**, and re-rendering a hundred rows would fight with anything else the user has
        clicked since. Read back from the Core service rather than assumed, so what is on screen is what
        the **Decision log** now resolves to.

        A row whose **Wallpaper** has entries always exists — nothing here can delete one — so `None` is a
        **Wallpaper** the database has never seen, which the refusals above have already ruled out.
        """
        row = core.get_history_row(wallpaper_id)
        if row is None:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="unknown wallpaper")
        return templates.TemplateResponse(request, "history_row.html", {"row": row})

    def refuse_history(refused: HistoryRefused) -> HTTPException:
        return HTTPException(status_code=_HISTORY_REFUSAL_STATUS[refused.reason], detail=refused.reason.value)

    @app.get("/history", response_class=HTMLResponse)
    def history_page(request: Request, verdict: str = "", page: int = 1) -> HTMLResponse:
        """**History**: one row per **Wallpaper** ever judged, newest activity first.

        A view over the **Decision log**, not a second store. Filtered by resolved **Verdict** and paged,
        because a week of ordinary use is thousands of **Ignores** and all of them on one page is not a
        page. The Core service clamps a page number out of range rather than refusing it, so a stale link
        lands on the last page.
        """
        listing = core.list_history_rows(verdict=_history_filter(verdict), page=page)
        return templates.TemplateResponse(
            request, "history.html", {"history": listing, "verdicts": _FILTERABLE_VERDICTS}
        )

    @app.post("/history/verdict", response_class=HTMLResponse)
    def history_verdict(
        request: Request,
        wallpaper_id: Annotated[str, Form()],
        verdict: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        """Change a **Wallpaper**'s **Verdict** from **History**, and swap its row back."""
        refused = core.edit_verdict(wallpaper_id, _chosen_verdict(verdict))
        if refused is not None:
            raise refuse_history(refused)
        return render_history_row(request, wallpaper_id)

    @app.post("/history/clear", response_class=HTMLResponse)
    def history_clear(request: Request, wallpaper_id: Annotated[str, Form()]) -> HTMLResponse:
        """Withdraw a **Wallpaper**'s **Explicit Verdict**, and swap its row back.

        Its own route rather than `/history/verdict` with an empty **Verdict**: a **Clearance** is an entry
        of its own, and posting "no verdict" to say so would be the one spelling that means two things.
        """
        refused = core.clear_verdict(wallpaper_id)
        if refused is not None:
            raise refuse_history(refused)
        return render_history_row(request, wallpaper_id)

    @app.post("/mix", response_class=HTMLResponse)
    def choose_mix(request: Request, mix: Annotated[str, Form()]) -> HTMLResponse:
        """Switch the active **Mix**, and swap the switcher back.

        **The live Batch is deliberately untouched.** The **Mix** is read when a **Batch** is minted, so
        switching applies to the next one — the same rule the batch size has had since #4. Rerolling the
        grid here would discard a **Draft Batch** the user is part way through, which is a far worse
        surprise than a **Batch** finishing under the **Mix** it started in. That is why the response is
        this section alone and not the page.

        A **Mix** nobody has heard of is a 400 with the switcher unchanged, not a stored name the draw
        would then have to make sense of. No control can post one — this answers a hand-made post, and
        the same 400 is what a **Mix** deleted in another tab at #12 would give.
        """
        refused = core.update_settings(active_mix=mix)
        status_code = HTTPStatus.BAD_REQUEST if isinstance(refused, SettingsRefused) else HTTPStatus.OK
        # Read back rather than assumed, so what swaps in is what the Core service actually holds — which
        # is the whole point on the branch where the switch was refused.
        return templates.TemplateResponse(request, "mix.html", _mix_context(core), status_code=status_code)

    def render_settings(
        request: Request,
        *,
        posted: Mapping[str, str],
        posted_mix: Mapping[str, str] | None = None,
        refused: SettingsRefused | None = None,
        saved: bool = False,
        deleted: bool = False,
        download: _DownloadCounts | None = None,
        status_code: int = HTTPStatus.OK,
    ) -> HTMLResponse:
        """The settings form, filled with the values given rather than with the values stored.

        A refused save re-renders with what the user typed, so correcting one field does not mean retyping
        the others. `posted` is every field as text, which is how a plain page load renders them too — the
        form fields are strings either way, and giving both paths one shape means a field added to the form
        is added in one place rather than three.

        `posted_mix` is the same idea for the **Mixes** section, kept apart from `posted` because those
        rows are forms of their own: a **Mix** row posts four fields under names every other **Mix** row
        posts too, so they cannot be spread into one flat context the way the settings fields are.
        """
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                **posted,
                **_mix_section(core, posted_mix),
                "refused": refused,
                "saved": saved,
                "deleted": deleted,
                "download": download,
                "min_batch_size": MIN_BATCH_SIZE,
                "max_batch_size": MAX_BATCH_SIZE,
                "min_pool_target_size": MIN_POOL_TARGET_SIZE,
                "max_pool_target_size": MAX_POOL_TARGET_SIZE,
                "max_filter_pixels": MAX_FILTER_PIXELS,
                "max_similarity_decay": MAX_SIMILARITY_DECAY,
                "wallhaven_ratios": ", ".join(sorted(WALLHAVEN_RATIOS)),
            },
            status_code=status_code,
        )

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(
        request: Request,
        saved: bool = False,
        deleted: bool = False,
        downloaded: bool = False,
        written: int = 0,
        skipped: int = 0,
        failed: int = 0,
    ) -> HTMLResponse:
        """The settings page, showing what is stored.

        The three counts arrive as query parameters rather than being worked out here, because the page
        that renders them is the *redirect target* of the download (#14) and not the download itself. They
        are declared `int`, unlike every settings field: these are wallpapi's own numbers coming back
        round a `303`, not anything the user typed, so FastAPI's 422 on a hand-edited URL is the right
        answer rather than the wrong kind of error page.
        """
        return render_settings(
            request,
            posted=_stored_fields(core),
            saved=saved,
            deleted=deleted,
            download=_DownloadCounts(downloaded, written, skipped, failed),
        )

    @app.post("/settings")
    def save_settings(
        request: Request,
        batch_size: Annotated[str | None, Form()] = None,
        thumbnail_cache_max_mb: Annotated[str | None, Form()] = None,
        library_path: Annotated[str | None, Form()] = None,
        pool_target_size: Annotated[str | None, Form()] = None,
        min_width: Annotated[str | None, Form()] = None,
        min_height: Annotated[str | None, Form()] = None,
        allowed_ratios: Annotated[str | None, Form()] = None,
        min_favourites: Annotated[str | None, Form()] = None,
        similarity_radius: Annotated[str | None, Form()] = None,
        similarity_decay: Annotated[str | None, Form()] = None,
        revisit_weight: Annotated[str | None, Form()] = None,
    ) -> Response:
        """Save the settings, or come back with the reason they were refused.

        Every field is taken as `str` and coerced by the Core service, not typed here. A declared `int`
        would make FastAPI answer a typo with its own JSON 422 — a wall of text in the browser, and a
        second validation rule living somewhere the tests for the first one cannot see it.

        A field the post did not carry is left alone, which is ADR 0004's calling convention arriving at
        the edge. The form always posts all of them; this is what keeps a partial post — a script, a
        half-rendered page — from resetting the fields it never knew about. An *empty* field is not that:
        it is a value, and the Core service refuses the ones that are not settings.

        Post-redirect-get on success, which is the opposite of `/submit`. Resubmitting a **Batch** is
        refused loudly because it would append to the **Decision log** twice; re-saving settings writes the
        same values, so a refresh may as well be a page load.
        """
        posted = {
            name: value
            for name, value in (
                ("batch_size", batch_size),
                ("library_path", library_path),
                ("pool_target_size", pool_target_size),
                ("min_width", min_width),
                ("min_height", min_height),
                ("allowed_ratios", allowed_ratios),
                ("min_favourites", min_favourites),
                ("similarity_radius", similarity_radius),
                ("similarity_decay", similarity_decay),
                ("thumbnail_cache_max_mb", thumbnail_cache_max_mb),
                ("revisit_weight", revisit_weight),
            )
            if value is not None
        }
        result = core.update_settings(**posted)
        if isinstance(result, SettingsRefused):
            # What was typed, over what is stored: correcting one field must not mean retyping the others,
            # and a field this post never carried has to render as the value it still has.
            return render_settings(
                request,
                posted={**_stored_fields(core), **posted},
                refused=result,
                status_code=HTTPStatus.BAD_REQUEST,
            )
        return RedirectResponse("/settings?saved=1", status_code=HTTPStatus.SEE_OTHER)

    @app.post("/settings/library/download")
    def download_favourites() -> Response:
        """Pull every **Favourite** whose **Library** file is missing, and come back saying how many (#14).

        The **Library** is derived from the **Decision log** and is otherwise only written when a
        **Batch** is submitted; this is the button that says "make the folder match the log *now*", for a
        **Library** deleted in Explorer or a database carried to another machine. One-way: it writes and
        it skips, and there is no path through it that deletes anything.

        Post-redirect-get with the counts in the query string, as `/settings` already does with `saved`.
        The operation is idempotent — a repost would download nothing a second time — but it is still a
        folder's worth of network calls, and a refresh should not be one. The counts travel round the
        redirect rather than being rendered from this response, so the page the user lands on is the
        ordinary settings page and the back button behaves.

        No refusal branch, because there is nothing here to refuse: a **Favourite** whose download failed
        is counted and is picked up by the next press, and one whose name or destination the guard turns
        down is counted the same way. A raise would be the wrong answer to either.
        """
        pulled = core.download_favourites()
        return RedirectResponse(
            "/settings?downloaded=1"
            f"&written={len(pulled.written)}"
            f"&skipped={len(pulled.skipped)}"
            f"&failed={len(pulled.failed)}",
            status_code=HTTPStatus.SEE_OTHER,
        )

    @app.post("/settings/mixes")
    def save_mix(
        request: Request,
        name: Annotated[str, Form()] = "",
        unknown: Annotated[str, Form()] = "",
        banger: Annotated[str, Form()] = "",
        dud: Annotated[str, Form()] = "",
    ) -> Response:
        """Create a **Mix** or edit one, and come back with the reason if those are not percentages.

        One route for both, because `save_mix` is one write: the add row and every edit row post the same
        four fields, and which of the two happened is a question about the table rather than about what
        the user asked for.

        Plain form posts rather than htmx, like the rest of this page — a **Mix** row changes the
        switcher on another page and the list this section renders, so there is no fragment worth
        swapping. Post-redirect-get on success for the reason `/settings` redirects: saving the same
        **Mix** twice is the same **Mix**.

        Every field is `str` with a default of empty, and the Core service decides. A declared `int` would
        answer a typo with FastAPI's own JSON 422 instead of the sentence this page renders, and an empty
        percentage — the add row submitted with a name and nothing else — would be a 422 rather than
        `MIX_PERCENTAGES_INVALID`.
        """
        result = core.save_mix(name, unknown=unknown, banger=banger, dud=dud)
        if isinstance(result, SettingsRefused):
            return render_settings(
                request,
                posted=_stored_fields(core),
                posted_mix={"name": name, "unknown": unknown, "banger": banger, "dud": dud},
                refused=result,
                status_code=HTTPStatus.BAD_REQUEST,
            )
        return RedirectResponse("/settings?saved=1", status_code=HTTPStatus.SEE_OTHER)

    @app.post("/settings/mixes/delete")
    def delete_mix(request: Request, name: Annotated[str, Form()] = "") -> Response:
        """Remove a **Mix**, or re-render with the reason it stays.

        The page renders no delete control on **Explore**, on **Refine** or on the active **Mix**, so
        every refusal here is a second tab or a hand-made post — but it is a rendered sentence rather
        than a bare status code, because this is a full page load and the user is looking at the section
        the answer belongs in.
        """
        refused = core.delete_mix(name)
        if refused is not None:
            return render_settings(
                request,
                posted=_stored_fields(core),
                refused=refused,
                status_code=HTTPStatus.BAD_REQUEST,
            )
        return RedirectResponse("/settings?deleted=1", status_code=HTTPStatus.SEE_OTHER)

    @app.get("/thumb/{wallpaper_id}")
    def thumbnail(wallpaper_id: str) -> FileResponse:
        """One tile, off the **Thumbnail cache**. Fetched from Wallhaven the first time and never again."""
        cached = core.get_thumbnail(wallpaper_id)
        if cached is None:
            raise HTTPException(status_code=404, detail="unknown wallpaper")
        return FileResponse(cached, media_type=guess_type(cached.name)[0] or "application/octet-stream")

    return app
