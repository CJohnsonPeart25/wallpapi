# Per-monitor wallpapers and screen enumeration from Python

Researched 2026-11-22. Sources are dated where the page gives a date. "Unverified" means the claim comes from
memory or a single community report and needs testing on a real machine.

## Recommendation

Write thin adapters of your own. No maintained library covers all three platforms per screen.

- **Windows 10/11.** Use `IDesktopWallpaper` through **comtypes**, declaring the interface by hand. It sets a
  different image on each monitor. Its position (fill/fit/span) is **one global setting**, not per monitor, so
  pre-crop each image to its screen's pixel size and use `DWPOS_FILL`. The fallback is `DWPOS_SPAN` with a
  stitched image, which you should rarely need.
- **macOS (27 "Golden Gate", Apple Silicon).** Use **pyobjc**
  `NSWorkspace.setDesktopImageURL_forScreen_options_error_`. It sets a different image on each screen, but
  **only on the current Space**. There is no API for "all Spaces". The call has to run on the **main thread**.
- **GNOME on Wayland (GNOME 51 in Fedora 45 and Ubuntu 26.10).** GNOME has **no per-monitor wallpaper**. Use
  the shared stitching fallback: build one virtual-desktop-sized image, set `picture-options='spanned'`, and
  write both `picture-uri` and `picture-uri-dark`. GNOME switches between the two by itself when the colour
  scheme changes, which gives day/night for free. List screens with **Mutter
  `org.gnome.Mutter.DisplayConfig.GetCurrentState`** over D-Bus. The wallpaper portal is not useful here.
- **KDE Plasma 6.** Run a script through `org.kde.plasmashell /PlasmaShell evaluateScript` that loops over
  `desktops()` and sets the `org.kde.image` plugin on each screen. It is per-screen, but a known refresh bug
  affects Wayland.
- **Fedora vs Ubuntu.** Same mechanism on both. The only difference I found is cadence: Ubuntu 26.04 LTS is on
  GNOME 50 (I didn't verify this release) and 26.10 is on GNOME 51, while Fedora 45 is on GNOME 51.

## Table

| Platform | Mechanism | Per-screen | Screen list | Fit mode | Fallback |
|---|---|---|---|---|---|
| Windows 10/11 | `IDesktopWallpaper` (comtypes) | Yes | `GetMonitorDevicePathAt` + `GetMonitorRECT` | Global `SetPosition` | Stitch + `DWPOS_SPAN` |
| macOS 27 | `NSWorkspace` (pyobjc 13.0) | Yes, current Space only | `NSScreen.screens()`, `NSScreenNumber` | Per screen (options dict) | Not needed |
| GNOME 50/51 | `org.gnome.desktop.background` gsettings | **No** | Mutter DisplayConfig D-Bus | `spanned` | Stitch (always) |
| KDE Plasma 6 | plasmashell `evaluateScript` | Yes | `desktops()[i].screen` | `FillMode` per screen | Stitch |
| Portal (any DE) | `org.freedesktop.portal.Wallpaper` v1 | No | n/a | n/a | n/a |

## Key evidence

**Windows**
- [IDesktopWallpaper
  interface](https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nn-shobjidl_core-idesktopwallpaper)
  (updated 2024-05-29). Supported from Windows 8. It has methods for monitor paths, `GetMonitorRECT`,
  Get/SetWallpaper and Get/SetPosition.
- [SetWallpaper](https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nf-shobjidl_core-idesktopwallpaper-setwallpaper)
  (2024-02-22) takes `(monitorID, path)`, and a NULL ID means all monitors.
  [SetPosition](https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nf-shobjidl_core-idesktopwallpaper-setposition)
  takes **no monitor ID**.
  [DESKTOP_WALLPAPER_POSITION](https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/ne-shobjidl_core-desktop_wallpaper_position)
  lists CENTER, TILE, STRETCH, FIT, FILL and SPAN, where SPAN means "a single image across all monitors".
- [GetMonitorDevicePathAt](https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nf-shobjidl_core-idesktopwallpaper-getmonitordevicepathat)
  also returns **detached monitors that have an image assigned**.
  [GetMonitorRECT](https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nf-shobjidl_core-idesktopwallpaper-getmonitorrect)
  returns `S_FALSE` for a detached monitor, and the rectangle is "in screen coordinates". Filter on `S_FALSE`.
  The monitor ID is a device path, so it is stable across reboots.
- Python bindings: comtypes 1.5.0 (2026-11-09) is pure Python. pywin32 312 (2026-06-04) has cp314 wheels. I
  found no maintained Python wrapper for `IDesktopWallpaper`, so expect to write about 40 lines of interface
  declaration.
- Virtual desktops: the evidence is thin. A [Neowin report on build
  25197](https://www.neowin.net/news/windows-11-build-25197-finally-lets-you-change-wallpaper-on-all-desktops-at-once/)
  (2022) says changes used to apply only to the current desktop. In a [Microsoft Q&A
  answer](https://learn.microsoft.com/en-us/answers/a/12251137), the per-desktop registry keys are
  unsupported. Slideshow has had bugs with multiple desktops on 24H2 ([Tech Community
  thread](https://techcommunity.microsoft.com/discussions/edgeinsiderdiscussions/desktop-slideshow-doesnt-work-with-multiple-virtual-desktops/4168345)).

**macOS**
- [setDesktopImageURL
  docs](https://developer.apple.com/documentation/appkit/nsworkspace/setdesktopimageurl(_:for:options:))
  (macOS 10.6+) say: "You must call this method from your app's main thread." The page says nothing about
  Spaces or sandboxing.
- In an [Apple forum thread](https://developer.apple.com/forums/thread/834630) (June 2026), an Apple
  Frameworks Engineer wrote: "There is currently no API to allow setting desktop images for all spaces on a
  screen."
- A [Tahoe report](https://developer.apple.com/forums/thread/814926) (Feb 2026, no replies) says setting the
  wallpaper from a script turns "Show on all spaces" off, after which changes hit only the active Space.
- A [Sequoia user reply](https://discussions-jp-prz.apple.com/thread/256063891) says per-screen images through
  NSScreen/NSWorkspace work with two displays, with no change from Sonoma. Sonoma ignores the `fillColor`
  option ([pascalpp/current-desktop-color](https://github.com/pascalpp/current-desktop-color)).
- pyobjc-framework-Cocoa 13.0 (2026-10-30) has cp314 wheels. I found no documented permission or entitlement
  requirement for an unsandboxed app.

**GNOME / Linux**
- Release versions: [Fedora 45 released 2026-11-03 with GNOME
  51](https://fedoramagazine.org/whats-new-fedora-workstation-45/). GNOME 51 came out 2026-09-16 ([GNOME
  Discourse](https://discourse.gnome.org/t/gnome-51-released/38820)). Ubuntu 26.10 (2026-10-15) ships GNOME 51
  ([UbuntuHandbook](https://ubuntuhandbook.org/index.php/2026/09/gnome-51-released-to-be-default-in-ubuntu-26-10-fedora-45/)).
  The [GNOME 51 feature roundup](https://itsfoss.com/news/gnome-51-features/) (2026-09-17) mentions no
  per-monitor wallpaper.
- Per-monitor status: there is only an unmerged proof of concept by Gianluca Masci
  ([Phoronix](https://phoronix.com/news/PoC-Per-Background-GNOME), about 2026-06-26). The request is still
  open ([gnome-shell#7033](https://gitlab.gnome.org/GNOME/gnome-shell/-/issues/7033),
  [Discourse](https://discourse.gnome.org/t/feature-request-per-screen-wallpaper-and-enhanced-slideshow-support/28557)).
- The [background
  schema](https://github.com/GNOME/gsettings-desktop-schemas/blob/master/schemas/org.gnome.desktop.background.gschema.xml.in)
  has `picture-uri` and `picture-uri-dark`, accepts local `file://` only, and offers `picture-options` values
  none, wallpaper, centered, scaled, stretched, zoom and spanned.
- The [Mutter DisplayConfig
  XML](https://github.com/GNOME/mutter/blob/main/data/dbus-interfaces/org.gnome.Mutter.DisplayConfig.xml)
  returns, per monitor, (connector, vendor, product, serial) and modes. Per logical monitor it gives x, y,
  scale, transform (rotation) and primary, plus a `layout-mode` (logical or physical). The XML says it is
  "used by mutter and gnome-settings-daemon" and makes no stability promise. dbus-fast 5.2.0 (2026-10-02) has
  cp314 wheels. GDK4's `GdkMonitor` (connector, geometry, scale) is the heavier alternative.
- [Wallpaper portal
  v1](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Wallpaper.html) offers
  `SetWallpaperFile` with `set-on` (background, lockscreen or both) and `show-preview`. It has **no monitor
  option and no dark-variant option**.
- [WallShuffle](https://extensions.gnome.org/extension/10449/wallshuffle/) does per-monitor through a shell
  extension but lists support for GNOME 46 to 50 only, not 51. Its docs don't say whether an external app can
  drive it.

**KDE**
- The [Plasma scripting docs](https://develop.kde.org/docs/plasma/scripting/) describe `evaluateScript` over
  D-Bus. [Bug 506862](https://bugs.kde.org/show_bug.cgi?id=506862) (filed 2025-07-10, still open, Plasma 6.4
  Wayland) says `reloadConfig()` doesn't repaint the wallpaper until plasmashell restarts. A possibly related
  merge request appeared in June 2026.

**Libraries**
- Rust [`wallpaper` 3.2.0](https://crates.io/crates/wallpaper) (2021) sets one wallpaper only.
  [`more-wallpapers` 0.4.0](https://docs.rs/crate/more-wallpapers/0.4.0) (2024-03) is per-screen on X11, KDE,
  Sway and XFCE, but **not** on Windows, macOS or GNOME.
- Python [superpaper 2.2.1](https://pypi.org/project/superpaper/) (2022-12) is stale and built around a GUI.
- [HydraPaper 3.3.2](https://flathub.org/apps/org.gabmus.hydrapaper) (about 2023) does GNOME stitching plus
  spanned, but it is an app, not a library. It confirms the stitching approach.
- [Variety's FAQ](https://answers.launchpad.net/variety/+faq/2771) (2016) says it has no per-monitor support.

## Risks and unknowns

1. **Windows DPI** (unverified). If the process is not per-monitor DPI aware, `GetMonitorRECT` may return
   virtualised coordinates. Set per-monitor-v2 awareness before listing screens. Getting orientation and
   refresh rate needs `EnumDisplaySettings` or `QueryDisplayConfig`, matched by device path.
2. **Windows virtual desktops** (thin evidence). `SetWallpaper` probably affects only the active virtual
   desktop on Windows 11. Test this.
3. **Windows file locking** (unverified). I believe Explorer copies the image into its own
   `TranscodedWallpaper` cache, so the source file can be replaced afterwards. COM needs an STA thread: give
   the adapter one dedicated thread.
4. **macOS main thread.** wallpapi runs its work on `threading.Thread`s under FastAPI, and the AppKit call
   needs the main thread. Plan either a main-thread run loop or a short-lived helper process for each apply. A
   helper process also suits "if the app crashes, the last wallpaper stays".
5. **macOS Spaces.** A user with several Spaces will see new images only on the current one. The workaround is
   to re-apply when the active Space changes (`NSWorkspaceActiveSpaceDidChangeNotification`), which needs a
   run loop.
6. **macOS 27.** I found no developer evidence either way on whether the API changed in macOS 27.
7. **GNOME spanned with mixed scales or rotation.** How Mutter maps a spanned image onto monitors with
   different scale factors (logical vs physical layout mode) and portrait transforms is undocumented. Test it
   on a real landscape plus portrait setup. HydraPaper's source is the best reference.
8. **Mutter DisplayConfig is not a public, stable API.** It works from an unsandboxed session process. It
   would need a portal-free path or a fallback to GDK if wallpapi were ever shipped as a Flatpak.
9. **GNOME future.** Native per-monitor support could land in GNOME 52 (March 2027) or later. Keep the GNOME
   adapter behind the same "plan → apply" seam so it can drop stitching later.
