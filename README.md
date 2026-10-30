<p align="center">
  <img src="Icon/icon.png" alt="AnyPS5 GUI logo" width="128">
</p>

<h1 align="center">AnyPS5 GUI</h1>

<p align="center">
  A simple graphical interface for <a href="https://github.com/boykopovar/AnyPS5">AnyPS5</a>.<br>
  Convert and launch your own PS5 game dumps on PC without using the command line.
</p>

<p align="center">
  <a href="https://github.com/Lenny4250gh/AnyPS5-GUI/releases/latest"><img src="https://img.shields.io/github/v/release/Lenny4250gh/AnyPS5-GUI?style=for-the-badge&label=Latest%20version" alt="Latest release"></a>
  <a href="https://github.com/Lenny4250gh/AnyPS5-GUI/releases"><img src="https://img.shields.io/github/downloads/Lenny4250gh/AnyPS5-GUI/total?style=for-the-badge&label=Downloads" alt="Downloads"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--2.0--only-blue?style=for-the-badge" alt="GPL-2.0-only license"></a>
</p>

<p align="center">
  <a href="#download">Download</a> ·
  <a href="#getting-started">Getting started</a> ·
  <a href="#what-does-it-do">What it does</a> ·
  <a href="#help">Help</a> ·
  <a href="#advanced">Advanced</a>
</p>

---

## Download

<p align="center">
  <a href="https://github.com/Lenny4250gh/AnyPS5-GUI/releases/latest">
    <img src="https://img.shields.io/badge/⬇%20DOWNLOAD-LATEST%20VERSION-2ea44f?style=for-the-badge" alt="Download the latest version">
  </a>
</p>

<p align="center">
  Open the latest release and download the file for your operating system.
</p>

| Your system | Download | How to open it |
|:--|:--|:--|
| **Windows** | `AnyPS5_GUI-windows.exe` | Double-click the file. |
| **Linux** | `AnyPS5_GUI-linux` | Run `chmod +x AnyPS5_GUI-linux`, then open it. |
| **macOS** | `AnyPS5_GUI-macos.zip` | Extract the ZIP and open the app. If blocked, right-click it and choose **Open**. |

[See all versions and release notes](https://github.com/Lenny4250gh/AnyPS5-GUI/releases).

> **Note:** You do not need to install Python or compile the app when using a release build.

---

## Getting started

The usual process is just: **select your game → convert it → launch it**.

### 1. Select your game

Open AnyPS5 GUI and choose **Convert a game**.

Select the folder containing your own game dump. You can also select a `.zip` archive; `.rar` archives require 7-Zip.

### 2. Convert it

Before starting, check the **Requirements** panel. It tells you if something is missing and can download the required AnyPS5 tools.

Then:

- Press **Unwrap** only if the game contains decrypted `SELF` / `fSELF` files that need to be converted to `ELF`.
- Press **Relink** to create the PC package for your selected target system.

The converted game is saved in the output folder. By default, this is a folder named `Converted games` next to the app. You can change it in Settings.

### 3. Launch the game

Open **Play a game**, select the converted game package and press **Launch**.

You can also create a shortcut to launch that package again later.

---

## What does it do?

AnyPS5 GUI is the graphical interface for [AnyPS5](https://github.com/boykopovar/AnyPS5). It helps you use the AnyPS5 tools without typing commands.

It can:

- **Unwrap** supported decrypted `SELF` / `fSELF` files.
- **Relink** a game dump for Windows, Linux or macOS.
- **Launch** a converted game and save its runtime settings.
- **Configure controls**, including keyboard, mouse and controller mappings.
- **Check requirements** and help obtain the tools needed for conversion.

The GUI does not perform the conversion by itself: it runs the AnyPS5 tools and shows their progress and logs.

## Main features

<details>
<summary><strong>Game conversion</strong></summary>

- Select a game folder or a `.zip` / `.rar` archive (`.rar` requires 7-Zip).
- Check CPU, GPU and other requirements before conversion.
- Download or build the required AnyPS5 tools from the selected source.
- Unwrap supported decrypted `SELF` / `fSELF` files. Original files are backed up and can be restored.
- Relink the game for Windows, Linux or macOS.
- Choose where converted games are saved.
- Configure relinker options, patches and fixed-base options where supported.
- View the operation log and export a report if something goes wrong.

</details>

<details>
<summary><strong>Launching and runtime settings</strong></summary>

The **Play a game** page lets you select a converted package and launch it.

Runtime settings include options such as:

- Graphics and presentation settings.
- Language and fonts.
- Performance overlay and diagnostics.
- Screenshot folder and shader cache.
- Controller vibration and speaker volume.
- Host game mode and other environment variables.

The app only sets optional runtime variables when you change them from their defaults. You can also create a shortcut with the selected launch settings.

</details>

<details>
<summary><strong>Controls and input</strong></summary>

- Configure controller buttons by clicking a control and pressing a keyboard key, mouse button or wheel direction.
- Set alternate inputs and stick deadzones.
- Configure mouse movement as a stick, including sensitivity, curve, smoothing and decay.
- Import and export input settings.
- Edit `anyps5-input.ini` directly in the text editor, which checks whether the file is valid.

</details>

<details>
<summary><strong>Game data, screenshots and diagnostics</strong></summary>

- Open the selected game's `_sd`, `_sd_mem` and `_trophy` folders.
- Back up save-data folders as ZIP files.
- Reset trophy data after a confirmation prompt.
- View screenshots in the app's gallery. The default screenshot folder is `anyps5-screenshots/` next to the game executable; it is created when the first screenshot is taken.
- View and clear the AnyPS5 shader cache.
- Export a diagnostic report containing system information, runtime variables and logs.

**Be careful:** resetting trophy data is destructive. Check the selected game package path before managing saves or trophies.

</details>

<details>
<summary><strong>Appearance and Discord</strong></summary>

- Choose the app's accent color and background.
- Enable, disable or adjust the volume of the app's sound effects.
- Discord Rich Presence shows when the launcher is open and the current conversion phase. It does not show the game you are playing. You can disable it in Appearance settings.

</details>

---

## Help

### The app does not open

- **Windows:** the app needs Microsoft Edge WebView2 Runtime. It is already included on most up-to-date Windows 10/11 installations.
- **macOS:** the app is not signed. Try right-clicking it and selecting **Open**.
- **Linux:** install the system libraries listed in [Requirements](#requirements).

### Windows or antivirus shows a warning

The release is not code-signed, and PyInstaller applications can sometimes trigger SmartScreen or antivirus warnings. Only run software you trust. You can inspect the source code or build the app yourself using the instructions below.

### Conversion or launch fails

1. Open the app's log and read the first reported error.
2. Check the **Requirements** panel.
3. Use **Export report…** or **Copy for bug** to collect diagnostic information.
4. If the issue is with this GUI, [open an issue](https://github.com/Lenny4250gh/AnyPS5-GUI/issues) and include your operating system, app version and report.

If the game itself fails because of an AnyPS5 compatibility issue, ask in the [official AnyPS5 Discord](https://discord.gg/BHFztBPUe) or the [AnyPS5 game bring-up discussions](https://github.com/boykopovar/AnyPS5/discussions/categories/game-bring-up).

### Where can I find the full changelog?

The [GitHub Releases page](https://github.com/Lenny4250gh/AnyPS5-GUI/releases) contains the notes for each version. Long version-by-version change lists are kept there so this page stays easy to read.

---

## Advanced

The sections below are for users who want to change the tool source, build the app themselves or inspect technical details. You can leave them closed if you only want to download and use the program.

<details>
<summary><strong>Installation details and requirements</strong></summary>

### First launch and app data

On first launch, the app creates an `AnyPS5_GUI` data folder in your user profile. It stores downloaded tools, settings, backups and other app data.

| System | Default folder |
|:--|:--|
| Windows | `%APPDATA%\AnyPS5_GUI` |
| macOS | `~/Library/Application Support/AnyPS5_GUI` |
| Linux | `~/.local/share/AnyPS5_GUI` |

You can move this folder from **Settings → Storage**.

To keep app data next to the program instead, create an empty file named `portable` next to the executable. In portable mode, the app uses a `Utilities/` folder.

### Requirements

- **Windows:** Microsoft Edge WebView2 Runtime.
- **macOS:** the app is unsigned; the first launch may need right-click → **Open**. You can also run `xattr -cr AnyPS5_GUI.app`.
- **Linux:** on Debian/Ubuntu, install:
  ```bash
  sudo apt install libegl1 libgl1 libxkbcommon-x11-0 libxcb-cursor0 libdbus-1-3
  ```

The app also checks the CPU and GPU requirements needed by AnyPS5 before conversion.

### Verify a downloaded file (optional)

Compare the SHA-256 checksum with the value shown on the GitHub release page:

- Windows (PowerShell): `Get-FileHash .\AnyPS5_GUI-windows.exe -Algorithm SHA256`
- Linux: `sha256sum AnyPS5_GUI-linux`
- macOS: `shasum -a 256 AnyPS5_GUI-macos.zip`

</details>

<details>
<summary><strong>Using a different AnyPS5 repository</strong></summary>

By default, the GUI uses the official [AnyPS5 repository](https://github.com/boykopovar/AnyPS5). You can change **Repository URL** in **Advanced: where the tools come from** to use a fork or another compatible repository.

There are two ways to get tools from a repository:

| Source | What it does |
|:--|:--|
| **Release from GitHub repo** | Downloads the tools attached to a GitHub release or tag. |
| **Build from GitHub source** | Clones the repository and builds the relinker with CMake. |

Each repository has its own folder, so files from different forks are kept separate.

### Release repository format

A compatible release should include:

- `relinker-*` — executable for the operating system.
- `prx-*.zip` — archive containing `libs/` or `libs-<system>/`.
- `USAGE.md`, `INPUT_MAPPING.md` or `release-notes.md` — optional documentation.

The repository should also contain `tools/unwrap_self.py` at the selected tag or on its default branch.

Downloads are checked using SHA-256 when GitHub provides a checksum.

### Build repository format

The repository should have:

- A root `CMakeLists.txt`.
- A CMake target that builds the relinker (normally named `relinker`).
- The expected AnyPS5 folders, including `core/`, `3rdparty/`, `cmake/` and `tools/`.
- `tools/unwrap_self.py`.

Building requires `git`, `cmake` and a C/C++ compiler. On Debian/Ubuntu:

```bash
sudo apt install git cmake build-essential
```

On Windows, the project uses the MinGW-w64 GCC toolchain supported by the AnyPS5 build. The GUI can install the required build tools.

Only use repositories you trust: the GUI downloads and runs code from the selected repository on your computer.

</details>

<details>
<summary><strong>Build AnyPS5 GUI from source</strong></summary>

Requires Python 3.9+ (the CI uses Python 3.11).

```bash
git clone https://github.com/Lenny4250gh/AnyPS5-GUI.git
cd AnyPS5-GUI
pip install -r requirements.txt
python AnyPS5_GUI.py
```

To build an executable:

```bash
pip install pyinstaller pillow
python build.py
```

Or choose a target without the prompt:

```bash
python build.py 1  # Windows
python build.py 2  # macOS
python build.py 3  # Linux
```

PyInstaller cannot cross-compile. Build on the operating system you want to target, or use [GitHub Actions](https://github.com/Lenny4250gh/AnyPS5-GUI/actions).

</details>

---

## Support

- **AnyPS5 GUI issue:** [open an issue](https://github.com/Lenny4250gh/AnyPS5-GUI/issues).
- **AnyPS5 game compatibility issue:** use the [AnyPS5 Discord](https://discord.gg/BHFztBPUe) or [game bring-up discussions](https://github.com/boykopovar/AnyPS5/discussions/categories/game-bring-up).

When reporting a problem, include your operating system, GUI version and diagnostic report if possible.

## Credits

[AnyPS5](https://github.com/boykopovar/AnyPS5) by its authors does the underlying work (relinker, system libraries and `unwrap_self.py`). AnyPS5 GUI provides the graphical interface.

## License and disclaimer

This project is licensed under **GPL-2.0-only**; see [LICENSE](LICENSE).

This project is for interoperability, research, preservation and compatibility purposes. It does not include games, firmware, cryptographic keys or proprietary libraries. You are responsible for obtaining and using software in accordance with applicable laws and license terms. The project is provided "as is", without warranty.

AnyPS5 GUI is unofficial and is not affiliated with Sony or the AnyPS5 project.
