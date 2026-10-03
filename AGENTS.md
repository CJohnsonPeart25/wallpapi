# wallpapi

## Agent skills

- **Issue tracker**: GitHub issues in `CJohnsonPeart25/wallpapi`. Prefix `gh` with
  `GH_TOKEN=$(gh auth token --user CJohnsonPeart25)`, never `gh auth switch`; the machine's global account is
  the wrong one. Commit identity is pinned in the local git config. See `docs/agents/issue-tracker.md`.
- **Triage labels**: five roles, each label its role name. See `docs/agents/triage-labels.md`.
- **Domain docs**: `CONTEXT.md` and `docs/adr/` at the root. See `docs/agents/domain.md`.
- **Delivery**: one agent per issue in its own worktree; PR shape, review note, lead review, numbers assigned
  before dispatch. See `docs/agents/delivery.md`.

## Stack

Locked. The original spec issue lists it under "Technology"; if the two disagree, the spec wins.

- **Python 3.14+** with `uv`. `python` is not on PATH here (a Store alias intercepts it): `uv run`.
- **FastAPI** on `127.0.0.1` only; Jinja, `{% include %}` partials and one `{% extends "base.html" %}` shell.
  A fragment htmx swaps in extends nothing. htmx vendored locally, no JavaScript build step.
- **Pico CSS 2.1.1** (`pico.indigo.min.css`, the default class-based build) and **Alpine.js 3.17.4**
  (`alpine.min.js`, the `cdn.min.js` build, loaded `defer`), both vendored into `web/static/` byte for byte
  with their licence headers, like htmx. Pico is the base and `base.css` is what Pico cannot say — ADR 0014.
  `scripts/vendor_assets.py` alone records each file's URL and SHA-256: upgrade by editing it and running
  `uv run python scripts/vendor_assets.py`. `.gitattributes` stops checkout rewriting them.
- **Synchronous throughout** (ADR 0001): `httpx2.Client`, standard library `sqlite3`, background work on
  `threading.Thread`s started in the FastAPI lifespan. Migrations are `user_version` plus numbered steps.
- **numpy** as a *direct* dependency, never left to arrive through onnxruntime.
- **onnxruntime** with **pillow** for the **Similarity provider** (ADR 0013): CPU, image tower only, the model
  fetched once into `~/.wallpapi/models/` and checksummed.
- **Pydantic at the edges only**; dataclasses and enums inside. **pytest**, **pyright** strict, **ruff**.
- Excluded: TypeScript/React/Svelte, SQLAlchemy/Alembic, pydantic-settings, FastHTML, Electron/Tauri, vector DBs.

What the page does to the server is an htmx attribute; what can be CSS is CSS. `web/static/wallpapi.js` sizes
the **Batch** grid and nothing else. Alpine owns four inline `x-data` islands (the preview `<dialog>`, the
**Mix** dropdown, the theme button, each settings **Mix** row's total), never on markup htmx replaces.

Run **single-worker**: `--workers N` is N refill threads and N writers on one SQLite file.

**Done** means `ruff check` and `ruff format` clean, `pyright` strict clean, `pytest` green, and a smoke test
that boots the app and hits `/`. No test touches the network.

## Invariants

Design intent no test can hold. Numbers are stable and never reused, because ADRs cite them. Retired:
1 -> 14 (ADR 0019); 8 -> ADRs 0003 and 0009; 11 -> ADR 0005 and Traps; 13 -> ADR 0013.

- **2. Scores are never stored.** Always derived from the **Decision log** in one array operation over the
  whole **Pool**: `similarities(pool, decided)` is a **Pool** x decided matrix, never pairwise and never
  **Pool** x **Pool**. A pairwise call means a Python loop per **Batch**, which tempts caching. ADR 0007.
- **3. SQLite**: WAL set once at migration; every connection, one per thread, opened `isolation_level=None`
  with `foreign_keys` on, writes in `BEGIN IMMEDIATE`, `synchronous` left at FULL, never NORMAL.
- **4. The Decision log is append-only and resolves by sequence.** One transaction's rows, or two **History**
  clicks in a second, share a timestamp, so "the latest entry decides" orders by autoincrement sequence.
  The rule is one SQL fragment, `_RESOLUTION_CTE`; build on it. A submitted **Batch** is never retracted.
  ADR 0015.
- **5. Timestamps are ISO 8601 UTC strings** from the injected clock; no `sqlite3` adapters.
- **6. A Draft Batch is not the Decision log**: a tile post sets its entry, never toggles; submit appends.
- **7. A Batch is submitted once**: resubmitting, or drafting against it, is refused with a reason.
- **9. The Library is confined.** Every write and deletion goes through `confined_to_library` and uses the
  path it returns. Deletion targets only a recorded path, tolerates it being gone, and unlinks regular files
  only; a recorded path that fails the guard is dropped from the record and left on disk. ADR 0006.
- **10. Library writes are atomic**: a temp file beside the confined destination, then `os.replace`.
- **12. Every wait is cancellable**: `stop_event.wait(n)`, never `time.sleep(n)`, and the HTTP timeout below
  the shutdown join timeout, or shutdown hangs on a thread stuck mid-request.
- **14. Modules have their own seams.** Every module exposes a small interface taking a connection and its
  collaborators. Tests build one module with a real in-memory database and fake only the external
  collaborator it talks to: the Wallhaven client, the **Library** writer, the clock or the embedder. ADR 0019.

## Traps

- `atleast` is a minimum resolution, `resolutions` exact-match. `ratios` buckets (a 3440x1440 is 2.39,
  served as `21x9`), so the local check is a band, `RATIO_TOLERANCE`, not equality.
- `q=like:<id>` finds lookalikes: sort by `relevance` (not the default `date_added`) and cap the walk.
- `meta.seed` stops repeats within one walk only. `meta.last_page` is no stop condition; an empty page is.
- A 429 raises `RateLimited`, the only failure told apart. `Retry-After` is parsed as seconds only.
- Tags come only from `GET /api/v1/w/{id}`, one **API call** per **Wallpaper**. Listings are 24 a page.
- **45 API calls a minute is counted across the machine.** `wait_needed` sees one process, so anything calling
  outside the refill must pace itself evenly. Image hosts have no published limit; throttle them modestly.
- No API key: purity is fixed to SFW.
- `create_app(refill=True)` starts the background threads, off by default so no test starts one. `build_app`
  in `main.py` is the only caller that turns it on, and is untested: lose that line and nothing fills.

## Deferred decisions

Decided, not built. Defer explicitly; do not quietly forget.

- Saying a **Library** write failed *at submission*: `submit_batch` discards `reconcile_library`'s report.
- Caching full-resolution images: never. The preview loads `full_url` from Wallhaven (ADR 0003).
- Restarting a dead refill thread: no supervisor until something is seen to kill one.
- Throttling full-resolution fetches: needs **Favourite** downloads off the request thread first.
- Renaming a **Mix**: delete and add covers it; a rename must move `active_mix` in the same transaction.
- **Decision log** backup: `VACUUM INTO`, the database being one file.
- Evicting a thumbnail on a **History** edit: the next submission's eviction picks it up (ADR 0009).
- Fading old **Verdicts**: a half-life in log sequence, not time; nothing real to tune it against yet.
- Showing decided **Wallpapers** again: must answer near-duplicates; dormant pre-marking waits for it.
- **Dud** build-up in the **Pool**: for now, **Ban** or **Ignore** a page of them and submit.
- An outlier guard on the varied **Unknown** draw: the learning loop is the guard for now (ADR 0018).
