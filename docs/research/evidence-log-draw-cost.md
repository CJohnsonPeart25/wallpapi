# What a draw costs as the Evidence log grows

Research for bead `wallpapi-zg3`, 2026-10-28. A recommendation only: nothing under `src/` or `tests/` changed.
Line numbers are at `origin/main` 8f40cfe.

## Question

Every **Batch** and **Line-up** draw goes through `Batches.ranked` (`src/wallpapi/batches.py:267-322`).
Since ADR 0029 every visit to either tab draws
(`docs/adr/0029-a-batch-and-a-line-up-are-drawn-on-each-visit.md:95-98`), and every **Pick**, **Order**
or **Gauntlet** answer draws the next **Line-up** through it too
(`src/wallpapi/lineup.py:247`, wired at `src/wallpapi/compose.py:66`). The review of PR #118 raised two
findings:

- (5) `ranked` reads the whole **Evidence log** twice over. It resolves in SQL (`batches.py:281`), then
  rebuilds every **Judgement** (`batches.py:291`), which re-hashes each one with SHA-256
  (`src/wallpapi/evidence_log.py:291-294`). Then `taste` runs every **Reduction** (`batches.py:292`) and
  resolves the whole log again in Python (`src/wallpapi/taste/ranking.py:62`).
- (7) Pre-marking in `Batches._mint` resolves the whole log a third time with
  `evidence_log.explicitly_decided` (`batches.py:237`), inside `BEGIN IMMEDIATE`, only to filter `resolved`,
  which already holds each chosen tile's standing (`batches.py:236-242`).

How does the cost grow? Where does the time go? Which fixes are worth their risk to Invariants 2, 4 and 14
and the `taste/` boundary?

## Summary of recommendations

**Correction to the question first.** The draw does not just slow down: it stops working. `ranked` passes
every **Pool** member and every decided **Wallpaper** ever recorded as bound parameters to one
`evidence_log.resolve` call (`batches.py:280-281`, `evidence_log.py:364-369`). SQLite allows at most 32,766
(<https://www.sqlite.org/limits.html>, "Maximum Number Of Host Parameters"), and that is this build's limit:
`getlimit(SQLITE_LIMIT_VARIABLE_NUMBER)` returns 32766 on SQLite 3.51.2. Once the **Pool** plus the decided
set passes that number, every draw raises `sqlite3.OperationalError: too many SQL variables` and the page
returns a 500. The comment at `evidence_log.py:364` assumes the call only ever gets a **Pool**. With the
default **Pool** target of 500 the failure starts at about 32,266 decided **Wallpapers**. At the maximum
target of 20,000 (`src/wallpapi/settings.py:40`) it starts at 12,766. In the synthetic mix below that is
about 5,900 **Judgements**. It failed at both 10k and 50k.

In order:

1. **Fix the parameter limit now.** It is a bug, not a speed-up. It can be a small change on its own
   (chunk `resolve`, as `EmbeddingCache.vectors_for` already does at `src/wallpapi/similarity.py:470-483`).
   Option 2 also removes it.
2. **Resolve once, in SQL, and rebuild no Judgement on the draw.** Read the standing of every **Item** and
   its **Wallpaper** row in one query over `_RESOLUTION_CTE`, and hand `rank` one `AbsoluteEvidence` per
   decided **Item**. `taste/` does not change. A prototype gives **bit-identical Scores** at all three
   scales and takes `ranked` from 1.80 s to 1.08 s at 10k and from 10.4 s to 6.0 s at 50k. It removes the
   parameter limit, the SHA-256 work and the Python resolution from the draw.
3. **Derive pre-marking from the chosen tiles, not from the whole log.** Narrow `explicitly_decided` to the
   ids being minted, through the restriction slot `_RESOLUTION_CTE` already has. The write lock is then held
   for about 3 ms at 50k instead of 567 ms (97 ms at 10k). Nothing behaves differently: the derived set
   equals the whole-log set at every scale.
4. **Add an index on `judgements (recorded_at, id)`** for the **Ranking stamp**'s log position (7.9 ms to
   under 0.01 ms at 50k) and the ordered read in `judgements()`. Indexes are a minor lever on resolution
   itself: covering indexes cut the whole-log query by about 20%, and it stays linear.
5. **Verify ids on an audit path.** After option 2 the draw no longer hashes, so this is only about keeping
   tamper detection reachable. `judgements()` keeps its check. It is already the audit path for `evaluate`.
6. **Do not cache Evidence or standing.** It is allowed by the letter of Invariant 2 but against ADR 0007.
   Once option 2 lands it would save about 1 s at 50k, and it would need a different key than the one
   proposed (see below). The remaining cost is the **Pool** x decided matrix. ADR 0007 already names the fix
   for that: narrow the decided set, do not cache.

After options 1 to 3 the floor is the **Pool** x decided similarity matrix: 2.0 s plus 0.66 s of masked
exponential at 50k. That cost is Invariant 2 working as designed, and only a decision about the decided set
reduces it (follow-up 6).

## Measurements

**Machine.** AMD Ryzen 7 9700X (8 cores, 16 threads), 15 GiB RAM, WSL2 on Linux 6.18. Python 3.14.3,
SQLite 3.51.2, numpy 2.5.3. Database files on tmpfs, so the page cache was warm. Windows on NTFS has not
been measured.

**Method.** The throwaway script `bench.py` lived in the session scratchpad and is not in the repo. It builds
`wallpapi.db` with `storage.connect` and `storage.migrate` and appends real `Judgement`s through
`evidence_log.append`. The mix:

- 68% submitted verdict-grid **Batches** of 8 new **Wallpapers**: 82% left unmarked (**Ignore**), 10%
  **Like**, 3% **Favourite**, 3% **Ban**, previews opened on 10%.
- 6% **Skips** of 8 **Pool** members.
- 2% **History** edits.
- 12% **Picks**, 6% **Orders** and 6% **Gauntlets** of 4 **Pool** members, with "none of these" on 10% and
  one tile starred on 3%.

Every grid and **Line-up** **Judgement** carries **Provenance**, a **Ranking stamp** and a theme context, so
rows are as wide as real ones (about 813 bytes of canonical JSON on average). The **Pool** is 500 members
(the default target), the **Batch** size 8 and the **Line-up** size 4 (`settings.py:30-45`).

Embeddings are random unit 512-d vectors in a real `EmbeddingCache` file. `Embeddings` is built with an
embedder and model source that raise if called, and is put in its open (CLIP space) steady state, so no
model is loaded and nothing touches the network.

Each figure is the median of 5 warm runs (3 at 50k) after one warm-up. Where `resolve` fails, `ranked` was
also timed with a 30k-chunked `resolve` patched in, to show the cost it would have if it ran.

| | 1k | 10k | 50k |
|---|---:|---:|---:|
| `judgement_items` rows / `wallpapers` rows | 6.9k / 6.0k | 69k / 55k | 345k / 273k |
| Decided columns in the matrix | 5,529 | 54,281 | 272,372 |
| **`Batches.ranked` as is** | **190 ms** | **fails** | **fails** |
| `Batches.ranked`, `resolve` chunked | 189 ms | 1,798 ms | 10,383 ms |
| `pool.members` | 1.5 | 1.3 | 1.3 |
| `pool.decided` | 17 | 196 | 1,059 |
| `evidence_log.resolve(members + decided)` | 16 | fails (55k params) | fails (273k params) |
| (same, chunked at 30k) | 16 | 183 | 1,056 |
| `evidence_log.judgements()` total | 52 | 591 | 3,557 |
| of which: SQL fetch of both selects | 10 | 136 | 690 |
| of which: `canonical()` JSON | 11 | 133 | 668 |
| of which: `sha256().hexdigest()` | 0.5 | 5.2 | 27 |
| of which: row decoding and dataclass building (the rest) | ~30 | ~317 | ~2,170 |
| `evidence()` (every **Reduction**) | 3.3 | 52 | 362 |
| `taste` resolve and decided sort (inside `rank`) | 0.5 | 7.6 | 117 |
| `near_duplicates` (**Pool** x **Bans**) | 4.6 | 19 | 76 |
| `Embeddings.measure` (**Pool** x decided) | 49 | 351 | 1,989 |
| of which: `vectors_for` (embedding store read) | 19 | 162 | 828 |
| `_scores` (masked exponential, float64) | 11 | 134 | 662 |
| `explicitly_decided()` | 9.6 | 97 | 565 |
| pre-marking block inside `BEGIN IMMEDIATE` | 7.4 | 97 | 567 |
| `resolve(8 chosen)` alone | 0.02 | 0.38 | 3.1 |
| **Prototype: `ranked` resolving once in SQL** | **115 ms** | **1,077 ms** | **6,031 ms** |
| Prototype Scores equal `ranked`'s, bit for bit | yes | yes | yes |
| Whole-log SQL resolution (CTE alone) | 11 | 132 | 735 |
| Newest log position, no index / with `(recorded_at, id)` | 0.08 / 0.01 | 1.4 / 0.00 | 7.9 / 0.00 |
| Peak RSS of the timing process | 222 MB | 1.4 GB | 6.8 GB |

Times are in milliseconds unless marked. Further 50k breakdowns from separate runs:

- The prototype's one standing query, joined to `wallpapers`, takes 1,175 ms, and building 272k
  `Wallpaper`s from its rows takes 443 ms.
- Inside `measure`, `_rows` (`similarity.py:432`) spends 403 ms in a Python loop with one iteration per
  column, and the matmul (`taste/clip.py:93-97`) takes 268 ms. The float32 matrix is 545 MB.
- Covering indexes on `judgement_items (item, judgement_id, favourite, verdict, entry)` and
  `judgements (id, instrument, instrument_version, submitted, recorded_at)`, followed by `ANALYZE`, took
  `explicitly_decided` from 466 ms to 360 ms.

These runs also checked three equivalences at every scale:

- The SQL standing's non-zero **Items** equal `rank`'s decided columns.
- `{w : resolved[w].verdict not in (None, Ignore)}` equals `explicitly_decided()` over every decided id.
- The prototype's Scores equal `ranked`'s.

**Two limits of the synthetic log.**

- *Static Pool.* The **Pool** never turns over, so its 500 members carry every **Skip** and **Line-up**
  appearance. That inflates queries restricted to **Pool** members, such as `resolve(8 chosen)` growing
  from 0.02 ms to 3.1 ms, but not whole-log costs.
- *How much time the sizes stand for.* A real log grows by "hundreds of **Ignores** a week"
  (`evidence_log.py:101-102`). At 300 decided a week the parameter limit is about two years away at the
  default **Pool** target, and 50k **Judgements** is many years. The sandbox could not open the real
  `~/.wallpapi/wallpapi.db`, so its actual size is an open question.

## Cost breakdown

**Growth.** Every part of `ranked` except `pool.members` is linear in the log. The matrix is also linear in
the **Pool**. From 1k to 50k the total grows about 55 times for 50 times the **Judgements**, so the growth is
linear plus allocation overhead. The decided set includes every **Ignore**: they stand at -10 and count
(`taste/verdict_grid.py:48-52`, `taste/ranking.py:63`, ADR 0007 lines 50-54). Since nothing decided returns
to the **Pool** (ADR 0016), the number of columns is roughly every tile ever submitted.

**Where `ranked` spends it** (`batches.py:277-322`), shares at 50k:

- *Three whole-log SQL passes before any ranking.*
  - `pool.decided`, about 10%, is `w.id IN (DECIDED)`, a full scan of `judgement_items` with a temp
    b-tree for `DISTINCT` (`src/wallpapi/pool.py:617-622`, `evidence_log.py:697-702`).
  - `resolve`, about 10%, runs `_RESOLUTION_CTE` restricted by an `IN` list of every decided id
    (`evidence_log.py:355-371`, `711-743`).
  - `judgements()`, about 34%, runs two full selects (`evidence_log.py:656-663`) and decodes them.
- *The second resolution.* `judgements()` rebuilds one `Judgement` per row (`evidence_log.py:264-296`),
  whose `__post_init__` builds the canonical JSON and hashes it (`taste/judgement.py:137`, `145-158`).
  `evidence()` runs every **Reduction** (`taste/reductions.py:33-46`), and `rank` resolves latest-wins
  again (`taste/ranking.py:62-63`, `taste/resolution.py:68-77`).
  - This repeats the SQL resolution of the same **Items** done two lines earlier. ADR 0007 already
    rejected two passes for one answer ("Two calls would be two scans of the **Decision log** for one
    answer", lines 47-48).
  - SHA-256 itself is under 1% of `judgements()` at every scale. The canonical JSON is about 19%, and
    decoding rows into dataclasses most of the rest.
  - CPython releases the GIL only for inputs over 2,047 bytes
    (<https://docs.python.org/3/library/hashlib.html>), so these roughly 813-byte hashes also hold the GIL.
- *The matrix* (`similarity.py:260-279`, `taste/ranking.py:64-100`), about 25% plus 6%.
  - Invariant 2 requires one **Pool** x decided matrix (AGENTS.md:57-59, ADR 0007 lines 42-48). Its cost
    is the floor.
  - Inside it, `vectors_for` reads 273k blobs in 10k chunks (`similarity.py:470-483`), and `_rows` loops
    in Python once per column (`similarity.py:432-445`).

**Query plans** (`EXPLAIN QUERY PLAN` at 10k; reading per <https://www.sqlite.org/eqp.html>, where SCAN
visits every row and "USE TEMP B-TREE" is a sort):

```
resolve, restricted          SEARCH i USING INDEX judgement_items_by_item (item=?)
                             SEARCH j USING INDEX sqlite_autoindex_judgements_1 (id=?)
                             USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY      -- the ROW_NUMBER window
explicitly_decided           SCAN i USING INDEX judgement_items_by_item        -- every item row
                             SEARCH j USING INDEX sqlite_autoindex_judgements_1 (id=?)
                             USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY
pool.decided                 SEARCH w USING INDEX sqlite_autoindex_wallpapers_1 (id=?)
                             LIST SUBQUERY 1: SCAN i; SEARCH j (id=?); CREATE BLOOM FILTER;
                             USE TEMP B-TREE FOR DISTINCT
judgements(): judgements     SCAN judgements; USE TEMP B-TREE FOR ORDER BY
newest log position          SCAN judgements; USE TEMP B-TREE FOR ORDER BY
```

- The restricted `resolve` does use the index on `item`. Its cost comes from being handed every decided id.
- The window orders by `j.recorded_at`, a column of the other table, so no index on `judgement_items` can
  save the sort. The schema (`src/wallpapi/storage.py:276-307`) indexes `judgement_items (item)` and
  nothing on `judgements` but its primary key.

**Pre-marking under the lock** (`batches.py:207-246`).

- `storage.write` opens `BEGIN IMMEDIATE` (`storage.py:40-54`), which "start[s] a new write immediately"
  (<https://www.sqlite.org/lang_transaction.html>). In WAL "there can only be one writer at a time"
  (<https://www.sqlite.org/wal.html>, section 2.2).
- So while `explicitly_decided` runs (97 ms at 10k, 565 ms at 50k), every submission, star, **Draft Batch**
  click and refill admission waits. Each waits up to the connection's busy timeout, which `storage.connect`
  leaves at the five-second default (`storage.py:29-31`; <https://docs.python.org/3/library/sqlite3.html>,
  `connect` `timeout`).
- The work is also nearly always wasted:
  - Pre-marking is dormant, because nothing decided is in the **Pool** (ADR 0016, Status and lines 55-60).
    A tile can only be pre-marked when a **History** edit reaches an undrawn **Pool** member.
  - `resolved`, read in the same transaction, already says the same thing. `_EXPLICITLY_DECIDED` is
    `resolved IS NOT NULL AND resolved != 'ignore'` over the same CTE (`evidence_log.py:785-788`).
  - `standing.verdict is not None and wallpaper_id in explicit` (`batches.py:238-242`) is therefore exactly
    `standing.verdict not in (None, Verdict.IGNORE)`.
  - The CTE's restriction filters `said` before `ROW_NUMBER() OVER (PARTITION BY item ...)`, so restricting
    to the chosen ids cannot change any one **Item**'s answer.

## Options, ranked by gain against risk

### 1. Fix the parameter limit (bug; small gain in speed, large gain in correctness)

- **What.** Chunk `evidence_log.resolve` under the limit, or stop sending decided ids as parameters at all
  (option 2).
- **Risk.**
  - Invariant 4 is unchanged: chunking cannot change a standing, because the window partitions by
    **Item**.
  - A chunked `resolve` makes several statements. Outside a read transaction each sees its own snapshot,
    which is exactly the tear `wallpapi-bb4` fixes.
- **Interaction with bb4.** Land the chunking inside bb4's snapshot, or after it.

### 2. Resolve once, in SQL; build no Judgement on the draw (largest gain, low risk)

**What.** Add one `evidence_log` reader, say `standing(connection)`, that selects from `_RESOLUTION_CTE`
joined to `wallpapers`: each decided **Wallpaper**, its deciding `judgement_id`, `decided_at` and resolved
word. `ranked` would then:

- take candidates and **Bans** from it, in place of `pool.decided` and `resolve`;
- build one `AbsoluteEvidence(item, strength, judgement_id, recorded_at)` per non-zero standing, with
  strengths from `_resolved_from`, which are `taste`'s own (`evidence_log.py:443-451`);
- call `rank` unchanged (its `resolve` over one piece per **Item** is trivial);
- take the stamp's log position from `SELECT recorded_at, id FROM judgements ORDER BY recorded_at DESC, id
  LIMIT 1`, the same **Judgement** `log[-1]` names now (`batches.py:311-313`).

**Gain.** 40% at 10k and 42% at 50k, measured. It removes `pool.decided`, `resolve`, `judgements()`,
`evidence()` and the Python resolution from the draw, and with them the parameter limit and all hashing.

**Risk.**

- *Invariant 4.* SQL becomes the spelling the Scores rest on. It is already held to `taste`'s rule:
  - by `EXAMPLE_CASES` (`tests/test_evidence_log.py:515-531`);
  - and by `test_the_sql_standing_of_every_item_is_tastes_reduction_then_resolution`
    (`tests/test_evidence_log.py:546-634`), which asserts the SQL standing equals
    `resolve(evidence(judgements))`, deciding id and strength, across every **Instrument**.

  ADR 0008 put the rule in one SQL fragment for exactly this reuse (lines 48-56, 72-74). The benchmark adds
  bitwise equality of the Scores.
- *`taste/` boundary and Invariant 14.* Untouched: `rank` still takes `taste`'s data shapes, and the new
  reader takes a connection.
- *Invariant 2.* Untouched: still one matrix, nothing kept.
- *Relative Evidence.* This is the real cost. `rank` ignores relative Evidence in the MVP
  (`taste/ranking.py:55`). When relative Evidence starts to count, `ranked` must again read the
  **Judgements** that give it. That is only the compare-**Screen** ones, about a quarter of the log with 4
  tiles each, so the reader should be shaped to read "absolute standing plus compare **Judgements**", not
  "everything".
- *The stamp.* The ordering must match `_SELECT_JUDGEMENTS`'s last row exactly; the existing
  `test_the_stamp_names_the_newest_judgement_ranking_read` (`tests/test_batches.py:551-567`) holds it.

**Interaction with bb4.** Strongly complementary.

- Today bb4 has to make five reads agree. This option cuts them to `pool.members`, the standing query, the
  newest-position query and `settings.get`.
- It removes both symptoms from the draw: with no `judgements()` read, nothing on the draw can be torn
  between its two selects; and `by_key` and the columns come from one statement, so there is no missing
  column.
- bb4 is still needed so that **Pool** membership, standing and settings come from one snapshot.
- Order: land bb4 first (in progress, P2). Both edit `ranked`, so this ticket should rebase on it.

### 3. Pre-marking reads only the chosen tiles (high gain under the lock, very low risk)

**What.** Give `explicitly_decided` an optional `wallpaper_ids` restriction through the CTE's existing
`{restriction}` slot (`evidence_log.py:727`, as `resolve` uses at `366-368`), and call it with the chosen
ids.

- That keeps "the only spelling of explicit" (`evidence_log.py:435`) in SQL.
- Filtering `resolved` with a new Python test of `verdict not in (None, IGNORE)` would be shorter, but
  would spell "explicit" a second time beside `thumbnails.evict`'s use (`src/wallpapi/thumbnails.py:141`).

**Gain.**

- The write lock is held for the cost of an 8-id restricted query: under 0.5 ms at 10k on a realistic
  **Pool**, and 3 ms in this benchmark's worst case.
- Today it is held for 97 ms (10k) and 565 ms (50k), so every queued writer gains that back.

**Risk.**

- None found in behaviour: equal sets at every scale, and the restriction cannot change a partition.
- Keep it inside the write transaction, as ADR 0015's race comment requires (`batches.py:234-236`).

**Interaction with bb4.** None: this runs in the write transaction, not in `ranked`.

### 4. Index `judgements (recorded_at, id)` (small gain, low risk)

- **Gain.** It removes the temp-b-tree sort from `_SELECT_JUDGEMENTS` (the whole ordered fetch takes
  102 ms at 50k with it) and from the newest-position query in option 2 (7.9 ms to effectively zero).
- **What it does not do.** It does not help the resolution CTE, measured at 563 ms against 565 ms, for the
  reason above. Covering indexes on both tables help that by about 20%, but it stays linear and they cost
  write amplification on every append.
- **Risk.** A numbered migration; no invariant touched.
- **Interaction with bb4.** None.

### 5. Verify ids on an audit path, not every draw (gain subsumed by option 2)

**What.** Keep the check in `judgements()`, which "never trusts a stored id" (`evidence_log.py:261-262`,
held by `tests/test_evidence_log.py:356-363`). Stop calling `judgements()` on the draw, which option 2 does.
Optionally add one whole-log verification at startup on a background thread, or a `verify-log` command
beside `evaluate`, which already reads every **Judgement** through `storage.read_only` (`main.py:152-154`).

**Gain on its own.** If the draw kept `judgements()` but skipped the hash, it would save only the canonical
JSON and SHA-256: about 23% of `judgements()` (11.3 + 0.5 of 52 ms at 1k, 138 of 591 ms at 10k, 695 of
3,557 ms at 50k) and about 7% of `ranked`. Skipping it also needs a `taste` change: the id is computed in
`Judgement.__post_init__` (`taste/judgement.py:137`), and "`id` is derived, never given"
(`taste/judgement.py:98-100`). A trusted-id constructor would weaken that contract for a 7% gain.

**Risk if done instead of option 2.** It opens a path that builds a `Judgement` without earning its id.

**Interaction with bb4.** None.

### 6. Cache Evidence or standing keyed by log position (moderate gain, the most risk)

**Where the line falls.**

- Invariant 2 forbids storing **Scores** (AGENTS.md:57-59).
- ADR 0007 goes further:
  - `classify_pool` has "nothing to invalidate, because nothing is kept" (lines 42-45);
  - storing **Scores** recomputed on submit is rejected (lines 134-136);
  - if the matrix stops being affordable, "the fix is to narrow the decided set ... and not to cache the
    result" (lines 121-125).
- So the following must never be cached, in memory or on disk: a **Score**, a **Zone**, the classified
  **Pool**, the similarity matrix and the weights. They are the result.
- Standing and Evidence are inputs derived from the log, not **Scores**. A process-local cache of them is
  inside the invariant's letter, but against ADR 0007's intent ("nothing to get stale", line 118).
- Embeddings are not derived from the log at all: they are regenerable data from thumbnails (ADR 0013).
  An in-memory cache of decided vectors is clear of the line. It would save some of `vectors_for` (828 ms)
  and `_rows` (403 ms) at 50k, at about 2 KB per decided **Wallpaper** held in memory.

**Why it still ranks last.**

- *Gain.* After option 2, standing costs about 1.2 s at 50k (0.2 s at 10k). That is all a standing cache
  saves, while the 2.7 s matrix floor remains.
- *The key is subtle.* The proposed key, the newest log position (`recorded_at`, id), is not a change
  counter. `recorded_time` only orders **Judgements** per **Item** (`evidence_log.py:299-320`). A clock
  stepped back gives a new **Judgement** on fresh **Items** a time earlier than the newest, so the
  position does not move and the cache goes stale silently.
  - A correct key is `SELECT max(rowid) FROM judgements`.
  - Without `AUTOINCREMENT`, SQLite gives each new row "one larger than the largest ROWID", and reuses
    rowids only after deletes or reaching the maximum rowid (<https://www.sqlite.org/autoinc.html>).
  - `judgements` refuses deletes by trigger (`storage.py:317-330`), so `max(rowid)` strictly increases
    with each append.
- *Threads and snapshots.* There is one connection per thread (`storage.py:73-85`), so a shared cache
  needs a lock. Its key must also be read in the same snapshot as its contents.

**Interaction with bb4.** It depends on bb4: a cache filled from one snapshot and tagged with a key from
another is the tear bb4 exists to stop.

### Beyond the brief: the matrix floor

After options 1 to 3, the **Pool** x decided matrix is about 45% of what remains at 50k (measure 2.0 s
plus `_scores` 0.66 s, of 6.0 s). Its column count grows with every **Ignore**. ADR 0007 already names
the lever: narrow the decided set to "the most recent N, or the **Explicit Verdicts** only" (lines
121-125). That would mean 44k columns instead of 272k at 50k, about 6 times fewer. It changes **Scores**,
so it needs an ADR and an `evaluate` replay. The deferred half-life idea (AGENTS.md, Deferred decisions;
ADR 0028) is the same question. The per-column Python loop in `_rows` (`similarity.py:432-445`) can be
vectorised without touching any invariant.

**On bb4's snapshot length.** If bb4 holds its read transaction across the numpy work, the snapshot stays
open for seconds at large logs. That holds back checkpoints: "a long-running read transaction can prevent a
checkpointer from making progress" (<https://www.sqlite.org/wal.html>, section 2.2). Recommend that the
snapshot covers the reads only, and the matrix runs after it closes.

## Proposed follow-up tickets

Not created as beads.

**1. Bug: every draw fails with "too many SQL variables" once about 32k Wallpapers are decided.**
`Batches.ranked` passes every **Pool** member and every decided **Wallpaper** ever recorded to one
`evidence_log.resolve` call (`batches.py:280-281`). That exceeds SQLite's limit of 32,766 bound parameters
once the **Pool** plus the decided set passes it: at about 32k decided with the default **Pool**, and
12,766 at the maximum **Pool** target. Every **Batch** and **Line-up** then returns a 500. Chunk `resolve`
under the limit as `EmbeddingCache.vectors_for` does, or fold this into ticket 3. Add a test that resolves
more ids than the limit (`getlimit(SQLITE_LIMIT_VARIABLE_NUMBER) + 1`), and fix the comment at
`evidence_log.py:364`. Land it inside or after `wallpapi-bb4`'s snapshot. P1 or P2.

**2. Pre-marking reads the explicit standing of the chosen tiles only.** `Batches._mint` resolves the whole
**Evidence log** under `BEGIN IMMEDIATE` (`evidence_log.explicitly_decided`, `batches.py:237`) to filter
the eight tiles it has just resolved. That holds the write lock for 97 ms at 10k **Judgements** and 565 ms
at 50k, while submissions, stars and the refill wait. Give `explicitly_decided` an optional `wallpaper_ids`
restriction through `_RESOLUTION_CTE`'s restriction slot, keeping the one spelling of "explicit", and call
it with the chosen ids. No behaviour change: add a test that the restricted and whole-log answers agree on
the restricted ids, beside `test_only_a_standing_explicit_verdict_is_explicitly_decided`. P2.

**3. Rank from one SQL standing: no Judgement rebuilt on the draw.** Replace `pool.decided`,
`evidence_log.resolve` and `evidence_log.judgements` + `evidence()` in `Batches.ranked` with one
`evidence_log` reader over `_RESOLUTION_CTE` joined to `wallpapers`. Feed `rank` one `AbsoluteEvidence` per
non-zero standing, with `taste`'s strengths via `_resolved_from`. Read the stamp's log position with one
ordered `LIMIT 1` query. In the benchmark, Scores are bit-identical and `ranked` is 40% faster at 10k
**Judgements**. Rebase on `wallpapi-bb4`. Keep `judgements()` for `evaluate`.

- Test that `ranked`'s Scores equal those from `rank(evidence(judgements()))` over a mixed log, and that
  the stamp is unchanged.
- Note in ADR 0008 or a new ADR that ranking now reads the SQL spelling.
- Shape the reader so compare-**Screen** **Judgements** can be added when relative Evidence counts.

**4. Index judgements by recorded time.** A numbered migration adding an index on
`judgements (recorded_at, id)`, so the newest log position is a lookup rather than a sort and
`judgements()` reads in index order. Measure before and after with `EXPLAIN QUERY PLAN`; no behaviour
change. Small; can ride with ticket 3.

**5. Verify the Evidence log's ids once, off the request path.** After ticket 3 nothing on a request
re-hashes the log, so rows edited behind the triggers are found only by `evaluate`. Add a whole-log
`judgements()` pass at startup on a background thread, reporting through the page's notice line, or a
`verify-log` command run beside the server as `evaluate` is. Decide which; a failure must not stop the app
from serving **History**. P4, optional.

**6. Measure a narrowed decided set against the matrix floor.** The **Pool** x decided matrix is the cost
Invariant 2 accepts, and it grows with every **Ignore** (272k columns at 50k **Judgements**). Use
`evaluate` on a real log to compare the current set against **Explicit Verdicts** only and against the
most recent N, per ADR 0007's own consequence. Record the decision in an ADR. Vectorise `_rows`'s
per-column loop in the same pass. An idea, not a fix: P4.

## Open questions

- **Real size and growth.** How big is the real log, and how fast does it grow? The sandbox could not open
  `~/.wallpapi/wallpapi.db`. One `SELECT COUNT(*)` on `judgements`, `judgement_items` and `wallpapers`
  would place the parameter limit on a calendar.
- **Windows timings.** The maintainer runs on Windows and NTFS; these figures are WSL2 on tmpfs.
- **Relative Evidence.** When does it start to count in `rank`? That decides whether ticket 3's reader
  should read compare **Judgements** from day one.
- **The stamp under a stepped-back clock.** Should `RankingStamp.read` mean "the newest by time" or "every
  row up to here"? When the clock steps back, a later append can be older than `read`. A replay that cuts at
  `read.recorded_at` would then drop a **Judgement** that ranking did read. This matters for `evaluate`
  and replay, not for the draw.
- **`thumbnails.evict`.** It also runs the whole-log `explicitly_decided` after each submission
  (`thumbnails.py:141`). It is off the write lock, but on the same request. Does it belong in ticket 2's
  scope, or on a background thread?
