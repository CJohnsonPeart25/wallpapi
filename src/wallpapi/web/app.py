"""The web layer: Jinja templates over an already-constructed Core service.

Endpoints are plain `def`, so FastAPI runs them in its threadpool and the synchronous core never blocks the
event loop (ADR 0001). The app is handed a Core service rather than building one, which is what lets the
smoke test inject the fake Wallhaven client.
"""

from __future__ import annotations

from http import HTTPStatus
from mimetypes import guess_type
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from wallpapi.core import Batch, BatchUnavailable, CoreService, SubmissionRefused
from wallpapi.model import Verdict

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
"""Vendored htmx, served by the app. No CDN reference anywhere in the templates."""


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


def create_app(core: CoreService) -> FastAPI:
    app = FastAPI(title="wallpapi")
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def render(
        request: Request,
        result: Batch | BatchUnavailable | SubmissionRefused,
        *,
        recorded: int | None = None,
        ignored: int | None = None,
    ) -> HTMLResponse:
        """One template and one status code per outcome, so every route answers the same way."""
        if isinstance(result, Batch):
            return templates.TemplateResponse(
                request, "batch.html", {"batch": result, "recorded": recorded, "ignored": ignored}
            )
        if isinstance(result, SubmissionRefused):
            already = result.reason is SubmissionRefused.Reason.ALREADY_SUBMITTED
            return templates.TemplateResponse(
                request,
                "batch.html",
                {"refused": result},
                status_code=HTTPStatus.CONFLICT if already else HTTPStatus.NOT_FOUND,
            )
        return templates.TemplateResponse(
            request, "batch.html", {"unavailable": result}, status_code=HTTPStatus.SERVICE_UNAVAILABLE
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

    @app.get("/thumb/{wallpaper_id}")
    def thumbnail(wallpaper_id: str) -> FileResponse:
        """One tile, off the **Thumbnail cache**. Fetched from Wallhaven the first time and never again."""
        cached = core.get_thumbnail(wallpaper_id)
        if cached is None:
            raise HTTPException(status_code=404, detail="unknown wallpaper")
        return FileResponse(cached, media_type=guess_type(cached.name)[0] or "application/octet-stream")

    return app
