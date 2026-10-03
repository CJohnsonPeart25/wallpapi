"""The web layer: Jinja templates over an already-constructed Core service (ADR 0001)."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http import HTTPStatus
from mimetypes import guess_type
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.types import Lifespan

from wallpapi.core import Batch, BatchUnavailable, CoreService, HistoryRefused, SubmissionRefused
from wallpapi.model import Verdict
from wallpapi.refill import RefillThread
from wallpapi.settings import (
    FORM_FIELDS,
    MAX_MIX_NAME_LENGTH,
    MIX_TOTAL,
    MixListing,
    SettingsRefused,
    form_values,
)
from wallpapi.similarity_thread import SimilarityThread
from wallpapi.thumbnail_thread import ThumbnailThread


@dataclass(frozen=True, slots=True)
class _DownloadCounts:
    """What the last "download all **Favourites**" did, as the settings page renders it."""

    downloaded: bool
    written: int
    skipped: int
    failed: int


TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
"""Vendored htmx, stylesheet and preview script: no third-party asset in any template."""


def _drafted_verdict(posted: str) -> Verdict | None:
    """The **Verdict** a tile control posted, or `None` for a clear; **Ignore** is derived at submit and
    refused.
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
    """The **Verdict** a **History** control posted: required, **Ignore** included."""
    if not posted:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail="a verdict is required")
    try:
        return Verdict(posted)
    except ValueError:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail="unknown verdict") from None


def _history_filter(posted: str) -> Verdict | None:
    """The resolved **Verdict** the **History** listing is narrowed to, or `None`; **Ignore** is accepted."""
    if not posted:
        return None
    try:
        return Verdict(posted)
    except ValueError:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail="unknown verdict") from None


_HISTORY_REFUSAL_STATUS = {
    HistoryRefused.Reason.UNKNOWN_WALLPAPER: HTTPStatus.NOT_FOUND,
}
"""A **History** refusal is a status code: only a hand-made post can produce one, and htmx skips a 4xx."""

_FILTERABLE_VERDICTS = (Verdict.FAVOURITE, Verdict.LIKE, Verdict.BAN, Verdict.IGNORE)
"""The **History** filter's choices, written out so **Ignore**, the commonest, is last."""


def _lifespan(core: CoreService) -> Lifespan[FastAPI]:
    """Start the background threads with the app and join them on the way out; a test's Core service must
    not.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        del app

        refill = RefillThread(core)
        similarity = SimilarityThread(core)
        thumbnails = ThumbnailThread(core)
        refill.start()
        similarity.start()
        thumbnails.start()
        try:
            yield
        finally:
            thumbnails.stop()
            similarity.stop()
            refill.stop()

    return lifespan


async def _posted_settings(request: Request) -> dict[str, str]:
    """The settings form's fields as posted, by key; a field not posted, or posted empty, is left out, and so
    left alone. Empty as `Form()` treats it, which is how the page has always behaved: a field cleared and
    saved keeps its value.

    Read off the form by the table rather than one `Form()` parameter each, so the web layer spells no field
    name. Async only to read the body: the route that depends on it stays synchronous (ADR 0001).
    """
    form = await request.form()
    return {
        field.key: value
        for field in FORM_FIELDS
        if isinstance(value := form.get(field.key), str) and value != ""
    }


def _mix_context(core: CoreService) -> dict[str, object]:
    """What the switcher needs. The stored active name, so a deleted **Mix** still shows as chosen."""
    return {"mixes": core.list_mixes(), "active_mix": core.get_settings().active_mix}


def _mix_section(core: CoreService, posted: Mapping[str, str] | None = None) -> dict[str, object]:
    """The **Mixes** section of the settings page: a row per **Mix**, plus the add row. `posted` is a refused
    save put back.
    """
    typed = dict(posted or {})
    typed_name = typed.get("name", "").strip()
    stored = core.mix_listings()
    active = core.get_settings().active_mix

    def fields(listed: MixListing) -> dict[str, object]:
        mix = listed.mix
        edited = typed if typed_name == mix.name else {}
        return {
            "name": mix.name,
            "unknown": edited.get("unknown", str(mix.unknown)),
            "banger": edited.get("banger", str(mix.banger)),
            "dud": edited.get("dud", str(mix.dud)),
            "active": mix.name == active,
            "deletable": listed.deletable,
        }

    added = typed if typed_name not in {listed.mix.name for listed in stored} else {}
    return {
        "mix_rows": [fields(listed) for listed in stored],
        "new_mix": {field: added.get(field, "") for field in ("name", "unknown", "banger", "dud")},
        "mix_total": MIX_TOTAL,
        "max_mix_name_length": MAX_MIX_NAME_LENGTH,
    }


def create_app(core: CoreService, *, refill: bool = False) -> FastAPI:
    """The app over a Core service; `refill` starts the background threads and only `main.py` turns it on."""
    app = FastAPI(title="wallpapi", lifespan=_lifespan(core) if refill else None)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def render(request: Request, result: Batch | BatchUnavailable | SubmissionRefused) -> HTMLResponse:
        """One fragment and one status code per outcome."""
        if isinstance(result, SubmissionRefused):
            already = result.reason is SubmissionRefused.Reason.ALREADY_SUBMITTED
            return templates.TemplateResponse(
                request,
                "banner.html",
                {"refused": result},
                status_code=HTTPStatus.CONFLICT if already else HTTPStatus.NOT_FOUND,
            )
        context: dict[str, object] = {
            "status": core.refill_status(),
            "similarity_notice": core.similarity_notice(),
        }
        if isinstance(result, Batch):
            return templates.TemplateResponse(request, "batch_view.html", {**context, "batch": result})
        # 503 and never a 500: nothing failed, the **Pool** is empty and the refill may fix it.
        return templates.TemplateResponse(
            request,
            "batch_view.html",
            {**context, "unavailable": result},
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        )

    @app.get("/", response_class=HTMLResponse)
    def batch_page(request: Request) -> HTMLResponse:
        """The **Batch** page's shell, which fetches its **Batch** from `/batch`. It mints nothing."""
        return templates.TemplateResponse(
            request,
            "batch.html",
            {"batch_size": core.get_settings().batch_size, **_mix_context(core)},
        )

    @app.get("/batch", response_class=HTMLResponse)
    def batch_view(request: Request) -> HTMLResponse:
        """The live **Batch** as the fragment the shell swaps in."""
        return render(request, core.get_next_batch())

    @app.post("/submit", response_class=HTMLResponse)
    def submit(request: Request, batch_id: Annotated[str, Form()]) -> HTMLResponse:
        """Submit the **Batch**, answer with the banner, and tell the page to fetch the next one (a refusal
        too).
        """
        result = core.submit_batch(batch_id)
        if not isinstance(result, Batch):
            response = render(request, result)
        else:
            # Counted from the Decision log, not the form.
            appended = core.list_history(batch_id=batch_id)
            ignored = sum(1 for entry in appended if entry.entry is Verdict.IGNORE)
            response = templates.TemplateResponse(
                request, "banner.html", {"recorded": len(appended), "ignored": ignored}
            )
        response.headers["HX-Trigger"] = "batch-submitted"
        return response

    @app.post("/draft", response_class=HTMLResponse)
    def draft(
        request: Request,
        batch_id: Annotated[str, Form()],
        wallpaper_id: Annotated[str, Form()],
        verdict: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        """Mark one tile, or clear it, and swap that tile back."""
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
        """Mark the whole **Batch**, or clear it, and swap the grid back: one post, one transaction (ADR
        0002).
        """
        refused = core.set_all_draft_verdicts(batch_id, _drafted_verdict(verdict))
        if refused is not None:
            return render(request, refused)

        live = core.get_next_batch()
        if not isinstance(live, Batch):
            return render(request, live)
        return templates.TemplateResponse(request, "grid.html", {"batch": live})

    def render_history_row(request: Request, wallpaper_id: str) -> HTMLResponse:
        """The one row an edit changed, read back so the screen shows what the **Decision log** resolves
        to.
        """
        row = core.get_history_row(wallpaper_id)
        if row is None:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="unknown wallpaper")
        return templates.TemplateResponse(request, "history_row.html", {"row": row})

    def refuse_history(refused: HistoryRefused) -> HTTPException:
        return HTTPException(status_code=_HISTORY_REFUSAL_STATUS[refused.reason], detail=refused.reason.value)

    @app.get("/history", response_class=HTMLResponse)
    def history_page(request: Request, verdict: str = "", page: int = 1) -> HTMLResponse:
        """**History**, filtered by resolved **Verdict** and paged; a page out of range is clamped."""
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

    @app.post("/mix", response_class=HTMLResponse)
    def choose_mix(request: Request, mix: Annotated[str, Form()]) -> HTMLResponse:
        """Switch the active **Mix**. The live **Batch** is untouched, so its **Draft Batch** survives; an
        unknown **Mix** is a 400.
        """
        refused = core.update_settings(active_mix=mix)
        status_code = HTTPStatus.BAD_REQUEST if isinstance(refused, SettingsRefused) else HTTPStatus.OK

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
        """The settings form, filled with the values given rather than those stored, so a refused save keeps
        what was typed.
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
                "fields": {field.key: field for field in FORM_FIELDS},
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
        """The settings page; the download counts are query parameters because it is the download's redirect
        target.
        """
        return render_settings(
            request,
            posted=form_values(core.get_settings()),
            saved=saved,
            deleted=deleted,
            download=_DownloadCounts(downloaded, written, skipped, failed),
        )

    @app.post("/settings")
    def save_settings(
        request: Request, posted: Annotated[dict[str, str], Depends(_posted_settings)]
    ) -> Response:
        """Save the settings, or come back with the reason. Fields are `str` and coerced by the settings
        module; one not posted is left alone.
        """
        result = core.update_settings(**posted)
        if isinstance(result, SettingsRefused):
            # What was typed, over what is stored.
            return render_settings(
                request,
                posted={**form_values(core.get_settings()), **posted},
                refused=result,
                status_code=HTTPStatus.BAD_REQUEST,
            )
        return RedirectResponse("/settings?saved=1", status_code=HTTPStatus.SEE_OTHER)

    @app.post("/settings/library/download")
    def download_favourites() -> Response:
        """Pull every **Favourite** whose **Library** file is missing, redirecting with the counts. It never
        deletes.
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
        """Create a **Mix** or edit one: one route, as the add row and every edit row post the same fields."""
        result = core.save_mix(name, unknown=unknown, banger=banger, dud=dud)
        if isinstance(result, SettingsRefused):
            return render_settings(
                request,
                posted=form_values(core.get_settings()),
                posted_mix={"name": name, "unknown": unknown, "banger": banger, "dud": dud},
                refused=result,
                status_code=HTTPStatus.BAD_REQUEST,
            )
        return RedirectResponse("/settings?saved=1", status_code=HTTPStatus.SEE_OTHER)

    @app.post("/settings/mixes/delete")
    def delete_mix(request: Request, name: Annotated[str, Form()] = "") -> Response:
        """Remove a **Mix**, or re-render with the reason it stays."""
        refused = core.delete_mix(name)
        if refused is not None:
            return render_settings(
                request,
                posted=form_values(core.get_settings()),
                refused=refused,
                status_code=HTTPStatus.BAD_REQUEST,
            )
        return RedirectResponse("/settings?deleted=1", status_code=HTTPStatus.SEE_OTHER)

    @app.get("/thumb/{wallpaper_id}")
    def thumbnail(wallpaper_id: str) -> FileResponse:
        """One tile, off the **Thumbnail cache**."""
        cached = core.get_thumbnail(wallpaper_id)
        if cached is None:
            raise HTTPException(status_code=404, detail="unknown wallpaper")
        return FileResponse(cached, media_type=guess_type(cached.name)[0] or "application/octet-stream")

    return app
