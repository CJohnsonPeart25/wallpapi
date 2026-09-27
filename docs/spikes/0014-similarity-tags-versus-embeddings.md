# Spike 14. Similarity method — tags versus image embeddings

Date: 2026-09-27

## Status

**Closed. The recommendation was taken** — see
`docs/adr/0013-image-embeddings-are-the-similarity-provider.md`, which is the decision; this note is the
evidence behind it and is kept for that reason alone.

The embedding provider is what wallpapi runs, the **Similarity radius** default moved to 0.15 with it, and
all three providers stay selectable through `WALLPAPI_SIMILARITY` because everything below was measured
against a *synthetic* **Decision log** and nobody has yet seen them on a real one.

## What was built

Two more **Similarity providers** behind the protocol #9 shipped, selected in `main.py` by
`WALLPAPI_SIMILARITY=metadata|tags|embedding` (which now defaults to `embedding`):

- **`tags`** (`similarity_tags.py`) — Jaccard index of two Wallhaven tag-id sets, blended
  `0.6 / 0.4` with the baseline's colour-and-category number, and the baseline alone for any pair where
  either side has no cached tags. Tags come from `GET /api/v1/w/{id}`, one **API call** each, cached
  permanently in `~/.wallpapi/tags.db`.
- **`embedding`** (`similarity_embedding.py`) — cosine between CLIP ViT-B/32 image-tower embeddings of the
  cached thumbnails, L2 normalised, mapped into `[0, 1]` as `(1 + cosine) / 2`, cached permanently in
  `~/.wallpapi/embeddings.db`. Same fallback to the baseline for anything not yet embedded.

Each cache is a SQLite file the provider opens itself, so **no migration was needed** and `SCHEMA_VERSION`
is untouched. Deleting a provider is deleting a file.

## The model

| | |
| --- | --- |
| Repository | `Xenova/clip-vit-base-patch32` (Hugging Face) |
| Revision | `d15189d7028b43f1d3e65039190477f6af591c2a` |
| File | `onnx/vision_model_quantized.onnx` |
| sha256 | `583fd1110a514667812fee7d684952aaf82a99b959760c8d7dca7e0ab9839299` |
| Size | 89,117,001 bytes (85 MiB) |
| Input | `pixel_values`, `(batch, 3, 224, 224)` float32 |
| Output | `image_embeds`, `(batch, 512)` |
| Kept at | `~/.wallpapi/models/clip-vit-b32-vision-quantized.onnx` |

A straight ONNX export of OpenAI's `clip-vit-base-patch32` — the same weights `open_clip`'s ViT-B/32 uses,
no fine-tuning — published with the text and vision towers in separate files, which is what makes taking
the image tower alone a download rather than surgery. The int8 export rather than the 335 MiB float32 one:
inside invariant 13's budget, the build ONNX Runtime's CPU kernels are fastest on, and what is asked of it
is a ranking rather than an absolute number. Swapping to float32 is one constant — `MODEL_FILE`, and its
checksum — and nothing else changes.

The download is verified against the checksum rather than trusted, so a truncated file shows up as an
error and not as a mysteriously bad provider.

`onnxruntime` and `pillow` are an optional extra (`uv sync --extra similarity-embedding`), imported inside
the methods that use them. A default install is unchanged, and `numpy` stays a direct dependency rather
than arriving transitively through onnxruntime (invariant 2).

## How the comparison was run

`scripts/similarity_spike.py`, which pytest does not collect. See its module docstring for the sequence.

**The Decision log is synthetic, and that is the biggest caveat here.** There is no real one on this
machine — `~/.wallpapi` does not exist — so there were no **Explicit Verdicts** to leave one out of. The
**Wallpapers** are real: 360 of them, 72 from each of five Wallhaven searches. The **Verdicts** over them
are made up:

| Theme | Search | Verdicts |
| --- | --- | --- |
| mountains | `mountains landscape` | 6 **Favourites**, 10 **Likes** |
| space | `nebula space` | 10 **Likes** |
| anime | `anime girl` | 8 **Bans**, 8 **Ignores** |
| cars | `sports car` | 8 **Bans** |
| cats | `cat` | none |

42 **Explicit Verdicts**, so 42 leave-one-out folds, over 50 decided **Wallpapers**. The ground truth is
*which search a **Wallpaper** came back from* — Wallhaven's own notion of what an expression means, and
not a feature any provider can see. A synthetic taste defined by colour would have handed the answer to
the baseline; one defined by tags would have handed it to the tag provider.

So what the numbers below measure is **how well each provider recovers a subject-level grouping it was
not told about**. What they cannot measure: whether a real person's taste is this cleanly themed (it is
not — a real **Decision log** has **Favourites** scattered across subjects that share only a mood), and
how any of this behaves as the log grows past a few dozen entries.

## Choosing the Similarity radius

The grid below made the embedding provider's radius the one number that had to be chosen deliberately, so
it got a finer sweep of its own, at the shipped decay of 4.0 and with all 50 decided **Wallpapers**:

| radius | right sign | Pool left Unknown |
| --- | --- | --- |
| 0.10 | 25/42 (60%) | 47% |
| 0.125 | 31/42 (74%) | 24% |
| **0.15** | **36/42 (86%)** | **7%** |
| 0.175 | 40/42 (95%) | 3% |
| 0.20 | 41/42 (98%) | 1% |
| 0.225 | 39/42 (93%) | 0% |
| 0.25 | 40/42 (95%) | 0% |

The mapped similarities over **Pool** x decided: 5th percentile 0.672, median 0.751, 95th 0.899, maximum
1.0 — so distances run from 0 to about 0.41 and a radius of 0.15 counts roughly the nearest one pair in
eight as a neighbour.

**0.15, not the accuracy-maximising 0.20.** **Unknown** is the **Zone** an **Explore** **Mix** draws
three-quarters of a **Batch** from (ADR 0010), so a radius that decides almost everything leaves nothing
to explore. 0.15 trades nine points of leave-one-out accuracy for more than double the **Unknown**
**Zone**, and still beats the baseline's best at any setting. The share falls as the **Decision log**
grows, so this errs towards **Explore** deliberately — and it is a setting either way.

## Results

Full output of `uv run python scripts/similarity_spike.py evaluate`, on this machine, 2026-09-27, with all
three providers still present:

```
Pool: 360 Wallpapers. Explicit Verdicts: 42. Decided (non-zero value): 50.

At the shipped settings (radius 0.5, decay 4.0):

provider      radius  decay  right  wrong  unknown  B prec  B rec  D prec  D rec  separ  pool ?
metadata         0.5    4.0     29     13        0    0.76   0.73    0.59   0.62   0.42    0.00
tags             0.5    4.0     38      0        4    1.00   0.96    1.00   0.81   0.34    0.29
embedding        0.5    4.0     39      3        0    0.90   1.00    1.00   0.81   0.50    0.00

Best of the grid, per provider:

provider      radius  decay  right  wrong  unknown  B prec  B rec  D prec  D rec  separ  pool ?
metadata         0.4    2.0     32     10        0    0.81   0.81    0.69   0.69   0.37    0.01

  metadata: right-sign share of 42 folds, decay (rows) by radius (columns)
            0.05    0.1   0.15    0.2    0.3    0.4    0.5   0.75    1.0
      2.0   0.33   0.33   0.60   0.71   0.64   0.76   0.74   0.62   0.62
      4.0   0.33   0.33   0.60   0.71   0.64   0.67   0.69   0.67   0.67
      8.0   0.33   0.33   0.60   0.74   0.71   0.67   0.67   0.69   0.69
     16.0   0.33   0.33   0.57   0.69   0.69   0.71   0.76   0.76   0.76
     32.0   0.33   0.33   0.57   0.71   0.69   0.76   0.76   0.76   0.76

  metadata: share of the Pool left Unknown, decay (rows) by radius (columns)
            0.05    0.1   0.15    0.2    0.3    0.4    0.5   0.75    1.0
      2.0   0.45   0.43   0.24   0.08   0.04   0.01   0.00   0.00   0.00
      4.0   0.45   0.43   0.24   0.08   0.04   0.01   0.00   0.00   0.00
      8.0   0.45   0.43   0.24   0.08   0.04   0.01   0.00   0.00   0.00
     16.0   0.45   0.43   0.24   0.08   0.04   0.01   0.00   0.00   0.00
     32.0   0.45   0.43   0.24   0.08   0.04   0.01   0.00   0.00   0.00

tags             1.0    2.0     42      0        0    1.00   1.00    1.00   1.00   0.51    0.00

  tags: right-sign share of 42 folds, decay (rows) by radius (columns)
            0.05    0.1   0.15    0.2    0.3    0.4    0.5   0.75    1.0
      2.0   0.05   0.05   0.05   0.19   0.38   0.74   0.90   0.93   1.00
      4.0   0.05   0.05   0.05   0.19   0.38   0.74   0.90   1.00   1.00
      8.0   0.05   0.05   0.05   0.19   0.38   0.74   0.90   1.00   1.00
     16.0   0.05   0.05   0.05   0.19   0.38   0.74   0.90   1.00   1.00
     32.0   0.05   0.05   0.05   0.19   0.38   0.74   0.90   1.00   1.00

  tags: share of the Pool left Unknown, decay (rows) by radius (columns)
            0.05    0.1   0.15    0.2    0.3    0.4    0.5   0.75    1.0
      2.0   0.88   0.88   0.85   0.82   0.72   0.56   0.29   0.00   0.00
      4.0   0.88   0.88   0.85   0.82   0.72   0.56   0.29   0.00   0.00
      8.0   0.88   0.88   0.85   0.82   0.72   0.56   0.29   0.00   0.00
     16.0   0.88   0.88   0.85   0.82   0.72   0.56   0.29   0.00   0.00
     32.0   0.88   0.88   0.85   0.82   0.72   0.56   0.29   0.00   0.00

embedding        0.2    2.0     41      1        0    0.96   1.00    1.00   0.94   0.53    0.01

  embedding: right-sign share of 42 folds, decay (rows) by radius (columns)
            0.05    0.1   0.15    0.2    0.3    0.4    0.5   0.75    1.0
      2.0   0.00   0.60   0.86   0.98   0.79   0.93   0.93   0.93   0.93
      4.0   0.00   0.60   0.86   0.98   0.86   0.93   0.93   0.93   0.93
      8.0   0.00   0.60   0.86   0.98   0.93   0.93   0.93   0.93   0.93
     16.0   0.00   0.60   0.86   0.98   0.93   0.95   0.95   0.95   0.95
     32.0   0.00   0.60   0.86   0.98   0.98   0.98   0.98   0.98   0.98

  embedding: share of the Pool left Unknown, decay (rows) by radius (columns)
            0.05    0.1   0.15    0.2    0.3    0.4    0.5   0.75    1.0
      2.0   0.78   0.47   0.07   0.01   0.00   0.00   0.00   0.00   0.00
      4.0   0.78   0.47   0.07   0.01   0.00   0.00   0.00   0.00   0.00
      8.0   0.78   0.47   0.07   0.01   0.00   0.00   0.00   0.00   0.00
     16.0   0.78   0.47   0.07   0.01   0.00   0.00   0.00   0.00   0.00
     32.0   0.78   0.47   0.07   0.01   0.00   0.00   0.00   0.00   0.00


Cost:

provider       API calls     2k Pool   cache KiB   classify ms
metadata               0           0           0           0.6
tags                 360        2000         196           2.5
embedding              0           0        1492           2.4
```

`B prec` / `B rec` are precision and recall for **Banger** over the held-out **Verdicts**, `D` the same
for **Dud**; `undecided` is a held-out **Wallpaper** whose **Score** came out at exactly zero, which is
neither right nor wrong but is also not an answer. `separ` is the mean |**Score**| over the largest one,
a scale-free measure of how firmly the folds were decided — the three providers' **Scores** are sums of
the same **Verdict** values through different weights and are not on one scale.

`classify ms` is one pass over the whole 360-**Wallpaper** **Pool** against 50 decided **Wallpapers**,
best of three. `cache KiB` is for those 360; the embedding figure excludes the 85 MiB model and the
thumbnails, which the **Thumbnail cache** already holds for its own reasons (invariant 8).

And the qualitative look, `neighbours`:

```
Nearest to 7396g9 (mountains, https://wallhaven.cc/w/7396g9)

  its tags: Caucasus Mountains, far view, landscape, mountains, snow, snowy mountain

  metadata:
    1. 0.884  cats       https://wallhaven.cc/w/4vqgm8
    2. 0.884  mountains  https://wallhaven.cc/w/p2813p
    3. 0.884  cars       https://wallhaven.cc/w/0wy7eq
    4. 0.884  cars       https://wallhaven.cc/w/285xog
    top ten themes: cats, mountains, cars, cars, cars, mountains, cars, cars, mountains, cars

  tags:
    1. 0.640  mountains  https://wallhaven.cc/w/45vzg3
    2. 0.607  mountains  https://wallhaven.cc/w/dpvg9l
    3. 0.607  mountains  https://wallhaven.cc/w/491lyd
    4. 0.603  mountains  https://wallhaven.cc/w/lq3jdq
    top ten themes: mountains, mountains, mountains, mountains, mountains, mountains, mountains, mountains, mountains, mountains

  embedding:
    1. 0.932  mountains  https://wallhaven.cc/w/0jl1yq
    2. 0.927  mountains  https://wallhaven.cc/w/83oxlk
    3. 0.926  mountains  https://wallhaven.cc/w/76389o
    4. 0.924  mountains  https://wallhaven.cc/w/lq3jdq
    top ten themes: mountains, mountains, mountains, mountains, mountains, mountains, mountains, mountains, mountains, mountains
```

## Comparison and recommendation

Three providers behind one protocol, 360 real Wallhaven **Wallpapers** from five themed searches, and a
**synthetic Decision log** of 42 **Explicit Verdicts** — there is no real one on this machine. Mountains
and space liked, anime and cars banned, cats undecided; the ground truth is which search a **Wallpaper**
came back from, not anything a provider can see. Leave-one-out over every **Explicit Verdict**.

**Zone quality**, at the settings the app ships with (**Similarity radius** 0.5, **Similarity decay** 4.0):

| | right sign | wrong | undecided | Banger P/R | Dud P/R | Pool left Unknown |
| --- | --- | --- | --- | --- | --- | --- |
| metadata | 29/42 (69%) | 13 | 0 | 0.76 / 0.73 | 0.59 / 0.62 | 0% |
| tags | 38/42 (90%) | **0** | 4 | 1.00 / 0.96 | 1.00 / 0.81 | 29% |
| embedding | 39/42 (93%) | 3 | 0 | 0.90 / 1.00 | 1.00 / 0.81 | 0% |

Swept over a radius × decay grid the ceilings are metadata 76%, tags 100% (radius 1.0), embedding 98%
(radius 0.2). Both candidates beat the baseline widely and are close to each other. The qualitative look
says why the baseline loses: asked for the nearest **Wallpapers** to a snowy Caucasus peak, its top ten
are four cars, three mountains, two cats and a car — it is matching palettes, and a grey mountain and a
silver car have the same palette. Tags return ten mountains; embeddings return ten mountains, the nearest
of them visibly the same peak from another valley.

**The shipped radius is wrong for the embedding provider, and that is a finding, not a footnote.** CLIP
cosines between natural images are bunched — mapped into `[0, 1]` they span 0.63 to 0.93 here — so every
distance is under 0.37 and a radius of 0.5 makes everything a neighbour of everything. It still scores
93%, but it leaves nothing **Unknown**, and #10's **Explore** **Mix** draws three-quarters of a **Batch**
from **Unknown**. Its useful band is narrow: radius 0.2 gives 98% and 1% **Unknown**, 0.15 gives 86% and
7%. The tag provider is the only one accurate *and* leaving a real **Unknown** **Zone** at one setting —
90% right, never once wrong, 29% of the **Pool** **Unknown** — because its fallback holds untagged pairs
near the baseline's value instead of at 1.0.

**Speed.** Classifying the whole **Pool** is 0.6 ms baseline, 2.5 ms tags, 2.4 ms embedding — one matmul
each, irrelevant beside everything else on the page. Filling the caches is where time goes: embedding is
7 ms per **Wallpaper** on CPU, so a 2,000-**Wallpaper** **Pool** in about 14 seconds, against 45 minutes
for the same **Pool**'s tags.

**API cost — the decisive number.** Tags come only from `GET /api/v1/w/{id}`, one **API call** each, out
of the same 45 a minute that stock the **Pool**. Two thousand **Wallpapers** is 2,000 calls; the 20,000
the settings allow is over seven hours of the entire budget. A search page admits 24 **Wallpapers** for
one call, so **tagging a Pool costs about 24 times as many API calls as filling it did** — and it recurs,
because the **Pool** turns over. The embedding provider costs zero **API calls** for ever: the thumbnails
it reads are already cached (invariant 8).

**Install weight.** Tags: no new dependency, 196 KiB of cache per 360 **Wallpapers**. Embedding: an 85 MiB
model file, `onnxruntime` and `pillow` as an optional extra, 1.5 MiB of cache per 360.

**Operational risk.** The tag provider's is recurring and external — it competes with the refill for the
one scarce resource wallpapi has, it depends on strangers having tagged things, and a **Wallpaper** that
has just entered the **Pool** is untagged, which is precisely the one you want scored. The embedding
provider's risk is up front and local: a checksummed download, an extra a default install does not carry,
and a model run per thumbnail that can be redone whenever.

**Recommendation: adopt the embedding provider, keep the baseline, delete the tag provider.** The two
candidates are within a couple of points on quality, so the tie breaks on cost, and one is free for ever
while the other permanently spends the budget that keeps the **Pool** stocked. Keep the baseline — both
candidates fall back to it for anything not yet cached, so it is a floor rather than a rival. Delete the
tag provider rather than leaving it selectable: a good idea the rate limit makes unaffordable, and an
unused provider is a cost with no payer. Adoption is more than flipping the default: it wants the
**Similarity radius** default moved to about 0.2, a decision on how much accuracy to trade for a
non-empty **Unknown** **Zone**, and a home for the embedding step — beside the thumbnail fetch, since an
embedding is only wanted once a thumbnail exists. And these numbers come from a synthetic taste with
clean thematic edges; a real **Decision log** will be messier, so read them as a ranking, not a score.

## What the maintainer decided

The recommendation above is left exactly as it was written, because a record of advice that has been
quietly edited to match the outcome is worth nothing. Two things went differently, and both are in ADR
0013:

**The tag provider was kept, not deleted.** The maintainer's reason is the caveat this whole note keeps
repeating: the comparison ran on a **Decision log** nobody has ever judged a **Wallpaper** into. Keeping
all three selectable costs a module and its tests, and buys the ability to look again with real
**Verdicts**. Its tag fetching stays an explicit step — its `catch_up` does nothing at all, selected or
not — so it can never quietly spend the refill's **API** budget. Choosing it means about 2,000 calls to
tag a **Pool** at its default target size.

**The radius went to 0.15 rather than "about 0.2".** The finer sweep above is what decided it: 0.2 is the
accuracy-maximising value and it leaves 1% of the **Pool** **Unknown**, which starves the **Explore**
**Mix** that #10 had landed in the meantime. 0.15 gives nine points back for seven times the **Unknown**
**Zone**.

The embedding step found the home the last paragraph guessed at, near enough: not beside the thumbnail
fetch — that is a request thread — but a background loop over the **Thumbnail cache**, which is the same
set of images one step later.
