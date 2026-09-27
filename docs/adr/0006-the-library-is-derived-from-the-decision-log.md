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

## Addendum, 2026-09-27: confinement, and one-way sync (#14)

Accepted alongside the original decision, which is unchanged except where this says otherwise.

The maintainer, reading this ADR back: "I'm okay with deleting a file managed by wallpapi, as long as we
guarantee it is only ever looking to and can access the **Library** folder chosen, and someone naming a
file `~/../../../Windows/system32` can't make me accidentally remove something. If the file cannot be
found, just remove it from the app, don't try to find it. If I delete the file myself, or I move devices
and bring the db, do not remove it from the **Decision log**; I want to pull all my **Favourites** with a
single download (one-way sync)."

### One guard, used by both directions

`core.confined_to_library(path, library_root)` answers with the **resolved** path, or `None`. It is the
only thing that decides where wallpapi may write and what it may delete, and both `_add_to_library` and
`_drop_from_library` go through it. Three conditions: the name matches `LIBRARY_FILE_NAME` — a Wallhaven
ID and one short lowercase extension — the resolved path lies strictly inside the resolved **Library**
folder, and it is not the folder itself.

Resolving, rather than normalising, is what makes it mean anything: a symlink or a Windows junction
sitting inside the **Library** and pointing at `C:\Windows` is inside the folder by every syntactic
measure. Both sides are resolved, so a **Library path** that itself goes through a link is not refused for
doing so, and the comparison is `os.path.normcase` by path component — NTFS is case-insensitive, and a
string prefix would put `Library2` inside `Library`.

**It returns the path rather than a verdict** because that path is what the caller must then use. A
**Library** write is a temp file and an `os.replace` beside its destination (invariant 10); checking one
path and writing another would leave exactly the gap this exists to close.

A destination is refused **before any download**, not after one. A refusal that had already spent ten
seconds and several megabytes would be a refusal in name only.

### What changes about deletion

Being recorded is still necessary and is no longer sufficient. **A recorded path that does not pass the
guard is dropped from `library_files` and left exactly where it is.** That reverses half of what this ADR
originally said: "the recorded path is used at delete time" still holds, but only while that path is still
inside the **Library** folder the setting names *today*. The commonest way for it not to be is the
setting having changed, and abandoning that file is the right answer — wallpapi has no claim on a folder
the user has stopped pointing it at.

The row is dropped rather than kept. Keeping it would mean refusing the same path on every submission for
ever, and the row's only meaning is "wallpapi holds a file for this **Wallpaper**", which stops being true
the moment wallpapi will not touch it.

On the writer's side, only a **regular file** is ever unlinked; a directory, junction or symlink is
declined. That check needs the filesystem, so it lives with the writer while confinement, which needs the
**Library path**, lives with the Core service. `remove` keeps `missing_ok` as well as the check: the stat
races with the user's file manager however carefully it is written.

### One-way sync: `download_favourites()`

`reconcile_library` still never looks at the folder, and "**Library** file deleted in Explorer,
**Decision log** unchanged" is still a reachable state that nothing repairs on its own. What #14 adds is a
second operation the user asks for explicitly, from a button on the settings page.

`download_favourites()` writes a file for every **Wallpaper** resolving to **Favourite** whose file is
absent, and **never deletes**. Absent means: no `library_files` row, the recorded path is not on the disk,
or the recorded path no longer resolves inside the **Library** folder. **This is the one place wallpapi
reads the folder**, and it reads it for one thing — whether a path it recorded itself is still there. It
never lists the folder and never notices a file it did not write.

The third case is a reading rather than a restatement of the ticket, which named only the first two. A
file wallpapi has already said it will not so much as delete is not one the **Library** can be said to
hold, so it counts as absent and the **Favourite** is written into the folder the user is pointing at now.
That makes moving the **Library** a single action: the old files stay where they are, untouched, and the
new folder is filled from the **Decision log**.

Idempotent, like the reconciliation, and for the same reason: the condition is read from the log and the
disk rather than from a queue. `FavouriteDownload` reports `written`, `skipped` and `failed` because the
settings page says all three, and because "nothing to do" and "tried and failed" look identical from a
total. A failure needs no retry counter — a **Favourite** with no file already is the record of what still
has to happen — so pressing the button again is the whole of the retry.

### Consequences

The "repair the **Library**" command this ADR said would take the shape of emptying `library_files` now
exists and did not need to: it is one pass that stats the recorded paths, which is cheaper and leaves the
rows of files that really are there alone.

A **Favourite** wallpapi cannot name a file for — a Wallhaven ID that is not alphanumeric — is reported as
a failure on every pass and never written. That is a permanent failure rather than a retryable one, and it
is honest: the **Verdict** stands, and a **Favourite** is a fact about taste rather than about a filename.

Nothing in this addendum changes the **Decision log** in any direction. Deleting a **Library** file, moving
the folder, or carrying the database to another machine still leaves **History** and **Verdict
resolution** exactly as they were.
