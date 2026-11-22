# Handoff: desktop-manager idea → grilling, then a spec draft for review

For `spec-desktop`, started by `solo-wallpapi` in Herdr. Your approver is `solo-wallpapi`; the human (Cameron)
works with you directly in your pane. Your role is spec author: grill, then draft the spec. No code changes.

The plan, in order:

1. **Grill** the human on the idea with `mattpocock-skills:grilling`, before any spec. Start from the open
   questions and the assumptions below and in the diagram's third tab, then follow where the human takes it.
   Record each answer as you go in `docs/research/stack-review-2026-11/grilling.md`.
2. **Confirm the test seams** with the human (to-spec step 2), revised by what the grilling settled.
3. **Draft the spec** with `mattpocock-skills:to-spec`, using its template and `CONTEXT.md`'s vocabulary, into
   `docs/research/stack-review-2026-11/spec-desktop-manager.md`. **Do not publish it to beads yet**: the
   human sends it to `solo-wallpapi` for review first. When the draft is committed, tell the human and send
   `solo-wallpapi` one line with the file's path.
4. After the review, apply what it settles; publish the bead only when the human says so.

Work on branch `spec/desktop-manager`, cut from `research/stack-review` (which holds the research, the diagram
and this file). Commit through the hooks and push. Wrap text at 110 columns (`docs/agents/environment.md`).

## State

- to-spec step 1 (explore): done. Read `AGENTS.md`, `CONTEXT.md` (Pool, Filters, Refill, Library, Favourite),
  `compose.py`, every module docstring, the route list in `web/app.py`, the public interfaces of `library.py`
  (`Library.reconcile`, `download_favourites`, `LibraryWriter`), `similarity.py` (`Embeddings.vectors`,
  `vectors_for`, `similarities`), `background.py` (`BackgroundLoop`), the Filters in `pool.py`
  (`_matches_an_allowed_ratio`, `RATIO_TOLERANCE`) and ADR 0006.
- to-spec step 2 (seams): drafted below, **not yet confirmed by the human**. The skill requires that check.
- to-spec step 3 (write and publish): not started. After review, publish as a bead per
  `docs/agents/issue-tracker.md`
  (`bd create ... -t epic` or `feature`, with `--labels ready-for-agent`), then `bd dolt push`.

## Context the next session needs (read these, don't re-derive)

- The idea and its research: `docs/research/stack-review-2026-11/README.md` and the six reports beside it.
- The shared picture: `docs/research/stack-review-2026-11/system-diagram.html` (open with
  `/mnt/c/Windows/System32/cmd.exe /c start "" <windows path>`; `cmd.exe` is not on this WSL PATH). Its
  third tab lists the assumptions; carry them into the spec as decisions or flagged assumptions.
- The human's constraints, from the conversation: share with non-technical people (a colleague, a partner,
  a friend on a Mac); Windows first, then macOS, then Fedora or Ubuntu (GNOME, Wayland), built so adding a
  platform is only an adapter; no code signing for now; autostart low priority, because each change of
  wallpaper only sets the wallpaper and the last one stays if wallpapi stops; multi-monitor of different
  sizes and orientations; journey order (each next wallpaper close to the last) rather than random; day and
  night later.

## Proposed seams (for the step 2 check)

1. **A new rotation module**, the highest seam and the main one: its interface takes a connection, the
   `Desktop`, the clock and the Similarity provider, and advances the rotation. Tests use a real in-memory
   database and a fake `Desktop` that records what each screen was asked to show (invariant 14, like the
   Wallhaven client fake). Covers screen-plan building, shape matching, cropping to screen pixels and the
   stitch fallback through what the fake receives.
2. **`taste/` pure functions** for the journey order and day/night classification, tested with plain arrays
   (existing pattern; the `ast` boundary test keeps them pure).
3. **`Desktop` adapter contract tests**, one per platform, running only on that OS. Needs a change to
   `docs/agents/environment.md`'s skip counts, which call any other skip a fault.
4. The tray shell and launcher stay thin and untested, as `build_app` is today; the web routes for rotation
   controls are covered by the existing app-level tests and the smoke test.

## Judgement calls so far

- Spec covers the whole idea (exe + tray + rotator + per-monitor + journey + day/night + macOS + GNOME),
  with Windows first and later platforms as adapters; `to-tickets` would slice it later.
- New vocabulary needed in `CONTEXT.md` (via `domain-modeling`, not in the spec itself): rotation set,
  journey, screen, screen plan, Desktop. Flag them in the spec's Further Notes.
- The spec must name the ADR reversal: `CONTEXT.md` says wallpapi "never touches the desktop itself", and the
  Library stops being a folder Windows reads (ADR 0006 still holds: it stays derived from the Evidence log).
- Stack additions (PySide6-Essentials, comtypes, pyobjc, PyInstaller, Inno Setup) change the locked stack in
  `AGENTS.md`: needs the human's approval and an ADR.

## Open questions (the human has not answered; the skill says not to interview, so record as assumptions)

- Rotation set: Favourites only (assumed), or Likes too, or a separate set?
- A new Favourite: inserted beside its nearest neighbour in the journey (assumed), or a full re-order?
- Screens: each walks its own journey of fitting shapes (assumed), or the screens move together?
- Portrait wallpapers into the Pool: one Pool with shape tags, or a Pool per screen shape. Real options:
  `sounding-board` before deciding, or leave it as an explicit open decision in the spec.

## Next

Read `AGENTS.md`, `CONTEXT.md`, this file, the research README and the diagram (all three tabs). Cut the
branch, load the grilling skill and open with the open questions. The seam proposal above is a starting
point: let the grilling change it.
