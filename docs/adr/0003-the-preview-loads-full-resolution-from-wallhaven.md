# 3. The fullscreen preview loads full resolution straight from Wallhaven

Date: 2026-09-27

## Status

Retired by epic #32: replaced by the `thumbnails` module (#66), which fetches and serves every thumbnail
wallpapi shows. That full resolution is never cached stands, as a deferred decision in `AGENTS.md`.

## Context

Issue #8 asks that any **Wallpaper** can be opened fullscreen at full resolution, because a thumbnail is
not enough to judge how an image will look filling a monitor. That is a second image source for a page
that already has one, and invariant 8 is explicit about the first: cache thumbnails locally and serve them
from the app; do not hotlink 32 tiles per **Batch**.

Read literally as "wallpapi never points a browser at Wallhaven", invariant 8 would force the preview
through a local cache too — which means downloading a multi-megabyte file per preview, storing it
somewhere, and then writing the eviction policy for that store. The **Library** (#5) already exists to hold
full-resolution files, and it holds exactly the ones worth keeping: **Favourites**. A second
full-resolution store would duplicate it for images the user has, by definition, not yet decided they want.

Read for what it is protecting, invariant 8 is about the *tiles*: a **Batch** of 32 is 32 unavoidable
requests to `th.wallhaven.cc` on every page load and every refresh, from a host behind DDoS protection
with no published limits (invariant 11). None of that is true of a preview.

## Decision

The preview `<img>` loads `wallpaper.full_url` directly from Wallhaven. Nothing about thumbnail serving
changes: tiles still come from `/thumb/{id}` off the **Thumbnail cache**.

Two consequences that are not optional:

- **Nothing is fetched until it is asked for.** The markup ships the URL in a `data-full-url` attribute on
  the tile's preview control, never as an `<img src>`. `wallpapi.js` sets the `src` when the dialog opens
  and removes it again when the dialog closes. A **Batch** of 32 tiles therefore costs zero requests to
  `w.wallhaven.cc` until somebody clicks preview, and a dismissed preview stops downloading.
- **Full-resolution images are never cached.** The only full-resolution files wallpapi keeps are
  **Favourites** written to the **Library**. If a preview cache ever looks tempting, the thing it would be
  optimising is a user-initiated click at human speed.

## Consequences

The preview is the one place the browser talks to Wallhaven directly, and it is the one place where an
offline wallpapi shows a broken image rather than degrading gracefully. That is acceptable: the **Batch**
itself came from Wallhaven, so an offline instance has nothing new to show anyway (#6 owns that behaviour).

Rate limiting does not apply. Invariant 11's 45 per minute is the **API call** budget for
`wallhaven.cc/api`; `w.wallhaven.cc` is an image host, and one click is one request.

Verdict-aware thumbnail eviction at #7 inherits nothing from this. There is no second store to evict.

## Alternatives considered

**Cache full-resolution images the way thumbnails are cached.** Rejected: it duplicates the **Library** for
images nobody has chosen to keep, and it needs its own eviction policy for files two orders of magnitude
bigger than a thumbnail. The disk cost of caching every previewed image is unbounded and the benefit is a
faster second look at an image the user is about to give a **Verdict** to and move on from.

**Preview the cached thumbnail, scaled up.** Rejected because it defeats the point. The reason to open a
**Wallpaper** fullscreen is to see detail, banding and noise at the size it will actually be displayed;
a 300px thumbnail stretched to 4K shows none of that and would make the feature a lie.

**Proxy the full-resolution image through the app without storing it.** Rejected: it spends wallpapi's
bandwidth and a request thread to hide a URL the tile already links to publicly a few pixels away, and the
synchronous core would be holding a threadpool worker for the length of a multi-megabyte download.
