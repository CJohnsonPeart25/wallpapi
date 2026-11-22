# Packaging and distribution: research for wallpapi (2026-11-22)

## Recommendation

**PyInstaller in one-folder mode (`--onedir`, `--windowed`), built from the uv lock file on a GitHub Actions
matrix of one native runner per OS.** Wrap each build for its platform:

- **Windows:** an Inno Setup installer that installs per user and needs no admin rights.
- **macOS:** a zipped `.app` (PyInstaller ad-hoc signs it automatically), sent with a one-page "how to open
  it" note.
- **Linux:** a tarball or AppImage with a `.desktop` file. At home, `uv run` from source also works.

For updates, the app checks the GitHub Releases API and links to the new installer. Leave in-place self-update
out until there are real users.

**Runner-up: Briefcase.** It produces the native installer formats itself (MSI, DMG/PKG, deb/rpm, Flatpak),
can use uv as of 0.4.5, and does not freeze code. Choose it if you would rather have its project conventions
than write three wrapper scripts. Avoid one-file builds for a tray app that runs for a long time, because they
unpack themselves on every launch.

## Comparison

| Tool | Py 3.14 | numpy / onnxruntime | Output | Platform output | Cross-build | Self-update | Status |
|---|---|---|---|---|---|---|---|
| **PyInstaller 6.22.3** (2026-09-12) | Yes since 6.15 (2025-08); 3.15 since 6.21 | numpy hook kept current (6.19 fixed uv-installed numpy DLLs); contrib ships `hook-onnxruntime.py` | onedir or onefile | Win exe; mac `.app` (ad-hoc signed); Linux folder | None, native runners | None built in (tufup or Velopack, both thin) | Very active |
| **Nuitka 4.3** | Official since 4.2 (2026-08-24) | 4.3 added onnxruntime DLL support off Windows | standalone or onefile | 4.2 added an NSIS installer, DMG, AppImage | None | None | Active; slow C compile |
| **Briefcase 0.4.7** (2026-10-27) | Since 0.3.25 (2025-08) | Ordinary wheels, nothing frozen | App folder plus installer | MSI, DMG/PKG, deb/rpm/pkg, Flatpak; AppImage "best effort" | None | None | Active |
| **PyApp 0.31** (2026-10-27) | CPython 3.7 to 3.15 from python-build-standalone | Installs real wheels with pip or uv at first run | One small exe that installs itself | Bare binary, no `.app`, no installers | Rust build per target | `self update` (from a package index) | Active, one maintainer |
| **cx_Freeze 8.7.x** | 8.5 and later support 3.10 to 3.14 | Unverified for onnxruntime | Folder plus MSI, DMG, AppImage, deb/rpm | Wide | None | None | Active, smaller community |
| **PyOxidizer** | No (stops at 3.10) | n/a | n/a | n/a | n/a | n/a | **Dead**: last release Dec 2022, last push Dec 2024 |
| **uv "bundle"** | n/a | n/a | n/a | n/a | n/a | n/a | **Does not exist.** Issue #5802 is open and labelled "not on the immediate roadmap" |

Antivirus false positives are worst for PyInstaller onefile and Nuitka onefile, mostly anecdotal and
community-reported. A onedir build without UPX does better. Briefcase and PyApp run a stock interpreter, so
they probably attract less suspicion, but I found no data on that.

## Key evidence

- **PyInstaller:** the changelog gives 6.15.0 adding 3.14, 6.21.0 adding 3.15, 6.22.3 as the latest, and
  6.19.0 fixing numpy built with uv (https://pyinstaller.org/en/stable/CHANGES.html, read 2026-11-22).
  `hook-onnxruntime.py` is in the contrib stdhooks (GitHub API, 2026-11-22). The docs say Apple Silicon
  requires arm64 code to be signed, and that PyInstaller ad-hoc signs by default
  (https://pyinstaller.org/en/v5.1/feature-notes.html; older docs, behaviour assumed unchanged).
- **onnxruntime 1.32.0 (2026-11-13):** cp314 wheels exist for win_amd64, win_arm64, manylinux_2_28
  x86_64/aarch64 and **macOS 14+ arm64 only, with no Intel Mac wheel**
  (https://pypi.org/project/onnxruntime/).
- **Nuitka:** 4.2 brought official 3.14 support plus NSIS, DMG and AppImage installers
  (https://nuitka.net/posts/nuitka-release-42.html, 2026-08-24). The 4.3 changelog covers onnxruntime DLLs
  (https://nuitka.net/doc/Changelog.html).
- **Briefcase:** the release notes cover 0.3.25 adding 3.14, 0.4.2 adding Windows ARM64, and 0.4.5 adding uv
  and signed Linux packages (https://briefcase.beeware.org/en/stable/about/releases/). The AppImage backend is
  "best effort" (https://briefcase.beeware.org/en/stable/reference/platforms/linux/appimage/).
- **PyApp:** sources are the releases page (https://github.com/ofek/pyapp/releases), the runtime docs on
  first-run install and `self update`/`restore`/`remove` (https://ofek.dev/pyapp/latest/runtime/), and the
  distribution-embedding docs (https://ofek.dev/pyapp/latest/config/distribution/).
- **PyOxidizer:** sources are the maintainer's post on shifting priorities
  (https://gregoryszorc.com/blog/2024/03/17/my-shifting-open-source-priorities/) and the GitHub API (last push
  2024-12-24).
- **uv:** in https://github.com/astral-sh/uv/issues/5802, the only maintainer comment from 2026 (2026-05-26)
  is about uvx.sh, an unadvertised install script, not bundling.
- **Windows, unsigned:** an unsigned file gets "Windows protected your PC" and the user must choose "Run
  anyway". Reputation is tracked per file hash, so every release starts again from zero. **Smart App Control
  on Windows 11 blocks unsigned files outright**
  (https://learn.microsoft.com/windows/apps/package-and-deploy/smartscreen-reputation, updated 2026-08-17).
- **macOS, unsigned:** Apple's documented route is System Settings > Privacy & Security > Open Anyway,
  available for about an hour after the blocked launch, then the login password. The page does not mention
  Control-click (https://support.apple.com/guide/mac-help/mh40616/mac, current at 2026-11-22). Eclectic Light
  tested Tahoe 26.6.1 on 2026-08-11
  (https://eclecticlight.co/2026/08/11/how-can-you-run-code-that-hasnt-been-notarised/). In that test the
  launch dialog for a quarantined, ad-hoc-signed app offered only Move to Trash, but the app ran once its
  quarantine attribute was removed. A **fully unsigned arm64 app never runs.** The practical upshot: ad-hoc
  signing is the minimum, and the friend uses Open Anyway or runs `xattr -dr com.apple.quarantine`.
- **Installer wrapper:** MSIX needs a trusted signature to install, so it does not fit an unsigned app (from
  general knowledge, not re-checked today). Inno Setup (per-user install, Start menu entry, uninstaller) or
  Briefcase's WiX MSI covers what a non-technical user expects. A bare onedir zip does not.

## Risks and unknowns

- **Smart App Control is the biggest Windows risk.** On a clean Windows 11 install with SAC on, an unsigned
  build may not run at all. Microsoft's Artifact Signing service, from $9.99 a month, is the cheap fix if this
  bites. Verify this on a real SAC machine.
- **Intel Macs cannot run it**, since onnxruntime ships no macOS x86_64 wheel. Confirm the friend has Apple
  Silicon.
- **The Gatekeeper evidence conflicts.** Apple documents Open Anyway, but Eclectic Light saw only Move to
  Trash. Test a downloaded build on the friend's macOS version.
- **The antivirus reputation evidence is old and anecdotal** (2021 to 2024 forum posts). I found no 2025-2026
  measurements for any tool.
- **I did not verify** cx_Freeze with onnxruntime, PyApp cross-compilation details, Velopack's Python package
  maturity (PyPI shows mostly dev builds), or current Inno Setup and WiX versions.
- **Linux on GNOME Wayland:** a Flatpak sandbox complicates setting wallpapers (it needs the wallpaper portal)
  and tray icons. Prefer an unsandboxed tarball or a deb/rpm.
- **Network at first run:** PyApp downloads Python and wheels on first launch. That is acceptable, since
  wallpapi already downloads its model, but a network failure is a second way for first run to fail.
