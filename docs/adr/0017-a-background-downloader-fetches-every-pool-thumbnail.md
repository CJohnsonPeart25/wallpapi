# 17. A background downloader fetches every Pool thumbnail, so the whole Pool is embedded

Date: 2026-10-02

## Status

Accepted. Amends ADR 0013's "the Thumbnail cache is the work list" paragraph: the work list is unchanged,
but it now covers the **Pool** rather than only what has been on screen. Amends ADR 0009's last
consequence: thumbnail fetches are now paced, and full resolution is still not.

## Context

`EmbeddingSimilarityProvider.catch_up` embeds whatever is in the **Thumbnail cache** (ADR 0013). Until
now the only thing that wrote to that cache was `CoreService.get_thumbnail`, and its only caller was the
thumbnail route as a tile rendered. The refill never fetches a thumbnail. A **Pool** member that had never
been shown therefore had no **Embedding**, and every pair it was in fell back to
`MetadataSimilarityProvider`.

#38 measured this on the maintainer's database: 175 vectors in `embeddings.db` against 1,912 non-banned
**Pool** members. About 90% of the **Pool** was being scored by colour and category, against a
**Similarity radius** of 0.15 that ADR 0013 tuned for CLIP distances. By #44's measurement it was 344
vectors against a **Pool** of 2,012.

## Decision

**Two independent workers, connected only by the Thumbnail cache.** A new background thread,
`wallpapi-thumbnails`, is the downloader. It fetches thumbnails and does nothing else. The existing
`wallpapi-similarity` thread stays the embedder, and its `catch_up` is unchanged: it already embeds any
cached file that has no vector, and it still knows nothing about the **Pool**.

- The downloader's decisions are a **Core service** step, `thumbnail_wait` and `thumbnail_step`, because
  the Core service owns the Wallhaven client and the **Thumbnail cache**. `thumbnail_thread.py` is a loop
  around those two and nothing else, the shape of `refill.py`.
- It is not a third duty for the similarity thread, so a fresh install's 85MiB model download cannot hold
  up thumbnail fetching. It never runs inside `refill_loop`, never on a request thread, and never on the
  similarity thread. `test_thumbnail_thread.py` checks that every fetch is made from `wallpapi-thumbnails`.
- It is started only by the `refill` flag on `create_app`, which `build_app` sets. That flag now starts
  three threads.

**The rule: any Pool member without a thumbnail file is fetched.**

- **A pass** is the **Pool** members missing a file, listed once in `wallpaper_id` order and then taken
  one per step. `wallpaper_id` is the primary key, so the order is deterministic in tests. There is no
  lookalike-first priority: at this pace a whole **Pool** takes minutes, so a priority would buy nothing.
- **One at a time, 0.25s apart.** `ratelimit.THUMBNAIL_GAP_SECONDS` is a constant, not a setting.
  `th.wallhaven.cc` is not the 45-per-minute API, but it sits behind DDoS protection with no published
  limit (invariant 11), and it is the host the user's own browser loads thumbnails from when browsing
  Wallhaven. About four a second looks like one person browsing.
- **Pacing is a pure function**, `ratelimit.gap_needed(last_fetch, now=...)`, over the injected clock's
  monotonic time, in the shape of `wait_needed`. The Core service says how long; the thread waits with
  `stop_event.wait(n)` (invariant 12).
- **Rechecked before each fetch.** A file that now exists, because the tile route fetched it during the
  pass, is skipped. So is a **Wallpaper** that has left the **Pool** since the pass was listed. A race
  with the tile route is still possible and harmless: both writes are atomic, temp file then `os.replace`,
  to the same path, so it costs one extra request at most and never corrupts anything.
- **Nothing missing: look again in 30s** (`THUMBNAIL_IDLE_RECHECK_SECONDS`). **The next pass also waits
  30s after this one ends.** Without that, a file the host refuses every time would be asked for four
  times a second whenever it was the only one missing.
- **Failures.** A refusal (`ThumbnailUnavailable`, any non-2xx other than 429) skips that file until the
  next pass, and the next member follows after the ordinary gap. A 429 or a connection error backs off
  60s (`THUMBNAIL_BACKOFF_SECONDS`). A `Retry-After` longer than that is honoured, and a shorter one does
  not shorten it. Anything else the client raises is treated like a connection error, as `refill_step`
  does, so the transport's spelling stays out of the Core service. A write the disk refuses costs that one
  file. `thumbnail_step` never raises.
- The real client now maps a thumbnail 429 to `RateLimited` and any other non-2xx to
  `ThumbnailUnavailable`, which is the only change to `wallhaven.py`. The downloader makes **no API
  calls**: it never touches `wallhaven.cc/api` or the 45-per-minute limiter.
- The thread joins at `REQUEST_TIMEOUT + 5`, above the fetch timeout. A stop that lands mid-fetch waits
  for the fetch to return, and the loop goes back to its `stop_event` rather than on to the next member.

**It runs whichever Similarity provider is selected.** `WALLPAPI_SIMILARITY=metadata|tags` reads no
thumbnails, but the downloader runs anyway. That keeps the seam free of a "wants thumbnails" member, and
it makes tile renders cache hits. At the measured ~23KiB a thumbnail, a **Pool** of 500 is about 11MB and
one of 2,000 about 46MB.

**It respects `thumbnail_cache_max_mb`.** Before a pass is listed, if the cache is at or over the cap, the
downloader fetches nothing and looks again in 30s. The size is computed once a pass, not once a file, so a
pass that starts under the cap can finish a little over it. Without the check, a cap below what the
**Pool** needs would churn: the size-cap pass at submission (ADR 0009) deletes **Pool** thumbnails, and
the downloader fetches them straight back. Their **Embeddings** survive in `embeddings.db` either way,
so the churn would achieve nothing. Coverage stalling short of 100% shows up in the notice. The first
eviction pass at submission already deletes the thumbnails of retired **Wallpapers** with no **Explicit
Verdict** (ADR 0016), so the cache tracks the **Pool** as it turns over.

**The request path is unchanged.** The tile route still calls `get_thumbnail` on a cache miss, so a
**Batch** minted ahead of the downloader still renders. The request thread still never embeds: a tile
minted before its **Embedding** existed was scored by the metadata fallback.

**Coverage on the page: `notice(pool)`.** The protocol's `notice()` becomes
`notice(pool: Sequence[Wallpaper]) -> str | None`, the same shape `similarities` already takes
(invariant 2). It is not a new method, and the Core service still does not know which provider it holds.

- The embedding provider counts the **Pool** members it has a vector for. While that count is below the
  **Pool** size it says so: "Image similarity covers 1,240 of 2,000 Pool wallpapers so far — the rest are
  compared by colour and category until their thumbnails are embedded." Once the count reaches the
  **Pool** size it says nothing. An empty **Pool** is not short of anything.
- Its pending, failed and unusable states take precedence over coverage.
- A provider with no model to manage (`model=None`, the test and spike shape) still reports nothing.
- `metadata` and `tags` ignore the argument.
- `CoreService.similarity_notice()` reads the **Pool** and passes it in. The page still calls it with no
  arguments, because the page's seam is the Core service and the **Pool** is storage.

**Masking is left open.** A pair whose **Wallpaper** has no **Embedding** still blends in the baseline's
similarity rather than counting as 0 (an honest **Unknown**). Masking would change **Scores** across the
whole **Pool**, and is decided in #55 with this ticket's coverage numbers in hand.

## Measurements

Taken on 2026-10-02 against the maintainer's `~/.wallpapi`. The database was opened read-only
(`immutable=1`), nothing was written to it, and the traffic was 40 sequential fetches from
`th.wallhaven.cc` only, at the 0.25s gap, of **Pool** members that had no cached thumbnail. These replace
the estimates the gap was chosen on.

| | measured |
| --- | --- |
| Per-thumbnail fetch time | median 27ms, mean 68ms, max 129ms |
| Effective pace at the 0.25s gap | 4.05 a second: 40 in 9.9s |
| Thumbnail size, the 40 fetched | mean 23.2KiB, median 23.3KiB |
| Thumbnail size, the 343 already cached | mean 22.0KiB, median 21.1KiB, max 54.6KiB |
| Model load (`OnnxClipEmbedder.warm`) | 0.23s, file already on disk |
| Per-image embed time, batches of 16 | 6.1ms |
| Per-image embed time, one at a time | mean 8.9ms |

Every fetch is shorter than the gap, so the gap sets the pace and the fetch time barely shows. Embedding
is about forty times faster than fetching, so the embedder never falls behind the downloader. A **Pool**
of 500 takes about 2 minutes to fetch, plus a 30s wait between passes, which is when members admitted
after a pass was listed are picked up.

The end-to-end acceptance criterion (95% of the non-banned **Pool** embedded within
`target × 0.25s + 10 minutes`) was not run. Running it means a fresh `WALLPAPI_HOME` and the refill's
**API calls** to fill a **Pool**, which is more Wallhaven traffic than this measurement was allowed. The
numbers above put a **Pool** of 500 at about `125s + 30s + 3s` of embedding, well inside the
`125s + 600s` the criterion allows.

## Consequences

The embedding provider now covers the **Pool** a **Batch** is drawn from, a few minutes after each
**Wallpaper** arrives. Before this, it covered only what had already been shown. The **Similarity
radius** ADR 0013 tuned for CLIP distances now applies to most pairs rather than about a tenth of them.

**wallpapi now fetches a thumbnail for every Wallpaper it admits**, not only for the ones it shows. That
is the main thing to weigh here. It is about 23KiB each from the thumbnail host the user's browser already
uses, a quarter of a second apart, and none of it touches the 45-per-minute API budget.

Turnover is new work. Since ADR 0016 every submitted **Batch** retires what it showed and the refill admits
newcomers. At about four thumbnails a second, the downloader outruns any realistic rate of submitting
**Batches**: the default `batch_size` is 8 and the default `pool_target_size` is 500.

A cap set below what the **Pool** needs is now visible: coverage stalls and the notice says how far it got.

## Alternatives considered

**Fetch thumbnails inside `refill_loop`, as Wallpapers are admitted.** Rejected. Its waits are the
45-per-minute budget's and the host is a different one, so the two paces would have to share one loop.
A thumbnail outage would also stall the **Pool** refill.

**Make it a third duty of the similarity thread.** Rejected. On a fresh install that thread spends its
first minutes on an 85MiB download, which would hold up the thumbnails it is about to embed.

**Fetch only when the embedding provider is selected**, via a "wants thumbnails" member on the protocol.
Rejected. It is a second seam method for one provider, and the cost of running the downloader anyway is
a few megabytes that turn every tile render into a cache hit.

**Lookalike-first priority**, fetching **Banger** candidates before the rest. Rejected: at four a second,
the whole **Pool** is minutes away, so a priority would buy nothing it could be measured by.

**Concurrent fetches.** Rejected: one at a time is what looks like a person browsing, to a host with no
published limit.

**Retry a failed file straight away, or at the next step.** Rejected. A file the host refuses would then
be asked for four times a second. It waits for the next pass instead.

**Check the cap once a file rather than once a pass.** Rejected as a directory scan on every step for a
backstop that, at the default 500MB, ordinary running never reaches. The cost is that one pass can end a
little over the cap.
