# 16. A decision is made once, and retires the Wallpaper from the Pool

Date: 2026-10-02

## Status

Accepted. Supersedes ADR 0005's sentence *"Other Verdicts may reappear: an Ignore that removed a Wallpaper
for ever would be an undocumented second Ban"*, and its default `pool_target_size` of 2000. Supersedes ADR
0012 whole. ADR 0007 and ADR 0015 stand unchanged.

## Context

#38 found the **Pool** frozen. Nothing removed a `pool` row when a **Wallpaper** was shown or decided; the
only `DELETE FROM pool` was the **Filter** prune. So once the refill reached its target it idled for good —
`refill_step` returns at target and `refill_wait` rechecks every 30 seconds for ever — and every **Batch**
after that was drawn from the same set. On the maintainer's database: 2012 members against a target of
2000, every one from the random walk, and none from the lookalike walk, which is gated behind the same
check.

ADR 0005 made the target large so there would "always be a backlog to process". The backlog existed, but
nothing ever processed it: it was a fixed set, not a stream.

## Decision

**Decide once.** Every **Verdict**, **Ignore** included, takes a **Wallpaper** out of the **Pool**. A
decision is made once; **History** is the only way to revisit it, and a **History** edit does not put the
**Wallpaper** back.

That is the deliberate reversal of ADR 0005's sentence. An **Ignore** now retires a **Wallpaper**, and it
is documented here rather than undocumented. It is still not a second **Ban**: an **Ignore** spreads −10
and a **Ban** −100, and an **Ignore** can be overturned from **History** like anything else. What the two
now share is only that neither is shown again — "seen once, done".

**The standing rule: the Pool holds no Wallpaper the Decision log mentions.** "Mentions" means any entry at
all, a legacy `cleared` one included: it was decided, whatever it resolves to. One rule, kept at three
points, the same shape as the **Filter** prune in ADR 0005:

- **Submission.** `submit_batch` deletes the `pool` row of every shown **Wallpaper** in the same
  transaction that appends the **Decision log** rows (invariant 6). A half-recorded **Batch** cannot leave
  a decided **Wallpaper** behind.
- **Admission.** `_admit_to_pool` refuses any **Wallpaper** with a **Decision log** entry, in the insert
  itself, so a random or lookalike walk that rediscovers one does not re-admit it. A refused **Wallpaper**
  does not count towards the **Pool**, so the refill keeps going until it finds ones nobody has seen. Asked
  of the log rather than of resolution, because a **Clearance** resolves to the same nothing as a
  **Wallpaper** never seen and only the log can tell them apart.
- **Migration.** Migration 10 deletes the `pool` rows of everything the log already mentions.

**History edits are not an enforcement point.** An edit appends to the log and touches no `pool` row. Through
the UI that can only reach a **Wallpaper** already retired, because **History** lists only **Wallpapers**
with entries. Through the Core service it can also reach a **Pool** member no **Batch** has shown, which
leaves a decided **Wallpaper** in the **Pool** — the one way pre-marking (ADR 0015) is still reachable, and
how its tests reach it.

**Retired Wallpapers keep shaping Scores.** No change to ADR 0007: the decided set is independent of
**Pool** membership, so a retired **Wallpaper** stays a decided column of every **Score**, and its
**Embedding** stays in `embeddings.db`. What retirement removes is only the row that says it may be shown.

**The Revisit weight is removed, and ADR 0012 with it.** With no decided **Wallpaper** in the **Pool** it has
nothing to act on, and a setting that does nothing is a lie on the settings page. The setting, its field and
refusal, `_revisit_weights` and its part in the draw order and the **Banger** ranking are gone. So is
`allocation.weighted_order`, which had no other caller: a **Zone**'s draw order is now a seeded shuffle,
and **Bangers** are sorted by **Score** over it. Migration 10 deletes the `revisit_weight` row.

**Pre-marking stays as ADR 0015 built it**, dormant. Nothing reshows a decided **Wallpaper** now, and it is
ready for when something does.

**`pool_target_size` defaults to 500, not 2000.** The **Pool** is a stream now — each submission retires
what it showed and the refill tops it back up — so it no longer has to be a backlog. A smaller **Pool**
makes every whole-**Pool** operation cheaper on every **Batch**: the **Similarity provider**'s matrix,
**Scoring** and the **Filter** prune are all linear in it. Migration 10 moves a stored 2000 to 500 *only
where it still says 2000*, on migration 9's precedent (`_RETUNE_SETTING`), so a database whose owner chose
a number keeps it. A fresh database gets 500 from the seed.

**Lowering the target never trims the Pool**, from the migration or from the settings page. A **Pool**
above target drains: each submission retires what it showed and the refill stays idle until the **Pool**
is below target. Trimming would throw away **Wallpapers** already fetched, filtered and thumbnailed, and
the **API calls** spent on them, for the chance of finding them again. On the maintainer's database that is
about 1,837 members draining to 500 before the refill resumes. That is accepted.

**No change to the refill loop.** Each submission takes the **Pool** below target, so the existing top-up
runs. A **Batch** retires at most its size: 32 is about two search pages, well inside 45 **API calls** a
minute.

## Consequences

The bug closes from the cause rather than the symptom. A **Pool** at target goes below it at the next
submission, `refill_wait` stops answering `IDLE_RECHECK_SECONDS`, and new **Wallpapers** arrive. The
lookalike walk gets its turns too, once there are **Favourites**.

**Zones** describe undecided **Wallpapers** only. A **Banger** is a prediction about something not yet
shown, never a past **Favourite** scoring its own +100. That is the case ADR 0012 existed to demote, and it
can no longer arise.

A retired **Ignored** **Wallpaper** has no **Explicit Verdict** and is no longer in the **Pool**, so
invariant 8's first pass evicts its thumbnail at the next submission — in practice the same submission that
retired it. **History** re-fetches it on demand. Intended.

**Duds can accumulate.** The default **Mixes** draw 5% **Dud**, so arrivals scored as **Duds** mostly stay
in the **Pool** until a **Shortfall** shows them. The remedy today is to **Ban** or **Ignore** a page of them
and submit, which retires them. Changing what the refill fetches is parked in #52, with showing decided
**Wallpapers** again.

Tests that showed a **Wallpaper** twice to give it a second entry now give it the second entry from
**History**, which is the only way left to make one.

## Alternatives considered

**Retire only Ignores, or only at an Ignore stack of two or more.** The first proposal on #38. Rejected:
it leaves **Explicit Verdicts** in the **Pool**, so the **Revisit weight** keeps a job and a **Favourite**
keeps topping the **Banger** ranking with its own +100. "Decide once" is one rule for every **Verdict**. A
stack threshold also no longer has a meaning, since **Ignores** do not stack (ADR 0015).

**Keep refilling at a trickle at target, evicting the oldest or most-shown members.** Rejected as
unnecessary. Submitting a **Batch** is what makes room, so the refill never has to choose what to throw
out, and nothing has to count how often a **Wallpaper** was shown.

**Enforce the rule on History edits too**, deleting the `pool` row of anything edited. Rejected. Through
the UI there is nothing for it to do, since everything **History** lists is already retired. Through the
seam it would leave pre-marking unreachable by any route at all.

**Trim the Pool to the new target in the migration.** Rejected above: it discards work already paid for,
for the chance of fetching it again.
