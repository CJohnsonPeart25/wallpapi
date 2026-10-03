# 4. Settings are a typed view over key-value rows, validated in the Core service

Date: 2026-09-27

## Status

Retired by the settings module of epic #32 (step 5, #63), where one field table takes over validating and
storing each setting from the **Core service**.

## Context

Settings existed from #2 as two string-keyed methods over a `settings` table: `get_setting(key) -> str` and
`set_setting(key, value)`. That was enough for one seeded row, and it is not enough for a settings page.

Nothing said what a value had to be. `set_setting("batch_size", "0")` stored fine and produced a **Batch**
with nothing on it and no way off the page except editing the database. The coercion — `int(...)` — lived at
the single call site that happened to need it, so every future reader of a setting would have to remember
the same trick and get it right.

The settings only grow from here, and every one of them arrives with a rule: **Filters** and a **Pool**
target size at #6, similarity radius and decay at #9, **Mixes** at #10, a revisit weight at #11. The
question is where those rules live and what shape the API takes so that adding one is a field rather than a
restructuring.

Two further constraints. Invariant 1 says the UI and every test talk only to the **Core service**, so a rule
that only exists in the web layer is a rule no behaviour test can reach. And `AGENTS.md` says Pydantic lives
at the edges only, with plain dataclasses and enums inside the core — a restriction on where Pydantic may
be used, not an instruction to put validation there.

## Decision

**`Settings` is a frozen dataclass — a typed view over the rows, not a new table.** Storage stays one row
per key. A new setting is a new field on the dataclass and a new seeded row, never an `ALTER TABLE` against
a widening settings table, and a database that predates the field keeps working because the field falls
back to its default.

**`update_settings` takes keyword-only fields, with `None` meaning "leave this one alone".** The alternative
— hand back a whole `Settings` value — was rejected for two reasons. A caller that must supply every field
has to read them all first, so the read and the write become two transactions with a lost update in
between. And the page will grow sections; a form that renders half the settings must not reset the other
half by omission. Adding a setting is a new keyword and a new field, and no existing caller changes. No
setting is nullable today; one that ever is needs a sentinel here rather than `None`.

**Validation lives in the Core service and returns `SettingsRefused`, in the style of `SubmissionRefused`.**
A reason enum rather than an exception, so the page has one error branch and can render the reason beside
the value that caused it. Everything is validated before anything is written, so a good batch size beside a
bad path does not half-apply and then get reported as a failure.

**The values are accepted as strings as well as typed.** The form posts strings, and the coercion rule
belongs with the validation rule. A declared `int` at the web edge would make FastAPI answer a typo with its
own JSON 422 — a wall of text in the browser — and would put a second rule somewhere the tests for the first
one cannot see it.

**The rules as of #4.** Batch size is a whole number from 1 to 64. One, because a **Batch** of none is a
page with nothing to decide on. Sixty-four, because the spec asks for "a big blitz of 16 or 32" and nothing
larger, and because it is what **Batch** building can fill: Wallhaven listings return 24 at a time and the
walk is capped at four **API calls**, so 96 candidates is the ceiling before **Bans** thin it. The
**Library** path must be non-empty and absolute (invariant 9) — a relative path moves the **Library** with
the working directory, and the absolute path recorded for each file written into it would then point
somewhere that cannot be found again.

**Validating a path does not create it.** The **Library** writer creates the folder on its first write
(#5). Creating it here would leave an empty folder behind for every path the user typed and thought better
of, including the default one on a machine that has never favourited anything.

**Defaults are seeded rows with one source of truth in code.** `_defaults()` is that source; migration 3
seeds from it with `ON CONFLICT DO NOTHING`, computing the **Library** default —
`Path.home() / "Pictures" / "wallpapi"` — in Python at migration time and inserting it as text. `DO NOTHING`
because the migration runs on databases that predate it and a size the user chose must survive it.
`get_settings` falls back to the same table for a row a hand-edited database has lost, and never raises: a
stored value this **Core service** would have refused cannot have come through `update_settings`, and the
settings page is where such a row gets fixed.

**`get_setting` and `set_setting` are gone.** Keeping them would leave a second, unvalidated way to write
`batch_size` beside the validated one, which is exactly the "one careless line away" failure the invariants
are written against.

## Consequences

Reading a setting is `core.get_settings().<field>` — typed, with no coercion at the call site and no key
string to misspell.

Changing the batch size applies to the next **Batch** minted, not to the one on screen. That follows from
ADR 0002 rather than being chosen here: the **Batch** persists until it is submitted, and rebuilding it to
fit a new size would discard the **Draft Batch** marked against it. The settings page says so.

Every later setting inherits the shape: a field, a default, a validator, a reason. A reason added to
`SettingsRefused.Reason` must be given words on the settings page, or it renders as the fallback branch.

Nothing in this change uses Pydantic. That is allowed — "Pydantic at the edges only" bounds where it may
appear, and here the edge has nothing left to validate once the form fields are known to be strings.

## Alternatives considered

**A `Settings` value in, a `Settings` value out.** The simplest API to describe, and rejected above: it
forces read-modify-write, which is a lost update across two transactions, and it makes a partial form
destructive.

**Pydantic validation at the web edge.** Rejected because invariant 1 puts every behaviour test through the
**Core service**, so the rule would be untestable where the tests are, and because a second entry point to
settings — the entry point, a script, a later API — would bypass it entirely.

**Raising on an invalid value.** Rejected for the same reason submitting an already-submitted **Batch**
returns a refusal: the page needs to say what was wrong and keep what was typed, and an exception path is a
worse shape for that than a result.

**A column per setting.** Rejected: every new setting becomes a migration, and a database one version behind
cannot be read at all. One row per key with a typed view costs nothing and degrades to a default.
