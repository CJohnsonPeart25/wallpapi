"""The web layer: Jinja templates over an already-constructed Core service.

Endpoints are plain `def`, so FastAPI runs them in its threadpool and the synchronous core never blocks the
event loop (ADR 0001). The app is handed a Core service rather than building one, which is what lets the
smoke test inject the fake Wallhaven client.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from wallpapi.core import Batch, CoreService

TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(core: CoreService) -> FastAPI:
    app = FastAPI(title="wallpapi")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    @app.get("/", response_class=HTMLResponse)
    def batch_page(request: Request) -> HTMLResponse:
        """The **Batch** page. Tiles are served from the **Thumbnail cache**, never hotlinked."""
        result = core.get_next_batch()
        batch = result if isinstance(result, Batch) else None
        return templates.TemplateResponse(
            request,
            "batch.html",
            {"batch": batch, "unavailable": None if batch else result},
        )

    return app
