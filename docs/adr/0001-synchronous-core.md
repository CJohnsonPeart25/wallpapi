# 1. A synchronous core behind an async web framework

Date: 2026-09-24

## Status

Accepted

## Context

wallpapi serves a local web page from FastAPI, an ASGI framework whose idiomatic style is `async def`
throughout: `httpx2.AsyncClient` for Wallhaven, an async SQLite driver, and `asyncio` tasks for the background
**Pool** refill.

Three things push the other way.

The **Core service** is the only seam the UI and every test enters through. If it is async, every test is an
async test, needs an event loop fixture, and the fakes for all five injected dependencies have to be async too.
That is a permanent tax on the thing the project tests most.

The standard library's `sqlite3` is synchronous, and the alternatives are a third-party async driver or running
it in a threadpool anyway. SQLite is an in-process embedded database; there is no network wait to overlap.

The load is one user on `127.0.0.1` clicking tiles. There is no concurrency problem to solve.

Against that, FastAPI runs a path operation declared with plain `def` in an external threadpool (anyio, 40
threads by default) rather than blocking the event loop. So a synchronous core costs nothing at this scale, and
a `threading.Thread` started in the lifespan does not consume a threadpool token at all. Both verified against
the installed stack: the default limiter reports 40 tokens, and a running `threading.Thread` leaves
`available_tokens` untouched.

## Decision

The core is synchronous throughout. Synchronous `httpx2.Client`, standard library `sqlite3`, plain `def`
endpoints, and the background **Pool** refill as a `threading.Thread` started and stopped in the FastAPI
lifespan rather than an `asyncio` task.

Consequences that follow and are not optional:

- One SQLite connection per thread. `check_same_thread` defaults to `True`, and the threadpool hands out a
  different thread per request.
- Every connection is opened with `isolation_level=None` and writes use an explicit `BEGIN IMMEDIATE`. Under
  Python's default legacy transaction control, `sqlite3` emits a plain `BEGIN` before DML on its own, and a
  transaction that upgrades from reader to writer gets `SQLITE_BUSY_SNAPSHOT` immediately with the busy handler
  skipped — `busy_timeout` does not help.
- The app runs single-worker. `--workers N` would mean N refill threads and N writers against one database
  file.
- Every wait in the refill thread is `stop_event.wait(n)`, and the httpx2 timeout is set below the shutdown
  join timeout, so shutdown cannot hang on a thread stuck mid-request.

## Consequences

Tests through the **Core service** are plain function calls. No event loop, no async fixtures, no async fakes.

The threadpool is a shared capped resource. At one user it is irrelevant, but a future endpoint that blocks for
a long time is now a queueing problem rather than a coroutine that yields. The cap is visible and adjustable via
`anyio.to_thread.current_default_thread_limiter()`.

If wallpapi ever became multi-user or network-served, this would need revisiting. The spec puts multiple users
and cloud sync out of scope, so that is a rewrite trigger rather than a risk being carried.

## Alternatives considered

**Async throughout.** Idiomatic for FastAPI, and the natural choice if the bottleneck were network concurrency.
Rejected: it makes every test async to buy concurrency a single-user local tool does not need, and SQLite would
end up in a threadpool regardless.

**Async web layer over a synchronous core.** Async endpoints calling the core via `run_in_threadpool`. Rejected
as strictly worse than plain `def` endpoints: the same threadpool, more ceremony, and an easy mistake where
someone calls the core directly from an async endpoint and blocks the event loop.
