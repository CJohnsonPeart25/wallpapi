# Stack review, November 2026

Research, not decisions. Six read-only research agents each checked one area of the stack against primary
sources dated 2025-2026, for an idea that is not yet a spec: wallpapi as a desktop program shared with
non-technical people, growing into a wallpaper manager (a wallpaper per monitor, a journey order through
similar favourites, day and night). Platforms: Windows first, then macOS, then Fedora or Ubuntu (GNOME,
Wayland). Code signing and autostart are out of scope for now: each change of wallpaper only sets the
wallpaper, so if wallpapi stops, the last one stays.

The lead spot-checked four claims (onnxruntime 1.32's wheel platforms, Pico's archived repository, the
vendored htmx version, the locked FastAPI version); the rest is the agents' reading, with their weak spots
listed in each report. Versions and dates are as of 2026-11-22.

| Area | Report | Verdict |
|---|---|---|
| Packaging | [packaging.md](packaging.md) | PyInstaller one-folder, one native runner per OS; Inno Setup on Windows |
| Desktop shell | [shell.md](shell.md) | PySide6 (Essentials) tray icon, the UI in the default browser |
| Wallpaper setting | [wallpaper.md](wallpaper.md) | Thin adapters per platform; GNOME 51 has no per-monitor, so stitch |
| Similarity model | [model.md](model.md) | Keep CLIP B/32 on onnxruntime; SigLIP 2 B/32 the one candidate |
| Web UI | [web.md](web.md) | Keep all; htmx 2.0.4 to 2.0.11; Pico archived but vendored |
| Tooling | [tooling.md](tooling.md) | Keep; add `required-environments`; pyright still the strict checker |

## What changed from the first guesses

- The tray: PySide6, not pystray, whose last release was in September 2023.
- No Intel Macs: onnxruntime ships macOS wheels for Apple Silicon on macOS 14 and later only.
- Windows Smart App Control can block an unsigned program outright on Windows 11, where the usual case is
  the "Run anyway" warning. Test an unsigned build on the real machines early.
- Stock Fedora GNOME shows no tray icon, Ubuntu's does, so the web UI must hold every control and the tray
  is a convenience.

## Design consequences the reports point to

1. The core crops each image to its screen's exact pixels and an adapter only shows it. Windows applies
   fill or fit to every monitor at once, and GNOME's spanned image is the same crops laid side by side.
2. Qt owns the main thread; the server and the background loops run on threads. macOS wants wallpaper calls
   on the main thread, which is Qt's already. uvicorn 0.53 runs off the main thread without changes.
3. On GNOME, day and night come free: it has a light and a dark wallpaper key and switches between them.
   Elsewhere wallpapi decides by the clock.
4. The journey order needs no new dependency: greedy nearest neighbour, then 2-opt, in numpy.

## On `uv build`

The uv packaging guide (`docs.astral.sh/uv/guides/package/`) builds a wheel and an sdist and publishes them
to an index; it makes no program that runs without Python. Two uses remain. A wheel on a GitHub Release with
`uv tool install` is the lightest way to run wallpapi on a machine whose owner can use a terminal. And the
wheel is what PyApp wraps into a small executable that sets up Python on first launch, a lighter alternative
to PyInstaller worth a spike, at the cost of a first launch that needs the network.

## Open questions, for real machines

- How GNOME spans one image across monitors of different scale, or one rotated to portrait.
- Whether Windows 11 sets a wallpaper on every virtual desktop or only the current one.
- What an unsigned, downloaded app does on current macOS: Apple's documentation and an August 2026 test
  disagree.
- Before trying SigLIP 2: `evaluate` cannot compare two models yet, and the radius and near-duplicate
  thresholds were tuned on CLIP.
