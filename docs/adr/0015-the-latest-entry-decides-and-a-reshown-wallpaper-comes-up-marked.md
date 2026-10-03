# 15. The latest entry decides, and a reshown Wallpaper comes up marked with it

Date: 2026-10-02

## Status

Superseded by ADR 0016, under which nothing decided is shown again, so pre-marking is dormant. The resolution
rule — the latest entry decides — stands, as does its supersession of ADR 0008.

## Context

ADR 0008's rule was: the latest entry that is not an **Ignore** decides, an **Explicit Verdict** disregards
every **Ignore** before and after it, and **Ignores** otherwise stack at −10 each. #37 found what that costs.
A **Wallpaper** the user once **Liked** comes round again, the user passes it over, and the **Ignore** that
writes is thrown away — every time. Nothing the **Batch** page can do pushes a **Liked** **Wallpaper** down;
only **History** or a **Ban** can.

The rule disregarded later **Ignores** for a reason: an **Ignore** was the *absence* of a click, and a
**Liked** **Wallpaper** reshown on a blank tile and not clicked again is not obviously a change of mind. The
fault was not in the rule alone but in the blank tile, which made "I already told you" and "not any more"
write the same entry.

## Decision

**A reshown Wallpaper comes up marked with its latest decision.** When a **Batch** is minted, a **Draft
Batch** row is written for every **Wallpaper** in it whose resolved **Verdict** is an **Explicit Verdict**,
wherever that **Verdict** was given — a **Batch** or **History**. Leaving the tile alone records the same
**Verdict** again; unmarking it records an **Ignore**. Rows rather than a fallback in the template, so
invariant 6 is untouched: absence still means **Ignore**, `submit_batch` does not change, the tile shows
exactly what will be recorded, and a reload keeps it. Select-none clears these rows like any others.

**The latest entry decides, and nothing before it counts.** With the tile pre-marked, an **Ignore** after
an **Explicit Verdict** can only be the user unmarking it, so it is trusted to mean what it says. **Ignores
no longer stack**: an **Ignore** resolves to −10 once, however many came before it. Being shown a
**Wallpaper** ten times and passing it over is not ten times the dislike of a **Dud** seen once.

**History withdraws a Verdict with an Ignore, not a Clearance.** The clear control becomes an `ignore`
control beside favourite, like and ban, writing the same entry unmarking a tile does, so the two screens
mean the same thing by the same action. No **Clearance** is written any more. The resolution rule still
reads `cleared` as "nothing stands" for a log that holds one; none had been written when this was decided.

**No migration.** The pre-filled rows go into the existing `draft_batch` table, and `cleared` stays a value
the column may hold.

## Consequences

Every reader of `_RESOLUTION_CTE` sees the new rule with no change of its own — **Scoring**, the **Revisit
weight**, **History** and its filter, thumbnail eviction, **Library reconciliation**. That is ADR 0008's
one-fragment decision paying out.

Unmarking a pre-filled **Favourite** deletes its **Library** file at the next submission, because the
**Library** is derived (ADR 0006) and the **Wallpaper** is no longer a **Favourite**.

A **Wallpaper** overturned to an **Ignore** is no longer an **Explicit Verdict**, so the **Revisit weight**
stops applying to it; its own −10 usually makes it a **Dud**. How often anything is drawn is out of scope
here, as is the **Banger** zone's deterministic ranking (#36).

A **History** edit made while a **Batch** is open does not reach that **Batch**'s **Draft Batch**. The tile
shows what it will record, and submitting it records that, after the edit — the latest entry, so it wins.
Accepted: the tile is where a **Wallpaper** in the open **Batch** is changed.

Applied retroactively, as anything derived is. Checked against the live **Decision log** on 2026-10-02: no
**Wallpaper** had an **Ignore** after an **Explicit Verdict** and no **Clearance** had been written, so
nothing resolves differently.

## Alternatives considered

**Count later Ignores against the Verdict without overturning it** — a **Like** at 50 less 10 per later
**Ignore**. Rejected: on a blank tile it wears a **Like** away by inaction, and it moves the **Score** of
every similar **Wallpaper** with it.

**Count later Ignores only against the Revisit weight** — shown less, scored the same. Rejected once the
tile was pre-marked: an **Ignore** is then a deliberate decision and should be the decision.

**Keep stacking, back to the latest Explicit Verdict.** Rejected by the user: repetition is not intensity.

**Keep the Clearance for History.** Rejected: one action on two screens should write one entry, and "back
to never seen" is a distinction nobody had used.
