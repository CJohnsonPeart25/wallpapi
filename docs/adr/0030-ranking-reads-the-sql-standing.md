# 30. Ranking reads the SQL standing

Date: 2026-11-17

## Status

Accepted. Builds on ADR 0008's one rule that still stands, that resolution lives in one SQL fragment; ADR 0008
itself is unchanged. ADR 0007 stands: one pass, one Pool x decided matrix, nothing stored. ADR 0027 stands:
`evidence_log` is still the only module that reads `judgements` and `judgement_items`, and it now reads
`wallpapers` as well, a table `pool` owns. Recorded on wallpapi-jep.4, research option 2 of
`docs/research/evidence-log-draw-cost.md`.

## Context

Each draw resolved the **Evidence log** twice to get one answer. `Batches.ranked` read the decided
**Wallpapers** in SQL (`pool.decided`), resolved them and the **Pool** in SQL (`evidence_log.resolve`), then
rebuilt every **Judgement** in Python (`evidence_log.judgements()`, which hashes each one again), ran every
**Reduction** (`taste.reductions.evidence`) and resolved again inside `taste.ranking.rank`. ADR 0007 already
rejects two passes for one answer. The research measured the draw's cost at 10k and 50k **Judgements** and
found the Python rebuild the largest part of it.

`_RESOLUTION_CTE` already gives each decided **Item** its deciding **Judgement**, the time it was decided and
the resolved word, and the tests already hold it to `taste`'s rule: `EXAMPLE_CASES`, and
`test_the_sql_standing_of_every_item_is_tastes_reduction_then_resolution` across every **Instrument**.

## Decision

**Ranking reads one SQL standing and rebuilds no Judgement.** `evidence_log.standing(connection,
wallpaper_from_row)` returns a `Standing`: every decided **Wallpaper** whole, in id order, with what it stands
at, the **Judgement** that decided it and when (`DecidedWallpaper`), and the position of the newest
**Judgement**. It runs two statements: `_STANDING`, which is `_RESOLUTION_CTE` joined to `wallpapers`, and
`_NEWEST`, which orders `judgements` by `recorded_at DESC, id` and takes one row: the **Judgement**
`judgements()[-1]` names, a **Skip** included, the lower id on an exact tie. `ranked` reads `pool.members`,
`standing` and `settings.get` in one `storage.read` snapshot. It then:

- takes the **Bans** from the standing, for the near-duplicate exclusion (ADR 0021);
- builds `by_key` from the candidates and the standing;
- passes `rank` `standing.evidence()`, one `AbsoluteEvidence` per **Wallpaper** standing at a non-zero value,
  with `_resolved_from`'s strengths, which are `taste`'s own, and calls `rank` unchanged;
- takes the **Ranking stamp**'s log position from `standing.newest`.

`taste/` does not change, and the Scores are bit-identical (the benchmark's digest, below).

**The join to `wallpapers` is an inner join.** The trigger `judgement_items_name_a_known_wallpaper`
(`storage.py`, made with the **Evidence log**) refuses any `wallhaven:` **Item** that has no `wallpapers` row,
and no statement in `src/` deletes a `wallpapers` row. So the inner join loses no decided **Item**, and the
standing holds exactly the set `pool.decided` gave. The two paths would part only if a `wallpapers` row could
go: the old path then raised `KeyError` building the similarity (`by_key` had no entry for a column), while
the inner join would drop the column without a word. Whatever first deletes a `wallpapers` row reopens this
choice.

**The draw no longer verifies Judgement ids, nor raises `UnknownInstrument`.** Both ran inside
`judgements()` and `evidence()`, which the draw no longer calls. `evaluate` (`main.py`, `_evaluate`) still
calls `judgements()` and `taste`'s **Reductions**, so it still runs both. Where the id check runs once
nothing on a request runs it is wallpapi-jep.6, since landed as the `verify-log` command
(`uv run python -m wallpapi verify-log`).

**`Standing` is the object relative Evidence joins later.** `rank` ignores **Relative evidence** today
(`taste/ranking.py`), so the standing carries none. When the evaluation shows it helps, the compare-screen
**Judgements** become one more field of `Standing`, read by the same function, and `evidence()` hands their
**Reductions** on beside the absolute pieces. `ranked` then changes only where it passes them to `rank`.
Nothing is built for this now: no field, no query, no adapter.

**The decoder is handed to `standing`.** `pool.wallpaper_from_row` turns a `wallpapers` row into a
**Wallpaper**, and `pool` imports `evidence_log` (it builds on `evidence_log.DECIDED`), so `evidence_log`
cannot import `pool` back. `standing` takes the decoder as its one collaborator, typed
`Callable[[sqlite3.Row], Wallpaper]`, and `pool.wallpaper_from_row` is the only argument it is ever given.
This keeps every statement that executes `_RESOLUTION_CTE`, and `_resolved_from`, inside `evidence_log`,
where ADR 0008 put them. The options set aside are below.

## Consequences

**What it bought.** `scripts/bench_draw.py`, seed 0, median of 5 after one warm-up, in ms, from `main`
(`71d8843`) to this decision's code (`deaa59d`, whose `src` is `55587c6`'s), with wallpapi-jep.2 and jep.3
inside it. One run per commit, back to back on 2026-11-17, with the databases on disk and `free -g` showing
11 GB available before each:

| Judgements | 1,000 | 10,000 | 50,000 |
|---|---:|---:|---:|
| `Batches.ranked` | 178 -> 126 | 1,822\* -> 1,237 | 11,146\* -> 6,835 |
| gain on `ranked` | 30% | 32% | 39% |
| `Batches.fresh` | 214 -> 149 | 1,984\* -> 1,221 | 11,635\* -> 6,754 |
| peak RSS, MB | 237 -> 214 | 1,447 -> 1,280 | 6,642 -> 6,088 |
| ranking digest | identical | identical | identical |

\* `main` fails as is ("too many SQL variables", wallpapi-jep.2): timed with `resolve` chunked at 30,000.

Runs of the same `ranked` code in different sessions differ by 6 to 7% at 50k, and no cause is known:
10,384 ms at `7c601bc` (2026-11-16) against 11,146 ms at `main` above, whose `ranked` differs only in how
`resolve` chunks; and 7,246 ms at `55587c6` (2026-11-17, an hour before, 10 GB available) against
6,835 ms at `deaa59d` above, the same `src`. Both earlier runs had their databases in `/tmp`; free memory was
not recorded at the first. So a gain is taken here only between runs of one session.

The draw binds no ids, so SQLite's parameter limit no longer reaches ranking, and no **Judgement** is hashed
on a request.

**What it cost.** The gain falls short of the research prototype's, each taken within its own session: 32%
and 39% at 10k and 50k here, against 40% and 42% in the research's run (1,798 -> 1,077 and 10,383 -> 6,031
ms, `docs/research/evidence-log-draw-cost.md`). At 50k the 3 points come to about 0.36 s of the 11,146 ms
above. Side by side, 6,835 against 6,031 ms is 0.8 s, but that pairs runs from different sessions, and 6 to 7%
of 6,031 ms is about 0.4 s. A probe of `standing` at `55587c6` on the benchmark's databases (median of 5 after
a warm-up, in ms) splits it into its SQL and its decoding. The probe ran on 2026-11-17, minutes after the
7,246 ms run, with the databases in `/tmp`, and has not been repeated, so its split is the only one there is;
the run above times `standing` whole at 435 and 2,562 ms at 10k and 50k, against the probe's 430 and 2,374.

| Part of `standing` | 10,000 (54,281 decided) | 50,000 (272,372 decided) |
|---|---:|---:|
| whole | 430 | 2,374 |
| `_STANDING`, `fetchall` | 219 | 1,250 |
| `_NEWEST` | 1.4 | 7.5 |
| decode: `pool.wallpaper_from_row` | 90 | 493 |
| decode: `_resolved_from` | 37 | 215 |
| decode: `decided_at` and the id | 12 | 83 |
| `Standing.evidence()` | 24 | 132 |

- **Common to the prototype, so no part of the shortfall.** The SQL costs what the research measured for the
  joined query (1,250 ms at 50k against 1,175 ms). Decoding the `Wallpaper`s, 493 ms here, the prototype paid
  too (443 ms for 272k, the research note), as the old path paid it inside `pool.decided`.
- **What this reader adds over the prototype: about 0.6 to 0.75 s at 50k, in the probe's session.**
  `_resolved_from` 215 ms, a new `ResolvedVerdict` per row for a handful of distinct words (reusing them is
  wallpapi-j9a); the time and the id 83 ms; and about 325 ms building the `DecidedWallpaper`s and their tuple,
  which is the whole less the SQL and the measured decode parts.
- **Not split.** The shortfall at 50k, about 0.35 s once runs are taken within a session, is smaller than the
  reader's own 0.6 to 0.75 s, measured in a third session, and moving between sessions shifts a 50k figure by
  about as much as the shortfall itself. Each commit was run once, and `rank(standing.evidence())` alone reads
  1,189, 985 and 1,278 ms at 50k in the runs of `deaa59d`, `0c4a89f` and `121146e`, between which its code
  does not change: it moves by 0.3 s. The runs place no remainder and size none.

`_NEWEST` costs 7.5 ms at 50k without an index on `recorded_at` (wallpapi-jep.5).

**Invariant 4 now carries the Scores on the SQL spelling.** A change to `_RESOLUTION_CTE` changes every
**Score**, not only **History**. The tests that hold it to `taste`'s rule are unchanged, and
`test_ranking_scores_the_pool_exactly_as_taste_ranks_every_judgement_in_the_log` checks the Scores against
`rank(evidence(judgements()))` over every **Instrument**.

**`pool.decided` has no caller in `src/`.** wallpapi-5ug deleted it and replaced it in the tests
with `decided_wallpapers` (`tests/conftest.py`), an oracle that reads the log's tables in its own SQL and
shares no rule with `evidence_log`.

## Alternatives considered

**Move the decoder into a module both can import** (`model`, or a new module owning `wallpapers`). `model`
holds plain dataclasses and no `sqlite3`, and the move re-points its callers in `batches` and `lineup`, which
this decision does not otherwise touch. Its own ticket if a second caller outside ranking needs
**Wallpapers** joined to standings: then a module owning the `wallpapers` table beats a third hand-off.

**Return ids and standings only, and fetch the Wallpapers in a second query.** A second scan of the decided
set, against the cost this decision exists to cut.

**`evidence_log` exports a fragment, and `pool` owns the joined reader**, as `pool.decided` builds on
`DECIDED`. `pool` would become the first module outside `evidence_log` to execute `_RESOLUTION_CTE`, and would
need `_resolved_from`, which is private so the pricing is never copied. `DECIDED` is a membership test, not
resolution, so it is no precedent.

**Amend ADR 0008 instead.** ADR 0008 is superseded except for one rule, and the footing of the Scores would be
hidden in it.
