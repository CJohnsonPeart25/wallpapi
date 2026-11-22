# wallpapi: runtime, core libraries and tooling (checked 2026-11-22)

## Recommendation

- **Python 3.14 (GIL build): keep, and cap below 3.15 for now.** onnxruntime 1.32.0 (2026-11-13) ships no
  cp315 wheels and no sdist. Its free-threaded wheels (cp313t/cp314t) are Linux-only. numpy and onnxruntime
  release the GIL in their heavy calls anyway, so free-threading buys nothing for a one-user app. Add
  `requires-python = ">=3.14,<3.15"` (an app may cap; a library should not), or at least keep
  `.python-version` at 3.14: uv 0.13 now installs 3.15 by default.
- **httpx2: keep.** It is the actively maintained continuation of httpx 0.28.1, run by Pydantic (Kludex as
  maintainer, Tom Christie still credited as author). It has the same API, uses the OS trust store (good for a
  desktop app behind corporate TLS), and has shipped about 20 releases between May and Nov 2026. Upstream
  httpx has had no stable release since 0.28.1 (Dec 2024); only 1.0.dev pre-releases. Not worth switching:
  niquests (3.21.3, active, but a requests-style API), requests/urllib3 (no change in benefit). Risk: it is
  young, and one company stewards it.
- **pyright strict: keep as the gate, and trial pyrefly beside it.** Pyright is still the only one with full
  `reportUnknown*` strict semantics and a per-root `pythonPlatform` (`executionEnvironments`). Pyrefly 1.x is
  stable (1.0 on 2026-05-12, 1.5.0 on 2026-11-19), has a `strict` preset with `unknown-*` rules, and takes
  `python-platform` as a list. ty is still beta (0.0.107, Stable milestone 99% but undated) and has no
  Unknown-type rules: do not adopt it yet. Pyright's release cadence has slowed (no release since 1.1.414 on
  2026-09-10), so watch it.
- **Platform adapters:** type-check them on Linux CI with a pyright `executionEnvironments` entry per adapter
  root (`pythonPlatform: "Windows"` / `"Darwin"` / `"Linux"`). Use `types-pywin32` for the stubs (current:
  2026-11-22). pyobjc ships no useful stubs, so expect `reportUnknown*` noise there, and scope relaxations to
  the macOS adapter.
- **uv: upgrade 0.13.1 to 0.13.9.** Declare platform deps with PEP 508 markers, e.g. `"pywin32>=312;
  sys_platform == 'win32'"`, `"pyobjc-framework-Cocoa; sys_platform == 'darwin'"`, `"dbus-fast; sys_platform
  == 'linux'"`. Add `[tool.uv] required-environments` for win32 x86_64, darwin arm64 (plus x86_64 if wanted)
  and linux x86_64, so `uv lock` fails when a wheel is missing for a target. Then one universal `uv.lock`
  covers all three. uv has no app bundler (issue #5802, "not on the immediate roadmap"); that belongs to the
  packaging research.
- **ruff: upgrade 0.16.8 to 0.17.x.** The explicit `select` shields you from 0.16's 413-rule default. Worth
  considering: `PTH`, `DTZ`, `S` (bandit) and `ANN`. The new `ruff: ignore` comments arrived in 0.16.
- **pytest 9.1 to 9.2, lefthook 2.1.16 to 2.2.2: keep and upgrade.** prek (0.5.x) is the fast pre-commit
  clone, but you have no `.pre-commit-config.yaml` to reuse. lefthook's YAML anchors already express the
  fast/suite/ci split. No reason to switch.
- **stdlib sqlite3 + `user_version` migrations: keep.** 3.14 only removed things (`sqlite3.version`, and
  positional sequences with named placeholders now raise). Packaging note: the SQLite version is whatever the
  interpreter bundles (here 3.51.2), so record `sqlite3.sqlite_version` in diagnostics.
- **pydantic v2: keep (2.14.4).** V3 is in progress and aims to be near-final by the end of 2026, with "no API
  overhaul on the scale of V1 to V2", and V2 support continues. At-the-edges use keeps your exposure small. V1
  is end-of-life.

## Table

| Item | Locked here | Latest (date) | Verdict |
|---|---|---|---|
| Python | 3.14.3 | 3.15.2 (2026-10-26) | Keep 3.14; cap <3.15 |
| httpx2 | 2.13.1 | 2.16.2 (2026-11-16) | Keep, upgrade |
| pyright | 1.1.414 | 1.1.414 (2026-09-10) | Keep; watch cadence |
| pyrefly | n/a | 1.5.0 (2026-11-19) | Trial as second checker |
| ty | n/a | 0.0.107 beta (2026-11-18) | Not yet |
| uv | 0.13.1 | 0.13.9 (2026-11-17) | Upgrade; add required-environments |
| ruff | 0.16.8 | 0.17.6 (2026-11-19) | Upgrade |
| pytest | 9.1.1 | 9.2.0 (2026-10-27) | Upgrade |
| lefthook | 2.1.16 | 2.2.2 (2026-10-13) | Keep, upgrade |
| pydantic | 2.13.5 | 2.14.4 (2026-11-19) | Keep v2 |
| onnxruntime | 1.30.0 | 1.32.0 (2026-11-13) | Blocks 3.15 / free-threaded |

## Key evidence

- httpx2 PyPI (releases 2026-05-11 to 2026-11-16, Pydantic as maintainer, Python 3.11 to 3.15):
  https://pypi.org/project/httpx2/. Migration guide ("fork of httpx 0.28.1", truststore, renames):
  https://pydantic.dev/docs/httpx2/get-started/migration/. Repo README ("limited recent activity" upstream):
  https://github.com/pydantic/httpx2
- httpx PyPI (0.28.1 2024-12-06; 1.0.dev6 2026-08-31): https://pypi.org/project/httpx/
- Pyrefly v1.0 post (2026-05-12): https://pyrefly.org/blog/v1.0/. Config (`python-platform` list or "all",
  `strict` preset): https://pyrefly.org/en/docs/configuration/
- ty PyPI (beta, no stable API): https://pypi.org/project/ty/. Pyright-to-ty mapping (no `reportUnknown*`
  equivalents): https://docs.astral.sh/ty/coming-from-mypy-or-pyright/. `python-platform` option:
  https://docs.astral.sh/ty/reference/configuration/
- Pyright `executionEnvironments[].pythonPlatform`:
  https://github.com/microsoft/pyright/blob/main/docs/configuration.md. Releases:
  https://github.com/microsoft/pyright/releases
- uv resolution (`environments`, `required-environments`): https://docs.astral.sh/uv/concepts/resolution/. uv
  0.13.0 (2026-10-09, 3.15 as default): https://github.com/astral-sh/uv/releases
- Ruff 0.16 (2026-07-23, 413 default rules, Markdown formatting): https://astral.sh/blog/ruff-v0.16.0
- Python 3.14 What's New (PEP 779 free-threading supported, 5-10% single-thread cost; sqlite3 removals):
  https://docs.python.org/3.14/whatsnew/3.14.html. PEP 790 (3.15.0 2026-10-09):
  https://peps.python.org/pep-0790/
- Wheel tags, from the PyPI JSON API, 2026-11-22: onnxruntime has cp314t on Linux only and no cp315; numpy,
  pillow, pydantic-core and pyobjc-core have cp315 and cp315t; pywin32 312 has cp315.
- Pydantic "validation next steps" (2026-10-30): https://pydantic.dev/articles/pydantic-validation-next-steps
- OpenAI to acquire Astral (2026-03-19, pending at announcement):
  https://openai.com/index/openai-to-acquire-astral

## Risks and unknowns

- **Governance concentration.** uv, ruff and ty now belong to OpenAI (I did not confirm the deal closed).
  httpx2 depends on Pydantic's continued interest. All are permissively licensed and forkable, so the risk is
  low but real.
- **Pyright's slowdown** is visible in the release list, but I found no explanation. Evidence is thin. A
  second checker (pyrefly) hedges it cheaply.
- **Pyrefly strict parity with pyright strict** is unmeasured here. Run both on the repo before you decide
  anything.
- **httpx2's API may drift from httpx 1.0** if upstream ever ships. No source says whether httpx2 will track
  it.
- **onnxruntime timing for cp315 wheels** is unknown. It is the only blocker to 3.15 that I found.
- **prek and lefthook comparisons** are informal (blog posts only).
- Some dates came via third-party mirrors (ruff 0.17.0, uv 0.13.0 notes); PyPI confirmed the versions.
