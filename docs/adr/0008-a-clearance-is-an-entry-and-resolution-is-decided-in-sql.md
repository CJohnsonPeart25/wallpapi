# 8. A Clearance is a Decision log entry, and Verdict resolution is decided in SQL

Date: 2026-09-27

## Status

Accepted

## Context

**History** lets any past **Verdict** be changed or withdrawn. Changing one is easy — the **Decision log**
is append-only and **Verdict resolution** already says the latest **Explicit Verdict** wins, so an edit is
one more row. Withdrawing one is not. There is no **Verdict** that means "I take that back": **Ignore** is
not it, because a stored **Ignore** *stacks* and what the user wants is for their earlier **Ignores** to
stack again, which is a different thing entirely.

CONTEXT.md had already named the answer before any of this was built — "**Clearance**: the decision log
entry that removes a wallpaper's explicit verdict... a clearance is an entry in its own right, not a
verdict" — and #3 deliberately left it here.

Separately, the **History** page has to list thousands of rows filtered by what each **Wallpaper**
*resolves to*. That is not a column anything holds. It is the output of a rule that, until this ticket,
existed only in Python.

## Decision

**A Clearance is stored as the text `cleared` in `decision_log.verdict`, and is not a fifth Verdict.**
The column already holds text, so this needs no migration — `SCHEMA_VERSION` stays at 5. `Clearance` is a
one-member `StrEnum` in `model.py` and `DecisionEntry.entry` is `Verdict | Clearance`. The field is called
`entry` rather than `verdict` precisely so that pyright makes every reader say which of the two it is
handling; renaming it is what found the callers that would otherwise have blown up on `Verdict('cleared')`.

**The resolution rule is extended in one sentence.** The latest entry that is not an **Ignore** decides. If
it is an **Explicit Verdict**, that alone counts and every **Ignore** on the **Wallpaper** is disregarded,
before it and after it alike. If it is a **Clearance**, or if there is none, every **Ignore** stacks —
again before and after. A **Wallpaper** whose only entries are a **Verdict** and the **Clearance** that
withdrew it resolves to nothing, which is the same answer as never having been seen.

**Un-Banning is not built.** **Batch** building excludes by *resolved* **Verdict**, so a **Cleared Ban**
stops excluding with no code anywhere that knows the word. The same is true of the **Library**: it is
derived from the **Decision log** (ADR 0006), so a **Cleared Favourite** loses its file because it is no
longer a **Favourite**, not because **History** asked for a deletion.

**The rule that chooses a resolved Verdict lives in SQL; the rule that prices one lives in Python.**
`_RESOLUTION_CTE` is a common table expression that every query needing resolution is built on —
`resolve_verdicts`, the **History** listing, the **History** filter, and the set of **Wallpapers** whose
thumbnails may not be evicted. `_resolved_from` turns the **Verdict** the CTE chose into its value.

This reverses the position ADR 0006's `_LIBRARY_CANDIDATES` took ("resolution is not expressed in SQL
here"), and for a reason that did not exist then: **History** filters and pages over the resolved
**Verdict**, and a page of 100 rows cannot be sliced out of a log resolved in Python. Once the rule has to
exist in SQL at all, the only safe number of places for it is one.

**Edits and Clearances carry `batch_id = NULL`.** That is the only thing in the log distinguishing a
judgement made from **History** from one given to a **Batch**, and it is what keeps
`list_history(batch_id=...)` — which is how the submit page counts what it recorded — from counting an
edit made later.

**Both refuse rather than doing nothing.** `edit_verdict` refuses **Ignore** and an unknown **Wallpaper**;
`clear_verdict` refuses a **Wallpaper** with no **Explicit Verdict** standing. An append-only log should
not accumulate entries that change nothing, and "nothing happened" is worth saying out loud.

## Consequences

Every reader of `decision_log.verdict` has to tolerate `cleared`. There is now exactly one — `_entry_from`
— and the union type is what keeps it that way.

**Verdict resolution** is now a property of one SQL fragment. #9's **Scoring** reads it through
`resolve_verdicts` exactly as before and does not see the change; anything that needs resolution in a
`WHERE` clause builds on `_resolution_query` rather than writing the `CASE` again.

Invariant 4 stops being tested by proxy. #3 could only approximate "two entries sharing a timestamp" with
two submitted **Batches**; a **Clearance** and an edit made from **History** under a frozen clock are the
real thing, and `test_a_clearance_and_a_verdict_sharing_a_timestamp_resolve_by_sequence` pins it.

A **Wallpaper** can now resolve to nothing while still having a **History** row. The page renders that as
"no verdict" and it matches no filter, which is correct: nothing is standing.

## Alternatives considered

**`CLEARED` as a fifth member of `Verdict`.** Rejected: CONTEXT.md forbids it, and the type system would
then let a **Clearance** be passed anywhere a **Verdict** is expected — including to `edit_verdict`, where
it would mean something quite different from what `clear_verdict` means.

**Deleting or rewriting the entry being withdrawn.** Rejected outright. The **Decision log** is
append-only and is the single source of truth; what the user thought in March is a fact, and changing
their mind in September is a second fact, not a correction of the first.

**A `cleared_at` column on `wallpapers`, or a separate `clearances` table.** Rejected: both are a second
account of what the log already says, and resolution would have to consult two places and reconcile them
by timestamp — the exact thing invariant 4 exists to avoid.

**Resolving in Python and filtering the result.** Rejected: **History** is thousands of rows within a week
of ordinary use, and resolving all of them to render a hundred is work proportional to the whole log on
every page load.

**Duplicating the rule — SQL for the filter, Python for the value.** Rejected as the worst of both: two
copies that would agree until the first time one was edited.
