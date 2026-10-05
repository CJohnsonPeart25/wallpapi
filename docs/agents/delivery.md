# Delivery conventions

How a ticket goes from `ready-for-agent` to merged. Written down after the first epic was delivered this way on
2026-09-27, so the next round does not have to rediscover it.

## One agent, one issue, one worktree

- Each ticket is implemented by one agent in its own worktree: `C:\Users\Cameron\code\wallpapi-wt\<id>`
  on branch `<id>-<short-slug>` (`<id>` the bead id), cut from the epic's integration branch (`origin/main`
  for a lone ticket). Work never happens in the main checkout. The agent claims the bead first:
  `bd update <id> --claim`.
- The agent reads, in order: `AGENTS.md`, `CONTEXT.md`, `docs/adr/`, the bead with all its comments
  (`bd show <id> --json --include-comments`; the "Agent Brief" and "Note from triaging" comments refine the
  body), the two most recent merged PRs for house style, then the code nearest the ticket.
- Tests first, one module at a time: a real in-memory database and a fake only for the external collaborator
  it talks to (`AGENTS.md` invariant 14). No test touches the network or loads the model.

## Numbers are assigned up front

Parallel agents must not both take "the next" migration or ADR number. The lead assigns each ticket its
migration number and ADR number before dispatch and records them in the dispatch. A number that is reserved
and then not used stays unused: ADR 0011 is a deliberate gap for that reason, not a lost file. Migration
numbers must stay contiguous because `storage.migrate` applies its steps by number, in order.

## The pull request

- Title: `<NN>: <issue title>` where `NN` is the ticket's sequence number in the epic.
- Body starts with `Beads: <id>.` (plus any extra beads the lead folded in), then a one-paragraph summary,
  **What changed**, **Checks** (with the test count before and after), **Deliberately not here**.
- A separate comment headed `## Review note — where judgement was needed` lists every place the agent chose
  between readings, every criterion tested by proxy or left untested and why, and anything a reviewer should
  read closely. A green tautology is worse than an honest "unreachable through the seam".
- Small logical commits, imperative mood, each with its own tests green; the full suite runs at push and in
  CI.

## The lead's side

- The checks are CI's and the lefthook hooks': read the PR's CI result rather than re-running them. Read
  the diff of the core change and anything the review note flags.
- Post a `## Lead review` comment stating what was read and a verdict on each judgement call, then
  `gh pr merge --merge --delete-branch` into the epic's integration branch. Merge commits, not squashes, so
  each agent's commits survive. The human reviews and merges the one epic PR into `main`; the epic's opening
  prompt states this, or a different merge policy.
- After each merge the lead closes the PR's beads, `bd close <id> --reason "<what landed, which PR>"`, then
  runs `bd dolt push`; no merge closes a bead by itself.
- When the integration branch moves under a running agent, have it rebase itself once, after the last
  sibling expected to merge first; it has the context to resolve its own conflicts. The lead resolves only
  conflicts left after that.
- Dispatch order follows the dependency graph and the shared-file conflict surface: tickets that touch the
  same module run in sequence, tickets on different surfaces run in parallel.

## Definition of done

As in `AGENTS.md`: `uv run lefthook run ci` exits 0, which the hooks and CI enforce. What a check
cannot measure is named in the review note for the human.
