# Issue tracker: beads

Issues and specs for this repo live in beads (`bd`), synced through `refs/dolt/data` on the GitHub remote. The
`beads` skill and `bd prime` (a SessionStart hook runs it in Claude Code) carry the commands; this file holds what
they cannot know about this repo. Pull requests stay on GitHub.

## Ids

- `wallpapi-<N>` is GitHub issue `#<N>`: all 45 GitHub issues, open and closed, were imported on 2026-10-05 with
  their comments, sub-issues (now parent-child) and blockers. A `#<N>` in a commit, PR, ADR or issue body names
  `wallpapi-<N>`; read it with `bd show wallpapi-<N> --json --include-comments`.
- Issues created since carry bd's hash ids (`wallpapi-a3f2dd`).
- The GitHub issues themselves are frozen: read them for history, and make every change in beads.

## Pull requests close nothing in beads

A PR's `Closes #<N>` closes only the GitHub issue. Name the bead in the PR body instead (`Beads: wallpapi-a3f2dd`),
and once the PR merges run `bd close <id> --reason "<what landed, which PR>"`, then `bd dolt push`.

## GitHub account for this repo

The machine's global `gh` account is `CJohnsonPeartMagna` (work). This repo is personal and owned by
`CJohnsonPeart25`, which has no write access under the work account — `gh` writes fail with `HTTP 404`.

**Don't run `gh auth switch`** — that changes the global default for every repo. Instead prefix each `gh`
command in this repo with the personal account's token:

```bash
GH_TOKEN=$(gh auth token --user CJohnsonPeart25) gh pr create --title "..." --body-file <file>
```

Both accounts are already authenticated, so `gh auth token --user` resolves without a prompt and no token
is written to disk. `git` push and pull need no prefix: `credential.https://github.com.username` is set to
`CJohnsonPeart25` in this repo's local config, and Git Credential Manager picks the account from it. The same
holds for `bd dolt push` and `bd dolt pull`, which go through git.

### Commit identity

The global `user.email` is the work address. This repo overrides it locally, so commits made here are
attributed to the personal GitHub account:

```
user.name   Cameron Johnson-Peart
user.email  89163342+CJohnsonPeart25@users.noreply.github.com
```

Both values live in `.git/config` and apply automatically — never pass `--author`, and never change the
global config. If a commit does go out on the work address, the fix is to rewrite it rather than leave it:

```bash
git rebase --root --exec 'git commit --amend --no-edit --reset-author'
git push --force-with-lease origin main
```

Check with `git log --format='%h %an <%ae>'` — every line should carry the `users.noreply.github.com`
address. Note that a fresh clone of this repo won't have the local config, so set it before the first
commit.

## Pull requests as a triage surface

**PRs as a request surface: no.** _(Set to `yes` if this repo treats external PRs as feature requests; `/triage` reads this flag.)_

When set to `yes`, PRs run through the same labels and states as issues, using the `gh pr` equivalents:

- **Read a PR**: `gh pr view <number> --comments` and `gh pr diff <number>` for the diff.
- **List external PRs for triage**: `gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments` then keep only `authorAssociation` of `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, or `NONE` (drop `OWNER`/`MEMBER`/`COLLABORATOR`).
- **Comment / label / close**: `gh pr comment`, `gh pr edit --add-label`/`--remove-label`, `gh pr close`.

## When a skill says "publish to the issue tracker"

Create a bead: `bd create "<title>" --description "<body>" -t <type> -p <0-4>`, with `--acceptance` and
`--design` where the skill produces them.

## When a skill says "fetch the relevant ticket"

Run `bd show <id> --json --include-comments`.

## Wayfinding operations

Used by `/wayfinder`. The **map** is an epic with **child** beads as tickets.

- **Map**: `bd create "<title>" -t epic --labels wayfinder:map`, holding the Notes / Decisions-so-far / Fog body.
- **Child ticket**: `bd create "<title>" --parent <map id> --labels wayfinder:<type>`
  (`research`/`prototype`/`grilling`/`task`).
- **Blocking**: `bd dep add <child> <blocker>`. A ticket is unblocked when every blocker is closed.
- **Frontier query**: `bd ready --parent <map id> --json`, which already drops blocked and claimed tickets; first
  in map order wins.
- **Claim**: `bd update <id> --claim` — the session's first write.
- **Resolve**: `bd comments add <id> -f <answer file>`, then `bd close <id>`, then append a context pointer (gist +
  id) to the map's Decisions-so-far.
