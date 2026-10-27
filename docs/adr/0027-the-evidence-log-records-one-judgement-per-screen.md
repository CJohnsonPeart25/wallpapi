# 27. The Evidence log records one Judgement per screen

Date: 2026-10-27

## Status

Accepted. Replaces the **Decision log** as the single source of truth. Re-bases ADR 0015's rule from sequence
to recorded time. Amends ADR 0016: an **Item** is decided by a **Verdict** or a **Favourite**, not by being
shown. Amends ADR 0026: a **Skip** is recorded, at zero weight. Rewrites invariant 4. Invariant 2 stands, and
so do ADR 0007 and ADR 0021.

## Context

The **Decision log** holds one row per **Wallpaper** per **Verdict**. That is all it can hold, and it forgets
most of what happened when you judged: which **Wallpapers** were shown together and in what order, why each
was drawn (its **Zone**, the **Slot**'s **Zone**, the **Mix**), what ranking drew them, when the screen first
appeared, and whether the page was light or dark. None of it can be recovered later, and every question about
your own taste a year on, or about whether ranking is any good, needs some of it.

It also cannot hold a comparison. "Which of these four is best?" is not four facts about four **Wallpapers**:
it says something about each only beside the others. The **Line-up** (**Pick**, **Order**, **Gauntlet**)
needs a record whose unit is the screen.

And its rule orders by autoincrement sequence, because one submission's rows share a timestamp (invariant 4,
ADR 0015). A sequence means something only inside one database, so a later merge of two installs would have
nothing to order their rows by.

## Decision

**One Judgement per screen answered.** A submitted **Batch**, a **History** edit, a **Skip** and a **Line-up**
answer are each one **Judgement**, carrying, in meaning:

- its **Instrument** name and version, the app's name and version, and the format version (1);
- its **Screen**: batch, history or compare;
- the **Shown set**: **Item** keys in on-screen order, keys only;
- the **Response**: fields per **Item** and optional fields for the whole **Judgement**, shaped by the
  **Instrument** and never read by the log;
- **Provenance** per **Item**, labels the **Taste engine** keeps and never interprets: the **Zone** drawn
  from, the **Zone** the **Slot** was meant for, the **Mix**;
- the **Ranking stamp**: the log position ranking read, the **Space**, the **Similarity radius** and
  **Similarity decay**, empty places for a distillation and a lens; absent when nothing ranked the set;
- the recorded time (submission, UTC, microseconds), the local UTC offset then, and the first-shown time (the
  screen's mint time);
- a context of named fields, open-ended: day one holds the page's theme. Absent means unknown, and a name it
  does not know survives.

**Identified by a hash of its content**: what was shown, what was done, when. The same **Judgement** has the
same id wherever it is kept, so a later merge of two databases is "add the **Judgements** I lack". No install
id, no local sequence.

**Append-only, and the latest recorded time decides.** Nothing is updated or deleted; a change of mind is a
new **Judgement**. ADR 0015's rule stands, re-based: a submission is one **Judgement**, so nothing inside it
competes for "latest", and times are kept to the microsecond, so two **History** clicks never share one. An
exact tie falls to the lower id. The rule and its example cases live in `taste/`; the **Evidence log**
module's SQL fragment, which **History** pages with, is tested against those cases.

**The log keeps the raw Judgement; a Reduction reads it.** One **Reduction** per **Instrument** name and
version turns a **Judgement** into **Evidence**, absolute or relative. A changed weight is a new **Reduction**
of the same log, never a rewrite of it. So what teaches nothing yet is still recorded: a **Skip** (every
**Response** blank), a preview opened (per **Item**), a **History** edit (a **Shown set** of one **Item**,
**Screen** history). A **Pool reset** acts on the **Pool**, not on any **Item**, and records nothing.

**Never recorded**: location, weather, IP address, user agent string, hover and dwell per **Item**, mouse
paths, the order of clicks before submission, screen or viewport size, the **Filters**, anything outside the
app. The log is a record of judgements, not surveillance.

**Kept in the existing database file**, by wallpapi: one row per **Judgement** and one per shown **Item**
(position, key, **Provenance**, and the per-**Item** **Response** in columns where SQL must read it: the
**Verdict**, the **Favourite**, preview opened). The context and the **Ranking stamp** are JSON text. Keys are
namespaced by **Item source**, `wallhaven:<id>`, turned to and from wallpapi's bare ids at the module's edge.
The **Taste engine** defines the shape and stores nothing.

**The Decision log is converted once, by one numbered migration, and frozen.** Each submitted **Batch**
becomes one **Judgement** (the verdict grid, version 0, **Screen** batch, **Shown set** in position order, the
stored **Zone** as **Provenance** where there is one, the entries' recorded time). Each entry with no
**Batch** becomes a **History** **Judgement** of one **Item**. A legacy **Clearance** is kept as a version-0
**Response** that wins as latest and adds nothing. Where two converted **Judgements** on one **Item** share a
recorded time, the later by old sequence moves on by one microsecond, and order is then kept by time alone.
Two copies of one log convert to the same ids. The old table is left unread and undropped until a later
migration.

**Decided means a Verdict or a Favourite recorded.** The log now holds **Items** that were shown and not
decided, so ADR 0016's "the **Pool** holds no **Wallpaper** the **Decision log** mentions" would retire every
**Line-up** and every skipped **Batch**. The **Pool** refuses instead every **Item** that has ever had a
**Verdict** or a **Favourite** recorded, a legacy **Clearance** included.

## Consequences

Every reader of the old table moves to **Judgements**: **History** paging, filtering and edits, the
**Favourite** list for the **Library**, **Lookalike search** subjects, eviction, pre-marking and the
**Pool**'s refusal. Ranking reads **Evidence**, not rows, and its **Score** is unchanged.

Invariant 2 holds. The **Ranking stamp** records what ranking read, never what it scored, so a past screen can
be replayed as that day's ranking saw it without a stored **Score**.

Converted **Judgements** lack what was never recorded: first-shown time, local offset, theme, **Ranking
stamp**, **Mix**. Absent means unknown, and every reader must allow for it.

Asking SQL about one **Item** now joins the **Item** rows to their **Judgement** for its recorded time. That
is the price of the screen as the unit, and the reason the **Response** fields SQL reads are columns rather
than JSON.

Clock drift between machines matters only for one **Item** judged on two machines seconds apart, and only
once two databases are merged, which is deferred.

## Alternatives considered

**Keep one row per Wallpaper and add columns** (a **Batch** id, the **Zone**, the **Mix**). Rejected: a
comparison is not a fact about one **Wallpaper**, a **Line-up** has no **Batch** to share an id with, and the
**Shown set** would be rebuilt from rows that happen to share one.

**Store each Judgement as one JSON document.** Rejected: **History**, the **Pool**'s refusal and the
**Library** read the **Verdict** and the **Favourite** in SQL, and would parse JSON on every page.

**Identify a Judgement by install plus local sequence, ordered by time, install and sequence.** Rejected as
more than a merge needs: it means naming installs and keeping their sequences apart, where a content hash
lets a merge skip what it holds with nothing to keep.

**Keep ordering by sequence.** Rejected: a sequence orders one database only, and the reason it was needed —
one submission's rows sharing a timestamp — goes once a submission is one **Judgement**.

**Let the Taste engine store the log.** Rejected: it would open files and own migrations, and could no longer
be tested with plain data or moved into a package of its own. wallpapi owns every table.

**Drop the old table in the converting migration.** Rejected: nothing about the history is rewritten or thrown
away until the conversion has been lived with.
