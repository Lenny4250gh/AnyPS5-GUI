# AnyPS5 GUI

A graphical interface for [AnyPS5](https://github.com/boykopovar/AnyPS5): unwrap, relink and run without touching the command line.

> **Unofficial.** Not affiliated with Sony or with the AnyPS5 project. This tool contains **no games, no firmware, no keys and no decryption**. Use it only on software you legitimately own (your own dumps or homebrew).

> **Vibe-coded.** Made by a hobbyist with the help of AI (Claude). Not professionally audited, so use it with a bit of care and feel free to report bugs. More [below](#about-this-project).

## Download (no build needed)

1. Open the [**Releases**](https://github.com/Lenny4250gh/AnyPS5-GUI/releases/latest) page.
2. Download the file for your system:

   | System  | File                                                  |
   | ------- | ----------------------------------------------------- |
   | Windows | `AnyPS5_GUI-windows.exe`                              |
   | Linux   | `AnyPS5_GUI-linux` (then `chmod +x AnyPS5_GUI-linux`) |
   | macOS   | `AnyPS5_GUI-macos.zip`                                |

3. *(Optional but recommended)* verify the file against `SHA256SUMS.txt` from the same release:
   - Windows (PowerShell): `Get-FileHash .\AnyPS5_GUI-windows.exe -Algorithm SHA256`
   - Linux / macOS: `sha256sum AnyPS5_GUI-linux` (macOS: `shasum -a 256 <file>`)
4. Run it. The first time it creates a `Utilities/` folder next to the program.

All three files are built automatically by GitHub Actions on real Windows, macOS and Linux machines (see [`.github/workflows/build.yml`](.github/workflows/build.yml)), straight from the source in this repository.

### Warnings you may see

- **Windows SmartScreen / antivirus:** the executables are not code-signed, and PyInstaller one-file programs are often flagged as false positives. If you don't trust the binary, read the source and [build it yourself](#build-from-source) (it takes a minute).
- **macOS:** the app is not signed. Right-click the app → **Open** the first time (or run `xattr -cr AnyPS5_GUI.app`).
- **Windows** needs the Microsoft Edge WebView2 runtime (already present on up-to-date Windows 10/11).
- **Linux** needs a WebKitGTK (or Qt) runtime for pywebview, see the [pywebview docs](https://pywebview.flowrl.com/guide/installation.html).

## Features

**Convert a game**
- **Unwrap:** already-decrypted SELF/fSELF → ELF, using the official `unwrap_self.py` (the original files are backed up, with a *Restore backup* button).
- **Relink:** builds the `<GAME>-<system>` package (app0 + libs + executable) for Windows, Linux or macOS.
- **Requirements check:** shows what is missing before you start, and downloads the tools for you when needed.
- **Options:** all relinker options, each marked with the systems it applies to. Options that do not fit the selected target are disabled automatically.
- **Audit:** runs `import_audit.py` (if present) after the relink.

**Play a game**
- **Launch** a converted game with your current settings, or **create a shortcut** that starts it with the right variables.

**Runtime**
- Language, GPU, present mode, fonts, tracing and more, passed to the game as environment variables.

**Input**
- Visual controller editor: click a button, then press a key, mouse button or wheel direction. Alternate inputs supported.
- Stick deadzones and mouse-as-stick settings (sensitivity, curve, smoothing, decay).
- Import / export your settings, or edit `anyps5-input.ini` directly in text view (with validity check).

**Appearance**
- Choose the main color (presets or custom hex) and the animated background (soft grid, orbs or none). It stops automatically if your system asks for reduced motion.

The relinker, libs and `unwrap_self.py` can come from the AnyPS5 GitHub releases, from a local clone built from source (needs git, CMake and a C/C++ compiler), or from files you put in `Utilities/`.

<details>
<summary><b>Screenshots: settings</b></summary>

<br>

**Conversion options** (each marked with the systems it applies to)

<img src="docs/screenshots/8.png" width="700" alt="Conversion settings">

**Runtime** (language, GPU, tracing, fonts...)

<img src="docs/screenshots/7.png" width="700" alt="Runtime settings">

**Input editor** (controller view, deadzones and mouse settings)

<img src="docs/screenshots/5.png" width="700" alt="Input mapping">
<img src="docs/screenshots/6.png" width="700" alt="Input deadzones and mouse settings">

**Appearance** (main color and animated background)

<img src="docs/screenshots/1.png" width="700" alt="Appearance settings">

</details>

## Build from source

Requires Python 3.9+.

```
git clone https://github.com/Lenny4250gh/AnyPS5-GUI.git
cd AnyPS5-GUI
pip install -r requirements.txt
python AnyPS5_GUI.py          # run directly
python build.py               # or build the executable (asks for the target OS)
python build.py 3             # same, no questions: 1 = Windows, 2 = macOS, 3 = Linux
```

`build.py` embeds `index.html` into the executable and calls PyInstaller. PyInstaller cannot cross-compile: it always packages for the system it runs on, so build **on** the system you are targeting (or let GitHub Actions do it, see above). The result is in `dist/`.

## About this project

I'm not a professional developer. This GUI was built almost entirely with the help of an AI assistant (Claude by Anthropic): I described what I wanted, tested it, and iterated. It works for me, but it may contain bugs or code that a more experienced developer would write differently.

If you find a problem or have suggestions, issues and pull requests are very welcome. Since the code is open, please review it yourself before trusting it.

## Support

- Problems with **this GUI** (it doesn't open, a button does nothing, a wrong option): open an [issue](https://github.com/Lenny4250gh/AnyPS5-GUI/issues) in this repository.
- Problems with **AnyPS5 itself** (a game doesn't start, crashes, missing library functions): this GUI cannot fix them. Ask in the official [AnyPS5 Discord](https://discord.gg/BHFztBPUe) or in the [Game bring-up discussions](https://github.com/boykopovar/AnyPS5/discussions/categories/game-bring-up), and read the [usage guide](https://github.com/boykopovar/AnyPS5/blob/main/docs/user/USAGE.md).

## License

GPL-2.0-only, see [LICENSE](LICENSE). The GUI loads the official `unwrap_self.py` from the AnyPS5 project (GPL v2 only) and does not contain a copy of it. If you redistribute it together with the GUI, include its license and sources.

## Credits

[AnyPS5](https://github.com/boykopovar/AnyPS5) by its authors, which does the real work (relinker, system libraries, `unwrap_self.py`). This GUI only makes it easier to use.

## Not affiliated

This is an independent, third-party project. It is **not affiliated with, endorsed by or sponsored by Sony Interactive Entertainment**, and it is **not part of the AnyPS5 project** or its developers. "PlayStation" and "PS5" are trademarks of Sony Interactive Entertainment Inc.; they are used here only to describe compatibility.

This GUI is essentially a launcher for [AnyPS5](https://github.com/boykopovar/AnyPS5). It does not include or redistribute the relinker, libraries or `unwrap_self.py`: it downloads them from the official AnyPS5 repository (or uses a local copy you provide). If that project becomes unavailable, this GUI will not be able to provide those files, and it will not work without them.

By default the GUI points to the official AnyPS5 repository, but you can change the repository in the settings and use another one with the same structure (same release names and layout). Any other repository you enter is a third-party source: it is **not made, hosted, reviewed or endorsed by me**, and I am not responsible for its content. Only use sources you trust and that you are allowed to use, and review what they contain before running it.

## Disclaimer

This project is intended for interoperability, research, preservation and compatibility purposes. It does not include, distribute or require copyrighted software, firmware, cryptographic keys or proprietary libraries. Users are responsible for ensuring that any binaries used with it are obtained and used in accordance with applicable laws and their respective license terms. Provided "as is", without warranty.
