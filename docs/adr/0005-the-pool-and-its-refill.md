# 5. The Pool and its refill

Date: 2026-09-27

## Status

Accepted

## Context

Until now a **Batch** was a live Wallhaven search on the page load path. #2 made it one **API call** per
**Batch** minted rather than per page load; #3 made it up to four, walking pages carrying `meta.seed` when
past **Bans** left a page short. Both were scaffolding with an expiry date written into them.

Three things come due at once here.

**The page load path must stop calling Wallhaven.** A user waits for it, it is capped at four calls because
of that, and — issue #15 — a transport failure on the first call propagates out of the **Core service** and
the browser gets a 500. The #3 rescue deliberately did not widen to cover it, because widening it would
have closed #15 by accident with a page nobody had designed.

**The Filters need somewhere to be applied.** Minimum resolution and allowed ratios are query parameters;
minimum **Favourites** is not, because Wallhaven's search has no parameter for it. Purity is fixed to SFW
and every category stays on.

**The maintainer's intent for the refill differs from the acceptance criterion as written.** The criterion
says the refill runs "when the **Pool** drops below" a target size. The intent recorded on the issue is for
wallpapi to make full use of the 45 **API calls** a minute so there is always a backlog to process, rather
than topping up and then sitting idle. Those are different designs: one treats the target as a ceiling, the
other as a floor that triggers urgency and never stops.

## Decision

**A Pool, filled by a background thread, and a Batch drawn from it locally.** `get_next_batch` samples the
**Pool** and makes no **API call** at all.

**One setting, `pool_target_size`, defaulting to 2000.** The refill spends the full 45-per-minute budget
whenever the **Pool** is below target and idles — a cancellable wait, re-checking every 30 seconds — once it
is at or above. There is no separate floor and ceiling. This satisfies the criterion literally, and it
satisfies the intent by making the default large: 2000 **Wallpapers** is a backlog nobody works through in
an evening, and the refill is at full speed for as long as it takes to build one.

The alternative — budget-driven with no ceiling, pulling for as long as the **Filters** yield anything new —
was rejected. Invariant 2 sizes the **Similarity provider**'s matrix off the **Pool**, and every
whole-**Pool** operation (the **Scoring** at #9, the **Filter** prune here) is linear in it. Unbounded
growth is a system that gets slower every day and never says why. `MAX_POOL_TARGET_SIZE` is 20,000 for the
same reason; the person who wants a bigger backlog turns one number up and can see what it costs.

**Foreground priority is moot.** The question the issue raised — does a **Batch** mint outrank the
background thread for the budget in one minute — has no answer to give, because after this ticket a
**Batch** mint costs nothing. The #3 walk is therefore deleted whole rather than left dormant behind the
**Pool**: `_gather_candidates`, `MAX_SEARCHES_PER_BATCH`, the mid-walk rescue and their tests are gone. The
`seed` parameter on the client stays — the refill is what uses it now.

**Pool membership is its own table**, `pool(wallpaper_id, fetched_at, source)`, never a column on
`wallpapers`. A **Wallpaper** leaves the **Pool** while the **Decision log** goes on referring to its row
for ever, so "is it in the **Pool**" and "does this row exist" must not be the same question. `source` is
`'random'` or, since #13, `'like'` — see the extension below.

**Batch building is a uniform random sample of the Pool, minus anything whose resolved Verdict is Ban.**
Other **Verdicts** may reappear: an **Ignore** that removed a **Wallpaper** for ever would be an
undocumented second **Ban**. The revisit weight at #11 tunes how often; **Zones** and **Mixes** at #9 and
#10 replace the uniform draw outright. ADR 0002's re-read-under-the-write-lock rule stands, and is cheaper
now that what precedes it is local reads rather than a network call.

**Filters are settings, and every Wallpaper entering the Pool is checked locally against all of them.**
Not only the minimum **Favourites**, which is the one Wallhaven cannot do. The API is trusted but not
relied upon: a parameter silently ignored, or a response shaped differently from the documentation, must
not be able to put a NSFW or 1280x720 **Wallpaper** in front of somebody. The ratio check is a band rather
than an equality, because Wallhaven's `ratios=` buckets — a 3440x1440 ultrawide is 2.39 and it is served
under `21x9`, which is 2.33 — and a strict local check would prune what the API correctly returned.

**Changing the Filters prunes the Pool.** Every member that no longer passes loses its membership row,
decided or not. **Pool** membership governs one thing — what may be *shown* — so a **Liked** 1080p
**Wallpaper** must stop appearing once the minimum is raised to 1440p exactly as an undecided one does, and
an **Ignored** one certainly must. A **Verdict** is not an exemption from the **Filters**.

Nothing is lost by it, because only the membership row goes. The `wallpapers` row and every **Decision
log** entry stay — the log is append-only, **History** at #7 renders a thumbnail for each of them, and a
**Clearance** there works on the log rather than on **Pool** membership. The live unsubmitted **Batch** is
untouched for free, because a **Batch** holds `batch_wallpapers` rows rather than **Pool** membership, so
nobody loses the **Draft Batch** they are part way through.

The prune runs after every settings write rather than only after one that named a **Filter**. The rule is
"the **Pool** holds no **Wallpaper** that fails the current **Filters**", which is one rule; the
alternative is that rule plus a list of which fields count as **Filters**, kept in step by hand.

**The rate limiter is a pure function** over **API call** timestamps — `wait_needed(call_times, now)` — and
the caller does the waiting (invariant 11). A sliding window, not a per-minute bucket, which would allow 45
calls at 11:59:59 and 45 more a second later. Monotonic seconds from the injected clock, never wall-clock
times, so an NTP correction cannot empty the window.

**The refill loop body is a Core service method.** `refill_wait()` says how long to wait and
`refill_step()` takes one step; `refill.py` is a loop over the two of them and nothing else. That split is
what lets the whole 45-per-minute budget, the seed carried across a walk, and every back-off be tested with
a fake clock, no thread, and no real time passing. The thread is started in the FastAPI lifespan and joined
on shutdown with a timeout greater than the client's 10-second request timeout (invariant 12); every wait
inside it is `stop_event.wait(n)`.

**`refill_step` never raises.** A transport error, a non-200, anything the client throws: it is recorded as
the refill's last error with the clock's time, it becomes a back-off, and the loop goes on. That is what
closes #15 from the other end. On a 429 the client raises `RateLimited` and Wallhaven's `Retry-After` is
honoured if it sent one; otherwise the back-off is 60 seconds, which is the window the budget is counted
over.

**Batch unavailable carries a reason and the error.** `POOL_EMPTY` is "nothing has arrived yet" and
`WALLHAVEN_UNREACHABLE` is "the last refill attempt failed", with the message and the time on the result so
the page can say them. `NO_RESULTS` is retired: nothing produces it any more. The page renders both at 503,
never a 500.

**Refill status is observable through the Core service** — **Pool** size, target, last run, last error,
whether the thread is alive — and is one line on every state of the **Batch** page. On the unavailable page
most of all: an empty **Pool** and a dead refill thread look exactly alike without it.

**A random refill walk carries `meta.seed` across its pages and starts a fresh seed on the next walk.**
Reusing a seed across runs returns the same **Wallpapers** (`AGENTS.md`). A walk ends when the **Pool**
reaches target or a page comes back empty. `meta.last_page` is returned by Wallhaven but is not carried:
on the captured random SFW search it was 14,055, which is not a stop condition, and an empty page is the
real end of a walk.

### Extended at issue #13 — like: searches

**Two strategies, taking strict turns, inside the one `refill_step`.** The random walk above feeds the
**Unknown** **Zone**; a like: walk searches `q=like:<wallhaven id>` on a **Favourite** and feeds the
**Banger** one. Step by step they alternate while both have work, and with no **Favourites** every step is
random.

Turns rather than a share of the budget, a period, or a priority. With one **API call** per step, "take
turns" *is* an even split, so "neither starves the other" needs no number for anybody to set wrong — and
the 45-per-minute budget covers both strategies without a second limiter, because what the limiter counts
is steps. Which strategy ran last is on `refill_status()` and so on the indicator: a **Pool** growing only
at random means an empty **Favourites** list, which is something the user can act on.

**A like: walk picks one Favourite at a time, and every Favourite gets a turn before any gets a second.**
The subject is drawn through the injected random source from the **Favourites** not yet walked this cycle;
when all of them have been, the cycle restarts. Without that rule a seeded draw is free to spend every
like: step on one **Wallpaper** while the rest of the user's taste goes unasked about.

Which **Wallpapers** are **Favourites** is resolved from the **Decision log** on every step rather than
kept in a list of subjects. A **Favourite** replaced by a **Like** or a **Ban**, or **Cleared** at #7,
therefore leaves the rotation by itself — mid-walk if need be — and there is no second copy of the truth
to drift out of step.

**A like: walk is capped at three pages per Favourite, or an empty page, whichever comes first.** A like:
result set is small and its tail is only weakly similar, so paging to the end would spend an unbounded
share of the budget on one **Favourite**'s worst matches. Three pages is 72 **Wallpapers** at Wallhaven's
listing size. The sort is `relevance` rather than `random` for the same reason: most similar first is the
whole point of the search, and Wallhaven's default of `date_added` would scatter them.

**The like: walk's seed and page are kept entirely apart from the random walk's.** The two interleave step
by step, so one shared position would have each clobbering the other's every other call. The like: walk
also keeps its place when the **Pool** reaches target, where the random one resets: only the random walk
has the seed trap, and `like:<id>` answers the same way whenever it is asked.

**Results go through the same `_admit_to_pool` and the same Filters**, tagged `source = 'like'`. A
**Wallpaper** the random walk already found keeps the `source` and `fetched_at` it arrived with — meeting
it again is not a second arrival — which the membership insert's `DO NOTHING` gives for free.

**A failed like: call is a failed call and nothing more:** recorded, backed off, never raised (#15), and
the page it failed on is retried rather than skipped. The turn is spent either way, so the step after the
back-off belongs to the other strategy; a strategy that could retry its way through the whole budget would
be exactly the starvation the alternation exists to prevent.

## Consequences

The **Batch** page is instant and cannot fail on the network, because it does not touch it. Wallhaven being
down is now a page that says so and a **Pool** that drains rather than an error.

The refill spends real API budget in the background from the moment the app starts. That is the intent, and
it is the reason the app must stay single-worker: `uvicorn --workers N` would be N threads each spending 45
calls a minute against one SQLite file.

Everything downstream that needs "the **Pool**" now has one. #9 scores it, #10 allocates **Batches** across
its **Zones**, #11 weights revisits within it, #13 adds a second `source`.

The first run of a fresh install shows the unavailable page until the refill has fetched a page. That is
correct and it is why the reason and the indicator exist; a first **Batch** is a few seconds away.

## Alternatives considered

**Budget-driven refill with no ceiling.** The maintainer's intent read literally. Rejected above: it makes
the **Pool** grow without limit, and every whole-**Pool** operation with it. The large default target gets
the same "always a backlog" behaviour with a number the user can see and change.

**A floor and a ceiling.** Refill hard below the floor, slowly between floor and ceiling, stop above. More
faithful to "a floor that triggers urgency", and rejected as two settings and three states where one
setting and two states produce the same behaviour anybody would notice.

**`in_pool` as a column on `wallpapers`.** Rejected: it conflates membership with existence, leaves
`fetched_at` and `source` nowhere to live that is about the image itself, and makes "everything in the
**Pool**" a scan of every **Wallpaper** ever seen rather than of the **Pool**.

**Leaving the #3 walk in place as a fallback for an empty Pool.** Tempting, because it would make the first
page load after a fresh install work. Rejected: it is a second, untested **Batch**-building path that only
ever runs in the situation where Wallhaven is least likely to answer, and it would quietly restore the 500
that #15 is about.

**Pruning only the undecided members on a Filter change.** Built first, and rejected on review. The
argument for it was that a tightened **Filter** should not undo a decision, and that a **Clearance** at #7
would want the **Wallpaper** still in the **Pool**. Neither holds: nothing about a decision is touched
either way — only the membership row goes, and the **Decision log** is where a **Clearance** does its work
— while the cost is real, because it would leave a **Liked** 1080p **Wallpaper** reappearing in **Batches**
after the minimum was raised to 1440p, which is precisely what the **Filter** was changed to stop.
