"""The web layer: Jinja templates over the modules the composition root built (ADR 0001).

Every route that writes calls one workflow; a route that only reads asks the modules. Nothing here decides
anything about the data, and no setting, **Mix** field or **Verdict** list is spelled here: forms are read by
the settings module's tables. A **Batch** or **History** refusal arrives from a module and leaves through
`REFUSALS`; a settings refusal is a 400 on the settings page, in `SettingsRefused.message`'s words (ADR 0004).
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from mimetypes import guess_type
from pathlib import Path
from typing import Annotated, Final, Literal, overload

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.types import Lifespan

from wallpapi import batches, evidence_log, pool, settings, workflows
from wallpapi.batches import Batch, BatchUnavailable, SubmissionRefused
from wallpapi.compose import Modules, background_loops
from wallpapi.evidence_log import HistoryEntry, HistoryRefused, ResolvedVerdict, Theme
from wallpapi.lineup import Lineup, LineupRefused, LineupUnavailable
from wallpapi.model import Verdict, Wallpaper
from wallpapi.settings import (
    FORM_FIELDS,
    MAX_MIX_NAME_LENGTH,
    MIX_FORM_FIELDS,
    MIX_TOTAL,
    Settings,
    SettingsRefused,
    form_values,
    mix_form,
)
from wallpapi.taste.pick import PICK_V1

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
"""Vendored htmx, stylesheet and preview script: no third-party asset in any template."""

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter()


@dataclass(frozen=True, slots=True)
class Refusal:
    status: HTTPStatus
    message: str
    banner: bool = False
    """A stale tab can provoke it, so the page says it in the banner. Otherwise only a hand-made post can,
    and it is a bare status: htmx skips a 4xx."""


REFUSALS: dict[StrEnum, Refusal] = {
    SubmissionRefused.Reason.ALREADY_SUBMITTED: Refusal(
        HTTPStatus.CONFLICT,
        "That batch has already been submitted — nothing was recorded a second time.",
        banner=True,
    ),
    SubmissionRefused.Reason.UNKNOWN_BATCH: Refusal(
        HTTPStatus.NOT_FOUND,
        "That batch is no longer live here: it was skipped or reset, or replaced when the page was opened "
        "again in another tab, or this instance never drew it. Nothing was recorded.",
        banner=True,
    ),
    SubmissionRefused.Reason.IGNORE_DRAFTED: Refusal(
        HTTPStatus.BAD_REQUEST, "an ignore is derived, not drafted"
    ),
    SubmissionRefused.Reason.NOT_IN_BATCH: Refusal(HTTPStatus.NOT_FOUND, "unknown wallpaper"),
    HistoryRefused.Reason.UNKNOWN_WALLPAPER: Refusal(HTTPStatus.NOT_FOUND, "unknown wallpaper"),
    LineupRefused.Reason.UNKNOWN_LINEUP: Refusal(
        HTTPStatus.CONFLICT,
        "That line-up is no longer on screen: it was answered or reset, or replaced when the compare tab was "
        "opened again in another tab, or a key was pressed twice. Nothing more was recorded; this is the one "
        "on screen now.",
        banner=True,
    ),
    LineupRefused.Reason.NOT_IN_LINEUP: Refusal(HTTPStatus.NOT_FOUND, "unknown wallpaper"),
}
"""Every refusal the **Batch**, **History** and compare pages answer, as the page answers it. Settings
refusals are not here: each is a 400 in `settings.py`'s words, which fill in the refused field's own bounds,
so they stay beside the field (ADR 0004)."""

COMPARE_INSTRUMENTS: Final = (PICK_V1.name,)
"""The **Instruments** the compare tab offers, by the name each is recorded under, the first the default:
the choice the URL carries (`?instrument=`). **Order** and **Gauntlet** join it, each with its own answer
route at `/compare/<name>`."""


@dataclass(frozen=True, slots=True)
class HistoryRow:
    """One line of **History** with its **Wallpaper**, as the row template shows it."""

    wallpaper: Wallpaper
    resolved: ResolvedVerdict
    latest_at: dt.datetime


@dataclass(frozen=True, slots=True)
class _DownloadCounts:
    """What the last "download all **Favourites**" did, as the settings page renders it."""

    downloaded: bool
    written: int
    skipped: int
    failed: int


def _modules(request: Request) -> Modules:
    modules: Modules = request.app.state.modules
    return modules


Wired = Annotated[Modules, Depends(_modules)]


@overload
def parse_verdict(posted: str, *, required: Literal[True]) -> Verdict: ...
@overload
def parse_verdict(posted: str, *, required: Literal[False]) -> Verdict | None: ...
def parse_verdict(posted: str, *, required: bool) -> Verdict | None:
    """The **Verdict** a control posted; empty is `None` unless one is `required`. An **Ignore** parses like
    any other: whether it may be drafted is `batches`' rule.
    """
    if not posted:
        if required:
            raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail="a verdict is required")
        return None
    try:
        return Verdict(posted)
    except ValueError:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail="unknown verdict") from None


def parse_theme(posted: str) -> Theme | None:
    """The page's resolved theme, if it posted `light` or `dark`. Anything else is dropped rather than
    refused: the theme describes the moment and must never cost the submission it rides on."""
    try:
        return Theme(posted)
    except ValueError:
        return None


def _refuse(request: Request, reason: StrEnum) -> HTMLResponse:
    """The banner for a refusal a stale tab can provoke; a bare status for any other."""
    refusal = REFUSALS[reason]
    if not refusal.banner:
        raise HTTPException(status_code=refusal.status, detail=refusal.message)
    return templates.TemplateResponse(
        request, "banner.html", {"refused": refusal.message}, status_code=refusal.status
    )


def _batch_view(request: Request, modules: Modules, result: Batch | BatchUnavailable) -> HTMLResponse:
    """The **Batch** fragment, or the words for why there is none."""
    context: dict[str, object] = {
        "status": modules.refill.status(),
        "similarity_notice": modules.similarity.notice(modules.thumbnails.obtainable(modules.connect())),
    }
    if isinstance(result, Batch):
        return templates.TemplateResponse(
            request,
            "batch_view.html",
            {**context, "batch": result, "unknown_short": result.unknown_short},
        )
    # 503 and never a 500: nothing failed, the **Pool** is empty and the refill may fix it.
    return templates.TemplateResponse(
        request,
        "batch_view.html",
        {**context, "unavailable": result},
        status_code=HTTPStatus.SERVICE_UNAVAILABLE,
    )


def _history_rows(connection: sqlite3.Connection, entries: Sequence[HistoryEntry]) -> tuple[HistoryRow, ...]:
    """Each line with its **Wallpaper** joined on, the page's in one query."""
    wallpapers = pool.wallpapers(connection, [entry.wallpaper_id for entry in entries])
    return tuple(HistoryRow(wallpapers[e.wallpaper_id], e.resolved, e.decided_at) for e in entries)


def _mix_context(connection: sqlite3.Connection) -> dict[str, object]:
    """What the switcher needs. The stored active name, so a deleted **Mix** still shows as chosen."""
    return {
        "mixes": tuple(listed.mix for listed in settings.list_mixes(connection)),
        "active_mix": settings.get(connection).active_mix,
    }


def _instrument(named: str) -> str:
    """The **Instrument** the URL names, the tab's first when it names none; one it does not offer is a
    404."""
    if not named:
        return COMPARE_INSTRUMENTS[0]
    if named not in COMPARE_INSTRUMENTS:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="unknown instrument")
    return named


def _lineup_size_context(connection: sqlite3.Connection) -> dict[str, object]:
    """What the **Line-up** size switcher needs: the stored size, and every size the setting accepts."""
    field = Settings.lineup_size
    return {
        "lineup_size": settings.get(connection).lineup_size,
        "lineup_sizes": range(int(field.minimum or 0), int(field.maximum or 0) + 1),
    }


def _lineup_view(
    request: Request,
    instrument: str,
    result: Lineup | LineupUnavailable,
    *,
    refused: Refusal | None = None,
) -> HTMLResponse:
    """The **Line-up** fragment, or the words for why there is none; after a refused answer, the one on
    screen now under the words for why, at the refusal's status."""
    context: dict[str, object] = {
        "instrument": instrument,
        "refused": None if refused is None else refused.message,
    }
    if isinstance(result, Lineup):
        status = HTTPStatus.OK if refused is None else refused.status
        return templates.TemplateResponse(
            request, "lineup_view.html", {**context, "lineup": result}, status_code=status
        )
    # 503 and never a 500: nothing failed, there is too little to compare and the refill may fix it.
    return templates.TemplateResponse(
        request,
        "lineup_view.html",
        {**context, "unavailable": result},
        status_code=HTTPStatus.SERVICE_UNAVAILABLE,
    )


def _pool_reset_context(connection: sqlite3.Connection) -> dict[str, object]:
    """What the **Pool reset** section counts: the **Pool**, and the live **Batch** without minting one."""
    return {"pool_size": pool.size(connection), "live": batches.live(connection)}


def _settings_page(
    request: Request,
    modules: Modules,
    *,
    posted: Mapping[str, str] | None = None,
    posted_mix: Mapping[str, str] | None = None,
    refused: SettingsRefused | None = None,
    download: _DownloadCounts | None = None,
    saved: bool = False,
    deleted: bool = False,
) -> HTMLResponse:
    """The settings form, filled with what was typed over what is stored, so a refused save keeps it."""
    connection = modules.connect()
    mixes = mix_form(connection, posted_mix)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "sections": settings.form_sections(),
            "values": {**form_values(settings.get(connection)), **(posted or {})},
            "mix_fields": MIX_FORM_FIELDS,
            "mix_rows": mixes.rows,
            "new_mix": mixes.add,
            "mix_total": MIX_TOTAL,
            "max_mix_name_length": MAX_MIX_NAME_LENGTH,
            "refused": refused,
            "saved": saved,
            "deleted": deleted,
            "download": download,
            **_pool_reset_context(connection),
        },
        status_code=HTTPStatus.OK if refused is None else HTTPStatus.BAD_REQUEST,
    )


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


async def _posted_mix(request: Request) -> dict[str, str]:
    """A **Mix** form as posted, by key, a field not posted as empty: `save_mix` refuses what is missing, and
    a refusal puts every field back. Read by `MIX_FORM_FIELDS` for the same reason as `_posted_settings`.
    """
    form = await request.form()
    return {key: value if isinstance(value := form.get(key), str) else "" for key in MIX_FORM_FIELDS}


@router.get("/", response_class=HTMLResponse)
def batch_page(request: Request, modules: Wired) -> HTMLResponse:
    """The **Batch** page's shell, which fetches a new **Batch** from `/batch` as it loads, and the next from
    `/batch/next` after an answer. It mints nothing itself."""
    connection = modules.connect()
    return templates.TemplateResponse(
        request,
        "batch.html",
        {"batch_size": settings.get(connection).batch_size, **_mix_context(connection)},
    )


@router.get("/batch", response_class=HTMLResponse)
def batch_view(request: Request, modules: Wired) -> HTMLResponse:
    """A new **Batch** for a visit, as the fragment the shell swaps in when it loads: the live one is
    replaced, unrecorded, with its **Draft Batch** (ADR 0029)."""
    return _batch_view(request, modules, modules.batches.fresh(modules.connect()))


@router.get("/batch/next", response_class=HTMLResponse)
def next_batch_view(request: Request, modules: Wired) -> HTMLResponse:
    """The live **Batch**, or a new one if there is none, as the fragment the shell swaps in after a
    submission or a **Skip**, refused or not: never a visit, so a refusal shows the one live now."""
    return _batch_view(request, modules, modules.batches.next(modules.connect()))


@router.post("/submit", response_class=HTMLResponse)
def submit(
    request: Request, modules: Wired, batch_id: Annotated[str, Form()], theme: Annotated[str, Form()] = ""
) -> HTMLResponse:
    """Submit the **Batch** with the page's theme, answer with the banner, and have the page fetch the next
    one, refused or not."""
    result = workflows.submit(modules, batch_id, theme=parse_theme(theme))
    if isinstance(result, SubmissionRefused):
        response = _refuse(request, result.reason)
    else:
        response = templates.TemplateResponse(
            request, "banner.html", {"recorded": result.recorded, "ignored": result.ignored}
        )
    response.headers["HX-Trigger"] = "batch-submitted"
    return response


@router.post("/skip", response_class=HTMLResponse)
def skip(
    request: Request, modules: Wired, batch_id: Annotated[str, Form()], theme: Annotated[str, Form()] = ""
) -> HTMLResponse:
    """Skip the **Batch** (ADR 0026) with the page's theme and answer as `/submit` does: the banner, and the
    same refetch."""
    refused = workflows.skip(modules, batch_id, theme=parse_theme(theme))
    if refused is not None:
        response = _refuse(request, refused.reason)
    else:
        response = templates.TemplateResponse(request, "banner.html", {"skipped": True})
    response.headers["HX-Trigger"] = "batch-submitted"
    return response


@router.post("/draft", response_class=HTMLResponse)
def draft(
    request: Request,
    modules: Wired,
    batch_id: Annotated[str, Form()],
    wallpaper_id: Annotated[str, Form()],
    verdict: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Mark one tile, or clear it, and swap that tile back."""
    live = workflows.set_draft(modules, batch_id, wallpaper_id, parse_verdict(verdict, required=False))
    if isinstance(live, SubmissionRefused):
        return _refuse(request, live.reason)
    # Refused above unless the **Batch** shows it.
    marked = next(w for w in live.wallpapers if w.id == wallpaper_id)
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


@router.post("/draft/all", response_class=HTMLResponse)
def draft_all(
    request: Request, modules: Wired, batch_id: Annotated[str, Form()], verdict: Annotated[str, Form()] = ""
) -> HTMLResponse:
    """Mark the whole **Batch**, or clear it, and swap the grid back: one post, one transaction."""
    live = workflows.set_all_drafts(modules, batch_id, parse_verdict(verdict, required=False))
    if isinstance(live, SubmissionRefused):
        return _refuse(request, live.reason)
    return templates.TemplateResponse(request, "grid.html", {"batch": live})


@router.post("/draft/preview")
def open_preview(
    request: Request, modules: Wired, batch_id: Annotated[str, Form()], wallpaper_id: Annotated[str, Form()]
) -> Response:
    """Record that a tile's full-size preview was opened. Nothing on the page changes, so nothing comes
    back: the preview swaps nothing in."""
    opened = workflows.open_preview(modules, batch_id, wallpaper_id)
    if isinstance(opened, SubmissionRefused):
        return _refuse(request, opened.reason)
    return Response(status_code=HTTPStatus.NO_CONTENT)


@router.get("/history", response_class=HTMLResponse)
def history_page(request: Request, modules: Wired, verdict: str = "", page: int = 1) -> HTMLResponse:
    """**History**, filtered by resolved **Verdict** and paged; a page out of range is clamped."""
    connection = modules.connect()
    chosen = parse_verdict(verdict, required=False)
    listing = evidence_log.history(connection, chosen, page)
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "history": listing,
            "rows": _history_rows(connection, listing.entries),
            "verdict": chosen,
            "verdicts": evidence_log.HISTORY_FILTERS,
        },
    )


@router.post("/history/verdict", response_class=HTMLResponse)
def history_verdict(
    request: Request,
    modules: Wired,
    wallpaper_id: Annotated[str, Form()],
    verdict: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Change a **Wallpaper**'s **Verdict** from **History**, and swap back its row as the log resolves."""
    edited = workflows.edit_verdict(modules, wallpaper_id, parse_verdict(verdict, required=True))
    if isinstance(edited, HistoryRefused):
        return _refuse(request, edited.reason)
    return templates.TemplateResponse(
        request, "history_row.html", {"row": _history_rows(modules.connect(), [edited])[0]}
    )


@router.post("/mix", response_class=HTMLResponse)
def choose_mix(request: Request, modules: Wired, mix: Annotated[str, Form()]) -> HTMLResponse:
    """Switch the active **Mix**. The live **Batch** is untouched, so its **Draft Batch** survives."""
    refused = isinstance(workflows.choose_mix(modules, mix), SettingsRefused)
    return templates.TemplateResponse(
        request,
        "mix.html",
        _mix_context(modules.connect()),
        status_code=HTTPStatus.BAD_REQUEST if refused else HTTPStatus.OK,
    )


@router.get("/compare", response_class=HTMLResponse)
def compare_page(request: Request, modules: Wired, instrument: str = "") -> HTMLResponse:
    """The compare tab's shell for the **Instrument** the URL names, which fetches a new **Line-up** from
    `/compare/lineup` as it loads. It draws nothing itself."""
    return templates.TemplateResponse(
        request,
        "compare.html",
        {
            "instrument": _instrument(instrument),
            "instruments": COMPARE_INSTRUMENTS,
            **_lineup_size_context(modules.connect()),
        },
    )


@router.get("/compare/lineup", response_class=HTMLResponse)
def lineup_view(request: Request, modules: Wired, instrument: str = "") -> HTMLResponse:
    """A new **Line-up** for a visit, as the fragment the shell swaps in, its tiles answering the named
    **Instrument**: the live one is replaced, unrecorded, with its stars (ADR 0029)."""
    chosen = _instrument(instrument)
    return _lineup_view(request, chosen, modules.lineups.fresh(modules.connect()))


@router.post("/compare/pick", response_class=HTMLResponse)
def pick(
    request: Request,
    modules: Wired,
    lineup_id: Annotated[str, Form()],
    wallpaper_id: Annotated[str, Form()] = "",
    theme: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Answer the **Line-up** with a **Pick**, or "none of these" when no wallpaper is posted, and answer
    with the next one, so it comes at once. A stale answer gets the one on screen now, under the words."""
    picked = workflows.pick(modules, lineup_id, wallpaper_id or None, theme=parse_theme(theme))
    refused = None
    if isinstance(picked, LineupRefused):
        refused = REFUSALS[picked.reason]
        if not refused.banner:
            raise HTTPException(status_code=refused.status, detail=refused.message)
    return _lineup_view(request, PICK_V1.name, modules.lineups.next(modules.connect()), refused=refused)


@router.post("/compare/star", response_class=HTMLResponse)
def star_lineup_tile(
    request: Request,
    modules: Wired,
    lineup_id: Annotated[str, Form()],
    wallpaper_id: Annotated[str, Form()],
    starred: Annotated[bool, Form()],
    instrument: str = "",
) -> HTMLResponse:
    """Star one **Line-up** tile, or take its star off, and answer with the same **Line-up**, its tiles
    answering the **Instrument** the URL names: a star never answers it or draws the next. A stale star gets
    the one on screen now, under the words, as a stale answer does."""
    chosen = _instrument(instrument)
    held = workflows.star_lineup_tile(modules, lineup_id, wallpaper_id, starred=starred)
    if isinstance(held, Lineup):
        return _lineup_view(request, chosen, held)
    refused = REFUSALS[held.reason]
    if not refused.banner:
        raise HTTPException(status_code=refused.status, detail=refused.message)
    return _lineup_view(request, chosen, modules.lineups.next(modules.connect()), refused=refused)


@router.post("/compare/size", response_class=HTMLResponse)
def choose_lineup_size(request: Request, modules: Wired, size: Annotated[str, Form()] = "") -> HTMLResponse:
    """Set the **Line-up** size for the next **Line-up**. The one on screen is untouched."""
    refused = isinstance(workflows.choose_lineup_size(modules, size), SettingsRefused)
    return templates.TemplateResponse(
        request,
        "lineup_size.html",
        _lineup_size_context(modules.connect()),
        status_code=HTTPStatus.BAD_REQUEST if refused else HTTPStatus.OK,
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    modules: Wired,
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
    return _settings_page(
        request,
        modules,
        saved=saved,
        deleted=deleted,
        download=_DownloadCounts(downloaded, written, skipped, failed),
    )


@router.post("/settings")
def save_settings(
    request: Request, modules: Wired, posted: Annotated[dict[str, str], Depends(_posted_settings)]
) -> Response:
    """Save the settings, or come back with the reason. Fields are `str`, coerced by the settings module."""
    result = workflows.save_settings(modules, **posted)
    if isinstance(result, SettingsRefused):
        return _settings_page(request, modules, posted=posted, refused=result)
    return RedirectResponse("/settings?saved=1", status_code=HTTPStatus.SEE_OTHER)


@router.post("/settings/library/download")
def download_favourites(modules: Wired) -> Response:
    """Pull every **Favourite** whose **Library** file is missing, redirecting with the counts. It never
    deletes.
    """
    pulled = workflows.download_favourites(modules)
    return RedirectResponse(
        f"/settings?downloaded=1&written={len(pulled.written)}"
        f"&skipped={len(pulled.skipped)}&failed={len(pulled.failed)}",
        status_code=HTTPStatus.SEE_OTHER,
    )


@router.post("/settings/pool/reset", response_class=HTMLResponse)
def reset_pool(request: Request, modules: Wired) -> HTMLResponse:
    """Reset the **Pool** (ADR 0025) and swap the section back in place, saying what went."""
    reset = workflows.reset_pool(modules)
    return templates.TemplateResponse(
        request, "pool_reset.html", {"reset": reset, **_pool_reset_context(modules.connect())}
    )


@router.post("/settings/mixes")
def save_mix(
    request: Request, modules: Wired, posted: Annotated[dict[str, str], Depends(_posted_mix)]
) -> Response:
    """Create a **Mix** or edit one: one route, as the add row and every edit row post the same fields."""
    result = workflows.save_mix(modules, **posted)
    if isinstance(result, SettingsRefused):
        return _settings_page(request, modules, posted_mix=posted, refused=result)
    return RedirectResponse("/settings?saved=1", status_code=HTTPStatus.SEE_OTHER)


@router.post("/settings/mixes/delete")
def delete_mix(request: Request, modules: Wired, name: Annotated[str, Form()] = "") -> Response:
    """Remove a **Mix**, or re-render with the reason it stays."""
    refused = workflows.delete_mix(modules, name)
    if refused is not None:
        return _settings_page(request, modules, refused=refused)
    return RedirectResponse("/settings?deleted=1", status_code=HTTPStatus.SEE_OTHER)


@router.get("/thumb/{wallpaper_id}")
def thumbnail(modules: Wired, wallpaper_id: str) -> FileResponse:
    """One tile, off the **Thumbnail cache**."""
    cached = modules.thumbnails.get(modules.connect(), wallpaper_id)
    if cached is None:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="unknown wallpaper")
    return FileResponse(cached, media_type=guess_type(cached.name)[0] or "application/octet-stream")


def _lifespan(modules: Modules) -> Lifespan[FastAPI]:
    """Start the background loops with the app and stop them in reverse on the way out."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        del app
        loops = background_loops(modules)
        for loop in loops:
            loop.start()
        try:
            yield
        finally:
            for loop in reversed(loops):
                loop.stop()

    return lifespan


def create_app(modules: Modules, *, refill: bool = False) -> FastAPI:
    """The app over the composed modules; `refill` starts the background loops and only `main.py` turns it
    on.
    """
    app = FastAPI(title="wallpapi", lifespan=_lifespan(modules) if refill else None)
    app.state.modules = modules
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)
    return app
