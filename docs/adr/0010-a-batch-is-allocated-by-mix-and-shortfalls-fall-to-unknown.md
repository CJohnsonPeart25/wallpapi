# 10. A Batch is allocated by Mix, and a shortfall falls to Unknown

Date: 2026-09-27

## Status

Accepted. **Partly superseded by ADR 0018**: "there is no best **Unknown**" becomes "the **Unknowns** are
spread one per group". The **Unknown** draw order is no longer only the random order; the allocation, the
**Banger** and **Dud** orders, the **Shortfall** rule and the shuffle before the **Batch** is written all
stand.

## Context

A **Mix** is three **Zone** percentages under a name: **Explore** is 75 **Unknown** / 20 **Banger** / 5
**Dud**, **Refine** is 25 / 70 / 5. **Allocation** turns one into **Slots** for a **Batch**. Until #10 the
draw was a uniform sample that ignored the **Zones** it had just been handed — deliberately, and in a
method of its own so that this ticket had exactly one thing to replace (ADR 0007).

Four things had to be settled before it could be.

**Where the Mixes live.** Two **Mixes** are named in the spec and nothing writes a third, so a pair of
module constants would do everything this ticket needs.

**What happens to the slots that do not divide.** Seventy-five per cent of two is one and a half. Whole
**Wallpapers** are the only kind there are.

**What happens when a Zone cannot fill its slots.** This is not an edge case; it is the first run. A
**Decision log** with nothing in it has no **Bangers** at all, so **Explore**'s twenty per cent has
nowhere to come from on every **Batch** until something is **Favourited**.

**What a Banger slot should pick, and what the others should.** "Favour the highest **Scores**" is a total
order with ties in it, and ties broken by whatever the **Pool** query returned would be the same tiles for
ever.

## Decision

**Mixes are stored rows, seeded by migration 7, not constants.** A `mixes` table of `name` and the three
percentages, with **Explore** and **Refine** inserted, and `active_mix` as an ordinary `settings` row
naming one of them. #12 makes **Mixes** editable and lets the user add their own, and the difference
between "a constant that #12 replaces with a table" and "a table #12 starts writing to" is a migration
against live **Batches** versus a new endpoint. One `INSERT` now is cheaper than that later.

One column per **Zone**, not a row per share. A **Mix** is three numbers that have to be read together and
mean nothing apart — the sum is the invariant — and a narrow table would let a **Zone** simply be missing,
which is a shape `Mix` cannot express.

No CHECK constraint on the sum. `core.validated_mix` is the one place that decides what a **Mix** is, and
it has to exist anyway for #12's form; a second statement of the rule in SQL is a second thing to keep in
agreement, and a row that breaks it would make the table unreadable rather than merely wrong. `list_mixes`
drops a row that is not a **Mix** instead of offering the switcher something the draw would then have to
make sense of.

**`active_mix` on `Settings` is the name, not the Mix.** The one place that typed view is deliberately not
fully typed. Resolving it would put a second query inside `get_settings`, which is read on the refill loop
and inside write transactions, and it would give that view a new way to fail — a name whose **Mix** was
deleted has no typed value to stand for it, and `get_settings` never refuses. `CoreService.active_mix()`
is the resolved one and falls back to the first **Mix** there is, because a **Batch** page that cannot
mint a **Batch** is worth more than insisting a deleted **Mix** still exists.

**Allocation is a pure function, and the leftover slots are rolled unbiased.** `allocate(mix, size,
random)` guarantees `floor(percentage * size / 100)` **Slots** per **Zone** and rolls the rest one at a
time, each roll weighted by the fractional remainders *as they were before any rolling*. That keeps the
expected share exactly `percentage * size / 100`: the remainders sum to the number of leftover **Slots**,
so `leftover * remainder / sum(remainders)` is `remainder`. A **Batch** of 32 in **Explore** is 24 / 6 / 1
guaranteed with the thirty-second **Slot** rolled at 0 / 40 / 60; a **Batch** of 2 is one **Unknown** and
a roll at 50 / 40 / 10.

The arithmetic is `divmod(percentage * size, 100)` in integers. The obvious spelling floors a float, and
the obvious worry about it is a product that ought to divide exactly coming out at 23.999999999999996. It
does not — a correctly rounded division of two exact integers lands on the integer when there is one — but
"it happens to be exact" is a worse thing to depend on than not dividing at all.

**A shortfall fills Unknown, then Banger, then Dud.** In that order, fixed, and the same order the three
**Zones** are visited in. It reads as a preference for discovery, and it has a second effect worth stating:
with no **Bangers** at all a **Batch** falls back to being entirely **Unknown** rather than being padded
with **Duds**. **Dud** is last because a **Wallpaper** the **Decision log** leans against is the one thing
the user has already said something about. If the whole **Pool** cannot fill the size, the **Batch** is
simply smaller — which was already true of the uniform draw.

**The Zone recorded against the Batch is the Wallpaper's own, never the Slot's.** A shortfall means a
**Slot** the **Mix** asked one **Zone** for was filled from another, and what goes on the tile is where
the **Wallpaper** came from. An **Unknown** shown because there were no **Bangers** to be had is an
**Unknown**; labelling it **Banger** for the **Slot** it occupies would be the page telling the user
something the **Decision log** does not say. It also means the **Zones** on a **Batch** need not match the
**Mix**, and that mismatch is the visible, honest sign that a **Zone** ran out.

**Each Zone is put into a draw order, and the slots are taken off the front of it.** Not sampled — ordered.
`weighted_order` gives a weighted random permutation (Efraimidis and Spirakis' `u ** (1 / weight)`, sorted
descending), and **Banger** is then stably sorted by **Score**, highest first, over that random order —
which is how ties are broken by the random source rather than by the **Pool** query, while staying
reproducible under the seed. **Unknown** and **Dud** keep the random order: there is no "best" **Unknown**,
and preferring the least negative **Dud** would be ranking by a number the user is not shown.

Ordering rather than sampling is what makes the shortfall rule cheap and safe. A **Zone** asked for more
than it was allocated gives up the next ones in an order it already has, so "take k" and "take k more" are
the same operation and no **Wallpaper** can come out of a **Zone** twice.

**The weight is 1.0 for every Wallpaper, and that is the seam for #11.** At 1.0 the key is the uniform
itself and `weighted_order` is exactly a shuffle. The revisit weight multiplies into
`CoreService._revisit_weight` and nothing above or below it moves.

**The chosen Wallpapers are shuffled before the Batch is written.** Otherwise the grid is every **Unknown**
first and the **Bangers** last, and the highest-scoring **Banger** is the same tile every time. The
**Zone** is on the tile as a label; it should not also be readable off the position.

**The switcher applies to the next Batch minted.** The **Mix** is read when a **Batch** is minted, exactly
as the batch size has been since #4. `POST /mix` swaps back the switcher alone and never the grid: a
switch that rerolled the live **Batch** would throw away a **Draft Batch** the user was part way through,
which is a far worse surprise than a **Batch** finishing under the **Mix** it started in.

## Consequences

`_choose` is now three steps a reader can check separately — allocate, order, fill — and two of the three
are pure functions in `allocation.py` with no **Pool**, no storage and no **Decision log** anywhere near
them. That is what lets the arithmetic be pinned over thousands of seeded rolls in milliseconds, and it is
why the seam tests only have to pin what the **Pool** brings with it.

A **Zone** cannot be set from outside the Core service — it is the sign of a derived **Score** — so every
test of the draw needs a **Pool** whose **Zones** it chose. `tests/zoned.py` arranges one **Favourite**
and one **Ban**, then moves the **Filters** so that the two seeds spread their values into a **Pool** they
are no longer part of. It leans on ADR 0007's decision that the decided set is not restricted to **Pool**
members; if that ever narrows, this arrangement goes with it.

Long-run **Zone** proportions cannot be read off repeated **Batches** on one database. Submitting is the
only way to draw again, and it writes an **Ignore** against every **Wallpaper** it showed — a **Verdict**,
which moves the very **Zones** being counted. The test arranges a fresh **Pool** per draw instead, which
costs about nine milliseconds each.

A **Batch** drawn from a **Pool** with no **Bangers** is indistinguishable from one drawn under a **Mix**
of 100 **Unknown**. That is correct and it is not visible on the page: the **Mix** says what was asked for
and the tiles say what was found, and the two disagreeing is the information.

## Alternatives considered

**Mixes as module constants, with a table at #12.** Rejected. It is the same table written twice — once as
a migration and once as a constant deleted — and the migration would land on databases already holding
**Batches** and a `settings` row naming a **Mix** that had never been stored.

**Striking a Zone's remainder out once it has won a leftover slot.** The largest-remainder method's
behaviour: it bounds each **Zone** at `ceil(percentage * size / 100)` so no **Zone** can take two spare
**Slots**. Rejected in favour of the unbiased roll. A **Zone** over-drawn by one **Slot** is corrected by
the very next **Batch**, there is no case where that extra **Slot** is *wrong* rather than merely
unlikely, and with three **Zones** at most two **Slots** are ever rolled — so the difference is close to
moot and the unbiased rule is the one that can be stated in a sentence.

**Filling a shortfall in proportion to the Mix's other two Zones.** Rejected: it needs a second
normalisation nobody can check by eye, and it gets the first run wrong — a new **Decision log** would
split its missing **Bangers** between **Unknown** and **Dud** at 75:5, which is showing the user
**Wallpapers** the log leans against before it has anything to lean on.

**Recording the Slot's Zone rather than the Wallpaper's.** Rejected. It would make every **Batch** match
its **Mix** on paper and lie on the tile, and the case where it lies most is the case that matters most —
the empty **Banger** **Zone** of a **Decision log** with nothing in it.

**Sampling each Zone with `random.sample` instead of ordering it.** Rejected: a shortfall would then be a
second sample from the same **Zone** with the first one's members excluded, which is the same thing said
twice and one exclusion away from a duplicate tile.

**Switching the Mix rerolling the Batch on screen.** Rejected for the reason #4 rejected it for the batch
size: the **Draft Batch** is the user's work, and a control that silently discards it is worse than one
that takes effect a **Batch** later. The switcher says "applies to the next batch" on the page.
