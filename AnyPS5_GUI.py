"""AnyPS5 GUI

Originally part of https://github.com/Lenny4250gh/AnyPS5-GUI (GPL-2.0-only).

Graphical interface to use AnyPS5 without touching the command line:
  - Unwrap   : already-decrypted SELF/fSELF -> ELF, using the OFFICIAL unwrap_self.py
               (external file in the app data folder, not embedded here)
  - Relink   : builds the "<GAME>-<system>" package with app0 + libs + executable
  - Options  : all relinker options (Windows / Linux / macOS, --to-intel,
               --to-rosetta, --to-avx, --windows-gui, --windows-fixed-base, --linux-fixed-base,
               --macos-fixed-base, unused-filter,
               --registry, --rpath, --module-dir, --executable, --trace-imports,
               --sce-module-path, --autorun, --trap-sdk-syscall-stubs and the deprecated
               debug flags). Every option shows the systems it applies to (Windows / Linux /
               macOS / all) and is disabled when it does not fit the selected target.
  - Runtime  : language, GPU, present mode, system fonts, store-at-flip, trace (calls, frames, seconds,
               summaries), SDL video driver, thread priority, pipeline stats, GPU checkpoints;
               creates a shortcut named after the game (.lnk on Windows) in the program folder that starts the
               executable with the right variables
  - Input    : anyps5-input.ini editor with validity check
  - Audit    : import_audit.py (if present in <data>/tools/) after the relink

What it does NOT do: no decryption, no keys, no game files included.
unwrap_self.py refuses containers that look encrypted.
Use it only on software you legitimately own (your own dumps or homebrew).
Not affiliated with Sony or AnyPS5. Provided "as is", without warranty.

License: this program no longer contains any code copied from unwrap_self.py:
it loads it from the external file. unwrap_self.py remains part of the AnyPS5
project (GPL v2 only); if you distribute it together with the GUI, include
its license and its sources.
(I am not a lawyer: check the project's license before distributing.)

Layout. Everything the program needs lives in ONE folder, the app data folder <data>
(created automatically if missing): AppData/Roaming/AnyPS5_GUI on Windows,
~/Library/Application Support/AnyPS5_GUI on macOS, ~/.local/share/AnyPS5_GUI on Linux.
The packaged app can move it from Settings → Storage; from source or in portable mode
it is a folder next to the script.

  <data>/
    unwrap_self.py      the official AnyPS5 unwrapper (NOT needed when "Build from GitHub source"
                        is selected: tools/unwrap_self.py of the cloned repo is used instead)
    relinker-*.exe      the relinker (the last one in alphabetical order is used;
                        on Linux/macOS also extension-less "relinker-*")
    prx-*.zip           the AnyPS5 libs downloaded from GitHub, NOT extracted: it must
                        contain a "libs" folder (or libs-windows, libs-linux, libs-macos
                        if different builds are needed per system); read directly by the
                        program and extracted into each generated package
    source/<repo>/      (created by the program) clone + CMake build of the relinker, used when
                        "Build from GitHub source" is selected (needs git, cmake and a C/C++ compiler)
    release/<repo>/<tag>/  (created by the program) files downloaded from the GitHub release, used when
                        "Release from GitHub repo" is selected: relinker-vX.Y.Z[.exe], prx-<system>-vX.Y.Z.zip,
                        USAGE.md, INPUT_MAPPING.md, release-notes.md and tools/unwrap_self.py of the tag
    anyps5-fonts/       (optional) system fonts, copied into the package
    anyps5-input.ini    (optional) input mapping, copied into the package
    tools/import_audit.py  (optional) import audit
    config.ini          settings (created by the program)
    backups/<GAME>/     backups of the originals for Restore mode (created by the program)

Generated game packages ("<GAME>-<system>") go next to the script, unless you
choose another output folder in the program.

Optional: `pip install tkinterdnd2` enables folder drag & drop.
Game archives: .zip and .rar (the prx-*.zip libs: only .zip). .zip works out of the box; .rar needs 7-Zip
(7z/7zz in PATH or in the app data folder) or `pip install rarfile` (+ UnRAR/WinRAR).

Source layout. This file only starts the app. The code is in the anyps5_gui/ folder next to it, one
module per topic (the list is in anyps5_gui/__init__.py). build.py packs everything into the executable.
"""
import os
import subprocess
import sys
import webview

from anyps5_gui.settings import (
    APP_VERSION, DATA_DIR, hidden_error, INTERIM_DATA_DIR, LEGACY_DATA_DIR, SCRIPT_DIR
)
from anyps5_gui.basic_ops import migrate_legacy_data
from anyps5_gui.api import Api


HERE = SCRIPT_DIR


def hide_own_console():
  """Windows: hides (SW_HIDE, not minimised) the console window that opens with the app when the
  script is started by double-click (also through the 'py' launcher, which shares the console).
  If the console belongs to a shell (cmd, PowerShell, Windows Terminal...) it is left alone."""
  if os.name != "nt":
    return
  try:
    import ctypes
    kernel32 = ctypes.windll.kernel32
    kernel32.GetConsoleWindow.restype = ctypes.c_void_p
    hwnd = kernel32.GetConsoleWindow()
    if not hwnd:
      return  # no console (pythonw, PyInstaller --noconsole)
    pids = (ctypes.c_uint * 32)()
    count = kernel32.GetConsoleProcessList(pids, 32)
    if count == 0 or count > 32:
      return
    shells = {"cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe", "wt.exe",
              "conhost.exe", "bash.exe", "wsl.exe", "mintty.exe", "code.exe"}
    kernel32.OpenProcess.restype = ctypes.c_void_p
    for pid in list(pids)[:count]:
      if pid == os.getpid():
        continue
      handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
      if not handle:
        continue
      try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_uint(1024)
        if kernel32.QueryFullProcessImageNameW(ctypes.c_void_p(handle), 0, buf, ctypes.byref(size)):
          if os.path.basename(buf.value).lower() in shells:
            return  # started from a terminal: do not hide it
      finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
    if not getattr(sys, "frozen", False) and not os.environ.get("ANYPS5_NO_RELAUNCH"):
      # restart without any console (pythonw.exe) and close this one: the window is gone for good
      pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
      if os.path.isfile(pyw):
        try:
          subprocess.Popen([pyw, os.path.abspath(sys.argv[0])] + sys.argv[1:],
                           env=dict(os.environ, ANYPS5_NO_RELAUNCH="1"), close_fds=True,
                           creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | NEW_PROCESS_GROUP
          os._exit(0)
        except OSError:
          pass
    ctypes.windll.user32.ShowWindow(ctypes.c_void_p(hwnd), 0)  # SW_HIDE (fallback)
  except Exception as _exc:
    hidden_error("hide_own_console", _exc)
    pass


def find_ui():
  for p in (os.path.join(HERE, "ui", "index.html"), os.path.join(HERE, "index.html")):
    if os.path.isfile(p):
      return p
  raise SystemExit("index.html not found. Looked in:\n  " + os.path.join(HERE, "ui", "index.html") +
                   "\n  " + os.path.join(HERE, "index.html"))


def load_embedded_ui():
  """Returns the HTML embedded by build.py (module _ui_data), or None when running from source."""
  try:
    import base64
    import zlib
    import _ui_data  # generated by build.py
    return zlib.decompress(base64.b64decode(_ui_data.DATA)).decode("utf-8")
  except Exception as _exc:
    hidden_error("load_embedded_ui", _exc)
    return None


def resource_path(name):
  return os.path.join(getattr(sys, "_MEIPASS", HERE), name)


def report_crash():
  """With --noconsole errors are invisible: write them to crash.log and show a message."""
  import traceback
  text = traceback.format_exc()
  path = os.path.join(SCRIPT_DIR, "crash.log")
  try:
    with open(path, "w", encoding="utf-8") as fh:
      fh.write(text)
  except OSError:
    pass
  if os.name == "nt":
    try:
      import ctypes
      ctypes.windll.user32.MessageBoxW(0, text[-1500:] + "\n\n(saved in " + path + ")", "AnyPS5 GUI - error", 0x10)
    except Exception:
      pass
  else:
    sys.stderr = sys.stderr or sys.__stderr__
    try:
      print(text, file=sys.stderr)
    except Exception:
      pass


def main():
  hide_own_console()
  if DATA_DIR != LEGACY_DATA_DIR:  # before Api(): it reads config.ini
    migrate_legacy_data()  # <program folder>/<LEGACY_DIR_NAME> (first versions)
    migrate_legacy_data(INTERIM_DATA_DIR)  # <user data>/<LEGACY_DIR_NAME> (previous build)
  _html = load_embedded_ui()
  _src = {"html": _html} if _html else {"url": find_ui()}
  api = Api()
  window = webview.create_window("AnyPS5 GUI | v" + APP_VERSION, js_api=api, width=1280, height=760,
                                 min_size=(900, 560), background_color="#111312", **_src)
  window.events.closing += api.on_closing  # asks for confirmation if an operation is running

  def _clear_update_marker(*_):
    m = os.environ.pop("ANYPS5_UPDATE_MARKER", "")  # set only after a self-update: closes its splash
    if m:
      try:
        os.remove(m)
      except OSError:
        pass
  window.events.loaded += _clear_update_marker
  # Windows (WinForms) needs a real .ico here: a PNG raises "picture must be an image usable as Icon"
  _icon = resource_path("icon.ico" if os.name == "nt" else "icon.png")
  try:
    webview.start(icon=_icon if os.path.isfile(_icon) else None)
  except TypeError:  # old pywebview without the "icon" parameter
    webview.start()


if __name__ == "__main__":
  try:
    main()
  except SystemExit:
    raise
  except BaseException:
    report_crash()
    sys.exit(1)
