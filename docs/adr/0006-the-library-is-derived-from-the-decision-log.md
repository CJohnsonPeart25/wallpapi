# 6. The Library is derived from the Decision log, not written as a side effect of a click

Date: 2026-09-27

## Status

Accepted

## Context

**Favouriting** a **Wallpaper** puts its full-resolution image in the **Library** folder; replacing that
**Favourite** with a **Like** or a **Ban** takes it away again. The obvious implementation is to make the
submit path do both: while appending the **Batch**'s **Verdicts**, download the new **Favourites** and
delete the files of the ones that stopped being **Favourites**.

Three things make that harder than it looks.

The **Decision log** is appended in a single `BEGIN IMMEDIATE` transaction (invariant 6), and a download is
a network call to `w.wallhaven.cc` that can take ten seconds or fail outright. Holding SQLite's write lock
across it would block every other writer for the duration, and a failure inside the transaction rolls back
**Verdicts** the user actually gave.

**Verdict resolution** is derived, not stored: a **Wallpaper**'s current **Verdict** is whatever its latest
**Explicit Verdict** says, computed from the log on every read (invariant 4). Anything that tracks
"**Favourites** added" and "**Favourites** removed" as events is a second, parallel account of the same
thing, and the two will disagree the first time one of them is interrupted.

And the **Library** is write-only. Nothing reads it back, which is what makes "file deleted in Explorer,
**Decision log** unchanged" a state the spec guarantees is reachable. So the folder cannot be the record of
what is in the folder.

## Decision

**One operation, `reconcile_library()`, makes the folder agree with the log.** It writes a file for every
**Wallpaper** whose *resolved* **Verdict** is **Favourite** and which has no recorded **Library** file, and
removes the recorded file of every **Wallpaper** that has one and is no longer a **Favourite**. Nothing
else. It is expressed as a condition over the **Decision log**, not as a queue of pending work, so there is
no second account to drift.

That makes it idempotent, and idempotence is what buys everything else: a download that fails is simply
still a **Favourite** with no file, and the next call picks it up with no bookkeeping, no retry counter and
no dead-letter anything.

**It runs at the tail of `submit_batch`, after the transaction has committed — never inside it.** No
network call happens inside a write transaction, and nothing the **Library** does can roll the
**Decision log** back. A **Favourite** is a fact about taste; it is not a claim that a download succeeded.

**Failures are collected and returned, not raised.** `LibraryReconciliation` reports `written`, `removed`
and `failed` by **Wallpaper** ID. Raising would turn a **Batch** that *was* recorded into an error page and
tell the user their **Verdicts** had not landed when they had. Returning them keeps the option of saying so
later (deferred to #15) and lets a test tell "nothing to do" from "tried and failed".

**The recorded absolute path is the record, and deletion only ever targets it.** Migration 4 adds
`library_files (wallpaper_id PRIMARY KEY, path, written_at)`. `path` is what the writer returned, not what
it was handed and not anything recomputed from the **Library** setting — that setting can change, and the
file does not move when it does (invariant 9). `remove` tolerates the file already being gone.

**The destination is computed at write time; the recorded path is used at delete time.** Those are
deliberately different sources. New files go wherever the setting points today; old files are deleted where
they actually are.

**The writer downloads and writes atomically, and the Core service decides where.** The `LibraryWriter`
protocol stays `write(wallpaper_id, source_url, destination) -> Path` and `remove(path)`.
`DownloadingLibraryWriter` streams `source_url` — `w.wallhaven.cc`, an image host, not an **API call**
(invariant 11) — to a temp file inside the **Library** folder and `os.replace`s it into position
(invariant 10), creating the folder if this is its first file. The naming rule,
`{wallhaven_id}{suffix of full_url}`, lives in the Core service because the Core service is what records
the result.

**A file deleted in Explorer is not noticed and not replaced.** The recorded row still says the file is
there, and nothing reads the folder to find out otherwise. That is not an oversight to fix later: the
alternative is stat-ing every recorded path on every reconciliation and re-downloading files the user
deliberately deleted.

## Consequences

The **Library** can be rebuilt from the **Decision log** at any time by emptying `library_files` — nothing
else would need to change — which is the shape a "repair the **Library**" command would take if one is ever
wanted.

`submit_batch` is now the only caller. **History** edits at #7 and **Clearance** get the removal half for
free: a **Cleared Favourite** resolves to something that is not **Favourite**, so the file goes, with no
new code in this module. #7 only has to call the operation.

`reconcile_library` reads the whole set of **Wallpapers** ever **Favourited** plus everything holding a
file, every submission. That is bounded by what the user has actually **Favourited**, which is small and
grows slowly; it is not bounded by the **Pool**. If it ever stops being small, the query narrows to the
**Batch** just submitted plus the `library_files` rows, at the cost of no longer self-healing.

A **Favourite** whose download fails is silent today. The report exists; nothing renders it yet.

## Alternatives considered

**Download inside the submit transaction, before the commit.** Rejected: it blocks the submission on the
network, holds SQLite's write lock across a ten-second timeout, and makes a failed download roll back
**Verdicts** the user gave. It also inverts the domain — it would mean a **Favourite** only counts if
Wallhaven was reachable.

**Download after the commit, but as a straight-line "write these, delete those" from the Draft Batch.**
Rejected: it is a second account of what the **Decision log** already says, and it has no answer for a
failure. Retrying needs a record of what did not happen, which is a queue, which is the state that drifts.

**A background thread for the **Library**.** Rejected for now as unnecessary: a **Batch** yields a
handful of **Favourites** at most, and #6's refill thread is the one place a thread is actually earned. If
the wait becomes noticeable, this operation is already the right unit of work to move onto one, because it
is idempotent and takes no arguments.

**Reading the folder to decide what to write.** Rejected outright. The **Library** is write-only; reading
it back would make Explorer part of wallpapi's state and would re-download files the user deleted on
purpose.
