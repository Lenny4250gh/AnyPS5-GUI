# AnyPS5 GUI

[![License: GPL-2.0](https://img.shields.io/badge/license-GPL--2.0--only-blue)](LICENSE)
[![Latest release](https://img.shields.io/github/v/release/Lenny4250gh/AnyPS5-GUI)](https://github.com/Lenny4250gh/AnyPS5-GUI/releases/latest)
[![Build](https://github.com/Lenny4250gh/AnyPS5-GUI/actions/workflows/build.yml/badge.svg)](https://github.com/Lenny4250gh/AnyPS5-GUI/actions/workflows/build.yml)

A graphical interface for [AnyPS5](https://github.com/boykopovar/AnyPS5): unwrap, relink and run without touching the command line.

> **Unofficial.** Not affiliated with Sony or with the AnyPS5 project. This tool contains **no games, no firmware, no keys and no decryption**. Use it only on software you legitimately own (your own dumps or homebrew).

## Download (no build needed)

1. Open the [**Releases**](https://github.com/Lenny4250gh/AnyPS5-GUI/releases/latest) page.
2. Download the file for your system:

   | System  | File                                                  |
   | ------- | ----------------------------------------------------- |
   | Windows | `AnyPS5_GUI-windows.exe`                              |
   | Linux   | `AnyPS5_GUI-linux` (then `chmod +x AnyPS5_GUI-linux`) |
   | macOS   | `AnyPS5_GUI-macos.zip`                                |

3. *(Optional but recommended)* verify the file: compare its SHA-256 with the `sha256:` value GitHub shows next to each file on the release page:
   - Windows (PowerShell): `Get-FileHash .\AnyPS5_GUI-windows.exe -Algorithm SHA256`
   - Linux: `sha256sum AnyPS5_GUI-linux`
   - macOS: `shasum -a 256 AnyPS5_GUI-macos.zip`
4. Run it. The first time it creates a folder in your user data folder (so it survives updates), where it keeps the downloaded tools (relinker, libs, `unwrap_self.py`). You can move it from **Settings → Storage**. To keep everything next to the program instead, put an empty file named `portable` next to it.

All three files are built automatically by GitHub Actions on real Windows, macOS and Linux machines, straight from the source in this repository. You can inspect the [workflow](.github/workflows/build.yml) and the [build runs](https://github.com/Lenny4250gh/AnyPS5-GUI/actions).

### Upgrading from an older version

The first versions kept a `Utilities/` folder next to the program. On startup the app moves it automatically to the new location (your user data folder, or the one chosen in **Settings → Storage**):

- Only what the app itself uses is moved: tools (`unwrap_self.py`, `relinker-*`, `prx-*.zip`, `tools/`), `config.ini`, `anyps5-input.ini`, backups, fonts and 7-Zip files.
- Items that already exist in the new folder are not overwritten, and any other file you left in the old folder stays where it is.
- The old folder is removed (sent to the trash) only if nothing else is left in it.
- The big folders `release/`, `source/`, `toolchain/`, `extracted/` and `update/` are moved only if the old and new folders are on the same drive; otherwise they stay behind and are downloaded or rebuilt when needed.

### Requirements and warnings

- **Windows:** needs the Microsoft Edge WebView2 runtime (already present on up-to-date Windows 10/11). SmartScreen or your antivirus may warn about the file: it is not code-signed, and PyInstaller one-file programs are often flagged as false positives. If you don't trust the binary, read the source and [build it yourself](#build-from-source) (it takes a minute).
- **macOS:** the app is not signed. Right-click the app → **Open** the first time (or run `xattr -cr AnyPS5_GUI.app`).
- **Linux:** the binary bundles Qt, but it still needs a few system libraries. On Debian/Ubuntu: `sudo apt install libegl1 libgl1 libxkbcommon-x11-0 libxcb-cursor0 libdbus-1-3`.

## Quick start

1. Open the app and go to **Convert a game**.
2. Check the **requirements**: the app tells you what is missing and can download the tools for you.
3. Pick your game folder (your own dump), or a `.zip` / `.rar` of it: archives are extracted automatically.
4. Run **Unwrap** if your files are decrypted SELF/fSELF, then **Relink**. The result is a `<GAME>-<system>` package next to the game.
5. Go to **Play a game** and press **Launch**, or create a shortcut.

Settings (runtime, input, appearance) can be changed at any time from their tabs.

## Features

**Convert a game**
- **Unwrap:** already-decrypted SELF/fSELF → ELF, using the official `unwrap_self.py` (the original files are backed up, with a *Restore backup* button).
- **Relink:** builds the `<GAME>-<system>` package (app0 + libs + executable) for Windows, Linux or macOS.
- **Game archives:** the game can be a folder or a `.zip` / `.rar` (`.rar` needs 7-Zip installed).
- **Requirements check:** shows what is missing before you start, and downloads the tools for you when needed.
- **Tools source:** choose where the relinker, libs and `unwrap_self.py` come from (see [below](#where-the-tools-come-from)).
- **Options:** all relinker options, each marked with the systems it applies to. Options that do not fit the selected target are disabled automatically.
- **Audit:** runs `import_audit.py` (if present) after the relink.

<img src="docs/screenshots/1.png" width="600" alt="Convert Page">
<img src="docs/screenshots/2.png" width="600" alt="Convert Page 2">

**Play a game**
- **Launch** a converted game with your current settings, or **create a shortcut** that starts it with the right variables.

<img src="docs/screenshots/3.png" width="600" alt="Play Page">

**Settings**
- **Conversion settings**
<img src="docs/screenshots/8.png" width="600" alt="Conversion settings">

- **Runtime settings:** Language, GPU, present mode, fonts, tracing and more, passed to the game as environment variables.
<img src="docs/screenshots/7.png" width="600" alt="Runtime settings">

- **Storage:** see where the app keeps its tools, builds, backups and `config.ini`, and move everything to another folder with one click.
- Any section of the settings can be exported as a shareable file.

**Updates**
- The app checks the latest release of this repository and, when a newer one exists, can download it for your system, replace itself and restart.

**Input**
- Visual controller editor: click a button, then press a key, mouse button or wheel direction. Alternate inputs supported.

<img src="docs/screenshots/4.png" width="600" alt="Input mapping">

- Stick deadzones and mouse-as-stick settings (sensitivity, curve, smoothing, decay).
<img src="docs/screenshots/5.png" width="600" alt="Input deadzones and mouse settings">

- Import / export your settings, or edit `anyps5-input.ini` directly in text view (with validity check).
<img src="docs/screenshots/6.png" width="600" alt="Input deadzones and mouse settings">

**Appearance**
- Choose the main color (presets or custom hex) and the animated background (soft grid, orbs or none). It stops automatically if your system asks for reduced motion.

<img src="docs/screenshots/9.png" width="600" alt="Appearance settings">

### Where the tools come from

The relinker, libs and `unwrap_self.py` are chosen in **Advanced: where the tools come from** (Convert a game page):

| Source | What it does |
| ------ | ------------ |
| **Local files (manual)** | Uses the files you provide. Press **Import files…** and pick `unwrap_self.py`, `relinker-*` and `prx-*.zip` (optionally `import_audit.py` and `pad_input.py`): they are copied into `Utilities/`. If a file already exists you are asked whether to replace it. |
| **Release from GitHub repo (auto-update)** | Downloads the latest release of the AnyPS5 repository (**Download now**). |
| **Build from GitHub source (auto-update)** | Clones the repository and builds it (**Build now**); needs git, CMake and a C/C++ compiler. |

Only the controls that apply to the selected source are shown.

## Build from source

Requires Python 3.9+ (3.11 is what the CI uses).

```
git clone https://github.com/Lenny4250gh/AnyPS5-GUI.git
cd AnyPS5-GUI
pip install -r requirements.txt
python AnyPS5_GUI.py          # run directly
```

To build the executable you also need PyInstaller and Pillow:

```
pip install pyinstaller pillow
python build.py               # asks for the target OS
python build.py 3             # no questions: 1 = Windows, 2 = macOS, 3 = Linux
```

`build.py` embeds `index.html` into the executable and calls PyInstaller. PyInstaller cannot cross-compile: it always packages for the system it runs on, so build **on** the system you are targeting (or let GitHub Actions do it). The result is in `dist/`.

When running from source on Linux, pywebview needs a WebKitGTK or Qt backend, see the [pywebview docs](https://pywebview.flowrl.com/guide/installation.html).

## Support

- Problems with **this GUI** (it doesn't open, a button does nothing, a wrong option): open an [issue](https://github.com/Lenny4250gh/AnyPS5-GUI/issues). Please include your OS, the GUI version and any error shown.
- Problems with **AnyPS5 itself** (a game doesn't start, crashes, missing library functions): this GUI cannot fix them. Ask in the official [AnyPS5 Discord](https://discord.gg/BHFztBPUe) or in the [Game bring-up discussions](https://github.com/boykopovar/AnyPS5/discussions/categories/game-bring-up), and read the [usage guide](https://github.com/boykopovar/AnyPS5/blob/main/docs/user/USAGE.md).

## License

GPL-2.0-only, see [LICENSE](LICENSE). The GUI loads the official `unwrap_self.py` from the AnyPS5 project (GPL v2 only) and does not contain a copy of it. If you redistribute it together with the GUI, include its license and sources.

## Credits

[AnyPS5](https://github.com/boykopovar/AnyPS5) by its authors, which does the real work (relinker, system libraries, `unwrap_self.py`). This GUI only makes it easier to use.

## Disclaimer

For interoperability, research, preservation and compatibility purposes only. This project does not include, distribute or require copyrighted software, firmware, cryptographic keys or proprietary libraries. You are responsible for obtaining and using any binaries in accordance with applicable laws and license terms. Provided "as is", without warranty.
