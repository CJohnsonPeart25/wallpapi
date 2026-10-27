# 29. A Batch and a Line-up are drawn on each visit

Date: 2026-10-27

## Status

Accepted. Supersedes ADR 0002: a **Batch** no longer persists until it is submitted. What still holds of it
is restated below: a **Batch** is stored before it is shown, storing it is not a **Verdict**, and a page load
costs no **API call**. Amends ADR 0025 and ADR 0026: a visit is a third way past an unanswered **Batch**,
beside a **Skip** and a **Pool reset**, and ADR 0026's "a refresh or a reopened tab still hands back the live
**Batch**" no longer holds. Amends ADR 0023: every visit mints, so the **Unknown**-short line shows on every
visit while the zone is short, not once per **Batch**. Cancels wallpapi-8pa.10's "a star survives a reload
before the screen is answered". ADR 0027 and ADR 0028 stand: a replaced screen is no **Judgement** and adds no
**Wear**.

The user made this decision on 2026-10-27, from #112's review note; it is recorded on wallpapi-8pa.10 and
wallpapi-8pa.15.

## Context

ADR 0002 kept the live **Batch** until it was submitted, and #110 kept the live **Line-up** until it was
answered "by ADR 0002's rule". Each holds its marks against it: the **Draft Batch** (#107) and the stars
(#112).

A **Line-up** is drawn from the **Pool** less the live **Batch**'s tiles, but a **Batch** drawn after a
**Line-up** may show that **Line-up**'s tiles. With both screens kept, that overturns a star:

1. **Line-up** L shows X.
2. A **Batch** is submitted, and the next, B, is drawn. B can show X.
3. X is starred on L and L answered: X is a **Favourite**, and its file is written.
4. B is submitted with X unmarked. The **Ignore** is the latest **Judgement** on X, so it overturns the
   **Favourite**, and the **Library** file is deleted.

The best **Bangers** are on most screens of both kinds, so this was likely rather than rare. The fixes on
offer each touched the draw or the **Batch**: excluding both ways, pre-marking B when a starred answer
commits, or accepting it.

## Decision

**Each visit to the Batches page draws a new Batch, and each visit to the compare tab a new Line-up.** The
screen it replaces is dropped, with its marks, in the transaction that writes its successor, and records
nothing. A reload is a visit, and so is a page the browser restores from its back-forward cache, which sends
no request of its own (`pageshow` with `persisted`).

- **Leaving a screen unanswered is not a Skip.** Only a submission, a **Skip** (itself recorded, at zero
  weight, ADR 0026 and ADR 0027) and a **Line-up** answer are recorded. A replaced screen leaves no
  **Judgement**, so it teaches nothing, retires nothing and adds no **Wear**. Its tiles stay in the **Pool**
  and may be drawn again.
- **Marks live only while their screen does**: the **Draft Batch**'s marks and opened previews, and a
  **Line-up**'s stars. They go with the screen, and the new one starts unmarked.
- **An answer to a replaced screen is refused with words.** The replaced screen's rows are gone, so a
  submission, **Skip**, mark or star posted against it is refused as unknown, through the refusals a skipped
  **Batch** (ADR 0026) and an answered **Line-up** (#110) already give. Two browser tabs on one page cannot
  both submit: the later visit wins.
- **A visit draws; nothing else does.** Marking a tile, marking them all, opening a preview, starring,
  switching the **Mix** or the **Line-up** size and changing the theme never draw. After a submission or a
  **Skip** the page asks for the next **Batch** (`Batches.next`, `/batch/next`): the live one if a refused
  answer left one, so a stale tab catches up without replacing another tab's screen; a new one otherwise,
  which after a successful answer is always. An answer to a **Line-up** is followed the same way.
- **With nothing to draw, a visit still drops the live screen**, and says why there is nothing. A visit ends
  the screen before it either way.

What stands from ADR 0002: a **Batch** is written down before it is shown, because a submission quotes only
its id, the **Draft Batch** hangs on it, and thumbnails resolve through storage; storing it is not a
**Verdict**; and drawing it costs no **API call**, since it samples the **Pool** (ADR 0005). Two requests
drawing at once both write, and the later replaces the earlier, so there is still at most one live screen of
each kind.

**Unchanged**: a submission or an answer draws the next screen as before; a **Line-up** is drawn from the
**Pool** less the live **Batch**'s tiles (ADR 0028); a **Pool reset** discards both live screens (ADR 0025);
a **Skip** is recorded at zero weight; thumbnail eviction keeps the live screens' tiles; the **Batch** draw,
the **Line-up** draw and the refill keep their own random sources.

No migration: the existing tables are cleared when a screen is replaced.

## Consequences

**The accepted risk: Batches and Compare open in two browser tabs at once.** Both screens are then live, so
the same **Wallpaper** can be on both. Starring it on the **Line-up** and answering, then submitting the other
tab's **Batch** with it unmarked, records an **Ignore** that overturns the **Favourite**, as in the Context.
In one tab it cannot happen: visiting the compare tab draws a **Line-up** without the live **Batch**'s tiles,
and a star on a **Line-up** left for the **Batches** page is never recorded, because the next visit to the
compare tab replaces it. The back-forward cache would have reopened the risk in one tab, which is why a
restored page counts as a visit.

**Marks do not survive a reload.** Closing a tab, reloading by accident, or going to **History** and back
loses the marks on the screen. ADR 0002 kept them; the user accepted losing them as part of this decision
(wallpapi-8pa.15). A screen worth keeping is one to answer.

**A reload rerolls, as a Skip does, but records nothing.** The **Unknown** and **Dud** slots are drawn again;
the best **Bangers**, drawn by **Score** (ADR 0026), come straight back on a **Batch** until they are
decided, and on a **Line-up** until **Wear** moves them on, which a replaced **Line-up** does not add. ADR
0002's worry, that a reroll button spends **API calls**, went with ADR 0005.

**Every visit classifies the whole Pool.** ADR 0023 declined to classify on a reload for the **Unknown**-short
line alone. A visit now mints, so it classifies anyway, and the line shows on every visit while the zone is
short. The **Batch** read back after a refused answer still says nothing. One whole-**Pool** similarity call
per visit is what drawing a **Batch** always cost (ADR 0007).

**A new batch size, Mix or Line-up size reaches the next visit**, not only the next answer (ADR 0004). The one
on screen keeps its own until it is left.

**For wallpapi-8pa.11 and wallpapi-8pa.12**: a partial **Order** and a **Gauntlet** streak live only as long
as their screen. A visit to the compare tab starts afresh: the **Order** begun is gone, and so is the
champion and its streak. Whatever either keeps beyond the stars must go with the **Line-up**, in the
transaction that replaces it.

## Alternatives considered

These were laid out on #112 and set aside by the user.

**Exclude both ways**: a **Batch** drawn without the live **Line-up**'s tiles as well. It closes the overlap,
but keeps both screens and their marks live across reloads and tabs, ADR 0002's model with one more exclusion
in the draw, and a **Batch** drawn beside a **Line-up** of **Bangers** goes without them for a reason it does
not show.

**Pre-mark the Batch when a starred answer commits**: ADR 0015's pre-marking, applied after mint. It repairs
the overlap after the fact, writes into a screen's marks from another screen's answer, and still leaves the
same **Wallpaper** on both tabs.

**Accept it, and say so.** A **Favourite** silently overturned, and its file deleted, is the worst outcome
the app has; it is too easy to reach in one tab to be documented rather than prevented.

These were set aside while building it.

**Treat leaving a screen as a Skip.** Every reload would be a **Judgement**, the log would fill with screens
nobody looked at, and "not now" would no longer be something the user chose to say.

**Keep the screen and drop only its marks on a visit.** The overlap is between two live screens, not their
marks, so it would stay.
