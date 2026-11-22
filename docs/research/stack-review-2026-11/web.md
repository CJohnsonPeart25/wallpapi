# Web/UI stack: is it still the right fit? (researched 2026-11-22)

## Recommendation

**Keep the stack. Make two patch bumps and one lock refresh. Defer htmx 4. Accept Pico as frozen.** Nothing in
the current landscape would improve wallpapi enough to justify rewriting a UI that works. The move to a
desktop app does not force a UI change either: an embedded webview (pywebview) or the default browser can
point at the existing 127.0.0.1 server.

- **FastAPI: keep, refresh the lock.** The lock pins 0.141.1 and the latest is 0.165.0. Sync `def` handlers
  are still documented as running in a threadpool, so ADR 0001 is safe. The recent breaking changes are the
  drop of Python 3.10 (irrelevant here) and 0.162.0's change to omitted bool form fields. `ranked` and
  `starred` are `Annotated[bool, Form()]`, but `hx-vals` always sends them, so they should be unaffected; the
  suite will confirm.
- **Jinja2: keep.** 3.1.6 (2025-03) is still the latest and is stable.
- **htmx: upgrade 2.0.4 to 2.0.11 now; do not move to 4 yet.** htmx 4.0.0 shipped on 2026-08-28 (4.0.2 on
  2026-11-18), but npm still tags 2.x as `latest`. 4.x stays on `next` "until early 2027", and the maintainers
  say 2.x will be supported indefinitely. For wallpapi, 4's gains (fetch, built-in morph, `<hx-partial>`, no
  localStorage history) solve nothing it currently has trouble with.
- **Alpine.js: optional bump from 3.17.4 to 3.17.5.** That release has one fix, for x-cloak during morph, and
  wallpapi does not use morph, so the bump only keeps the version current. There is no sign of an Alpine 4.
- **Pico CSS: keep 2.1.1, and treat it as frozen.** **Pico is no longer maintained.** The repo was archived on
  2026-10-04; the README says "v2.1.1 is the final release" and names no successor. Because the file is
  vendored, the archive breaks nothing, and CSS has almost no security surface. Revisit only if a browser
  change breaks something. The forks that have appeared (for example Pico Reforged) are too new to adopt.
- **Alternatives: none would be better here.** See the table.

## Table

| Component / option | Pinned | Latest (date) | Trajectory | Verdict |
|---|---|---|---|---|
| FastAPI | 0.141.1 | 0.165.0 (2026-11-18) | Very active, frequent small breaks | Keep; refresh lock |
| Starlette | 1.7.0 | 1.10.0 (2026-10-24) | Active, post-1.0 | Comes with FastAPI |
| uvicorn | 0.53.0 | 0.54.0 (2026-09-25) | Active | Refresh |
| Jinja2 | 3.1.6 | 3.1.6 (2025-03-05) | Stable, mature | Keep |
| htmx | 2.0.4 | 2.0.11 (`latest`); 4.0.2 (`next`, 2026-11-18) | 2.x maintained; 4 is the future | Bump to 2.0.11; plan 4 for 2027 |
| Alpine.js | 3.17.4 | 3.17.5 (2026-10-20) | Steady patches | Optional bump |
| Pico CSS | 2.1.1 | 2.1.1 (2025-03-15), archived 2026-10-04 | **Dead upstream** | Keep frozen |
| Datastar | n/a | v1.0.5 (2026-11-09) | 1.0 shipped, active | No: rewrite to SSE/signals |
| Unpoly | n/a | 3.15.0 (2026-10-26) | Active | No: same niche as htmx |
| Litestar / Flask / Quart | n/a | 2.25.0 / 3.1.3 / 0.23.1 | Active | No gain; Quart is async (against ADR 0001) |
| NiceGUI | n/a | 4.0.0 (2026-11-04), native mode via pywebview | Active | No: full rewrite onto its component model |
| Flet | n/a | 1.1.2 (2026-11-06), `flet build` with signing docs | Active | Best packaging story, but a total rewrite in Flutter |
| Reflex | n/a | 0.11.0 (2026-11-12) | Pre-1.0 | No: compiles to React, needs a Node build |
| pywebview | n/a | 6.2.1 (2026-04-15) | Active | The way to embed the current UI |

## Cost of moving to htmx 4 later (from reading the templates)

The surface is small: about 100 `hx-*` attributes. These items would need work:

- `hx-disabled-elt` becomes `hx-disable` (7 uses). The rename must be done in order, since the name
  `hx-disable` is reused.
- `lineup_view.html` sets `hx-vals` on the `<ul>` for every tile to inherit. In 4 that needs `:inherited` (and
  probably `:append`, because each tile also has its own `hx-vals`). The merge behaviour is not verified.
- Event renames: `htmx:afterSettle` in `wallpapi.js` and `hx-on::after-settle` in `preview.html` become
  `htmx:after:settle`.
- **4xx/5xx responses now swap by default.** The app sends 400, 404 and 503 responses to htmx requests, so set
  `htmx.config.noSwap = [204, 304, '4xx', '5xx']` or rework the error paths.
- The default request timeout becomes 60 s (it was none). History no longer caches in localStorage, which is
  harmless here.
- Tooling exists: `npx htmx.org@4 upgrade-check`, plus an `htmx-2-compat` extension.

## Key evidence

- htmx 4.0.0 announcement (2026-08-28): fetch, explicit `:inherited`, renamed events, 2.x supported
  indefinitely, `next` tag until early 2027.
  https://four.htmx.org/announcements/2026-08-28-htmx-4.0.0-is-released
- What's new in htmx 4 (renames, swapping all responses, `noSwap`, timeout, history):
  https://four.htmx.org/docs/whats-new-in-htmx-4 (undated, current for 4.0.x)
- htmx releases: v4.0.2 (2026-11-18), v2.0.11 (backports `<hx-partial>`).
  https://github.com/bigskysoftware/htmx/releases ; npm dist-tags `latest=2.0.11`, `next=4.0.2` (checked
  2026-11-22)
- Pico archive notice and README, "no longer maintained… v2.1.1 is the final release" (archived 2026-10-04):
  https://github.com/picocss/pico
- Alpine v3.17.5 (2026-10-20, morph x-cloak fix): https://github.com/alpinejs/alpine/releases/tag/v3.17.5
- FastAPI release notes (0.165.0, 2026-11-18): https://fastapi.tiangolo.com/release-notes/ ; breaking changes
  in https://github.com/fastapi/fastapi/releases/tag/0.162.0 (bool form fields) and /tag/0.162.6 (drop 3.10)
- FastAPI on sync `def` running in an external threadpool: https://fastapi.tiangolo.com/async/
- Datastar releases (v1.0.5, 2026-11-09): https://github.com/starfederation/datastar/releases
- Flet releases (1.1.x, 2026-11; packaging and signing notes): https://github.com/flet-dev/flet/releases
- Versions and dates for the PyPI and npm packages come from the registries' JSON APIs, queried 2026-11-22.

## Risks and unknowns

- **Pico is a dead dependency.** It is harmless today. The risk is a slow one: a new browser behaviour, or a
  dark-mode or accessibility gap, that nobody upstream will fix. If that happens, the cheapest fix is to patch
  it in `base.css`. A replacement framework is not justified.
- **htmx 2 "indefinite" support** is a promise from the maintainers, not a date. Expect the docs and the
  community to move to 4 during 2027.
- **Desktop packaging falls outside this area, but two points touch it.** pywebview's last release was
  2026-04; that is fine, but it is worth checking it on Python 3.14. pystray, often used for tray icons, has
  not released since 2023-09. I have not checked which webview engines (WebView2, WKWebView, WebKitGTK) work
  well with htmx and Alpine; they all should, since they are evergreen.
- **Thin evidence.** I did not read Datastar or Unpoly in depth beyond their release status. The judgement
  that they give "no gain" rests on the fact that each would replace htmx and Alpine wholesale, not on a
  trial. The htmx 4 inheritance-and-merge behaviour for `hx-vals` comes from the docs and has not been tested.
