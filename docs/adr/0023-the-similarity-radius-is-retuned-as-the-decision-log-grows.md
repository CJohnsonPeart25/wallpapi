# 23. The Similarity radius is retuned as the Decision log grows

Date: 2026-10-06

## Status

Accepted. Supersedes ADR 0013's **Similarity radius** default of 0.15 and nothing else in it: the
provider, the cache, the mapping into `[0, 1]` and migration 9 all stand. Amends ADR 0016's remedy for
**Dud** build-up.

## Context

ADR 0013 tuned the radius to 0.15 against a synthetic **Decision log**, where it left 7% of the **Pool**
**Unknown**. A radius is a fixed distance, so every new decision covers more of the space, and the
**Unknown** zone shrinks as the log grows.

Measured 2026-10-06 on a read-only copy of the maintainer's database, classified with the real
`Batches.classify` and the CLIP provider (decay 4.0): 692 decided **Wallpapers** (407 **Bans**, 257
**Ignores**, 27 **Likes**, 1 **Favourite**) and a **Pool** of 1,322, all embedded.

| radius | Unknown | Dud | Banger |
| --- | --- | --- | --- |
| 0.15 | 0 | 1,322 | 0 |
| 0.12 | 249 | 1,039 | 34 |
| **0.10** | **608** | **670** | **44** |
| 0.08 | 1,025 | 275 | 22 |
| 0.06 | 1,244 | 76 | 2 |

At 0.15 every member was a **Dud**: the median member had 31 decided neighbours within the radius, and 90%
had one within 0.13. The **Pool** was above its target, so the refill idled (ADR 0016: never trim), and
every **Shortfall** fell through to **Dud**: on 2026-10-04, 137 of the 172 tiles shown were **Duds**. Nothing
on the refill side helps, because random arrivals resemble the **Pool** and arrive as **Duds** too.

The remedy ADR 0016 documented, "**Ban** or **Ignore** a page of them", made it worse: each **Ban** spreads
−100, so it grows the **Dud** zone it was meant to clear.

## Decision

**The default moves from 0.15 to 0.10, by migration 11**, which rewrites the row only where it still holds
0.15, then re-seeds: migration 9's shape exactly. A radius the owner chose is kept. `settings.py` keeps the
superseded value as `RETUNED_SIMILARITY_RADIUS`, beside migration 9's `SUPERSEDED_SIMILARITY_RADIUS`, so
neither migration changes meaning. 0.10 leaves 46% of the measured **Pool** **Unknown**, which **Explore**'s
75% share can mostly draw from, and keeps **Duds** and **Bangers** meaningful; 0.08 leaves 78% but only 22
**Bangers**.

**The remedy for Dud build-up is to Ignore a page of them and submit.** An **Ignore** retires them and spreads
only −10.

**When a mint finds the Unknown zone short, the Batch page says so.** The line shows when the **Unknown**
zone holds fewer **Wallpapers** than the active **Mix**'s **Unknown** slots, and names the **Similarity
radius** as the setting to lower.

- The slots are the **Mix**'s **Unknown** share of the **Batch** rounded up: the most any allocation roll
  can ask of the zone, so a roll it cannot serve counts.
- It says nothing when the classified **Pool** is smaller than a **Batch**. That is a small **Pool**, not a
  wide radius, and the refill line already explains it.
- It is derived in `Batches.next` from the classification the mint already computes, and never stored
  (invariant 2). It rides on the minted `Batch` as `unknown_short`, outside the **Batch**'s equality.
- **It shows at mint only.** A reload of the live **Batch** reads it back from storage without classifying,
  so it has nothing to say. Every submission mints the next **Batch**, so the line appears once per
  **Batch**, which is enough to make decay visible. Classifying on every reload, or storing a flag, was
  not worth that.

## Consequences

**This retune is not the last.** A fixed radius decays as the log grows; 0.10 will cover the space in turn.
The notice is how the next one gets noticed, rather than after days of all-**Dud** **Batches**.

The long-term option is a radius derived per mint to hold an **Unknown** share (bead wallpapi-zbh, parked).
It would reverse CONTEXT.md's "a setting", and must take a connection so the refill can call it too.

Tests that placed a neighbour at similarity 0.9 (distance 0.10) now sit on the cliff edge, so they use 0.95.
One of them, the equal-and-opposite cancellation, used 0.8, which was outside the radius at 0.15 as well.

## Alternatives considered

**0.08.** Rejected for now: it buys more **Unknowns** than **Explore** can use at the cost of half the
**Bangers**, and the notice will say when 0.10 is no longer enough.

**Refill-side fixes**: evicting the worst **Duds**, weighting fetches by **Zone** need. Rejected: random
arrivals resemble the **Pool**, so they arrive as **Duds** while the radius covers the space, and eviction
would spend **API calls** for nothing.

**Classifying on every reload so the notice persists.** Rejected: a whole-**Pool** classification on a page
load that today does none, for a line the next submission shows anyway.
