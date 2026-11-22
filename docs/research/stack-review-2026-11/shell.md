# Desktop shell: background process, tray icon, UI window (researched 2026-11-22)

## Recommendation

**PySide6 `QSystemTrayIcon` for the tray, with the UI in the user's default browser at
`http://127.0.0.1:<port>`. Keep pywebview as an optional later step.**

- Qt's event loop owns the main thread on every OS. uvicorn runs as `uvicorn.Server(config).run()` in a
  `threading.Thread`, and the tray's Quit sets `server.should_exit` and the app's `stop_event`. uvicorn 0.53
  skips signal handling when it is off the main thread (`server.py` `capture_signals`, checked in wallpapi's
  venv), so this works with no hacks. It also fits ADR 0001 (synchronous code, threads).
- Install only `PySide6-Essentials` (QtCore, QtGui, QtWidgets), not the `PySide6` meta package, which pulls in
  Addons and WebEngine.
- On Linux, Qt talks StatusNotifierItem over D-Bus itself. It needs no PyGObject and no libappindicator, which
  matters for a frozen bundle. `QSystemTrayIcon.isSystemTrayAvailable()` gives a clean signal for the Fedora
  fallback.
- The UI already runs in a browser (htmx/Pico/Alpine), so a browser tab costs nothing extra. An embedded
  window is polish, not a requirement.
- **Lighter alternative:** pystray plus the browser. It adds almost no size, but it is effectively
  unmaintained, and on Linux it needs PyGObject and the AppIndicator typelibs.

**Fedora fallback** (stock GNOME, no tray): if `isSystemTrayAvailable()` is false, open the browser on every
launch and put Pause, Next and Quit controls in the web UI itself. The first-run page should tell the user to
install "AppIndicator and KStatusNotifierItem Support" (`dnf install gnome-shell-extension-appindicator`) or
"Status Tray". Qt adds the icon automatically once a tray appears. GNOME's "Background Apps" panel is no
substitute: it is built for portal or Flatpak apps.

## Comparison

| Option | Py 3.14 | Maintenance | Win 11 | macOS | GNOME Wayland | Size / freezing |
|---|---|---|---|---|---|---|
| **PySide6 QSystemTrayIcon** | Yes. Declares 3.14; abi3 wheels | Very active: 6.12.0, Oct 2026 | Yes | NSStatusItem; Qt needs the main thread | SNI via D-Bus; nothing on stock Fedora, works on Ubuntu | Essentials wheel 82 MB (Win), 116 MB (mac universal2). First-class PyInstaller and Nuitka support. LGPL |
| **pystray** | Probably (pure Python plus ctypes/PyObjC). Classifiers stop at 3.4 | Stale: last release 0.19.5, Sep 2023; last commit Mar 2025 | Yes (win32) | `run()` must be on the main thread | appindicator backend needs PyGObject and Ayatana typelibs; xorg backend useless on GNOME | Tiny. Picks its backend at runtime, so a frozen app may need hidden imports (unverified) |
| **pywebview** (window only) | Works in practice: pythonnet 3.2.1 declares 3.14; pywebview's classifiers stop at 3.13 | Very active: 6.2.1 Apr 2026, commits Nov 2026 | WebView2, WinForms | WKWebView; `start()` on the main thread | GTK WebKit2 (system PyGObject) or Qt WebEngine (heavy) | Small on Win/mac, awkward on Linux. **No tray of its own** |
| **rumps** | Unknown | Last release 0.4.0, Oct 2022; commits Feb 2026 | n/a | Menu bar only | n/a | py2app-oriented |
| **wxPython** `TaskBarIcon` | Declares 3.14 (4.3.2, Oct 2026) | Active | Yes | Yes | GTK; same SNI story | No Linux wheels (builds from source) |
| **Toga** `StatusIcon` | Not checked | Active (BeeWare) | Yes | Yes | GTK path uses libxapp; docs say GNOME needs an extension | Briefcase-centric |
| **PyTauri / CrossTray** | Not checked | Young; maturity unverified | ? | ? | ? | Not recommended yet |

## Key evidence

- PySide6 6.12.0 and Essentials wheel sizes: https://pypi.org/project/PySide6-Essentials/ (release
  2026-10-08). Requires Python >=3.10,<3.16; lists 3.14 (https://pypi.org/project/PySide6/).
- Qt 6.12 `QSystemTrayIcon` docs: Linux support is through the StatusNotifierItem spec; since GNOME Shell 3.26
  some activation reasons need shell extensions; `isSystemTrayAvailable()`:
  https://doc.qt.io/qt-6/qsystemtrayicon.html (current).
- pystray FAQ: `Icon.run()` must be called from the main thread on macOS; `run_detached` is needed only on
  macOS: https://pystray.readthedocs.io/en/latest/faq.html. Release 0.19.5 dated 2023-09-17
  (https://pypi.org/project/pystray/). Last commit 2025-03-27 (GitHub API).
- pywebview's official tray example runs pystray in the parent process and pywebview in a `spawn` subprocess,
  because on macOS both need the main thread:
  https://github.com/r0x0r/pywebview/blob/master/examples/pystray_icon.py. Release notes 6.0 to 6.2.1 (2025-08
  to 2026-04) mention menus but no tray: https://github.com/r0x0r/pywebview/releases. Master `pyproject.toml`
  adds an optional WinUI3 backend; the CEF renderer was removed on 2026-11-19.
- pythonnet 3.2.1 (2026-10-08) declares 3.14: https://pypi.org/project/pythonnet/.
- Fedora Workstation issue #246 (status notifiers by default): no decision to ship the extension. On
  2025-06-09 a maintainer said the path is still an upstream freedesktop.org spec. On 2026-01-13 the working
  group asked ngompa (Neal Gompa, who opened the issue) to finish that spec. The issue moved to Fedora Forge
  on 2026-02-04: https://pagure.io/fedora-workstation/issue/246.
- Status Tray extension (SNI, GNOME 46 to 51, ships its own `StatusNotifierWatcher`):
  https://github.com/keithvassallomt/status-tray.
- macOS 26 (Nov 2025): a PySide6 app's menu-bar icon vanished because its launcher `exec`'d a second binary.
  Apple filed it as FB21015611 and the thread gives no workaround:
  https://developer.apple.com/forums/thread/806691. Lesson: the bundle's main executable must be the real
  process (PyInstaller and Nuitka bootloaders need testing on 26).
- Windows 11 puts new tray icons in the overflow menu, and app updates can reset this (community sources, 2021
  to 2025): https://learn.microsoft.com/en-us/answers/questions/5600219/.
- GNOME "Background Apps" is portal-based and missing in GNOME 50 per
  https://bugzilla.redhat.com/show_bug.cgi?id=2457805 (2026).

## Risks and unknowns

- **Size:** Qt adds roughly 40 to 80 MB after freezing (estimate, not measured). It sits next to onnxruntime
  and numpy, which are already large. Measure with a PyInstaller or Nuitka spike that excludes unused Qt
  plugins.
- **Licence:** PySide6 is LGPL. Shipping the Qt libraries as separate shared files (the PyInstaller default)
  is the usual compliant setup; confirm before distributing.
- **Tray discoverability:** on Windows 11 the icon starts hidden, so first run should open the browser and
  tell the user where the icon is. On Fedora there may be no tray at all, so the web UI must be able to do
  everything.
- **AGENTS.md exclusions:** none of this needs Electron or Tauri. Qt is a new dependency, used for the shell
  only. Adding it means a stack-section and ADR change.
- **Thin evidence:** I found no first-hand 2026 reports of pystray on macOS 26 or Python 3.14. pywebview's
  3.14 support is inferred, not declared. Toga, CrossTray and PyTauri were not tested. Whether frozen pystray
  needs hidden imports is unverified.
- **Browser versus window:** a browser tab can be closed or lost among other tabs. If users want a real app
  window, add pywebview later in a child process (the pattern from pywebview's own example), or use Qt
  WebEngine, which costs another 100 MB or more.
