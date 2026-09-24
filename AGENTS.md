# wallpapi

## Agent skills

### Issue tracker

Issues live as GitHub issues in `CJohnsonPeart25/wallpapi`, managed via the `gh` CLI. This repo needs the
personal GitHub account, not the machine's global one — prefix `gh` commands with
`GH_TOKEN=$(gh auth token --user CJohnsonPeart25)` rather than running `gh auth switch`. See
`docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles, each label string equal to its role name. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context — `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
