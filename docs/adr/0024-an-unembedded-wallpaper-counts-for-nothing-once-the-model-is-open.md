# 24. An unembedded Wallpaper counts for nothing once the model is open

Date: 2026-10-06

## Status

Accepted. Amends ADR 0013: the colour-and-category baseline is the floor only while the model is not open.
Settles the masking question that ADR 0017 left open. The **Similarity radius**, the **Similarity decay**,
the **Score** formula and the **Zone** rule are unchanged.

## Context

`Embeddings.similarities` maps cosine by `(1 + cos) / 2` where both sides have an **Embedding**. Until now
it answered every other pair with `metadata_similarity`. Both kinds of answer then meet one **Similarity
radius** (0.10 since ADR 0023), and that radius is tuned for CLIP distances (ADR 0013, ADR 0023), so the
matrix held two calibrations under one radius. ADR 0013's own example is the baseline calling a grey mountain and a silver car alike: an
unembedded **Wallpaper** was placed by its palette against embedded neighbours.

ADR 0017 embeds the whole **Pool**. On 2026-10-06 the maintainer's database had 1,322 of 1,322 **Pool**
candidates and 692 of 692 decided **Wallpapers** embedded, so in steady state this decision changes no
**Score**. It does act on a newcomer's first two or three minutes, on thumbnails that were given up on or
could not be read, and on a cache stalled at its cap. Those are where the baseline mis-scores.

## Decision

**Once the model is open, a pair where either side has no Embedding scores 0.0.** A similarity of 0.0 is a
distance of 1.0, beyond every radius, so the weight is exactly 0. An unembedded **Pool** member with no
other reach scores exactly 0.0 and is an honest **Unknown**. An unembedded decided **Wallpaper** contributes
nothing to any **Score**.

**While the model is not open, the baseline still answers those pairs.** "Not open" covers pending, failed,
unusable, and the time after boot before `catch_up` opens the model. Pairs with two **Embeddings** use
cosine in every state.

**"Open" is `Embeddings._opened`.** `_open` sets it once on the similarity thread. `similarities` reads it
once per call with no lock, because it is a bool that only ever goes from False to True. With the model open
the fallback is not called at all. With nothing held for the pairs asked, the answer is a zero matrix. The
contract stays `len(pool)` x `len(decided)`, float32, in `[0, 1]` (invariant 2).

**The coverage notice says what this means.** "Image similarity covers {embedded:,} of {pool:,} Pool
wallpapers so far — the rest count as Unknown until their thumbnails are embedded." It shows only when the
model is open, which is exactly when masking applies. The pending, failed and unusable notices are
unchanged: in those states the baseline does answer.

No threshold, no setting and no migration.

## Consequences

- A **Wallpaper** waiting for its thumbnail is in the **Unknown** draw, not placed by palette. The varied
  **Unknown** draw (ADR 0018) already treats a missing **Embedding** as its own group, through `vectors()`,
  which is unchanged.
- A **Favourite** or **Ban** with no **Embedding**, because its thumbnail was given up on or could not be
  read, no longer spreads at all. Before, it spread by palette.
- After every boot the baseline answers again for unembedded pairs until `catch_up` opens the model,
  usually within a second when the model is on disk.
- A model that cannot be fetched or opened leaves wallpapi as it was before ADR 0013, one notch cruder,
  as ADR 0013 promised.

## Alternatives considered

**The whole matrix on the baseline until the model is open.** This would be one calibration at a time.
But after every boot, vectors already held would be discarded until `catch_up` opens the model, and a
**Batch** minted in that window would be scored by palette alone.

**Mask whenever any vector is held, model open or not.** A failed model fetch on a machine that once had
the model would then leave every newcomer **Unknown** for ever, which breaks ADR 0013's "a failed model
still works, one notch cruder".

**A coverage threshold** (mask only once most of the **Pool** is embedded). The model-open switch replaces
it: ADR 0017's downloader makes coverage near total within minutes. A threshold would be one more number
to tune with nothing real to tune it against.
