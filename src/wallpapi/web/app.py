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
from fastapi.templating import Jinja2Templates

from wallpapi.core import Batch, BatchUnavailable, CoreService, SubmissionRefused

TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(core: CoreService) -> FastAPI:
    app = FastAPI(title="wallpapi")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def render(
        request: Request,
        result: Batch | BatchUnavailable | SubmissionRefused,
        *,
        recorded: int | None = None,
    ) -> HTMLResponse:
        """One template and one status code per outcome, so both routes answer the same way."""
        if isinstance(result, Batch):
            return templates.TemplateResponse(request, "batch.html", {"batch": result, "recorded": recorded})
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
        return render(request, result, recorded=len(core.list_history(batch_id=batch_id)))

    @app.get("/thumb/{wallpaper_id}")
    def thumbnail(wallpaper_id: str) -> FileResponse:
        """One tile, off the **Thumbnail cache**. Fetched from Wallhaven the first time and never again."""
        cached = core.get_thumbnail(wallpaper_id)
        if cached is None:
            raise HTTPException(status_code=404, detail="unknown wallpaper")
        return FileResponse(cached, media_type=guess_type(cached.name)[0] or "application/octet-stream")

    return app
