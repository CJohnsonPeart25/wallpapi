# 25. A Pool reset discards the live Batch

Date: 2026-10-24

## Status

Accepted. Amends ADR 0002: a **Batch** persists until it is submitted *or a **Pool reset** discards it*.
ADR 0002's "there is no skip" stands, and so does invariant 4's "a submitted **Batch** is never retracted".
ADR 0016's "decide once" stands: a reset decides nothing. Amended by ADR 0029, which supersedes ADR 0002: a
visit also replaces the live **Batch** and **Line-up**, unrecorded, so a reset is one of three ways past an
unanswered screen. A reset still discards both, and records nothing.

## Context

Most of the maintainer's **Pool** is the ~2000 **Wallpapers** the random walk pulled at the start of the
project, drawn before the **Decision log** held much taste. The lookalike walk now follows that taste (ADR
0022), but it only reaches the **Pool** as fast as **Batches** are submitted, and a **Pool** that size drains
slowly. Nothing could start the **Pool** again: lowering the **Pool target size** never trims it (ADR 0016),
and the **Filter** prune removes only what a **Filter** excludes.

Emptying the **Pool** alone is not enough. The live **Batch** holds its own rows (ADR 0002), so it would go on
showing old draws, and eviction keeps a thumbnail that `batches.showing` names.

## Decision

**A Pool reset empties the Pool and discards every unsubmitted Batch, with its Draft Batch, in one write
transaction.** After the commit it evicts thumbnails as a submission does: everything not in the **Pool**, not
showing and without an **Explicit Verdict** goes. The refill notices the empty **Pool** on its next idle
recheck and restocks to the **Pool target size**.

- **Nothing is recorded.** An unsubmitted **Batch** has no **Decision log** rows, so discarding it leaves
  nothing dangling, and the **Draft Batch** marks are dropped rather than appended. The `wallpapers` rows,
  the **Embeddings** and the **Library** are untouched; every **Score** carries over.
- **Purged Wallpapers leave no trace** and may be admitted again: the refill refuses only what the
  **Decision log** mentions (ADR 0016).
- **Size stays a setting.** The reset takes no size. To restock smaller, lower the **Pool target size** first,
  then reset; lowered afterwards, it would not trim a **Pool** that had already refilled.
- **It runs beside the server.** Every write takes `BEGIN IMMEDIATE`, the live **Batch** is read from the
  database on every request, and a tab still holding the discarded **Batch** gets the unknown-**Batch**
  refusal it already shows for a second tab.

This is not ADR 0002's rejected skip. A skip discards one **Batch** to draw the next from the same **Pool**,
which is a reroll button. A reset discards the **Batch** because the **Pool** it was drawn from is gone.

## Alternatives considered

**An append-only marker in the Decision log for each purged Wallpaper.** Rejected. The **Decision log** holds
**Verdicts**, and a purge is not one: a marker would either count towards **Scores** or need every reader
taught to skip it. Read as the refill reads the log, it would also keep a purged **Wallpaper** out of the
**Pool** for ever though nobody decided on it, where ADR 0016 retires only what was decided.

**A target size on the reset itself.** Rejected: two places would own the **Pool**'s size, and the setting
would quietly disagree with the reset that last overrode it.

**Keeping the live Batch.** Rejected: its tiles would stay on screen and in the cache, drawn from the **Pool**
the reset was meant to replace, and the first **Batch** after a reset would not come from the new **Pool**.
