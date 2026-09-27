"""The web layer: Jinja templates over an already-constructed Core service.

Endpoints are plain `def`, so FastAPI runs them in its threadpool and the synchronous core never blocks the
event loop (ADR 0001). The app is handed a Core service rather than building one, which is what lets the
smoke test inject the fake Wallhaven client.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
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
    MAX_POOL_TARGET_SIZE,
    MIN_BATCH_SIZE,
    MIN_POOL_TARGET_SIZE,
    WALLHAVEN_RATIOS,
    Batch,
    BatchUnavailable,
    CoreService,
    SettingsRefused,
    SubmissionRefused,
)
from wallpapi.model import Verdict
from wallpapi.refill import RefillThread

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
        context: dict[str, object] = {"status": core.refill_status()}
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
        ignored = sum(1 for entry in appended if entry.verdict is Verdict.IGNORE)
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
            {"batch": live, "wallpaper": marked, "draft": live.drafts.get(wallpaper_id)},
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

    def render_settings(
        request: Request,
        *,
        posted: Mapping[str, str],
        refused: SettingsRefused | None = None,
        saved: bool = False,
        status_code: int = HTTPStatus.OK,
    ) -> HTMLResponse:
        """The settings form, filled with the values given rather than with the values stored.

        A refused save re-renders with what the user typed, so correcting one field does not mean retyping
        the others. `posted` is every field as text, which is how a plain page load renders them too — the
        form fields are strings either way, and giving both paths one shape means a field added to the form
        is added in one place rather than three.
        """
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                **posted,
                "refused": refused,
                "saved": saved,
                "min_batch_size": MIN_BATCH_SIZE,
                "max_batch_size": MAX_BATCH_SIZE,
                "min_pool_target_size": MIN_POOL_TARGET_SIZE,
                "max_pool_target_size": MAX_POOL_TARGET_SIZE,
                "max_filter_pixels": MAX_FILTER_PIXELS,
                "wallhaven_ratios": ", ".join(sorted(WALLHAVEN_RATIOS)),
            },
            status_code=status_code,
        )

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, saved: bool = False) -> HTMLResponse:
        """The settings page, showing what is stored."""
        return render_settings(request, posted=_stored_fields(core), saved=saved)

    @app.post("/settings")
    def save_settings(
        request: Request,
        batch_size: Annotated[str | None, Form()] = None,
        library_path: Annotated[str | None, Form()] = None,
        pool_target_size: Annotated[str | None, Form()] = None,
        min_width: Annotated[str | None, Form()] = None,
        min_height: Annotated[str | None, Form()] = None,
        allowed_ratios: Annotated[str | None, Form()] = None,
        min_favourites: Annotated[str | None, Form()] = None,
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

    @app.get("/thumb/{wallpaper_id}")
    def thumbnail(wallpaper_id: str) -> FileResponse:
        """One tile, off the **Thumbnail cache**. Fetched from Wallhaven the first time and never again."""
        cached = core.get_thumbnail(wallpaper_id)
        if cached is None:
            raise HTTPException(status_code=404, detail="unknown wallpaper")
        return FileResponse(cached, media_type=guess_type(cached.name)[0] or "application/octet-stream")

    return app
