# 28. A Line-up is drawn by Mix and wear

Date: 2026-10-27

## Status

Accepted. Amends the spec's (wallpapi-8pa) "The Line-up (wallpapi)": "a new **Line-up** module mints a screen
with the same draw as a **Batch**" becomes "a **Line-up** is drawn by the active **Mix** and **Wear**; each
**Instrument** may add its own rule (the **Gauntlet** champion stays)". The **Batch** draw does not change:
ADR 0010's allocation, orders and **Shortfall** and ADR 0018's varied **Unknown** draw stand for a **Batch**,
and so does ADR 0026's "a skip rerolls the **Unknown** and **Dud** slots, not the **Bangers**". ADR 0027
stands: comparing decides nothing and retires nothing.

## Context

#110 drew a **Line-up** the way a **Batch** is drawn: **Bangers** best **Score** first, **Unknowns** and
**Duds** in a seeded order. For a **Batch** that works. Submitting it retires every tile (ADR 0016), so the
next **Batch** gets the next best. A **Line-up** retires nothing (ADR 0027), and in the MVP a **Pick** moves
no **Score**: **Relative evidence** reaches ranking only once the evaluation shows it helps. So the top
**Banger** for each **Banger** **Slot** came back on every **Line-up**. On **Refine** at four that is the same
two or three **Bangers** on every screen. **Gauntlet** (wallpapi-8pa.12) would get the same challenger on
every screen.

Deciding and comparing are different jobs. A **Verdict** ends a **Wallpaper**'s turns: it leaves the **Pool**,
as now. A comparison may show a **Wallpaper** again, but each showing should make it wait longer.

The user made this decision on 2026-10-27; it is recorded on wallpapi-8pa.9.

## Decision

**A Line-up is drawn by the active Mix and wear.** **Wear** is how many times a **Wallpaper** was a tile on
an answered compare-**Screen** **Judgement**, "none of these" included. It is read in one **Evidence log**
query per draw (`evidence_log.times_compared`). There is no new table and nothing is stored. A **Line-up**
discarded by a **Pool reset** was never recorded, so it adds no wear. **Batch**, **Skip** and **History**
screens add none.

- **The Mix and the allocation do not change.** Wear decides which **Wallpaper** fills a **Slot**, never how
  many **Slots** a **Zone** gets. A **Shortfall** is filled as before (**Unknown**, then **Banger**, then
  **Dud**), each **Zone** giving up its **Wallpapers** in its wear order.
- **Bangers go by Score ÷ (1 + wear)**, highest first, and ties break by the seed. A **Banger** shown once
  counts half its **Score** to the draw, so it comes back once the next best is below that. With **Bangers**
  A, B and C at 0.9, 0.5 and 0.3 and one **Banger** **Slot**, three answered **Line-ups** show A, then B,
  then A.
- **Unknowns and Duds go fewest wear first.** Within one count they keep their **Zone**'s own order:
  random for **Duds**, and for **Unknowns** ADR 0018's varied draw. The varied draw runs on each count in
  turn, with k the **Slots** still to fill when that count is reached. A count reached once the **Slots** are
  full keeps its seeded order and is not clustered. Before any **Line-up** is answered there is one count,
  and the draw is ADR 0018's exactly.
- **A Batch passes no wear** and draws number for number as before.
- **Wear is a stand-in, worked out in one place.** The end goal is "show the comparison that teaches the
  most". That needs **Relative evidence** in ranking (wallpapi-e8j) and a measure of how sure a **Score** is;
  `taste.ranking.Ranking` is an object so that such a figure can join `scores`. The draw takes wear as a
  number per **Wallpaper**, zero or more (`allocation.Wear`), and only orders by it. `lineup.wear` is the one
  place it is worked out, from the candidates and their **Scores**, so a later rule replaces that body and
  not the draw.
- **Each Instrument may add its own rule to the draw.** The rule narrows what the shared draw is handed,
  never how it draws (`Lineups._drawn`). **Gauntlet**'s champion stays, so **Gauntlet** hands the draw the
  candidates less its champion and asks for n − 1. **Pick** and **Order** add no rule.

## Consequences

- The top **Banger** no longer sits on every **Line-up**. **Bangers** take turns, still best first: over many
  screens each is shown roughly in proportion to its **Score**.
- Every **Unknown** in reach is lined up once before any is lined up twice, and likewise every **Dud**.
- Wear counts the compare **Screen**, not one **Instrument**, so **Order** and **Gauntlet** add to it
  without a change. A **Gauntlet** champion gains wear on every screen it holds. That does not matter while
  it stays, and once beaten it waits its turn like any other **Wallpaper**.
- Wear never fades, and it survives a **Pool reset**, as the **Evidence log** does. A **Wallpaper** purged and
  fetched again keeps its wear. Fading it is the same open question as fading **Verdicts** (`AGENTS.md`,
  Deferred decisions).
- A **Line-up** no longer follows from the seed and the **Scores** alone: the same seed over a different log
  draws differently. A **Batch** still does.
- Minting a **Line-up** costs one more query, over an index on the **Item**, restricted to the candidates.
  Clustering runs once per count that fills **Slots**: in practice once, as for a **Batch**.

## Alternatives considered

These were rejected by the user on wallpapi-8pa.9.

**No change.** **Gauntlet** would repeat its challenger, and the top **Bangers** would fill every **Line-up**.

**Rest the tiles of the last k screens.** Only the top k + 1 **Bangers** per **Slot** would ever be shown.

**Random Bangers.** This loses best first, the reason a **Banger** is drawn by **Score** at all.

**Strict least-shown first for Bangers.** Every **Banger** would take its turn whatever its **Score**. The
user wants the **Score** to keep its weight.

**A dial, Score ÷ (1 + wear)^k.** k = 0 ignores wear and a large k is strict least-shown first. It is parked
as wallpapi-5c6, discover and refine modes on the compare tab. The MVP fixes k at 1, with no setting.

These were rejected while building it.

**A stored count of showings.** The **Evidence log** already holds every compare **Judgement**. A counter
would be a second record that could disagree with it.

**The varied draw once over every Unknown, then sorted by wear.** A look-alike group whose pick happened to be
worn would lose its place to more members of the groups whose picks were not. The spread would be lost
exactly when wear matters.

**Clustering every Unknown and taking the least-worn member of each group.** This keeps the spread but breaks
"no wear before wear": a group whose members were all worn would put a worn tile ahead of an unworn one from
another group.
