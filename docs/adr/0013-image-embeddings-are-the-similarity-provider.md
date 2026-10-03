# 13. Image embeddings are the Similarity provider

Date: 2026-09-27

## Status

Accepted. Supersedes ADR 0007's choice of provider and its **Similarity radius**; the rest of 0007 — the
matrix interface, the **Score** formula, the **Zone** rule — stands unchanged. Amended by ADR 0017: a
background downloader now fetches every **Pool** member's thumbnail, so the work list below covers the
**Pool** rather than only what has been shown, and `notice` takes the **Pool**. Amended 2026-10-03: the two
losing providers' selectability is withdrawn — the tag provider, its cache and the Wallhaven tag endpoint, the
`WALLPAPI_SIMILARITY` switch and `scripts/similarity_spike.py` are deleted, and the spike's write-up
`docs/spikes/0014-similarity-tags-versus-embeddings.md` survives in git history only. The baseline stays as
the per-pair fallback, now `metadata_similarity` in the one `similarity` module.

## Context

ADR 0007 shipped `MetadataSimilarityProvider`: a coarse HSV histogram of the dominant colours Wallhaven
returns, plus a quarter for a matching category. It cost no **API call** and no model, and it was written
as a placeholder — "#14's spike is where that stops being the only option".

#14 built the two alternatives the spec named and measured all three. The full comparison, the harness and
the raw numbers are in `docs/spikes/0014-similarity-tags-versus-embeddings.md`. In short, on 360 real
Wallhaven **Wallpapers** across five themed searches with a **synthetic Decision log** of 42 **Explicit
Verdicts**, leave-one-out at the settings the app then shipped:

| | right sign | wrong | Pool left Unknown |
| --- | --- | --- | --- |
| metadata | 29/42 (69%) | 13 | 0% |
| tags | 38/42 (90%) | 0 | 29% |
| embedding | 39/42 (93%) | 3 | 0% |

Both candidates beat the baseline widely and were within a couple of points of each other. The
qualitative look says what the table does not: asked for the nearest **Wallpapers** to a snowy Caucasus
peak, the baseline's top ten were four cars, three mountains, two cats and a car — it matches palettes,
and a grey mountain and a silver car have the same palette. Tags returned ten mountains; embeddings
returned ten mountains.

**The Decision log was synthetic, and that is the load-bearing caveat.** There was no real one to measure
against. The **Wallpapers** were real and the ground truth was which Wallhaven search each came back
from — chosen so that no provider was handed the answer — but a real taste is not this cleanly themed.

## Decision

**The embedding provider is what wallpapi runs.** `WALLPAPI_SIMILARITY` defaults to `embedding`;
`onnxruntime` and `pillow` are ordinary dependencies and the optional extra is gone. `numpy` stays a
*direct* dependency rather than being left to arrive through onnxruntime (invariant 2).

**Because the tie on quality broke on cost.** A tag costs one **API call** out of Wallhaven's 45 a minute
— about 2,000 to tag a **Pool** at its default target, roughly 24 times as many calls as admitting those
**Wallpapers** cost in the first place, and it recurs as the **Pool** turns over. That is the same budget
the refill spends keeping the **Pool** stocked, and it is the scarcest thing wallpapi has. The embedding
provider costs no **API calls** at all, ever: it reads the thumbnails the **Thumbnail cache** already
holds (invariant 8), at about 7ms of CPU each.

**All three stay selectable.** `WALLPAPI_SIMILARITY=metadata|tags|embedding`. The comparison that chose
between them ran on a synthetic **Decision log**, and nobody has yet seen the three on a real one — so
the losers are kept as options rather than deleted, and the maintainer can switch and look. Choosing
`tags` costs about 2,000 **API calls** to fill its cache for a default-sized **Pool**, and that fill stays
an explicit step somebody runs (`scripts/similarity_spike.py tags`): the tag provider's `catch_up` does
nothing at all, selected or not, so it can never quietly spend the refill's budget.

**The baseline is not a rival but a floor.** Both other providers fall back to it per pair, for anything
their cache has not reached. It cannot be deleted and should not be.

**The model.** A straight ONNX export of OpenAI's CLIP ViT-B/32 with no fine-tuning, int8-quantised,
image tower only. Fetched once into `~/.wallpapi/models/` and verified against its checksum on the way in.

| | |
| --- | --- |
| Repository | `Xenova/clip-vit-base-patch32` |
| Revision | `d15189d7028b43f1d3e65039190477f6af591c2a` |
| File | `onnx/vision_model_quantized.onnx` |
| sha256 | `583fd1110a514667812fee7d684952aaf82a99b959760c8d7dca7e0ab9839299` |
| Size | 89,117,001 bytes (85 MiB) |

**It is fetched on a background thread of its own, and until it arrives the baseline answers.** Not inside
the refill's loop: on a first boot the refill is filling an empty **Pool** while somebody watches an empty
page, and 85MiB in front of its first search would hold that page empty — for **Scores** that cannot
matter until there are **Wallpapers** and **Verdicts**. Every megabyte checks the `stop_event`, so
shutdown never waits on a download (invariant 12).

**While it is not at full strength, the page says so** — one line beside the refill indicator, in the
provider's own words, passed through `CoreService.similarity_notice()`. A **Score** computed from the
fallback is indistinguishable from one computed properly, so a wallpapi that had never managed to fetch
its model would look exactly like one working perfectly, for as long as nobody noticed the **Bangers**
were only the right colour.

**The protocol gains `catch_up` and `notice`.** Both on `SimilarityProvider` rather than only on the
provider that needs them, because the alternative is the Core service knowing which provider it holds —
and the point of the seam is that it does not. A provider with nothing to keep up returns
`NOTHING_TO_CATCH_UP` and the whole thing costs one wake-up an hour.

**The Thumbnail cache is the work list.** `catch_up` embeds every file in it that has no embedding yet,
oldest first, one batch of 16 per call. A **Wallpaper** whose thumbnail has not been fetched is not a file
in that directory, so nothing embeds it and every pair it appears in falls back to the baseline — until
its tile renders and the page fetches the thumbnail, after which the next pass picks it up. Nothing has to
tell the provider what is in the **Pool**, which is what keeps it out of a database it knows nothing about.

**The Similarity radius default moves from 0.5 to 0.15, by migration 9.** CLIP cosines between natural
images are bunched: mapped into `[0, 1]` they spanned 0.59 to 1.0 in the spike, so every distance is under
0.41 and a radius of 0.5 made every decided **Wallpaper** a neighbour of the whole **Pool**.

**0.15 is deliberately not the accuracy-maximising value.** The sweep:

| radius | right sign | Pool left Unknown |
| --- | --- | --- |
| 0.10 | 60% | 47% |
| 0.125 | 74% | 24% |
| **0.15** | **86%** | **7%** |
| 0.175 | 95% | 3% |
| 0.20 | 98% | 1% |

0.20 wins on sign and leaves nothing **Unknown** — and **Unknown** is not waste. It is the **Zone** an
**Explore** **Mix** draws three-quarters of a **Batch** from (ADR 0010), so a radius that decides
everything leaves nothing to explore, and a tool for finding **Wallpapers** you have not seen stops being
one. 0.15 gives up nine points of leave-one-out accuracy to more than double the **Unknown** **Zone**,
and still beats the baseline's best at any setting (76%).

**Migration 9 changes the row only where it still holds 0.5.** `UPDATE settings SET value = ? WHERE key =
? AND value = ?` — a database whose owner has already tuned the radius keeps what they chose. A migration
must never undo a setting, and deleting the row to let it re-seed would throw away a tuned value without
being able to tell it from an untouched one.

## Consequences

A fresh install works immediately and gets better by itself: it behaves exactly as the baseline did,
fetches 85MiB once in the background, and improves as its cache fills, with a line on the page while that
is happening. Nothing to install first and nothing to run first.

**wallpapi now downloads something on first boot.** That is new, and it is the main thing a reader of this
ADR should weigh. It is one file, from a pinned revision, checksummed, to a path the user can delete — and
if it fails, everything still works, one notch cruder, and says so.

The embedding cache is a provider-owned SQLite file (`~/.wallpapi/embeddings.db`), about 4KiB a
**Wallpaper**, so roughly 8MB for a **Pool** at its default target. No migration of `wallpapi.db` was
needed for it and deleting the provider is deleting a file.

`SCHEMA_VERSION` is 9. Anyone reading a **Score** should know it is now derived from the picture rather
than from the palette: two **Wallpapers** with the same colours are no longer necessarily alike, which is
the whole point and also a behaviour change for anybody used to the old **Zones**.

The **Similarity radius** and **Similarity decay** remain settings and remain starting points. They have
now been measured once, against a synthetic **Decision log**. Measuring them against a real one is what
`scripts/similarity_spike.py` is kept for.

## Alternatives considered

**The tag provider.** Better than the baseline, never once wrong on a held-out **Verdict**, and the only
one leaving a usable **Unknown** **Zone** at the old radius. Rejected on cost alone: a permanent, recurring
claim on the one budget that also keeps the **Pool** stocked, plus a dependency on strangers having tagged
things and a blind spot for exactly the **Wallpapers** that have just arrived. Kept selectable.

**Rescaling the embedding provider's similarities to fill `[0, 1]`,** so that a radius of 0.5 would keep
meaning something. Rejected: it hides a per-provider calibration constant inside the provider where nobody
can check it, and the radius is already a setting whose whole purpose is to be the thing that changes.

**The float32 model** (335MiB against 85MiB). Rejected: what is asked of the encoder is a ranking, the
quantised export produced ten mountains out of ten for a mountain, and ONNX Runtime's CPU kernels are
fastest on int8. `MODEL_FILE` and its checksum are the only two lines that would change.

**Embedding on the request path, as thumbnails are fetched.** Rejected: the first one would pay an 85MiB
session load inside a page render, and a **Batch** of 32 new tiles would pay 32 model runs at once.

**Deleting the two losing providers.** Rejected by the maintainer, and rightly: the comparison that ranked
them ran on a **Decision log** that nobody has ever judged a **Wallpaper** into.
