# AnyPS5 GUI

A graphical interface for [AnyPS5](https://github.com/boykopovar/AnyPS5): unwrap, relink and run without touching the command line.

> **Unofficial.** Not affiliated with Sony or with the AnyPS5 project. This tool contains **no games, no firmware, no keys and no decryption**. Use it only on software you legitimately own (your own dumps or homebrew).

## Download (no build needed)

1. Open the [**Releases**](../../releases/latest) page.
2. Download the file for your system:

   | System  | File |
   |---------|------|
   | Windows | `AnyPS5_GUI-windows.exe` |
   | Linux   | `AnyPS5_GUI-linux` (then `chmod +x AnyPS5_GUI-linux`) |
   | macOS   | `AnyPS5_GUI-macos.zip` |

3. *(Optional but recommended)* verify the file against `SHA256SUMS.txt` from the same release:
   - Windows (PowerShell): `Get-FileHash .\AnyPS5_GUI-windows.exe -Algorithm SHA256`
   - Linux / macOS: `sha256sum AnyPS5_GUI-linux` (macOS: `shasum -a 256 <file>`)
4. Run it. The first time it creates a `Utilities/` folder next to the program.

### Warnings you may see

- **Windows SmartScreen / antivirus:** the executables are not code-signed, and PyInstaller one-file programs are often flagged as false positives. If you don't trust the binary, read the source and [build it yourself](#build-from-source) (it takes a minute).
- **macOS:** the app is not signed. Right-click the app → **Open** the first time.
- **Windows** needs the Microsoft Edge WebView2 runtime (already present on up-to-date Windows 10/11).
- **Linux** needs a WebKitGTK (or Qt) runtime for pywebview, see the [pywebview docs](https://pywebview.flowrl.com/guide/installation.html).

## What it does

- **Unwrap:** already-decrypted SELF/fSELF → ELF, using the official `unwrap_self.py`.
- **Relink:** builds the `<GAME>-<system>` package (app0 + libs + executable) for Windows, Linux or macOS.
- **Options:** all relinker options, each marked with the systems it applies to.
- **Runtime:** language, GPU, present mode, fonts, tracing, and more; can create a shortcut that starts the game with the right variables.
- **Input:** editor for `anyps5-input.ini` with validity check.
- **Audit:** runs `import_audit.py` (if present) after the relink.

The relinker, libs and `unwrap_self.py` can come from the AnyPS5 GitHub releases, from a local clone built from source (needs git, CMake and a C/C++ compiler), or from files you put in `Utilities/`.

## Build from source

Requires Python 3.9+.

```sh
git clone https://github.com/Lenny4250gh/AnyPS5-GUI.git
cd AnyPS5-GUI
pip install -r requirements.txt
python AnyPS5_GUI.py          # run directly
python build.py               # or build the executable (asks for the target OS)
```

`build.py` embeds `index.html` into the executable and calls PyInstaller. PyInstaller cannot cross-compile: run it **on** the system you are building for. The result is in `dist/`.

## Support

- Problems with **this GUI** (it doesn't open, a button does nothing, a wrong option): open an [issue](../../issues) in this repository.
- Problems with **AnyPS5 itself** (a game doesn't start, crashes, missing library functions): this GUI cannot fix them. Ask in the official [AnyPS5 Discord](https://discord.gg/BHFztBPUe) or in the [Game bring-up discussions](https://github.com/boykopovar/AnyPS5/discussions/categories/game-bring-up), and read the [usage guide](https://github.com/boykopovar/AnyPS5/blob/main/docs/user/USAGE.md).

## License

GPL-2.0-only, see [LICENSE](LICENSE). The GUI loads the official `unwrap_self.py` from the AnyPS5 project (GPL v2 only) and does not contain a copy of it. If you redistribute it together with the GUI, include its license and sources.

## Credits

Originally part of [Lenny4250gh/AnyPS5-GUI](https://github.com/Lenny4250gh/AnyPS5-GUI).

[AnyPS5](https://github.com/boykopovar/AnyPS5) by its authors, which does the real work (relinker, system libraries, `unwrap_self.py`). This GUI only makes it easier to use.

## Disclaimer

This project is intended for interoperability, research, preservation and compatibility purposes. It does not include, distribute or require copyrighted software, firmware, cryptographic keys or proprietary libraries. Users are responsible for ensuring that any binaries used with it are obtained and used in accordance with applicable laws and their respective license terms. Provided "as is", without warranty.