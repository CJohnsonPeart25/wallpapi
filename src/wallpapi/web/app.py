"""The web layer: Jinja templates over an already-constructed Core service.

Endpoints are plain `def`, so FastAPI runs them in its threadpool and the synchronous core never blocks the
event loop (ADR 0001).
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
from wallpapi.similarity_thread import SimilarityThread
from wallpapi.thumbnail_thread import ThumbnailThread


@dataclass(frozen=True, slots=True)
class _DownloadCounts:
    """What the last "download all **Favourites**" did, as the settings page renders it. `downloaded` tells a
    page load after a download from a plain one.
    """

    downloaded: bool
    written: int
    skipped: int
    failed: int


TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
"""Vendored htmx, stylesheet and preview script. No third-party asset in any template."""


def _drafted_verdict(posted: str) -> Verdict | None:
    """The **Verdict** a tile control posted, or `None` for a clear. **Ignore** is refused: it is derived at
    submit, and no control posts it.
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
    """The **Verdict** a **History** control posted. Required, and **Ignore** included: a **History** edit is
    appended at once, so withdrawing a **Verdict** posts the **Ignore** itself. An empty **Verdict** is
    refused.
    """
    if not posted:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail="a verdict is required")
    try:
        return Verdict(posted)
    except ValueError:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail="unknown verdict") from None


def _history_filter(posted: str) -> Verdict | None:
    """The resolved **Verdict** the **History** listing is narrowed to, or `None`. **Ignore** is accepted
    here, as a resolved value.
    """
    if not posted:
        return None
    try:
        return Verdict(posted)
    except ValueError:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail="unknown verdict") from None


_HISTORY_REFUSAL_STATUS = {
    HistoryRefused.Reason.UNKNOWN_WALLPAPER: HTTPStatus.NOT_FOUND,
}
"""What each **History** refusal is over HTTP: a status code, since only a hand-made post can produce one and
htmx does not swap a 4xx.
"""

_FILTERABLE_VERDICTS = (Verdict.FAVOURITE, Verdict.LIKE, Verdict.BAN, Verdict.IGNORE)
"""The **History** filter's choices, written out so **Ignore**, the commonest and least interesting, is
last.
"""


def _lifespan(core: CoreService) -> Lifespan[FastAPI]:
    """Start the three background threads with the app and join them on the way out.

    Started here, not in the Core service, because a test constructs that and must not start a thread that
    talks to Wallhaven. Joined with a timeout above the longest read they can be inside (invariant 12).
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


def _stored_fields(core: CoreService) -> dict[str, str]:
    """Every settings form field as text, read from the Core service.

    Adding a setting takes six edits: its key, `Settings` field and refusal reason in `core.py`; its read in
    `get_settings`; its validator; its keyword on `update_settings`; an entry here plus the settings POST's
    `Form()` parameter; and the input and refusal message in `settings.html`.
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
    }


def _mix_context(core: CoreService) -> dict[str, object]:
    """What the switcher needs: every **Mix**, and the stored active name. The name, not
    `core.active_mix().name`, so a deleted **Mix** still shows as the choice made.
    """
    return {"mixes": core.list_mixes(), "active_mix": core.get_settings().active_mix}


def _mix_section(core: CoreService, posted: Mapping[str, str] | None = None) -> dict[str, object]:
    """The **Mixes** section of the settings page: a row per **Mix**, plus the row that adds one.

    Built here so the template does not know the rules. `posted` is a refused save put back over its row; a
    name matching no stored **Mix** belongs to the add row. A row is deletable unless it is **Explore**,
    **Refine** or the active **Mix**.
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

    `refill` starts the three background threads in the lifespan. Off by default, and `main.py` is the one
    caller that turns it on: a test that started a thread would have a race in it, so tests drive the steps by
    hand.
    """
    app = FastAPI(title="wallpapi", lifespan=_lifespan(core) if refill else None)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def render(request: Request, result: Batch | BatchUnavailable | SubmissionRefused) -> HTMLResponse:
        """One fragment and one status code per outcome. A **Batch** or an empty **Pool** is `#batch`, with
        the refill status because it is the answer to "why empty"; a refusal is `#batch-banner`.
        """
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
        """The **Batch** page's shell, which fetches its **Batch** from `/batch`. It mints nothing: empty
        tiles hold the grid's shape.
        """
        return templates.TemplateResponse(
            request,
            "batch.html",
            {"batch_size": core.get_settings().batch_size, **_mix_context(core)},
        )

    @app.get("/batch", response_class=HTMLResponse)
    def batch_view(request: Request) -> HTMLResponse:
        """The live **Batch**, as the fragment the shell swaps in. Tiles are served from the **Thumbnail
        cache**, never hotlinked.
        """
        return render(request, core.get_next_batch())

    @app.post("/submit", response_class=HTMLResponse)
    def submit(request: Request, batch_id: Annotated[str, Form()]) -> HTMLResponse:
        """Submit the **Batch**, answer with the banner, and tell the page to fetch the next one.

        `HX-Trigger: batch-submitted` has `#batch` fetch `/batch` again, so a refusal triggers it too: the
        second tab (invariant 7) is told why, then shown the live **Batch**.
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
        """Mark one tile, or clear it, and swap that tile back. `hx-sync` on the control stops a late response
        swapping a stale tile in.
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

        One post and one transaction, never one per tile (invariant 6). The grid and tiles disable each
        other's controls in flight (`hx-disabled-elt`) so a bulk post and a tile post never overlap.
        **Ignore** is refused as for a single mark.
        """
        refused = core.set_all_draft_verdicts(batch_id, _drafted_verdict(verdict))
        if refused is not None:
            return render(request, refused)

        live = core.get_next_batch()
        if not isinstance(live, Batch):
            return render(request, live)
        return templates.TemplateResponse(request, "grid.html", {"batch": live})

    def render_history_row(request: Request, wallpaper_id: str) -> HTMLResponse:
        """The one row an edit changed, read back from the Core service so the screen shows what the
        **Decision log** resolves to.
        """
        row = core.get_history_row(wallpaper_id)
        if row is None:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="unknown wallpaper")
        return templates.TemplateResponse(request, "history_row.html", {"row": row})

    def refuse_history(refused: HistoryRefused) -> HTTPException:
        return HTTPException(status_code=_HISTORY_REFUSAL_STATUS[refused.reason], detail=refused.reason.value)

    @app.get("/history", response_class=HTMLResponse)
    def history_page(request: Request, verdict: str = "", page: int = 1) -> HTMLResponse:
        """**History**: one row per **Wallpaper** ever judged, newest activity first, filtered and paged. A
        page out of range is clamped, so a stale link lands on the last page.
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

    @app.post("/mix", response_class=HTMLResponse)
    def choose_mix(request: Request, mix: Annotated[str, Form()]) -> HTMLResponse:
        """Switch the active **Mix**, and swap the switcher back.

        The live **Batch** is untouched: the **Mix** is read when a **Batch** is minted, and rerolling would
        discard a **Draft Batch**. An unknown **Mix** is a 400 with the switcher unchanged; read back so what
        swaps in is what is held.
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
        """The settings form, filled with the values given rather than the values stored, so a refused save
        keeps what was typed.

        `posted_mix` is the same for the **Mixes** section, apart from `posted` because every **Mix** row
        posts the same four field names.
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

        The download counts arrive as query parameters, because this page is the redirect target of the
        download and not the download itself. They are `int`: wallpapi's own numbers, so FastAPI's 422 on a
        hand-edited URL is right.
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
    ) -> Response:
        """Save the settings, or come back with the reason they were refused.

        Every field is `str` and coerced by the Core service, so a typo gets this page's sentence and not
        FastAPI's JSON 422. A field the post did not carry is left alone; an empty one is a value, and the
        Core service refuses it. Post-redirect-get on success.
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
            )
            if value is not None
        }
        result = core.update_settings(**posted)
        if isinstance(result, SettingsRefused):
            # What was typed, over what is stored.
            return render_settings(
                request,
                posted={**_stored_fields(core), **posted},
                refused=result,
                status_code=HTTPStatus.BAD_REQUEST,
            )
        return RedirectResponse("/settings?saved=1", status_code=HTTPStatus.SEE_OTHER)

    @app.post("/settings/library/download")
    def download_favourites() -> Response:
        """Pull every **Favourite** whose **Library** file is missing, and redirect to the settings page with
        the counts.

        One-way: it writes and skips and never deletes. No refusal branch: a failure or a guarded name is
        counted and picked up by the next press.
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

        One route for both: the add row and every edit row post the same four fields. A plain form post,
        redirected on success. Every field is `str` so an empty percentage reaches `MIX_PERCENTAGES_INVALID`
        and not a 422.
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

        No control is rendered for **Explore**, **Refine** or the active **Mix**, so a refusal is a second tab
        or a hand-made post, but it is a rendered sentence because this is a full page load.
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
