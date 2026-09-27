# 7. Scores are derived in one pass over the whole Pool

Date: 2026-09-27

## Status

Accepted

## Context

A **Score** is a **Wallpaper**'s derived value: every resolved **Verdict** in the **Decision log** spread to
**Wallpapers** that look like the one it was given to, fading with distance. **Zones** are the three bands a
**Score** falls into, and a **Batch** is eventually drawn by **Mix** across them (#10).

Invariant 2 says **Scores** are never stored, and it says why: stored **Scores** are a second account of
what the **Decision log** already says, and the two disagree the first time an edit is missed. But a
derived **Score** is only affordable if deriving one is cheap, and it is only cheap if the whole **Pool**
is done at once. A **Pool** is up to 20,000 **Wallpapers**, and a per-**Wallpaper** similarity call is a
Python loop over all of them — seconds per **Batch**, which is exactly the pressure that makes caching
tempting. That is why the **Similarity provider** interface is a matrix, and why `resolve_verdicts` is
plural.

Three further things had to be settled before any of it could be written.

**What the Similarity provider is handed.** It was `similarities(pool_ids, decided_ids)`. The baseline
provider reads a **Wallpaper**'s dominant colours and its category, and neither is in its ID.

**What a Batch records.** A tile has to show the **Zone** it came from. That **Zone** is not a property of
the **Wallpaper** — it changes with every submission — so rendering it means either recomputing the
classification on every page load, including the loads that only swap one tile, or writing down what the
draw actually used.

**Where "no decided Wallpaper within the radius" lives.** The spec gives **Unknown** two causes: a
**Score** of exactly zero, and nothing decided inside the radius.

## Decision

**`CoreService.classify_pool()` is the one operation, and it takes no arguments.** It returns a
`ScoredWallpaper` — **Wallpaper**, **Score**, **Zone** — for every **Pool** member that is not **Banned**,
in **Pool** order. There is no per-**Wallpaper** entry point to reach for, and nothing to invalidate,
because nothing is kept. `get_next_batch` calls it once and draws from what comes back.

One `resolve_verdicts` call covers the **Pool** and the decided set together. Two calls would be two scans
of the **Decision log** for one answer.

**The decided set is every Wallpaper the Decision log mentions whose resolved value is non-zero.** Not
only **Pool** members: a **Favourite** that a **Filter** change pruned still says what the user likes.
**Ignores** are in it — they resolve to -10 a stack, they are mild negatives, and they spread like anything
else. A **Wallpaper** whose resolution comes to zero is not: it would contribute a weighted nothing to
every **Score** and only widen the matrix.

**Bans are excluded from the rows and kept in the columns.** A **Banned** **Wallpaper** is in no **Zone**,
so it is never classified and never drawn. It goes on spreading its -100 to everything like it, which is
half of what banning something means.

**The formula.** `distance = 1 - similarity`; a decided **Wallpaper** contributes
`weight = exp(-decay * distance)` while `distance <= radius` and exactly nothing beyond it; a **Score** is
the sum of `weight * value`. The radius is a cliff and the decay is a slope, and both are settings —
`similarity_radius` 0.5 and `similarity_decay` 4.0 by default, seeded by migration 6 and editable on the
settings page. **Those two numbers are starting points, not tuned values.** Nothing has been measured
against a real **Decision log**; that they are settings is the point.

**Zone is the sign of the Score and nothing else.** **Banger** above zero, **Dud** below, **Unknown** at
exactly zero. The spec's second cause of **Unknown** needs no branch: every decided **Wallpaper** beyond
the radius is weighted at exactly zero, and every member of the decided set has a non-zero value, so
"nothing decided within the radius" *is* a **Score** of exactly 0.0. Stating it as two rules and
implementing it as one is deliberate — the alternative is a branch that can never be false.

Exact zero, never a tolerance. A **Score** that merely rounds to zero has a sign, and that sign is the
answer.

**The Similarity provider takes `Sequence[Wallpaper]` on both sides.** Whole values, not IDs. The
alternative is a provider that goes back to storage for the colours it needs, which is a second seam into
the database opened by the one dependency that is meant to know nothing about it. The fake still keys its
hand-defined values by `(pool.id, decided.id)`, so no test changed shape.

**The baseline provider is colours and category, and nothing outside it may depend on the formula.**
`MetadataSimilarityProvider` bins each **Wallpaper**'s dominant colours into a coarse HSV histogram,
normalises it, and takes the cosine; a matching category is worth a fixed quarter and the colours the rest.
It costs no **API call**, is deterministic, and is one matmul plus one broadcast comparison — no Python
loop over pairs. It is crude on purpose: hard bins mean two near-identical reds either side of a boundary
score nothing against each other, and a palette says nothing about subject or composition. #14's spike is
where that stops being the only option; until then, everything downstream sees a number in `[0, 1]`.

**Migration 6 adds a nullable `zone` column to `batch_wallpapers`, written when the Batch is minted.**
That is not a stored **Score** and not a cache of one. It is the record of which **Zone** the **Wallpaper**
was *drawn from*, which is a fact about the **Batch** rather than about the **Wallpaper** — and it has to
be, because the classification changes the moment the user marks anything, and a label that moved under
them as they worked would be worse than no label. Nullable because a **Batch** minted before this migration
has no such fact; those tiles render without a label rather than claiming a **Zone** nobody put them in.

**The draw stays uniform, in a method of its own.** `_choose` is a uniform random sample that ignores the
**Zones** it was handed. **Allocation** by **Mix** is #10, and `_choose` is the only thing it has to
replace: the classification above it and the write transaction below it are already the shape it needs.

**The Scores are summed elementwise, not with a matrix-vector product.** `weights @ values` is the obvious
spelling and it is wrong here: a BLAS product may reorder its terms and use fused multiply-add, so a
**Wallpaper** equally similar to a **Favourite** and to a **Ban** comes out at a few times 1e-15 rather
than at zero — and the **Zone** rule reads the sign of that. `(weights * values).sum(axis=1)` adds in a
fixed pairwise order, where `w * 100 + w * -100` is exactly 0.0. It costs no extra memory: `weights` is
already **Pool** x decided and is multiplied in place.

## Consequences

Every **Score** on the page is as fresh as the last **Decision log** write, with no restart, no
invalidation and nothing to get stale. #7's **History** edits and **Clearances** get that for free: they
append to the log, and the next classification reads it.

`classify_pool` is linear in the **Pool** and in the decided set, and the decided set grows for ever. At
20,000 **Pool** members and a few thousand decided **Wallpapers** the matrix is tens of millions of
float32 — tens of megabytes, once per **Batch** mint, and not on the path of a page load that has a live
**Batch** or of a tile swap. If it ever stops being affordable, the fix is to narrow the decided set — the
most recent N, or the **Explicit Verdicts** only — and not to cache the result.

A **Zone** on a tile can disagree with what `classify_pool` would say right now, as soon as anything has
been submitted since. That is intended and is the reason the column exists.

Nothing renders a **Score** anywhere. The **Zone** is what the user sees; the number is a means to it.

## Alternatives considered

**Storing Scores, recomputed on submit.** Rejected: invariant 2, and the reason behind it. It is a second
account of the **Decision log**, it needs invalidating from every path that writes one — including #7's
edits, which do not exist yet — and the first missed path is a silent wrong answer rather than a crash.

**Keeping the provider's interface on IDs and letting the Core service pass metadata alongside.** Rejected
as the worst of both: the Core service would have to guess what a provider needs, and #14's embedding
provider needs something different again. A **Wallpaper** is the value both sides already share.

**Recomputing the Zone at render time instead of recording it.** Rejected: it would reclassify the whole
**Pool** on every page load and every tile swap, and the label would change as the user marked the
**Batch** in front of them, because their own marks are what changed it.

**A tolerance around zero for Unknown.** Rejected. It would need a number nobody can justify, and it throws
away the answer in exactly the case where the evidence is finest — a **Wallpaper** the log leans on by a
hair is still leaned on.

**Soft binning in the baseline provider.** A colour spread across its two nearest bins would remove the
boundary artefact, at the cost of index arithmetic nobody outside the provider can check. Rejected for now
because the bins are already coarse and because the honest answer to "this measure is crude" is #14, not a
more elaborate crude measure.
