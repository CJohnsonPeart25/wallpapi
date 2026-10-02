# 18. The Unknowns are spread one per look-alike group

Date: 2026-10-02

## Status

Accepted. Partly supersedes ADR 0010: its sentence "there is no 'best' **Unknown**" becomes "the
**Unknowns** are spread one per group". Everything else in ADR 0010 stands — the allocation, the **Banger**
and **Dud** orders, the **Shortfall** rule and the shuffle before the **Batch** is written. Adds a method
to the **Similarity provider** protocol, on ADR 0013's precedent.

## Context

The **Unknown** slots took a uniform seeded random order (`CoreService._draw_order`, ADR 0010). Since
ADR 0016 every **Pool** member is undecided, and the **Revisit weight** and `weighted_order` are gone, so
that shuffle was the whole of the **Unknown** draw.

A uniform draw copies the **Pool**'s lopsidedness onto the **Batch**. If 30% of the **Unknowns** are dark
forests, about four of a 16-size **Explore** **Batch**'s twelve **Unknown** slots are dark forests. The user
judges one idea four times, and because all four lie within one **Similarity radius** of each other, the
next **Batch** has learned about one neighbourhood instead of four.

ADR 0010 said there is no best **Unknown**. There is: the best **Unknown** is the one that teaches the
most, and covering new ground is a good proxy for that. "Far from what is already decided" needs no term
of its own — every **Unknown** lies, by definition, outside every decided **Wallpaper**'s radius — so what
is left is "far from each other".

#44 made that possible: an **Unknown** with no **Embedding** has no position to be far from anything in,
and #44 embeds the whole **Pool**.

## Decision

**Vectors cross the seam through a new protocol method**, `vectors(pool) -> NDArray[float32] | None`.
`len(pool)` x d, a row of zeros meaning no **Embedding**. The embedding provider answers its cached CLIP
rows — a row of another width is a zero row, as it is absent in `similarities` — and `metadata` and `tags`
answer `None`. **Pool** x d, never **Pool** x **Pool**: 1MB at 500 members. On the protocol and answered
with a neutral value where it does not apply, as `catch_up` and `notice` are (ADR 0013), because the Core
service must not know which provider it holds.

**The clustering is a pure function in `allocation.py`**, `varied_order(vectors, favourites, k, random)`,
next to `allocate` and tested the same way: generated vectors, the real `SeededRandom`, no **Pool**.

- **Candidates** are the **Unknowns** of this mint's classification with a non-zero vector.
- **Spherical k-means** — cosine, on the rows scaled to unit length — with **k equal to the Unknown slots
  allocated**, k-means++ seeding and a fixed `KMEANS_ITERATIONS` of ten. Every random number comes from
  `SeededRandom`, so a seed reproduces the **Batch** exactly.
- **k scales with batch size and Mix.** On **Explore**, batch sizes 2/4/8/16/32/64 give k = 1–2/3/6/12/24/48.
  Small **Batches** get a few broad groups; big ones get many fine ones.
- **The pick** is one member per cluster, a seeded weighted pick with weight `log(1 + favourites)` —
  Wallhaven's favourite count, a fixed formula and not a setting. A 1,000-favourite picture is about 3x as
  likely as a 10-favourite one, not 100x: a nudge towards a decent example of each kind. It is
  `weighted_order`'s Efraimidis–Spirakis key, `u ** (1/w)`, and `weighted_order` comes back for it.
  No favourites at all is a weight of zero, which sorts last.
- **The output is still an ordering of all the Unknowns**, the contract `_draw_order` already had, so
  slot-taking is unchanged: the clustered picks first, in the seeded order k-means++ drew their clusters
  in, then the other embedded **Unknowns**, then the unembedded ones, those two in today's order. The
  input rows are today's seeded shuffle, so "today's order" is simply the order given.
- **Today's draw is used unchanged** — not one extra random number spent — when the provider answers
  `None`, or when fewer **Unknowns** are embedded than there are **Unknown** slots.

**No outlier guard.** No minimum cluster size and no merging of small clusters.

- A small cluster of junk may get a slot. The learning loop corrects that: once its tile is **Ignored**, its
  look-alikes fall within the **Similarity radius** of something decided, stop being **Unknowns** and leave
  the clustering. Junk costs one tile, once.
- A minimum size relative to the **Pool** or the **Batch** was considered and dropped: any threshold is
  arbitrary across batch sizes from 2 to 64.
- Whether that holds up is an empirical question, so it is a deferred decision in `AGENTS.md`: watch the
  **Explicit Verdict** rate on **Unknown** tiles in real use, and revisit a guard if it falls.

**A centre left with no members stays where it was**, rather than being re-seeded at the point farthest
from its centre, which is the usual repair. That repair is farthest-point sampling in another form, and
CLIP's extreme points are disproportionately poor pictures. A cluster that ends empty — reachable only when
rows duplicate each other — gives no pick, and its slot is taken from the next embedded **Unknown** in
today's order.

**Nothing about the clusters is stored.** They are recomputed on every mint from the classification
(invariant 2), exactly as the **Zones** are.

**Unchanged:** the **Banger** and **Dud** orders, the **Shortfall** order (**Unknown**, then **Banger**,
then **Dud**), and the shuffle before the **Batch** is written.

## Mint time

Measured once on generated vectors, not a timed test: `varied_order` over 2,000 x 512 float32 rows drawn
from a standard normal, k = 48, favourite counts uniform in [0, 2000), twenty seeds. Median **57ms**
(56–59ms) on the maintainer's machine — AMD64 Family 26, Python 3.14.7, numpy 2.5.3. That is the worst case the settings allow: k = 48 is a 64-**Batch** on **Explore**, and
2,000 is four times the default `pool_target_size`. At the defaults it is a fraction of that, and it is
spent once per mint, never per page load.

## Consequences

A **Batch** covers more kinds of picture. On a fixture with a third of the **Pool** in one group and the
rest in fourteen small ones, **Explore** at 16 shows on average about 12.6 groups per **Batch** against
about 8.9 for the uniform draw, over the same thirty seeds.

The **Banger** and **Dud** draw orders are no longer seed-for-seed identical to what they were: the
**Unknown** order now spends random numbers before them. Their rule is the same; only which numbers they
get has moved.

With the metadata or tags provider selected, nothing changes at all, because neither has positions. Before
the model has downloaded, the embedding provider has no rows, so the draw is today's until enough
**Unknowns** are embedded to fill the slots.

`weighted_order` is back in `allocation.py`, two tickets after #38 removed it.

## Alternatives considered

**k-medoids over `similarities(unknowns, landmarks)`.** No new protocol method, but it clusters on
similarity profiles to an arbitrary set of landmark **Wallpapers**, which is harder to reason about and to
test than positions. Rejected.

**Farthest-point sampling.** The obvious way to cover ground: start anywhere and keep taking the
**Unknown** farthest from everything taken. Rejected because CLIP's extreme points are disproportionately
low quality, so it would fill the **Batch** with the oddest pictures in the **Pool**.

**An outlier guard** — a minimum cluster size, or merging clusters below a relative threshold. Dropped as
arbitrary across batch sizes; see above.

**Weighting the pick by raw favourites.** A 1,000-favourite picture would be 100x as likely as a
10-favourite one, which is a popularity ranking inside each group rather than a nudge.
