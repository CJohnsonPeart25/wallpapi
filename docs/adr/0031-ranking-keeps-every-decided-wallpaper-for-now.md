# 31. Ranking keeps every decided Wallpaper, for now

Date: 2026-11-21

## Status

Accepted. Keeps ADR 0007 as it stands: one pass, one **Pool** x decided matrix, nothing stored, and every
decided **Wallpaper** a column of it. Answers the lever ADR 0007's Consequences name, "narrow the decided
set — the most recent N, or the **Explicit Verdicts** only", with evidence, and bears on ADR 0028's deferred
fading. Recorded on wallpapi-jep.7, research option 6 of `docs/research/evidence-log-draw-cost.md`.

## Context

The matrix is the part of a draw that grows with the decided set, and the decided set grows for ever. ADR
0007 named two ways to narrow it if it ever stopped being affordable. Narrowing changes the **Scores**, so
whether either is worth it is two questions: does it rank as well, and what does it save?

`evaluate --decided all|explicit|recent:N` now answers the first by replaying the **Evidence log** with each
test **Judgement** scored over a narrowed decided set (`taste.evaluation.narrowed`): `explicit` drops every
**Item** standing at an **Ignore**, after latest-wins, so an **Ignore** after a Like takes the **Item** out
rather than leaving its Like standing; `recent:N` keeps the **Items** the N **Judgements** before the test
decided, counting **Judgements** of any kind. `scripts/bench_draw.py --decided` answers the second. Neither
changes what the server does.

**The real log is small.** Triage counted 818 rows in it. They are 818 tiles of the converted log at schema
11, which migration to schema 18 makes **100 Judgements**, all `verdict grid v0`: 90 past the 10 warm-up,
**68 tested** (the rest gave the reading no pair to test). They decided 776 **Wallpapers**, 499 of them
by an **Explicit Verdict**. The replay used a migrated copy, read-only on the original; only these
aggregates leave it.

## Decision

**Ranking keeps every decided Wallpaper.** The server's decided set does not change, and no candidate is
adopted: on 68 tests none ranks measurably better than today's, and today's draw is affordable.

Each candidate replayed at the current settings (radius 0.1, decay 4), with `evaluate`'s 95% range from
2,000 resamples of whole **Judgements**:

| Decided set | Accuracy | 95% range | Difference from today's | 95% range | `evaluate` says |
|---|---:|---:|---:|---:|---|
| every decided (today's) | 0.539 | 0.478 to 0.598 | | | |
| Explicit Verdicts only | 0.559 | 0.503 to 0.614 | +0.020 | +0.001 to +0.042 | candidate better |
| the most recent 50 Judgements | 0.558 | 0.498 to 0.615 | +0.019 | -0.009 to +0.051 | no difference shown |
| the most recent 25 Judgements | 0.544 | 0.494 to 0.590 | +0.005 | -0.043 to +0.053 | no difference shown |

**Explicit Verdicts only clears `evaluate`'s rule, but not by enough to act on.** Its range's low end sits
on zero: under eight other bootstrap seeds it is +0.000 or +0.001 seven times and -0.001 once. Three
candidates were tried, so one clearing a 95% range by chance is more likely than 1 in 20; widened for
three comparisons (98.3%), its range is -0.003 to +0.047, no difference shown. The test **Judgements** are
of one **Instrument**, the converted log, and every accuracy is near 0.5, so the replay barely tells
**Items** apart under any decided set. It stays the candidate to try first.

**Revisit when one current Instrument has about 200 tested Judgements of its own.** `evaluate` compares
per **Instrument**, and the 68 converted tests never grow. Today's ranges put the standard error of a
difference near 0.011 at 68 tests. Showing a difference of +0.02, the size seen here, across three
candidates, four times in five, needs it near 0.006: about three times the tests, so about 200, or some 275
**Judgements** at today's three tested in four past the warm-up. Then rerun the four rows above for that **Instrument**.

## Consequences

**What a narrower set would save.** `scripts/bench_draw.py`, seed 0, median of 5 after one warm-up, in ms,
on the synthetic logs: `68ab74f` with no `--decided`, then this branch (`2b4e200`) with `--decided explicit
recent:25 recent:50 recent:1000`, back to back on 2026-11-21, databases on disk, `free -g` showing 12 GB
available before each. Each candidate's rows are measured in the branch's process after every default row.
The candidate's whole draw is **derived**, not measured: `ranked` - `measure` - `rank` + its narrowing,
`measure` and `rank`. The ranking digest was identical before and after at every size.

| Judgements | 1,000 | 10,000 | 50,000 |
|---|---:|---:|---:|
| `Batches.ranked`, every decided (68ab74f -> branch) | 108 -> 124 | 1,025 -> 1,054 | 6,599 -> 6,323 |
| of which `measure` + `rank` (the matrix and the sum) | 75 | 543 | 2,855 |
| decided columns | 5,529 | 54,281 | 272,372 |
| explicit: narrowing / `measure` / `rank` | 2.7 / 7.0 / 1.1 | 39 / 66 / 19 | 331 / 291 / 123 |
| explicit: columns | 911 | 8,870 | 44,128 |
| explicit: whole draw, derived | 60 | 635 | 4,213 |
| recent:25: narrowing / `measure` / `rank` | 3.3 / 2.3 / 0.17 | 37 / 4.0 / 0.18 | 225 / 2.4 / 0.17 |
| recent:25: columns | 129 | 128 | 137 |
| recent:25: whole draw, derived | 55 | 552 | 3,696 |
| recent:50: narrowing / `measure` / `rank` | 3.7 / 2.9 / 0.35 | 37 / 3.0 / 0.35 | 228 / 3.0 / 0.34 |
| recent:50: columns | 274 | 288 | 265 |
| recent:50: whole draw, derived | 56 | 551 | 3,699 |
| recent:1000: narrowing / `measure` / `rank` | 3.0 / 38 / 13 | 36 / 43 / 14 | 228 / 36 / 11 |
| recent:1000: columns | 5,529 | 5,411 | 5,341 |
| recent:1000: whole draw, derived | 103 | 604 | 3,743 |

The default rows ran the same code twice: 1,000's 108 -> 124 and 50,000's 6,599 -> 6,323 are run to run
noise, 4 to 15%, as ADR 0030 found. At 1,000 the noise is the size of the saving, and recent:1000 narrows
nothing there (its `measure` of 38 against the default's 59 is the same matrix).

- **The matrix floor goes, the rest of the draw stays.** At 50k any narrowed set takes the draw from about
  6.3 s to 3.7 to 4.2 s, 33 to 42%; the 3.5 s left is the SQL standing (2.2 s), the **Pool** and the
  **Ban** neighbours among others, which narrowing does not touch. Below 1,000 decided columns the matrix costs a few ms, so the
  window's length hardly matters: recent:25, recent:50 and recent:1000 all save about 41% at 50k.
- **Explicit Verdicts only saves least**, because it keeps the most columns: 16% of the synthetic log's
  (its mix is **Ignore**-heavy) and 64% of the real log's. Its narrowing here resolves in Python, 331 ms at
  50k; a server would rather filter the SQL standing, as `explicitly_decided` does (573 ms alone at 50k,
  though as a separate query). Either way it is one pass over the standing.
- **The real log is far below any of this.** 776 decided columns is less than the 1,000-**Judgement**
  synthetic log's 5,529, whose whole draw is about 0.1 s. Nothing yet makes the draw unaffordable, which is
  ADR 0007's condition for narrowing.

**ADR 0028's fading.** A most-recent-N window is the bluntest fade: an old **Verdict** counts fully, then
not at all. On these 68 tests, windows of 25 and 50 **Judgements** rank no better and no worse than the
whole log, so there is still nothing to tune a half-life against (`AGENTS.md`, Deferred decisions). What
this does settle is the cost side: a fade that cuts off at some N **Judgements** back costs a draw about
what recent:N costs, and any cut-off up to 1,000 back removes the matrix's cost almost wholly, so a half-life
will be chosen for how it ranks, not for what it saves. No accuracy evidence exists for a window as long as
1,000; recent:1000 is a cost row only.

**What stays.** `evaluate --decided` and `bench_draw --decided` stay, to rerun at the revisit size.
`taste.evaluation` holds the narrowing as Evidence in and Evidence out, so a server adopting a candidate
calls `narrowed` (or its SQL equal) on `standing.evidence()` before `rank`, and ADR 0007's one pass holds.

## Alternatives considered

- **Adopt Explicit Verdicts only now.** It cleared `evaluate`'s rule by +0.001 on one seed and not on
  another, and not at all once the range is widened for the three candidates tried; and it would change
  every **Score** an **Ignore** now pulls down, to save time the real log does not yet spend.
- **Adopt the most recent 50.** The cheapest set at every size, but no accuracy difference shown, and a
  **Ban** older than 50 **Judgements** would stop pulling its neighbours' **Scores** down; only the
  near-duplicate exclusion (ADR 0021) would still read it.
- **Measure on the synthetic logs instead.** Their **Judgements** are random, so they time a draw but say
  nothing about how well it ranks.
