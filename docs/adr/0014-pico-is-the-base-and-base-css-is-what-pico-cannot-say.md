# 14. Pico is the base, and base.css is what Pico cannot say

Date: 2026-09-28

## Status

Accepted. Supersedes the implementation detail in ADR 0003 that `wallpapi.js` sets the preview's `src`;
the decision in 0003 — full resolution loaded from Wallhaven, on demand, never cached — stands, and the
Alpine component on the preview dialog is now what sets and removes it.

## Context

Every template stood alone. `batch.html`, `history.html` and `settings.html` each wrote their own head
and nav, the **Batch** page's 512-line `wallpapi.css` was loaded by History and not by Settings, and
nothing made the three pages agree on type, spacing or colour. `wallpapi.js` had grown to three jobs —
the preview, the grid's column count and the **Mix** dropdown's closed label — in one delegated listener.

The **Batch** page's layout had just been settled by six rounds of prototype (#39). Much of what that
stylesheet said was not about the **Batch** at all: greys, border shades, a pill radius, a button reset,
a type stack, a dark palette. Those are what a CSS framework is for.

Issue #40 asked for a shared shell, Pico CSS underneath it, and Alpine.js for the client behaviour, with
no build step.

## Decision

**Pico CSS is the foundation, not an add-on.** `pico.indigo.min.css`, the default class-based build, in
the indigo variant because its light primary (`#655cd6`) is one shade from the accent the prototype
settled on (`#5b5bd6`), so the accent survives with no override. Not the scoped `.conditional` build:
every page is Pico's. Its greys, radius, type stack, spacing, buttons, forms, nav and modal overlay are accepted.

**`base.css` is what Pico cannot say, and nothing else.** The root font size pinned to 100% (Pico grows
it with the viewport, and the grid is already sized by the viewport); the sticky nav; the three verdict
colours; the `ul.batch` grid with `list-style: none` and `--tile-ratio: 3 / 2`; the tile and its
thumbnail; the ring; the rail and its glyphs; the **Zone** label; the `:has()` rule hiding the refill line
behind a **Batch**; the preview's size; `[x-cloak]`; reduced motion. Any colour in it is a `--pico-*`
variable, a verdict colour, or sits over a thumbnail, so light and dark both read. `wallpapi.css` is gone.

**One shell.** `base.html` is the head, the nav (`nav.html`, with `theme.html` last in it), a `main` block
inside `<main class="container-fluid">`, and a `tail` block. A page extends it; a fragment — grid, tile,
mix, refill, similarity, history_row — never does, because each is also an htmx swap on its own. The nav
is the dock: Pico's `nav > ul + ul`, and the right-hand list is the page's `actions` block, where the
**Batch** page puts the **Mix** `<select>` and a plain primary Submit.

**The preview is Pico's overlay round a frame of our own.** `<dialog><article>` so that Pico paints the
overlay, but the article is not Pico's card: a card sizes to its content and scrolls when it is too tall,
and the prototype's conclusion was the opposite — one frame of one fixed size, 75vw by 75vh, with the
image letterboxed inside it, the Wallhaven link and fullscreen as two pills over its top corner, and the
preview's **Verdict** rail across its foot. `base.css` undoes the card's size, padding and background.

**Alpine.js owns three things**, each an inline `x-data` on the element it belongs to:

- the preview, on the `<dialog>`, which htmx never swaps. It opens from a window-level click on a tile's
  thumbnail, sets the image source by hand on open and removes it on close — never a binding, so the
  markup carries no source and nothing is fetched until asked for — and after every htmx swap reads the
  tile's mark back so the rail and the ring agree. Its footer clicks the tile's own control behind it:
  one route, one swap, one marked state;
- the **Mix** dropdown's two labels, on `#mix-switcher`, read off the options' data attributes so the
  outerHTML swap on a switch costs it nothing;
- the theme button: system, light, dark. "system" is no `data-theme` on `<html>`; the others set it. The
  choice is a preference of the browser, kept in `localStorage`, and never reaches the Core service. A
  short inline script in the head applies it before first paint.

`wallpapi.js` keeps only the grid fit.

**Vendored, byte-identical to the release files, licence headers kept.** Line-ending conversion is off
for them in `.gitattributes`. `scripts/vendor_assets.py` fetches them, and htmx, and checks each SHA-256
before writing; the script is the only place a URL or hash is recorded, so an upgrade is an edit there and
a run of `uv run python scripts/vendor_assets.py`.

| File | Version | Source |
| --- | --- | --- |
| `pico.indigo.min.css` | Pico CSS 2.1.1 | `css/pico.indigo.min.css` at the `v2.1.1` tag of picocss/pico (identical from GitHub, jsDelivr and unpkg) |
| `alpine.min.js` | Alpine.js 3.17.4 | `dist/cdn.min.js` in the `alpinejs@3.17.4` npm package |

## What survives from the prototype, and what was traded

The six rounds of #39 settled a layout by trying it. What they concluded about *behaviour* survives
intact, because none of it is anything Pico has an opinion on:

- the **Verdict** rail, hidden until hover or keyboard focus, translucent, the hovered segment coloured;
- the ring round a marked thumbnail, readable across the grid, and a banned one faded;
- 3:2 tiles, because `thumbs.small` is 3:2 whatever the **Wallpaper** is;
- the grid sized to the viewport, so a **Batch** of 4 fills the screen as readily as one of 32;
- the thumbnail opens the preview, and there is no preview button to aim at;
- a click on anything in the preview that is not a control dismisses it;
- no hover-to-enlarge;
- Submit far from the tiles.

The preview frame survives too — a first cut on Pico's card was tried and reverted the same day: the card
resized round each image and scrolled, which is exactly what the frame exists to prevent.

What they concluded about *look* was traded for a stylesheet well under half the size: the floating dock and
its blur (the sticky nav is the dock now), the preview's own `::backdrop` (Pico's overlay), the pill radius
(Pico's 0.25rem), and the dark palette (Pico's, in both themes). None of those was the reason a round was
won.

## Consequences

Adding a page is writing its `main` block. History and Settings extend the shell here with their bodies
untouched; their restyling on Pico's own elements is #41.

Alpine's default build compiles every directive expression with `new Function`, which a strict Content
Security Policy would forbid. That is acceptable here: wallpapi sets no CSP, binds to `127.0.0.1` only,
and every expression Alpine evaluates is in a template in this repository. A CSP would mean Alpine's CSP
build and moving the components into `Alpine.data` in a vendored file.

Pico styles bare elements, so markup that assumed a blank page can change appearance without a line of
CSS changing — History's rows, which still reuse the tile's class names, are the case in point until #41.
The rail and glyph rules are scoped to `.tile` and the preview so that nothing outside them picks them up.

## Alternatives considered

**Pico's scoped `.conditional` build**, styling only inside a `.pico` container. Rejected: it exists for
adding Pico to a page that has another framework, and wallpapi has none. Every page is meant to be Pico's.

**Keep `wallpapi.css` and add Pico beneath it.** Rejected: most of `wallpapi.css` re-said what Pico says,
differently, and two stylesheets disagreeing about greys and radius is what the issue set out to end.

**Grow `wallpapi.js` instead of adding Alpine.** Rejected: the preview's state (which tile, what mark) and
the **Mix** labels are exactly the small reactive state Alpine exists for, and a delegated listener
reconstructing that state from the DOM on every click was most of the file.
