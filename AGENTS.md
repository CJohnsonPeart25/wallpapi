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

### Delivery

One agent per issue in its own worktree, PR shape, review-note comment, lead review, and how migration and
ADR numbers are assigned before dispatch. See `docs/agents/delivery.md`.

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
- **onnxruntime** and **pillow** for the **Similarity provider** (#14, ADR 0013). CPU only, image
  tower only, imported lazily. The model is not vendored: it is fetched once into
  `~/.wallpapi/models/` and checksummed.
- **Pydantic at the edges only** — Wallhaven responses, settings validation, request bodies. Plain dataclasses
  and enums inside the core.
- **pytest**, **pyright** strict, **ruff** (lint + format).
- Deliberately excluded: TypeScript/React/Svelte, SQLAlchemy/Alembic, pydantic-settings, FastHTML,
  Electron/Tauri, any vector database.

"No JavaScript build step" does not mean no JavaScript. Hover-to-enlarge and fullscreen preview (#8) are client
concerns htmx does nothing for, and a small amount of vendored vanilla JS there is not a stack violation.
As built at #8, hover-to-enlarge needed none of it — it is a CSS rule — and the preview is one delegated
listener in `web/static/wallpapi.js`. Every asset the page loads is served from `/static`.

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
   `similarities(pool, decided) -> ndarray` — a matrix, never a pairwise call. A pairwise interface
   forces a Python loop over a 10k **Pool**, which is seconds per **Batch**, which makes caching **Scores**
   tempting, which breaks the rule. The matrix is **Pool** x decided (decided is small), never **Pool** x
   **Pool** — 10k x 10k is 800MB of float64 and is never needed. `numpy` must be a **direct** dependency
   and never transitive via onnxruntime — onnxruntime arrived as a real dependency at #14, so this is now
   a thing that can actually be got wrong by someone tidying `pyproject.toml`.

   The protocol carries two more methods since #14, and both are on it rather than on the one provider
   that needs them: `catch_up`, one step of a provider's own upkeep, called only from the background
   thread; and `notice`, one line for the page when a provider is not at full strength. On the protocol,
   because the alternative is the Core service knowing which provider it is holding, and the whole point
   of the seam is that it does not. A provider with nothing to do returns `NOTHING_TO_CATCH_UP`.

   Both sides are `Sequence[Wallpaper]` and not IDs, since #9: the baseline provider reads `.colours` and
   `.category`, and one handed only IDs would have to open a second seam into storage to get them.
   `CoreService.classify_pool()` is the one operation — whole **Pool**, one call, nothing memoised. The
   `zone` column on `batch_wallpapers` is *not* a stored **Score**: it records which **Zone** a **Batch**
   drew a **Wallpaper** from, which is a fact about the **Batch** and not about the **Wallpaper**. Since
   #10 it is also *not* the **Zone** of the **Slot** that **Wallpaper** filled — a **Shortfall** means
   those two differ, and what the tile shows is where the **Wallpaper** came from. See
   `docs/adr/0007-scores-are-derived-in-one-pass-over-the-whole-pool.md`.

   Sum the weighted values with `(weights * values).sum(axis=1)`, never `weights @ values`. BLAS may
   reorder its terms and use fused multiply-add, so equal and opposite contributions come out at a few
   times 1e-15 rather than at zero — and **Unknown** is the *exact* zero of the **Score**.

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
   autoincrement sequence. Timestamp is display-only. A **Clearance** and an edit made from **History** share
   one too — the clock has whole-second granularity and both are clicks — so this is not only about the rows
   of one transaction.

   The rule itself, extended at #7 for the **Clearance**, lives in one SQL fragment (`_RESOLUTION_CTE`)
   because **History** filters and pages over the resolved **Verdict** and cannot resolve a log of thousands
   of rows in Python first. The latest entry that is not an **Ignore** decides: an **Explicit Verdict** counts
   alone and disregards every **Ignore** before and after it; a **Clearance**, or no such entry, lets every
   **Ignore** stack. Anything needing resolution in a `WHERE` clause builds on that fragment rather than
   spelling the rule a second time. See
   `docs/adr/0008-a-clearance-is-an-entry-and-resolution-is-decided-in-sql.md`.

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
   write-only). Eviction is *not* "when it leaves the **Pool**": **History** renders a thumbnail for every
   past **Verdict**, and **Banned** **Wallpapers** leave every **Zone** immediately and permanently. Keep
   thumbnails for anything with an **Explicit Verdict**; evict only undecided **Wallpapers** that left the
   **Pool**; size cap as a backstop.

   Built at #7 as `evict_thumbnails()`, run at the tail of `submit_batch`. Two passes: everything with no
   **Explicit Verdict** standing, not in the **Pool** and not in the live **Batch** goes outright; then, over
   `thumbnail_cache_max_mb`, the oldest-modified of the still-to-be-shown ones go until the cache is under it.
   **The cap may never evict an Explicit Verdict**, so a cache over the cap on **Favourites** alone stays over
   it and says so. Safe to be wrong about in one direction only: `get_thumbnail` re-fetches what is missing,
   so an over-eager eviction costs a request and a too-timid one costs disk. See
   `docs/adr/0009-thumbnails-are-evicted-by-verdict-with-a-size-cap-behind-it.md`.

9. **Store the absolute path of every Library file written, and never touch a path outside the Library
   folder.** The **Library** path is a setting that can change; removing a **Favourite** must delete the
   file where it was actually written, not a path recomputed from current settings. Deletion must only ever
   target a path wallpapi itself recorded — never an unguarded `os.remove` on a derived path. Deletion must
   tolerate the file already being gone: the spec guarantees "**Library** file deleted in Explorer,
   **Decision log** unchanged" is a reachable state.

   Recorded is necessary and, since #14, not sufficient. **Every write and every deletion goes through
   `core.confined_to_library`, and uses the path it returns.** It answers `None` unless the name matches
   `LIBRARY_FILE_NAME` — a Wallhaven ID and one short lowercase extension, so a name can never be a
   traversal — and the path, `Path.resolve(strict=False)`d, lies strictly inside the resolved **Library**
   folder. Resolving is the whole point: a symlink or a Windows junction sitting in the folder is inside it
   by every syntactic measure and outside it in fact. Compared through `os.path.normcase` and by path
   component, never by string prefix — `Library2` is not inside `Library`. It **returns the resolved path**
   because that is the path the caller must then use; checking one path and writing another (invariant 10's
   temp file is a sibling of whatever it is handed) would leave the gap the guard exists to close.

   A recorded path that fails the guard is **dropped from `library_files` and left on disk** — the setting
   may have changed, or the row may be hand-edited, and neither is a licence to delete. The row goes so the
   refusal happens once rather than on every submission. On the writer's side, only a **regular file** is
   ever unlinked: a directory, junction or symlink is declined, which is the half of the guarantee only the
   filesystem can answer. The **Library** is still never listed and never read back; `download_favourites`
   asks the disk one question only — does *this recorded path* exist — and never deletes.

10. **Library writes are atomic** — temp file plus `os.replace`. The temp file must be created **in the
    Library folder**, not in `%TEMP%`: `os.replace` is only atomic within one filesystem and raises across
    drives on Windows. Both it and the `os.replace` are siblings of the destination they are given, so the
    destination handed to the writer must already be the confined, resolved path from invariant 9 —
    otherwise the atomic write lands somewhere the guard never looked at.

11. **Rate limit.** Wallhaven's documented 45/min applies to **API calls** (`wallhaven.cc/api`); images come
    from separate hosts (`th.wallhaven.cc`, `w.wallhaven.cc`). Still throttle image fetches modestly — those
    hosts sit behind DDoS protection with no published limits, and a **Batch** of 32 is 32 thumbnails plus
    full-resolution downloads. The rate limiter is a pure "how long must I wait" function over call
    timestamps; the caller does the waiting.

12. **Every wait is cancellable.** Sleeps are `stop_event.wait(n)`, never `time.sleep(n)`, and the httpx2
    timeout is set below the shutdown join timeout. Otherwise shutdown hangs on a thread stuck mid-request.

13. **ONNX Runtime, not open_clip/torch.** Image tower only: 85MiB against ~2.5GB. Shipped at #14 and no
    longer a spike — `EmbeddingSimilarityProvider` is what wallpapi runs (ADR 0013) — so this is now a
    standing rule rather than a budget for a throwaway. `onnxruntime` and `pillow` are ordinary
    dependencies but are imported **inside the methods that use them**: importing onnxruntime costs the
    best part of a second, and the app starts, the page renders and the whole test suite runs without
    needing it. No test may load the model or the network: the `ModelSource` and the `Embedder` are
    injected so that the download, the fallback and the failure are all checkable without either.

    The model is fetched on a background thread of its own, never on a request and never inside the
    refill's loop — 85MiB in front of the refill's first search would hold an empty page empty. Until it
    is there, every pair falls back to `MetadataSimilarityProvider` and the page says so: a **Score** from
    the fallback is indistinguishable from a real one, so the difference has to be stated or it is
    invisible.

### Wallhaven API traps

- `atleast` is the **minimum** resolution. `resolutions` is an **exact-match** list. The **Filters** call for a
  minimum, so it is `atleast`. `ratios` does accept a comma-separated list.
- `ratios` **buckets rather than matching exactly**. A 3440x1440 ultrawide is 2.39 and Wallhaven serves it
  under `21x9`, which is 2.33. A local ratio check that demands equality prunes **Wallpapers** the API
  correctly returned, so the check in `core.py` is a band (`RATIO_TOLERANCE`) around each named ratio.
- `q` takes a search **expression**, not only words. `q=like:<wallhaven id>` asks for the **Wallpapers**
  Wallhaven considers similar to that one — the **Banger** refill at #13. Its result set is small and its
  tail is weakly similar, so it is sorted by `relevance` and the walk through it is capped at
  `LIKE_PAGES_PER_FAVOURITE` rather than paged to the end. `sorting` otherwise defaults to `date_added`,
  which for a `like:` search is the wrong order entirely.
- `meta.seed` is returned on `sorting=random` and is carried between pages to avoid repeats *within one walk*.
  Reusing a seed across refill runs returns the same **Wallpapers**.
- `meta.last_page` is returned but is not a usable stop condition for a random walk: it was 14,055 on the
  captured SFW search. An empty page is the real end of a walk.
- A 429 is the only failure worth telling apart from any other, which is why `wallhaven.py` raises
  `RateLimited` for it and nothing else. `Retry-After` may be seconds or an HTTP date; only the seconds form
  is parsed, and the caller's own back-off covers the rest.
- Search listings return 24 per page. Tags are only on the single-wallpaper endpoint, `GET /api/v1/w/{id}`,
  which is on `wallhaven.cc/api` and so spends the same 45 a minute the refill's searches do — one call per
  **Wallpaper**, which is 45 minutes for a 2,000-strong **Pool**.
- **The 45 a minute is Wallhaven's, counted across everything this machine does.** `wait_needed` counts only
  the calls the process in front of it has made, and starts every process with an empty window — so a second
  process will happily spend all 45 in six seconds on top of whatever the app has already spent, and be
  refused. Seen at #14: 15 searches from one command and 30 tag calls from the next, inside one of
  Wallhaven's minutes, is 45 and the 46th was a 429. Anything that makes **API calls** outside the refill
  thread has to pace itself evenly as well as stay inside the window. Every API response carries
  `x-ratelimit-limit` and `x-ratelimit-remaining`, which are the authoritative count; `wallhaven.py` does not
  read them today.
- No API key is needed: NSFW is what requires one, and purity is fixed to SFW.

### Wiring traps

- The background threads — the **Pool** refill and the **Similarity provider** upkeep — are opt-in via the
  `refill` flag on `create_app`, off by default so that no test starts a thread against the fakes. `build_app`
  in `main.py` is the only caller that turns it on, and nothing tests `build_app` because it builds real clients
  against `~/.wallpapi`. Deleting or mistyping that one line silently stops the **Pool** filling.

## Deferred decisions

Decided, but deliberately not built yet. Defer explicitly; do not quietly forget.

| What | Lands in | Note |
| --- | --- | --- |
| Telling the user a **Library** write failed *at submission* | later | `reconcile_library` collects failures rather than raising — a **Favourite** is recorded whether or not its download worked — and `submit_batch` still discards the report. #14 gave the *asked-for* half a voice: `download_favourites` reports written, skipped and failed, and the settings page says all three. The submit path has no such surface yet. Because the **Library** is derived, the retry needs no UI; saying so does. |
| Caching full-resolution images | never | #8's fullscreen preview loads `full_url` straight from Wallhaven on demand. The only full-resolution files wallpapi keeps are **Favourites** in the **Library** (#5). See `docs/adr/0003-the-preview-loads-full-resolution-from-wallhaven.md`. |
| Restarting a refill thread that has died | later | #6 landed the thread, its clean shutdown and the indicator. There is no supervisor: `refill_step` never raises and `refill_wait` only reads locally, so the loop has nothing to die of short of SQLite being gone — and `refill_status().running` puts that on the page rather than hiding it. Build a supervisor when something is actually seen to kill it. |
| Throttling thumbnail and full-resolution fetches | later | Invariant 11 asks for modest throttling of `th.wallhaven.cc` and `w.wallhaven.cc`, which are not the 45-per-minute hosts. Deliberately not built at #6: those fetches happen on a request thread, which has no `stop_event` to wait on cancellably (invariant 12), and the **Thumbnail cache** already means each tile is fetched once ever. It needs the fetches to move off the request path first. |
| Renaming a **Mix** | later | #12 identifies a **Mix** by its name: `save_mix` upserts, so a new name makes a second **Mix** and the old one stays. A rename would have to move the `active_mix` setting with it in the same transaction — a second write and a second thing to get wrong — for a case delete-and-add already covers in two clicks. **Explore** and **Refine** would have to refuse it outright, which is a fourth refusal reason nobody has asked for. |
| Tuning the **Revisit weight** against a real **Decision log** | later | #11 landed it at 0.2 by reasoning, not by measurement — the same footing as the similarity radius and decay. Nothing is instrumented to say how often a decided **Wallpaper** actually comes round, so the default is a starting point and the settings page is how it gets corrected. |
| **Decision log** backup procedure | later | Cheap because the database is a single file at a known path: `VACUUM INTO`. |
| Evicting a thumbnail the moment a **Clearance** withdraws its **Verdict** | later | #7 evicts at the tail of `submit_batch` only. A **History** edit is a single-row htmx post and a directory scan does not belong on one; the next submission picks the file up. See `docs/adr/0009-thumbnails-are-evicted-by-verdict-with-a-size-cap-behind-it.md`. |
