# Delivery conventions

How a ticket goes from `ready-for-agent` to merged. Written down after the first epic was delivered this way on
2026-09-27, so the next round does not have to rediscover it.

## One agent, one issue, one worktree

- Each ticket is implemented by one agent in its own worktree: `C:\Users\Cameron\code\wallpapi-wt\issue-<N>`
  on branch `issue-<N>-<short-slug>`, cut from `origin/main`. Work never happens in the main checkout.
- The agent reads, in order: `AGENTS.md`, `CONTEXT.md`, `docs/adr/`, the issue with all its comments (the
  "Agent Brief" and "Note from triaging" comments refine the body), the two most recent merged PRs for house
  style, then the code nearest the ticket.
- Tests first, one module at a time: a real in-memory database and a fake only for the external collaborator
  it talks to (`AGENTS.md` invariant 14). No test touches the network or loads the model.

## Numbers are assigned up front

Parallel agents must not both take "the next" migration or ADR number. The lead assigns each ticket its
migration number and ADR number before dispatch and records them in the dispatch. A number that is reserved
and then not used stays unused: ADR 0011 is a deliberate gap for that reason, not a lost file. Migration
numbers must stay contiguous because `_migrate` chains `if applied < n` steps in order.

## The pull request

- Title: `<NN>: <issue title>` where `NN` is the ticket's sequence number in the epic.
- Body starts with `Closes #<N>.` (plus any extra `Closes` the lead folded in), then a one-paragraph summary,
  **What changed**, **Checks** (with the test count before and after), **Deliberately not here**.
- A separate comment headed `## Review note — where judgement was needed` lists every place the agent chose
  between readings, every criterion tested by proxy or left untested and why, and anything a reviewer should
  read closely. A green tautology is worse than an honest "unreachable through the seam".
- Small logical commits, imperative mood, each green on its own.

## The lead's side

- Verify in the agent's worktree, not by trusting the report: `ruff check`, `ruff format --check`, `pyright`
  strict, `pytest`. Read the diff of the core change and anything the review note flags.
- Post a `## Lead review` comment stating what was read, what was run, and a verdict on each judgement call,
  then `gh pr merge --merge --delete-branch`. Merge commits, not squashes, so each agent's commits survive.
- When `main` moves under a running agent, tell it what moved and have it rebase itself before opening the
  PR; it has the context to resolve its own conflicts. The lead resolves only conflicts left after that.
- Dispatch order follows the dependency graph and the shared-file conflict surface: tickets that touch the
  same sections of `core.py` run in sequence, tickets on different surfaces run in parallel.

## Definition of done

Unchanged from `AGENTS.md`: `ruff check`, `ruff format`, `pyright` strict and `pytest` clean, the smoke test
that boots the app and hits `/` still passing, no test touching the network.
