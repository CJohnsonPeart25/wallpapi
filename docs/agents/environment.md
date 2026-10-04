# Environment

What an agent working in this repo on this machine needs before its first command. Pasted into every brief;
the lead keeps it short and is its only writer, and an agent that finds something new reports it.

- `python` is not on PATH (a Store alias intercepts it): every command is `uv run ...`. Run `uv sync` once in a
  new worktree.
- The checks are lefthook's (`lefthook.yml`): a commit runs format, lint and pyright, a push runs pytest, and
  `uv run lefthook run ci` runs all four, as CI does on every PR. Its exit code is the verdict, not a grep of
  its output; the summary marks the failed check. One test: `uv run pytest tests/<file>.py::<name>`.
- The hooks are on once per clone, run from the main checkout so they outlive any worktree:
  `uv run lefthook install`. Never skip them (`--no-verify`, `LEFTHOOK=0`); fix what they report.
- Three symlink tests skip on this machine (Windows refuses symlinks without Developer Mode); "3 skipped" is
  expected, a fourth is not.
- Write and edit files with the harness's file tools, never shell heredocs: quotes inside them break the shell.
- Lines stop at 110 columns. `ruff format` wraps code but not docstrings, comments or long strings, so wrap
  those by hand; `uv run ruff check <file>` after each write finds them before the hook does.
- Text files are LF in the working tree (`.gitattributes`); the vendored files in `web/static/` are left as
  fetched, byte for byte.
- The smoke test is `tests/test_web_routes.py::test_every_page_boots_on_the_shell_and_tiles_come_from_the_cache`
  and runs with the rest of pytest. A real boot (`uv run python -m wallpapi`) calls Wallhaven and downloads
  an 85 MB model, so run one only when the brief asks, with its own `WALLPAPI_HOME` and `WALLPAPI_PORT`.
- Do not upgrade a tool while other agents are running; if one changes, say so and restart the agents.
