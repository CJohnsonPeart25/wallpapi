# 9. Thumbnails are evicted by Verdict, with a size cap behind it that may not override one

Date: 2026-09-27

## Status

Accepted

## Context

Invariant 8 has said since #2 that the **Thumbnail cache** needs "verdict-aware eviction", and #2 shipped
the serving seam with an unevicted directory and a deferral pointing here.

The obvious rule — evict when a **Wallpaper** leaves the **Pool** — is wrong, and invariant 8 says why.
**History** renders a thumbnail for every **Wallpaper** that has ever been judged, and a **Banned**
**Wallpaper** leaves every **Zone** immediately and permanently while still needing a picture on the one
page where the **Ban** can be undone. **Pool** membership governs what may be *shown*; it says nothing
about what may be *remembered*.

AGENTS.md also left a second job here: databases written before ADR 0002 hold **Batches** that can never be
submitted, **Wallpapers** with neither a **Verdict** nor a place in the **Pool**, and the thumbnails those
**Wallpapers** left behind.

## Decision

**`evict_thumbnails()` is one Core service operation, run at the tail of `submit_batch` after the Library
reconciliation.** A submission is the moment the answer changes: every **Wallpaper** just shown now has an
entry, and the ones that resolve to an **Ignore** and are no longer in the **Pool** will never be asked for
again. It is called there and nowhere else, and it is cheap when there is nothing to do — an empty or
absent directory returns immediately, and in ordinary running every file belongs to a **Pool** member or a
decided **Wallpaper**, so the first pass deletes nothing and the second never runs.

**Pass one: evict everything nothing will ever ask for.** A cached thumbnail goes if its **Wallpaper** has
no **Explicit Verdict** standing, is not in the **Pool**, and is not in the live **Batch**. The live
**Batch** is named separately from the **Pool** because a **Filter** change prunes the **Pool** and
deliberately leaves the **Batch** on screen alone, so a tile in front of the user can be in one and not the
other.

This sweeps the pre-ADR-0002 leftovers for free, including files whose `wallpapers` row was never written:
the filename is the whole of the mapping from a file to a **Wallpaper**, so a stem matching no row is
matched by no protection either. Their `wallpapers` rows, where they exist, are left alone — a row is
bytes, a thumbnail is a file.

**Pass two: a size cap, `thumbnail_cache_max_mb`, default 500.** Over it, the oldest-modified of the
*still-to-be-shown* thumbnails go until the cache is under it. These are the ones pass one protected
because they are **Pool** members, and evicting one costs exactly one re-fetch from `th.wallhaven.cc` the
next time it is shown.

**A thumbnail whose Wallpaper has an Explicit Verdict is never evicted, by either pass.** The cap does not
get to override the rule invariant 8 states. A cache that is over the cap on **Favourites**, **Likes** and
**Bans** alone therefore stays over it, and `ThumbnailEviction.over_cap` says so rather than hiding it.
The alternative is a tool that silently deletes the pictures of everything the user has ever liked because
a number in a settings box was too small.

**The setting needs no migration.** `get_settings` already falls back to `_defaults()` for a key with no
stored row, and `_defaults()` is what migrations 3 and 5 seed from, so a fresh database gets a row and an
existing one reads the default. `SCHEMA_VERSION` stays at 5.

## Consequences

Eviction is safe to be wrong about, which is what lets the rule be simple: `get_thumbnail` re-fetches a
file that is not there, so the cost of an over-eager eviction is one request to an image host, not a broken
page. That is also why nothing here is transactional and why a file that vanishes mid-pass is skipped
rather than raised on — this runs after a **Batch** has already been recorded and must not turn that into
an error.

A **Clearance** withdraws the protection along with the **Verdict**. A **Wallpaper** whose **Ban** is
**Cleared** keeps its **History** row and loses its cached thumbnail at the next submission; the row
re-fetches it when it is next rendered.

Invariant 11's second half still applies and is still deferred: eviction makes thumbnail re-fetches
slightly more likely, and those fetches remain unthrottled because they happen on a request thread with no
`stop_event` to wait on cancellably.

The cap is a backstop, not a budget. With the default **Pool** target of 2000 and Wallhaven thumbnails at
tens of kilobytes, five hundred megabytes is never reached in ordinary use and eviction is decided entirely
by **Verdict**.

## Alternatives considered

**Evict when a Wallpaper leaves the Pool.** Rejected by invariant 8 before this ticket started: it deletes
exactly the thumbnails **History** needs, and it deletes a **Ban**'s first.

**Let the size cap evict anything, oldest first.** Rejected: it makes the cap silently override the
**Verdict** rule, and the files it would reach first are the oldest **Favourites** — the ones most likely
to be scrolled past in **History** and least likely to be in the **Pool**.

**Track a last-served time per thumbnail in SQLite and evict least-recently-used.** Rejected as a second
store to keep in step with a directory, for a cache whose correct contents are already derivable from the
**Decision log** and the **Pool**. It would also mean a write on every tile served.

**A background eviction thread.** Rejected as unearned. The work is proportional to the number of files in
one directory, it runs once per submission, and #6's refill remains the one place a thread is justified.

**Evicting on a **Clearance** or an edit as well as on submit.** Rejected for now: it would put a directory
scan on a single-row htmx post, and the next submission picks it up. Said here so it is a deferral rather
than an oversight.
