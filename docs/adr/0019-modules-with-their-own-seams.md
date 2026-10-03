# 19. Modules with their own seams

Date: 2026-10-03

## Status

Accepted. Retires invariant 1 of `AGENTS.md`, one seam and five fakes, and the testing half of ADR 0001's
context that rests on it.

## Context

The walking skeleton was built behind one seam. The UI and every test talked only to the **Core service**,
which took five injected dependencies (Wallhaven client, **Library** writer, random source, **Similarity
provider**, clock) and had a fake for each. That was right while there was little behind the seam: one place
to enter, one set of fakes, and no test that could reach past the rule it was testing.

It did not stay little. The **Core service** grew to about 3,200 lines holding storage, the **Decision log**,
settings, the **Pool** and its refill, **Batches**, the **Library** and the thumbnail cache. Every test boots
all of it, so a test of one rule is a test of everything that rule sits behind, and the same rule ends up
asserted in several files. Every ticket edits the same module, so tickets that would otherwise run in parallel
conflict.

## Decision

Every module exposes a small interface taking a connection and its collaborators. Tests build one module with
a real in-memory database and fake only the external collaborator it talks to.

The database is not faked. It is in-process, an in-memory one costs nothing to build, and a fake of it would
be a second implementation of the schema to keep in step. What is faked is what is outside the process or
outside wallpapi's control: the Wallhaven client, the **Library** writer's disk, the clock, the embedder.

Collaborators are passed, not discovered. A module that needs another receives it, and the composition root is
the only place that knows the whole graph.

## Consequences

A test reads as the rule it checks: one module, one database, at most one fake. A rule is asserted once, next
to the code that keeps it.

A module's dependencies are visible in its constructor. The two hidden ones in the **Core service** — settings
read inside its own write transaction, and **Batch** minting resolving **Verdicts** inside its — become
explicit, by passing the write handle through.

There is no longer one place where every behaviour can be driven from. The web tests take that role for what a
user can observe, over real modules.

## Alternatives considered

**Keep one seam and thin the tests.** Cuts the duplication but not the cost: every test still builds the whole
**Core service**, and every ticket still edits it.

**Mock the database.** Cheaper to construct per test in theory, but SQLite in memory is already cheap, and the
rules worth testing — resolution in SQL, one-transaction submit — live in the SQL a mock would not run.
