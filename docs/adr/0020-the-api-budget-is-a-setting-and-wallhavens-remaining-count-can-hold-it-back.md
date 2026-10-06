# 20. The API budget is a setting, and Wallhaven's remaining count can hold it back

Date: 2026-10-06

## Status

Accepted. Amends ADR 0005: the **Refill** no longer paces itself against Wallhaven's whole 45 **API calls** a
minute. The sliding window, `wait_needed` as a pure function, the back-off and single-worker running are
unchanged.

## Context

Wallhaven allows 45 **API calls** a minute per IP address, not per process. ADR 0005 paced the **Refill**
against all 45 and counted only its own calls. So while the **Refill** ran at full rate, at a cold start or
after a **Filter** prune took the **Pool** below target, any other API client on this machine got 429s, and
so did a second wallpapi process. Issue #14 saw a 429 when two processes shared one minute.

Wallhaven's search response carries `X-RateLimit-Remaining`, its own count of what is left this minute on
this IP. That count sees traffic the **Refill** never made. Wallhaven sends no reset time with it.

## Decision

**The API budget is a setting: `api_calls_per_minute`, 1 to 45, default 35.** `Refill.wait` passes it to
`wait_needed` as the limit, read at every wait, so lowering it takes effect on the next call. A window
already fuller than a lowered budget waits until enough calls age out, not a whole fresh minute.
`pool.CALLS_PER_MINUTE` stays 45 as Wallhaven's own limit: the budget's ceiling and the call-time deque's
cap. A database from before this decision has no row, and `settings.get` falls back to the default, so there
is no migration.

**Wallhaven's count holds the Refill back.** The client parses `X-RateLimit-Remaining` into
`SearchPage.remaining`. A header that is absent or not a whole number is `None`. `step` records it and the
monotonic time the answer arrived. When `remaining <= 45 - budget`, other traffic has used the share the
budget leaves it, and `wait` holds until a minute after that answer. A minute because there is no reset
header, and asking sooner would spend a call to find out. `wait` returns the longest of four waits: idling at
target, the local window, the back-off and this hold. The stricter of Wallhaven's count and ours wins.

**The latest answer decides.** A later answer replaces the count, and an answer with no header clears it,
so a missing header behaves exactly as before. A failed call clears it too. A 429's `Retry-After`, or the
default back-off, is the fresher word.

**No gatekeeper.** The **Refill** is the only API caller, and any future caller shares its limiter. A wrapper
around the client cannot wait cancellably without the caller's `stop_event`, so the thread still waits with
`stop_event.wait(n)` (invariant 12).

## Consequences

- At the default, a cold start fills the **Pool** at 35 calls a minute instead of 45. That is 840
  **Wallpapers** a minute at most, before **Filters**, so a first **Batch** is still seconds away.
- The **Refill**'s own calls lower Wallhaven's count too. Alone on the IP, a budget of 35 that has spent 35 is
  answered 10 left and holds for a minute from that answer. The local window would have held it anyway.
- Thumbnail and full-resolution fetches go to other hosts and are not **API calls**. They neither spend the
  budget nor are paced by it.
- Neither the budget nor the remaining count shows in the refill status. That is the refill indicator's work,
  not this decision's.

## Alternatives considered

**A fixed lower limit.** Simpler, but how much to leave depends on what else runs on the machine, and only
the user knows that.

**A gatekeeper module wrapping the client.** It would put the limiter in front of every caller. But it would
have to sleep, and invariant 12 wants every wait cancellable on the caller's `stop_event`. There is only one
caller.

**Probing for a reset.** Wallhaven gives no reset time. Asking again sooner to see whether the count has
risen costs the call the hold exists to save.
