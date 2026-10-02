# 12. The revisit weight multiplies into the draw order, and into the Banger ranking

Date: 2026-09-27

## Status

Superseded by ADR 0016. A submission now retires every **Wallpaper** it showed from the **Pool**, so
nothing decided is left for the weight to act on, and the setting is removed.

## Context

Any **Wallpaper** may come round again; one the user has already given an **Explicit Verdict** should come
round less often. #10 left the exact seam for it — `CoreService._revisit_weight` returning 1.0 for every
**Wallpaper**, multiplied into `allocation.weighted_order` — so the arithmetic was settled before this
ticket started. What was not settled was four things.

**Which Wallpapers qualify.** "Has an **Explicit Verdict**" is a question about the **Decision log**, and
the log is append-only with **Ignores** that stack and **Clearances** that withdraw (#7).

**How strongly, and where the number lives.** A hard-coded reduction is a number nobody can correct.

**What a Banger slot does.** A **Banger**'s place in a **Batch** is a *ranking* by **Score**, not a draw.
A weight that only tilted the random order would do nothing to it at all — and a **Banger** is precisely
where a **Wallpaper** the user has already judged would otherwise sit at the top of every **Batch** for
ever, because its own **Favourite** scores +100 at distance 0.

**What a weight of nothing means.** "Never again" is easy to say and has an edge: a **Pool** that cannot
fill the **Batch** without the decided **Wallpapers**.

## Decision

**A Wallpaper qualifies when `resolve_verdicts` says its resolved Verdict is present and is not an
Ignore.** Never a second query, and never a rule restated. **Verdict resolution** already knows that
**Ignores** stack into a resolved **Ignore** rather than an **Explicit Verdict**, and that a **Clearance**
makes a **Wallpaper** count as undecided again; a separate "has it been judged" query would have to learn
both and would drift from the first the day either moved (invariant 4, and the triage note on #11). The
question is asked in `_is_explicit`, of an answer resolution has already given.

`_revisit_weights` is plural for the reason `resolve_verdicts` is: one query for the whole classified
**Pool**, never one per **Wallpaper** (invariant 2). It is a second pass over the **Decision log** within
one mint — `classify_pool` made the first — and that is deliberate. The classification is about **Scores**
and this is about how often something is shown; nothing but minting a **Batch** wants both, and folding
the resolution into `ScoredWallpaper` would widen ADR 0007's shape to save one query per **Batch**.

**One setting, `revisit_weight`, a float in `[0, 1]`, default 0.2.** An ordinary `settings` row seeded by
migration 8 and a field on the typed `Settings` (ADR 0004), with a validator and its own refusal reason.
Bounded above at 1 because it is a *reduction*: above 1 it would make a judged **Wallpaper** more likely
than an unjudged one, which is the feature inverted rather than a stronger setting for it. Both ends are
accepted and both mean something — 0 is "never again", 1 is "no reduction". `nan` and `inf` are refused
here rather than left to the draw, because `float()` accepts both and a NaN key sorts a **Zone** by
nothing at all. 0.2 is a starting point arrived at by reasoning, not measurement — the same footing as the
similarity radius and decay, and the same reason it is on the settings page.

**Unknown and Dud slots feel it through the draw order, unchanged from #10.** `weighted_order` is
Efraimidis and Spirakis' `u ** (1 / weight)`, whose prefixes are a weighted sample without replacement at
every length; the weight going in is the setting for a decided **Wallpaper** and 1.0 for every other.
`allocation.py` learns nothing new: which **Wallpaper** is worth how much is the Core service's question,
because it is the only thing holding the **Decision log**.

**Banger slots feel it by multiplying the Score the ranking uses.** `_draw_order` sorts **Bangers** by
`score * weight`, highest first, over the same weighted random order as before. Every **Banger**'s
**Score** is positive by definition, so a factor in `[0, 1]` is a demotion that cannot reorder two
**Wallpapers** carrying the same weight, and a weight of nothing puts one behind every **Banger** there
is. "Highest **Score** first" survives as the rule among comparable **Wallpapers**, which is every
comparison the user could have an opinion about; what changes is that a **Favourite** no longer outranks
everything it taught the **Pool**.

**A weight of nothing means last, not excluded.** A decided **Wallpaper** is put behind every other member
of its **Zone** and is reached only when the **Batch** would otherwise be short. That is the shortfall
rule (ADR 0010) applying to one more thing, and it is the right way round: a **Pool** that has run dry
should show a **Wallpaper** again rather than show a **Batch** with holes in it.

**A Ban is an exclusion, not a weight.** `classify_pool` leaves **Banned** **Wallpapers** out of the draw
entirely, before any weight exists, so no value of this setting can bring one back. Pinned by a test at
every weight including 1.0, where there is no reduction to hide behind.

## Consequences

`_choose` still reads as allocate, order, fill. The weights are computed once for the whole classified
**Pool** and handed to `_draw_order`, so the per-**Wallpaper** method #10 left behind is gone in favour of
a plural one — which is the shape invariant 2 asks for everywhere else.

The **Revisit weight** and the **Score** now both act on a **Banger**'s place, and they are not
commensurable: a **Score** of 300 reduced to 60 still outranks an undecided **Banger** at 50. The weight
biting is therefore a matter of degree in the **Banger** **Zone** and a matter of probability in the
others. That is the price of keeping "**Banger** slots take the highest **Scores**" true, and it is
visible in the tests: the **Banger** test is arranged so that the reduction actually crosses the ranking,
rather than asserting a proportion.

Every test of the weight arranges a **Pool** in which nothing resembles anything, a **Wallpaper** and
itself included. That is what separates this from **Scoring**: a **Favourite** that scored its own +100
would be a **Banger** the moment it was marked, and the thing under test would be tangled up with the
**Zone** it moved into. It also makes repeated draws off one database legitimate, which ADR 0010 warned
they are not — the warning is that submitting writes **Ignores** that move **Zones**, and with nothing
resembling anything no **Verdict** can move a **Zone** at all.

`SeededRandom` gained nothing. The dispatch for #11 allowed for it needing a weighted-sample method; #10's
`weighted_order` already is one, built on `fraction()` and unit-tested under a fixed seed, and a second
spelling of weighted sampling would be a second thing to keep reproducible.

## Alternatives considered

**A cooldown — "not for N Batches" — rather than a weight.** Rejected. It needs a per-**Wallpaper** count
of **Batches** since it was last shown, which is state to store and keep true, and the spec asks for a
single number the user can turn down to nought. A weight needs nothing stored: the **Decision log**
already says whether a **Wallpaper** has been judged.

**Excluding decided Wallpapers outright at a weight of nothing.** Rejected in favour of putting them last.
The only case the two differ in is a **Pool** too small to fill the **Batch**, and there a short **Batch**
is worse than a repeat — the same judgement the shortfall rule already made.

**Leaving Banger slots alone.** Rejected, and it was the tempting option because it is the one that
changes nothing. It would make the setting quietly inapplicable in the **Zone** where repeats are most
likely: **Refine** is 70 per cent **Bangers**, and the highest-scoring **Banger** in a young **Decision
log** is usually a **Favourite** scoring its own +100.

**A weighted choice among the top Bangers instead of a multiplied ranking.** The other option the dispatch
allowed. Rejected as the more complicated of the two for the same effect: it needs a cut-off for "top",
which is a second number nobody can check by eye, and it makes **Banger** slots stop being the plain
"highest **Score** first" that ADR 0010 pinned and the settings page implies.

**Weighting by *how long ago* the Verdict was given.** Rejected as out of scope and as a second setting in
disguise. The **Decision log**'s timestamps are display-only by invariant 4, and resolution orders by
sequence; making the draw read timestamps would give them meaning they are documented not to have.
