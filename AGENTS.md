# wallpapi

## Agent skills

### Issue tracker

Issues live as GitHub issues in `CJohnsonPeart25/wallpapi`, managed via the `gh` CLI. This repo needs the
personal GitHub account, not the machine's global one — prefix `gh` commands with
`GH_TOKEN=$(gh auth token --user CJohnsonPeart25)` rather than running `gh auth switch`. Commit identity
is pinned to that account in this repo's local git config. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles, each label string equal to its role name. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context — `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## Stack

Decided before issue #2 and locked for everything downstream. Mirrored in issue #1 under "Technology"; if the
two disagree, issue #1 is the spec and wins.

- **Python 3.14+**, managed with `uv` — Python version, virtual environment and lockfile. Python is not on PATH
  as `python` on this machine (the Microsoft Store alias intercepts it). Always `uv run`.
- **FastAPI**, bound to `127.0.0.1` only, serving Jinja templates with htmx vendored locally. No JavaScript
  build step. Plain `{% include %}` partials, not `jinja2-fragments`.
- **Core synchronous throughout** — synchronous `httpx2.Client`, standard library `sqlite3`. Background **Pool**
  refill is a `threading.Thread` started in the FastAPI lifespan, not asyncio. FastAPI runs non-async endpoints
  in a threadpool, so tests stay plain function calls with no event loop. See
  `docs/adr/0001-synchronous-core.md`.
- **SQLite** via the standard library. Migrations are a `user_version` pragma plus numbered steps applied on
  startup. No ORM, no migration framework.
- **numpy** as a direct dependency — see invariant 2.
- **Pydantic at the edges only** — Wallhaven responses, settings validation, request bodies. Plain dataclasses
  and enums inside the core.
- **pytest**, **pyright** strict, **ruff** (lint + format).
- Deliberately excluded: TypeScript/React/Svelte, SQLAlchemy/Alembic, pydantic-settings, FastHTML,
  Electron/Tauri, any vector database.

"No JavaScript build step" does not mean no JavaScript. Hover-to-enlarge and fullscreen preview (#8) are client
concerns htmx does nothing for, and a small amount of vendored vanilla JS there is not a stack violation.

Run the app **single-worker**. `uvicorn --workers N` would give N background refill threads and N writers
against one SQLite file.

### Definition of done for every ticket

`ruff check` and `ruff format` clean, `pyright` strict clean, `pytest` green, plus a smoke test that boots the
app and hits `/`. No test touches the network.

## Invariants

Each of these is one careless line away from being silently violated.

1. **Single seam.** The UI and every test talk only to the **Core service**. Its five dependencies are
   injected: Wallhaven client, **Library** writer, random source (seedable), **Similarity provider**, clock.
   Fakes for all five. The clock is a dependency so that timestamps and the 45-calls-per-minute limit are both
   testable without real time passing.

2. **Scores are never stored.** Always derived from the **Decision log**. That only stays affordable as an
   array operation across the whole **Pool**, so the **Similarity provider** interface is
   `similarities(pool_ids, decided_ids) -> ndarray` — a matrix, never a pairwise call. A pairwise interface
   forces a Python loop over a 10k **Pool**, which is seconds per **Batch**, which makes caching **Scores**
   tempting, which breaks the rule. The matrix is **Pool** x decided (decided is small), never **Pool** x
   **Pool** — 10k x 10k is 800MB of float64 and is never needed. `numpy` must be a direct dependency, not
   transitive via onnxruntime; onnxruntime does not arrive until the #14 spike, which may never merge.

3. **SQLite.** `journal_mode=WAL` is set once at migration time — it persists in the database file.
   `synchronous` and `foreign_keys` are **per-connection** and do not persist. `busy_timeout` comes from
   `sqlite3.connect(timeout=...)`, which already defaults to 5 seconds. Verified here against SQLite 3.50.4:
   a second connection to a WAL database reads `journal_mode=wal`, but loses a `synchronous` the first
   connection set, and reads `foreign_keys=0` until it turns them on itself. Connection per thread:
   `check_same_thread` defaults to `True`, and the threadpool hands out a different thread per request.

   Open every connection with `isolation_level=None`. Under Python's default legacy transaction control,
   `sqlite3` implicitly emits a plain `BEGIN` before any INSERT/UPDATE/DELETE, so you silently get DEFERRED no
   matter what you intended. Any transaction that will write is then opened with an explicit `BEGIN IMMEDIATE`:
   a transaction that starts as a reader and upgrades to a writer gets `SQLITE_BUSY_SNAPSHOT` immediately and
   the busy handler is **not** invoked, so `busy_timeout` does not save it. Verified here: with `busy_timeout`
   at 10,000ms, the upgrading write failed in under a millisecond.

   **Leave `synchronous` at its default of FULL (2); never set NORMAL.** In WAL, NORMAL omits the sync on
   commit and loses recent commits on power loss. The **Decision log** is irreplaceable and accumulates over
   months; the write rate is a handful per minute. Writing `PRAGMA synchronous=FULL` is a no-op against the
   default — the thing to guard against is someone "optimising" it down to NORMAL.

4. **Verdict resolution orders by sequence, not timestamp.** Every **Decision log** row written in one submit
   transaction can share a timestamp, so "the latest **Explicit Verdict** wins" is only well defined against an
   autoincrement sequence. Timestamp is display-only.

5. **Timestamps are ISO 8601 UTC strings.** Register no `sqlite3` adapters — the default `datetime` adapters
   have been deprecated since Python 3.12. On 3.14 they still work but emit a `DeprecationWarning`, and 3.14
   has already removed other sqlite3 API deprecated in 3.12 (`sqlite3.version` is gone), so treat removal as
   coming. Time comes from the injected clock, never from `datetime.now()`.

6. **A Draft Batch is not the Decision log.** Tile clicks are htmx posts that **set** a **Draft Batch** entry
   rather than toggling it, so a replayed or duplicated click cannot flip the state the wrong way. Entries are
   keyed `(batch_id, wallpaper_id)`, which enforces one **Verdict** per **Wallpaper** per submission for free.
   Setting a tile to none deletes the row — absence already means **Ignore**. Each tile carries
   `hx-sync="this:replace"` so an out-of-order response cannot swap a stale tile back in. Select-all and
   select-none (#8) are one post that rewrites the whole **Draft Batch** in one transaction, not 32 posts.

   Submit is a **single transaction** that appends the **Explicit Verdicts** and the derived **Ignores**
   together, then clears the **Draft Batch**. The **Decision log** is append-only; a half-recorded **Batch**
   cannot be retracted.

7. **A Batch can be submitted once.** Submitting, or drafting against, an already-submitted **Batch** returns a
   refusal — not a silent no-op. Two browser tabs is a real case, and the second tab needs to be told why
   nothing happened.

8. **Thumbnail cache, with verdict-aware eviction.** Cache thumbnails locally and serve them from the app;
   do not hotlink 32 tiles per **Batch**. Separate directory from the **Library** (which is favourites-only and
   write-only). Eviction is *not* "when it leaves the **Pool**": **History** (#7) renders a thumbnail for every
   past **Verdict**, and **Banned** **Wallpapers** leave every **Zone** immediately and permanently. Keep
   thumbnails for anything with an **Explicit Verdict**; evict only undecided **Wallpapers** that left the
   **Pool**; size cap as a backstop.

9. **Store the absolute path of every Library file written.** The **Library** path is a setting that can
   change; removing a **Favourite** must delete the file where it was actually written, not a path recomputed
   from current settings. Deletion must only ever target a path wallpapi itself recorded — never an unguarded
   `os.remove` on a derived path. Deletion must tolerate the file already being gone: the spec guarantees
   "**Library** file deleted in Explorer, **Decision log** unchanged" is a reachable state.

10. **Library writes are atomic** — temp file plus `os.replace`. The temp file must be created **in the
    Library folder**, not in `%TEMP%`: `os.replace` is only atomic within one filesystem and raises across
    drives on Windows.

11. **Rate limit.** Wallhaven's documented 45/min applies to **API calls** (`wallhaven.cc/api`); images come
    from separate hosts (`th.wallhaven.cc`, `w.wallhaven.cc`). Still throttle image fetches modestly — those
    hosts sit behind DDoS protection with no published limits, and a **Batch** of 32 is 32 thumbnails plus
    full-resolution downloads. The rate limiter is a pure "how long must I wait" function over call
    timestamps; the caller does the waiting.

12. **Every wait is cancellable.** Sleeps are `stop_event.wait(n)`, never `time.sleep(n)`, and the httpx2
    timeout is set below the shutdown join timeout. Otherwise shutdown hangs on a thread stuck mid-request.

13. **For #14, ONNX Runtime, not open_clip/torch.** Image tower only: ~150MB against ~2.5GB. The spike stays
    disposable.

### Wallhaven API traps

- `atleast` is the **minimum** resolution. `resolutions` is an **exact-match** list. The **Filters** call for a
  minimum, so it is `atleast`. `ratios` does accept a comma-separated list.
- `meta.seed` is returned on `sorting=random` and is carried between pages to avoid repeats *within one walk*.
  Reusing a seed across refill runs returns the same **Wallpapers**.
- Search listings return 24 per page. Tags are only on the single-wallpaper endpoint.
- No API key is needed: NSFW is what requires one, and purity is fixed to SFW.

## Deferred decisions

Decided, but deliberately not built yet. Defer explicitly; do not quietly forget.

| What | Lands in | Note |
| --- | --- | --- |
| Absolute **Library** file path column | #5 | Not in migration 1. Needs its own numbered migration step alongside the **Library** writer. |
| Verdict-aware thumbnail eviction and size cap | #7 | #2 ships the serving seam and an unevicted directory. |
| Refill thread supervision, restart and a visible indicator | #6 | #2 has no **Pool**, so no refill thread. Clean shutdown arrives with the thread. |
| Offline / Wallhaven-down behaviour | #6 | Falls out of the "**Batch** unavailable" result once **Batches** come from the **Pool**. |
| **Decision log** backup procedure | later | Cheap because the database is a single file at a known path: `VACUUM INTO`. |
| `set_draft_verdict` and the `draft_batch` table | #3 | #2 submits a **Batch** as all **Ignores**, so there is nothing to draft against yet. Needs its own numbered migration step. Setting a tile to none deletes the row — see invariant 6. |
| Bulk **Draft Batch** writes — select-all and select-none | #8 | One post rewriting the whole **Draft Batch** in one transaction, not one post per tile. |
