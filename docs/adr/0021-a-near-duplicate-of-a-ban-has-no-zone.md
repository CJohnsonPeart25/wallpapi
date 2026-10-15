# 21. A near-duplicate of a Ban has no Zone

Date: 2026-10-06

## Status

Accepted. Amends ADR 0007: "**Bans** are excluded from the rows" now covers their near-duplicates too.
The −100 spread at the **Similarity radius**, the **Score** formula and the **Zone** rule are unchanged.

## Context

A **Ban** spreads −100 additively (ADR 0007). A dense positive neighbourhood outweighs it, so a repost of a
banned image can still come up as a **Banger**. Reposts are real: Wallhaven holds the same picture under
more than one ID, and the real **Pool** held one on 2026-10-06. `e7q2zw` is an identical repost of the
**Banned** `xlwkxz`.

A veto across the whole radius would catch it, but it would punish a style. At 0.15 a radius covers a whole
style, and a **Ban** is often about one image's flaw that its neighbours do not share. What is wanted is a
veto for the same image only.

## Decision

**A Pool member at or above `NEAR_DUPLICATE_SIMILARITY` against any Banned Wallpaper is left out of
classification**, so it has no **Zone** and is never drawn, exactly as the **Ban** itself is not.
`Embeddings.near_duplicates(pool, banned)` answers one `len(pool)` x `len(banned)` mask (invariant 2), and
`Batches.classify` drops every candidate whose row has a True before scoring. "Banned" is the resolved
**Verdict**, so a **History** edit away from **Ban** lifts the veto at the next mint. Nothing is stored and
the member stays in the **Pool**, so there is nothing to invalidate.

**The threshold is a mapped similarity, `(1 + cos) / 2 >= 0.985`.** It is in the radius's units and is a
module constant in `similarity.py`, not a setting: it is measured, and a user has nothing to tune it
against.

**Only two Embeddings can make a near-duplicate.** A pair where either side has no **Embedding** never
triggers the veto, however high the metadata fallback scores it. Colours and category put flat-colour
images at 1.0 against each other. An unembedded member is vetoed at the first mint after it is embedded,
and ADR 0017 embeds the whole **Pool**.

**Reposts, not crops.** A near-duplicate is the same image re-uploaded, rescaled or re-encoded. Crops are
not promised: a real crop usually falls below 0.985, and lowering the threshold to reach crops would take
near-empty images with it. Crops stay with the ordinary spread.

## The measurement

Read-only, over `~/.wallpapi` on 2026-10-06: 2,014 **Wallpapers** with an **Embedding**, every pair
compared, and the thumbnails of the top pairs looked at side by side. The script was throwaway and is not
committed. Triage measured the same sample on 2026-09-30 and got the same figures.

| Mapped similarity | Pair | Same image? |
| --- | --- | --- |
| 0.9927 | `0wj2mp` / `ner79o` | Yes, identical (both in the **Pool**) |
| 0.9899 | `e7q2zw` / `xlwkxz` | Yes, identical (a **Pool** member and a **Ban**) |
| **0.985** | | **the threshold** |
| 0.9837 | `9oxg2d` / `0q1p97` | No: two near-black fields, one grainy, one flat grey |
| 0.9790 | `rqqvo7` / `v96o3l` | No: black, a different small figure at the right edge of each |
| 0.9777 | `wq7p5r` / `0q1p97` | No: near-black with a figure at the edge, against flat grey |
| 0.9757 | `8o516j` / `m9l8wk` | No: two different dark liquid-swirl abstracts |

Two pairs lie above the threshold, and both are duplicates. Everything below it that was looked at is a
different image. Near-empty, flat-colour and dark images cluster just under it. The margin is thin: 0.0049
above the nearest non-duplicate (0.9837), 0.0049 below the lowest duplicate (0.9899). 0.985 sits in the
middle of that gap, so it is kept. Triage could not view `9oxg2d`, whose thumbnail had been evicted; it was
fetched from Wallhaven's image host for this look, and it is not a duplicate.

Of the **Pool**'s 1,322 embedded members against the 407 embedded **Bans**, the veto removes one member
today: `e7q2zw`.

### Between the threshold and the radius

The ticket's 2026-09-30 comment asks for these, because they decide the separate question of how far a
**Ban** should spread. With reposts vetoed, the −100 spread only carries likeness of style. The figures
below are for the default radius of 0.15, so a mapped similarity of 0.85 and up.

| Band | All pairs | **Pool** x **Ban** pairs | **Pool** members reached |
| --- | --- | --- | --- |
| 0.950 to 0.985 | 126 | 22 | 16 |
| 0.900 to 0.950 | 9,414 | 1,920 | 561 |
| 0.850 to 0.900 | 125,920 | 26,505 | 1,275 |

Sampled **Pool** x **Ban** pairs, looked at:

- 0.95 to 0.985 is shared composition, not a shared image. Examples: two flat colour gradients
  (`j5jemw` / `g8p3zq`, 0.9574), two different anime figures on white (`e72rpl` / `x8dkxl`, 0.9508), and
  dark frames with a figure at the edge.
- 0.90 to 0.95 is a broad look. Examples: two dark frames with a dim subject (`z8277w` / `g7exvl`, 0.9414),
  and a geometric abstract against a gradient (`34xkv0` / `g8p3zq`, 0.9031).
- 0.85 to 0.90 is barely a shared theme. Examples: an Earth from orbit against a word-cloud map
  (`gjjmkq` / `nml5k4`, 0.8633), and a cosplay photo against a painted dancer (`d6335m` / `6k67ql`, 0.8879).

Nearly every **Pool** member, 1,275 of 1,322, is within the radius of some **Ban**. That is the spread
question, and this ADR does not settle it.

## Consequences

A repost of a **Banned** image is never shown, however strong the **Favourites** around it.

A vetoed member is not retired, and it is never shown, so it is never decided. It holds a **Pool** slot
against the **Pool target size** until a settings prune takes it, or a **History** edit away from **Ban**
lets it be drawn. On today's numbers that is one slot. In exchange, there is nothing stored that a
**History** edit would have to undo. If the vetoed members ever build up, they are the same kind of problem
as **Dud** build-up, which is deferred in `AGENTS.md`.

A **Pool** of nothing but **Bans** and their reposts says `POOL_EMPTY`, as a **Pool** of **Bans** already
did.

The threshold is sized for this model, the int8-quantised CLIP ViT-B/32 image tower (ADR 0013). Another
model needs the measurement again.

Near-duplicates of **Favourites** and of other decided **Wallpapers** are a separate question
(wallpapi-52). That work should call `near_duplicates` with this threshold rather than measure again.

## Alternatives considered

**A veto across the whole radius.** It catches every repost, and it bans a style for one image's flaw.
Rejected.

**A lower threshold that also catches crops.** Near-empty images reach 0.98 against each other, so this
would veto minimal wallpapers wholesale. Rejected.

**A setting.** The threshold is a property of the model's geometry, and no user could tell 0.985 from
0.98 without this measurement. Rejected.

**Using the blended `similarities` matrix.** No new method would be needed, but the fallback would enter
it, and flat-colour pairs score 1.0 there. Rejected.
