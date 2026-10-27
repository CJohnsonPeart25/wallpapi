# 26. A Batch may be skipped

Date: 2026-10-24

## Status

Accepted. Supersedes ADR 0002's "There is no skip" and its rejected alternative "A skip that discards the
Batch without recording anything". The rest of ADR 0002 stood until ADR 0029 superseded it: a refresh or a
reopened tab now draws a new **Batch** and drops the live one unrecorded, which is not a **Skip**, since only
a **Skip** is recorded. Invariant 4 stands: a submitted **Batch** is never retracted. ADR 0016 stands: a skip
decides nothing, so it retires nothing. Amended by ADR 0027: a **Skip** is now recorded, as a **Judgement**
with every **Response** blank, before the **Batch** and its **Draft Batch** are dropped. Its **Reduction**
gives it zero weight, so it still teaches nothing and still retires nothing.

## Context

ADR 0002 refused a skip for two reasons. A skip would be a reroll button, and held down that was one **API
call** per click against Wallhaven's 45 a minute. The other reason was that the only motive for a skip was
finding the decision hard, and **Ignore** already says "none of these".

The first reason went with ADR 0005: a **Batch** is drawn from the **Pool** and costs no **API call**. The
**Pool reset** (ADR 0025) then added code that drops an unsubmitted **Batch** unrecorded, so a skip is that
code on one **Batch** rather than a new mechanism.

The second reason does not hold in use. An **Ignore** is a **Verdict**. It retires the **Wallpaper** for
good (ADR 0016) and spreads its weight to lookalikes. A **Batch** you don't want to judge *now*, because
you're tired or because it is all one subject, needs a way past it that teaches the model nothing.

## Decision

**A Skip drops the live Batch and its Draft Batch, records nothing, and the next Batch is drawn.** The
skipped **Wallpapers** stay in the **Pool** and may be drawn again. It goes behind the same claim as a
submission, so a stale tab cannot skip what another has submitted, and a **Batch** already skipped is
refused as unknown. The banner says so ("no longer live here: it was skipped or reset…").

**One click, no confirmation.** The button sits in the nav beside Submit, styled secondary. The maintainer
chose no confirmation dialog. Rapid skipping is a slot machine only if it costs something, and it now costs
nothing but the marks on the skipped **Batch**.

## Consequences

- **Ignore** and **Skip** are now different answers. "None of these" is an **Ignore**, and the model learns
  from it. "Not now" is a **Skip**, and the model learns nothing.
- Marks are lost on a skip. That is the price of one click.
- **A skip rerolls the Unknown and Dud slots, not the Bangers.** **Bangers** are drawn best **Score** first
  (`allocation._draw_order`), and a skip changes no **Score**, so the top **Bangers** come straight back
  until they are decided. On a **Banger**-heavy **Mix** such as **Refine**, most of the next **Batch** is
  the last one. The maintainer chose this over taking the skipped tiles out of the **Pool**, and over a
  stored marker that keeps them out of the next draw: a **Skip** stays a reroll of the random part and
  touches nothing else.
- The shell refetches on the same `batch-submitted` event after a skip as after a submission.
