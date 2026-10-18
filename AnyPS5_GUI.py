"""AnyPS5 GUI

Originally part of https://github.com/Lenny4250gh/AnyPS5-GUI (GPL-2.0-only).

Graphical interface to use AnyPS5 without touching the command line:
  - Unwrap   : already-decrypted SELF/fSELF -> ELF, using the OFFICIAL unwrap_self.py
               (external file in the Utilities folder, not embedded here)
  - Relink   : builds the "<GAME>-<system>" package with app0 + libs + executable
  - Options  : all relinker options (Windows / Linux / macOS, --to-intel,
               --to-rosetta, --windows-gui, --windows-fixed-base, unused-filter,
               --registry, --rpath, --module-dir, --executable, --trace-imports,
               --sce-module-path, --autorun, --trap-sdk-syscall-stubs and the deprecated
               debug flags). Every option shows the systems it applies to (Windows / Linux /
               macOS / all) and is disabled when it does not fit the selected target.
  - Runtime  : language, GPU, present mode, system fonts, store-at-flip, trace (calls, frames, seconds,
               summaries), SDL video driver, thread priority, pipeline stats, GPU checkpoints;
               creates a shortcut named after the game (.lnk on Windows) in the program folder that starts the
               executable with the right variables
  - Input    : anyps5-input.ini editor with validity check
  - Audit    : import_audit.py (if present in Utilities/tools/) after the relink

What it does NOT do: no decryption, no keys, no game files included.
unwrap_self.py refuses containers that look encrypted.
Use it only on software you legitimately own (your own dumps or homebrew).
Not affiliated with Sony or AnyPS5. Provided "as is", without warranty.

License: this program no longer contains any code copied from unwrap_self.py:
it loads it from the external file. unwrap_self.py remains part of the AnyPS5
project (GPL v2 only); if you distribute it together with the GUI, include
its license and its sources.
(I am not a lawyer: check the project's license before distributing.)

Layout. Next to this script there is ONE folder, "Utilities", that holds
everything the program needs (it is created automatically if missing):

  AnyPS5_GUI.py
  Utilities/
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
(7z/7zz in PATH or in Utilities/) or `pip install rarfile` (+ UnRAR/WinRAR).
"""
import configparser
import hashlib
import importlib.util
import os
import queue
import re
import shlex
import shutil
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
import time
import tarfile
import tempfile
import zipfile

# imported here because unwrap_self.py uses them: they are also needed when the
# GUI is bundled with PyInstaller
import argparse  # noqa: F401
import collections  # noqa: F401
import math  # noqa: F401
import struct
import pathlib  # noqa: F401


# ─────────────────────────────────────────────────────────────
#  Settings
# ─────────────────────────────────────────────────────────────
if getattr(sys, "frozen", False):  # PyInstaller executable
  SCRIPT_DIR = os.path.dirname(sys.executable)
else:
  SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Everything the program needs lives in this folder
UTILITIES_DIR_NAME = "Utilities"
LEGACY_UTILITIES_DIR = os.path.join(SCRIPT_DIR, UTILITIES_DIR_NAME)  # old location: next to the program
PORTABLE_MARK = "portable"  # empty file next to the program: keep everything next to it (old behaviour)


def _user_data_dir():
  """Per-user data folder (never inside an .app bundle or a read-only install folder)."""
  if os.name == "nt":
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
  elif sys.platform == "darwin":
    base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
  else:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
  return os.path.join(base, "AnyPS5_GUI")


LOCATION_FILE = os.path.join(_user_data_dir(), "location.txt")  # tiny pointer: where Utilities/ was moved by the user
DEFAULT_UTILITIES_DIR = _user_data_dir()  # the app's files go directly in here (location.txt is the only extra)
INTERIM_UTILITIES_DIR = os.path.join(_user_data_dir(), UTILITIES_DIR_NAME)  # layout of an earlier build: migrated
# the location can be changed only in the packaged app (from source / portable mode: next to the script)
STORAGE_EDITABLE = bool(getattr(sys, "frozen", False)) and not os.path.exists(os.path.join(SCRIPT_DIR, PORTABLE_MARK))
STORAGE_PROBLEM = ""  # set when the chosen folder is unavailable and the default one is used instead


def _read_location():
  try:
    with open(LOCATION_FILE, encoding="utf-8") as fh:
      text = fh.read().strip()
  except OSError:
    return ""
  return text if text and os.path.isabs(text) else ""


# Executable: Utilities/ lives in the user data folder, or in the folder chosen in Settings → Storage
# (the old Utilities/ next to the program is moved there at startup, see migrate_legacy_utilities).
# From source or in portable mode: next to the script.
if STORAGE_EDITABLE:
  UTILITIES_DIR = DEFAULT_UTILITIES_DIR
  _custom = _read_location()
  if _custom:
    try:
      os.makedirs(_custom, exist_ok=True)
      UTILITIES_DIR = _custom
    except OSError as _e:
      STORAGE_PROBLEM = "The folder you chose (%s) is not available (%s): using the default one." % (_custom, _e)
else:
  UTILITIES_DIR = LEGACY_UTILITIES_DIR
try:
  os.makedirs(UTILITIES_DIR, exist_ok=True)
except OSError:
  pass

RELINKER_PREFIX = "relinker-"
LIBS_DIR_NAME = "libs"
LIBS_ZIP_PREFIX = "prx-"  # the zip with the libs must be named prx-*.zip
FONTS_DIR_NAME = "anyps5-fonts"
EXECUTABLE_NAME = "eboot.bin"
BACKUP_DIR_NAME = "backups"
UNWRAP_SCRIPT_NAME = "unwrap_self.py"
INPUT_INI_NAME = "anyps5-input.ini"
AUDIT_SCRIPT = os.path.join("tools", "import_audit.py")  # relative to Utilities/
CONFIG_FILE = os.path.join(UTILITIES_DIR, "config.ini")
LEGACY_CONFIG_FILE = os.path.join(UTILITIES_DIR, "UseRelinker.ini")
INPUT_INI_FILE = os.path.join(UTILITIES_DIR, INPUT_INI_NAME)

# relinker built from a GitHub repository (alternative to the prebuilt relinker-*.exe)
DEFAULT_REPO_URL = "https://github.com/boykopovar/AnyPS5"
SOURCE_DIR_NAME = "source"            # Utilities/source/<repo>-<hash>/{repo,build}
SOURCE_INFO_NAME = "build_info.ini"
SOURCE_CLONED_MARK = ".cloned"        # written after a complete clone
RELINKER_TARGET_GUESS = "relinker"    # CMake target tried first when none is configured
RELINKER_SOURCES = ("prebuilt", "github", "release")
RELEASE_DIR_NAME = "release"          # Utilities/release/<repo>/<tag>/ (files named as in the release)
RELEASE_CURRENT_NAME = "current.ini"  # tag in use for that repository
RELEASE_INFO_NAME = "release.json"    # assets of the release (name, url, size, sha256): offline reuse
RELEASE_DOCS = ("USAGE.md", "INPUT_MAPPING.md", "release-notes.md")  # optional, small
RELEASE_LIBS_FOR = {"windows": "windows", "linux": "linux", "macos": "linux"}  # prx-<this>-*.zip per target
NOT_BINARY_EXT = (".md", ".zip", ".gz", ".txt", ".ini", ".py", ".json", ".sha256", ".part", ".log")

# module folder names accepted by the relinker (USAGE.md)
MODULE_DIRS = ("sce_module", "sce_modules", "prx")
# only used to recognise a SELF (the real unwrap is done by unwrap_self.py)
SELF_MAGICS = (0x1D3D154F, 0xEEF51454)

# True -> executable in "<GAME>-<system>" (next to app0); False -> inside app0
OUTPUT_NEXT_TO_APP0 = True

# threads used to copy the game into app0 (plain copy only; the move mode stays serial)
COPY_WORKERS = 32

TARGETS = {
  "windows": {"label": "Windows", "exe": "eboot.exe", "suffix": "windows"},
  "linux": {"label": "Linux", "exe": "eboot.elf", "suffix": "linux"},
  "macos": {"label": "macOS (x86-64, Rosetta on Apple silicon)", "exe": "eboot", "suffix": "macos"},
}

LANGUAGES = [
  (0, "Japanese"), (1, "English (US)"), (2, "French"), (3, "Spanish"),
  (4, "German"), (5, "Italian"), (6, "Dutch"), (7, "Portuguese (PT)"),
  (8, "Russian"), (9, "Korean"), (10, "Chinese (Traditional)"), (11, "Chinese (Simplified)"),
  (12, "Finnish"), (13, "Swedish"), (14, "Danish"), (15, "Norwegian"),
  (16, "Polish"), (17, "Portuguese (BR)"), (18, "English (UK)"), (19, "Turkish"),
  (20, "Spanish (Latin America)"), (21, "Arabic"), (22, "French (Canada)"),
  (23, "Czech"), (24, "Hungarian"), (25, "Greek"), (26, "Romanian"), (27, "Thai"),
  (28, "Vietnamese"), (29, "Indonesian"), (30, "Ukrainian"),
]
PRESENT_MODES = ["", "fifo_latest_ready", "mailbox", "immediate"]

# default option values (all saved in config.ini as strings)
DEFAULTS = {
  "settings": {"output_dir": "", "intel": "no", "auto_unwrap": "yes", "mode": "relink", "notify": "yes",
               "keep_game": "yes", "window_size": "", "open_sections": ""},
  "relinker": {
    "target": "windows", "rosetta": "no", "gui": "no", "diagnostics": "no",
    "fixed_base": "", "unused_filter": "0", "registry": "yes", "rpath": "",
    "trace_imports": "no", "module_dirs": "", "executables": "",
    "unwrap_modules": "yes", "import_audit": "no", "make_launcher": "yes",
    "names_file": "",
    "auto_module_dirs": "yes",
    "relinker_source": "release", "repo_url": DEFAULT_REPO_URL, "repo_ref": "",
    "release_tag": "", "release_update": "yes",
    "repo_target": "", "repo_update": "yes", "repo_cmake_args": "",
    "repo_libs_from_repo": "yes",
    "sce_module_path": "", "autorun": "no", "trap_syscall_stubs": "no",
    "lazy_binding": "no", "skip_sce_module": "no", "exclude_sce_modules": "",
    "skip_syscall_check": "no",
  },
  "runtime": {
    "language": "", "gpu": "", "present_mode": "", "fonts_dir": "",
    "store_at_flip": "no", "trace_calls": "", "disable_dozen": "no",
    "copy_input_ini": "yes",
    "extra_env": "", "pad_input_file": "",
    "sdl_videodriver": "", "thread_priority": "", "thread_nice": "", "thread_rt": "",
    "trace_frames": "", "trace_seconds": "", "trace_summary_every": "", "trace_events": "",
    "trace_dir": "", "pipeline_stats": "no", "gpu_checkpoints": "no",
  },
}


class Cancelled(Exception):
  pass


# ─────────────────────────────────────────────────────────────
#  External unwrapper (official unwrap_self.py)
# ─────────────────────────────────────────────────────────────
_unwrapper = None
_unwrapper_path = None
_active_rl = None  # relinker options in use (set by the logic/UI): decides where unwrap_self.py comes from
REPO_UNWRAP_REL = ("tools", UNWRAP_SCRIPT_NAME)  # position in the official repo: tools/unwrap_self.py


def set_active_relinker_cfg(rl):
  global _active_rl
  _active_rl = rl


def repo_unwrapper_path(rl=None):
  """tools/unwrap_self.py of the cloned repository when "Build from GitHub source" is active
  and the file exists there, otherwise None."""
  rl = rl if rl is not None else _active_rl
  if not rl or rl.get("relinker_source") != "github":
    return None
  paths = source_paths(rl)
  if not paths:
    return None
  path = os.path.join(paths["repo"], *REPO_UNWRAP_REL)
  return path if os.path.isfile(path) else None


def release_unwrapper_path(rl=None):
  """unwrap_self.py downloaded with the release (Release option active), else None."""
  d = release_dir(rl)
  path = os.path.join(d, UNWRAP_SCRIPT_NAME) if d else None
  return path if path and os.path.isfile(path) else None


def unwrapper_path():
  """unwrap_self.py to use: the one of the release (Release option) or of the cloned repo (GitHub
  option), else Utilities/."""
  path = release_unwrapper_path() or repo_unwrapper_path()
  if path:
    return path
  path = os.path.join(UTILITIES_DIR, UNWRAP_SCRIPT_NAME)
  return path if os.path.isfile(path) else None


def github_mode():
  return bool(_active_rl) and _active_rl.get("relinker_source") == "github"


def load_unwrapper():
  """Load unwrap_self.py (from the cloned repo if the GitHub option is active, else from the
  Utilities folder). Returns the module or None."""
  global _unwrapper, _unwrapper_path
  path = unwrapper_path()
  if path is None:
    return None
  if _unwrapper is not None and _unwrapper_path == path:
    return _unwrapper
  spec = importlib.util.spec_from_file_location("unwrap_self_external", path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  if not hasattr(module, "unwrap") or not hasattr(module, "Refused"):
    raise RuntimeError("%s does not expose unwrap()/Refused: incompatible version" % UNWRAP_SCRIPT_NAME)
  _tolerate_empty_orphans(module)
  _unwrapper = module
  _unwrapper_path = path
  return module


def _tolerate_empty_orphans(module):
  """The official unwrap_self.py rejects EMPTY non-LOAD segments (filesz 0) whose vaddr lies
  outside every PT_LOAD, even when there is nothing to copy. Here they are let through without
  touching the official file: the vaddr is zeroed only during place_segments and then restored."""
  original = getattr(module, "place_segments", None)
  if original is None or getattr(original, "_tolerant", False):
    return
  PT_LOAD = getattr(module, "PT_LOAD", 1)

  def tolerant(data, headers, records):
    loads = [h for h in headers if h["type"] == PT_LOAD]
    saved = []
    for h in headers:
      if h["type"] == PT_LOAD or h["vaddr"] == 0 or h["size"] != 0:
        continue
      inside = any(l["memory"] and l["vaddr"] <= h["vaddr"] < l["vaddr"] + l["memory"] for l in loads)
      if not inside:
        saved.append((h, h["vaddr"]))
        h["vaddr"] = 0
        h["source"] = 0  # empty segment: no data to copy, avoids the offset check
    try:
      return original(data, headers, records)
    finally:
      for h, vaddr in saved:
        h["vaddr"] = vaddr

  tolerant._tolerant = True
  module.place_segments = tolerant


def unwrapper_status():
  return unwrapper_path()


# ─────────────────────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────────────────────
def load_config():
  cfg = configparser.ConfigParser(interpolation=None)
  source = CONFIG_FILE
  if not os.path.isfile(CONFIG_FILE) and os.path.isfile(LEGACY_CONFIG_FILE):
    source = LEGACY_CONFIG_FILE  # migrate settings from the old name
  try:
    cfg.read(source, encoding="utf-8")
  except Exception:
    pass
  values = {}
  for section, defaults in DEFAULTS.items():
    values[section] = {}
    for key, default in defaults.items():
      values[section][key] = cfg.get(section, key, fallback=default).strip().strip('"')
  s = values["settings"]
  if s["output_dir"].lower() in ("d", "default"):
    s["output_dir"] = ""
  if s["mode"] not in ("unwrap", "relink"):
    s["mode"] = "relink"
  if values["relinker"]["target"] not in TARGETS:
    values["relinker"]["target"] = "windows"
  if values["relinker"]["relinker_source"] not in RELINKER_SOURCES:
    values["relinker"]["relinker_source"] = "release"
  if not values["relinker"]["repo_url"]:
    values["relinker"]["repo_url"] = DEFAULT_REPO_URL
  return values


def save_config(values):
  cfg = configparser.ConfigParser(interpolation=None)
  for section, items in values.items():
    cfg[section] = {k: (v if v != "" or section != "settings" else "default")
                    for k, v in items.items()}
  cfg["settings"]["output_dir"] = values["settings"]["output_dir"] or "default"
  try:
    os.makedirs(UTILITIES_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
      f.write("# settings: output_dir (default = script folder), intel yes/no,\n"
              "#   auto_unwrap yes/no, mode unwrap/relink, window_size WIDTHxHEIGHT (empty = default)\n"
              "# relinker: relinker options (see AnyPS5 USAGE.md); relinker_source prebuilt/github/release,\n"
              "#   release_tag / release_update for the release source (empty tag = latest release),\n"
              "#   repo_url / repo_ref / repo_target / repo_update for the GitHub source build\n"
              "# runtime: environment variables used to launch the game\n")
      cfg.write(f)
  except Exception:
    pass


def yes(value):
  return str(value).strip().lower() in ("yes", "true", "1")


def yn(flag):
  return "yes" if flag else "no"


def host_target():
  """Target system that matches the computer the GUI runs on."""
  if os.name == "nt":
    return "windows"
  if sys.platform == "darwin":
    return "macos"
  return "linux"


# ─────────────────────────────────────────────────────────────
#  Logic (independent of the UI: communicates through the log callback)
#  log(kind, msg) with kind in: info ok warn err step detail progress
# ─────────────────────────────────────────────────────────────
def find_relinker():
  base = release_dir() or UTILITIES_DIR
  try:
    names = sorted(os.listdir(base))
  except OSError:
    return None
  matches = []
  for f in names:
    low = f.lower()
    path = os.path.join(base, f)
    if not low.startswith(RELINKER_PREFIX) or not os.path.isfile(path):
      continue
    # Windows: relinker-*.exe · Linux/macOS: relinker-vX.Y.Z (no extension; the dots of the
    # version are not an extension)
    if os.name == "nt":
      if low.endswith(".exe"):
        matches.append(f)
    elif not low.endswith(".exe") and not low.endswith(NOT_BINARY_EXT):
      matches.append(f)
  return os.path.join(base, matches[-1]) if matches else None


def find_python():
  """Python interpreter for external tools (import_audit.py)."""
  if not getattr(sys, "frozen", False):
    return sys.executable
  return shutil.which("python") or shutil.which("python3") or shutil.which("py")


def file_state(path):
  """'missing' | 'elf' | 'self' | 'unknown'"""
  if not os.path.isfile(path):
    return "missing"
  try:
    with open(path, "rb") as f:
      header = f.read(64)
  except OSError:
    return "unknown"
  if header[:4] == b"\x7fELF":
    return "elf"
  if len(header) >= 4 and struct.unpack_from("<I", header, 0)[0] in SELF_MAGICS:
    return "self"
  return "unknown"


def existing_module_dirs(folder):
  return [d for d in MODULE_DIRS if os.path.isdir(os.path.join(folder, d))]


def check_module_dirs(folder):
  """USAGE.md rules. Returns None if ok, otherwise the error message."""
  found = existing_module_dirs(folder)
  if not found:
    return "no module folder (sce_module, sce_modules or prx)"
  if "sce_module" in found and "sce_modules" in found:
    return "sce_module and sce_modules are both present: the relinker does not accept this"
  return None

def find_extra_module_dirs(folder):
  """Folders (relative, with /) that contain .prx modules outside sce_module/sce_modules/prx."""
  found = []
  for root, dirs, files in os.walk(folder):
    if os.path.normpath(root) == os.path.normpath(folder):
      dirs[:] = [d for d in dirs if d not in MODULE_DIRS and d != "sce_sys"]
      continue
    for f in files:
      if f.lower().endswith(".prx") and file_state(os.path.join(root, f)) in ("elf", "self"):
        found.append(os.path.relpath(root, folder).replace("\\", "/"))
        break
  return found


def _dir_module_names(folder, d):
  """{lowercase name: real name} of the module files directly inside folder d."""
  full = os.path.join(folder, d)
  out = {}
  if not os.path.isdir(full):
    return out
  for name in os.listdir(full):
    if os.path.isfile(os.path.join(full, name)) and not name.lower().endswith((".esbak", ".json")):
      out[name.lower()] = name
  return out


def find_module_conflicts(folder, extra_dirs=()):
  """Modules with the same name in different module folders: the relinker would produce the same
  output file (e.g. libSceFace.prx.guest.prx). Returns [(name, [folders...])]."""
  seen = {}
  dirs = list(existing_module_dirs(folder))
  for d in extra_dirs:
    if d not in dirs:
      dirs.append(d)
  for d in dirs:
    for low, name in _dir_module_names(folder, d).items():
      seen.setdefault(low, (name, []))[1].append(d)
  return [(name, ds) for name, ds in seen.values() if len(ds) > 1]


def effective_module_dirs(folder, rl, log=None, explicit_base=False):
  """Manual folders + the auto-detected ones (no duplicates).
  Auto-detected folders that contain a module with the same name as one already present
  elsewhere are excluded (the relinker would report 'Conflicting guest output filename')."""
  if yes(rl.get("skip_sce_module", "no")):
    return []  # --skip-sce-module conflicts with --module-dir
  dirs = split_list(rl["module_dirs"])
  if explicit_base:
    return dirs  # with --sce-module-path the folders are relative to that path: no auto-detection
  if yes(rl.get("auto_module_dirs", "yes")):
    taken = {}  # lowercase name -> folder that already contains it
    for d in list(existing_module_dirs(folder)) + dirs:
      for low in _dir_module_names(folder, d):
        taken.setdefault(low, d)
    for d in find_extra_module_dirs(folder):
      if d in dirs:
        continue
      names = _dir_module_names(folder, d)
      clash = sorted(names[low] + " (already in " + taken[low] + ")" for low in names if low in taken)
      if clash:
        if log:
          log("warn", f"Auto-detected folder '{d}' skipped: it contains modules that already exist "
                      f"elsewhere, the relinker would report a conflicting output filename: "
                      + ", ".join(clash))
        continue
      dirs.append(d)
      for low in names:
        taken.setdefault(low, d)
  return dirs

def module_files(folder, extra_dirs=()):
  """Files directly inside the module folders (as the relinker does)."""
  out = []
  for d in list(existing_module_dirs(folder)) + [e for e in extra_dirs if e not in MODULE_DIRS]:
    full = os.path.join(folder, d)
    if not os.path.isdir(full):
      continue
    for name in sorted(os.listdir(full)):
      p = os.path.join(full, name)
      if os.path.isfile(p) and not name.lower().endswith((".esbak", ".json")):
        out.append((os.path.join(d, name).replace("\\", "/"), p))
  return out


def game_targets(game_folder, with_modules=True, extra_dirs=(), executables=()):
  targets = [("eboot.bin", os.path.join(game_folder, EXECUTABLE_NAME))]
  for rel in executables:
    targets.append((rel, os.path.join(game_folder, rel)))
  if with_modules:
    targets.extend(module_files(game_folder, extra_dirs))
  else:
    libc = os.path.join(game_folder, "sce_module", "libc.prx")
    targets.append(("sce_module/libc.prx", libc))
  return targets


def get_backup_dir(game_name):
  return os.path.join(UTILITIES_DIR, BACKUP_DIR_NAME, game_name)


_libs_cache = {}


def _scan_libs_zip(path, wanted):
  """Looks for a folder called `wanted` inside the zip (at any depth, the shallowest wins).
  Returns {"zip", "name", "entries": [(zip name, relative path, size, unix mode)], "size"} or None."""
  try:
    infos = [((n, size, mode), n.replace("\\", "/")) for n, size, mode in archive_listing(path)]
  except ArchiveError:
    return None
  best = None
  for _zi, name in infos:
    parts = name.split("/")
    for i, part in enumerate(parts[:-1]):
      if part.lower() == wanted.lower():
        if best is None or i < best[0]:
          best = (i, "/".join(parts[:i + 1]) + "/")
        break
  if best is None:
    return None
  prefix = best[1]
  entries = [(zi[0], name[len(prefix):], zi[1], zi[2])
             for zi, name in infos if name.startswith(prefix) and not name.endswith("/")]
  if not entries:
    return None
  return {"zip": path, "name": prefix.rstrip("/").split("/")[-1], "entries": entries,
          "size": sum(e[2] for e in entries)}


def libs_zip_info(target):
  """A prx-*.zip in Utilities that contains the libs folder (libs-<system> preferred over libs).
  The zip is read as it is: no need to extract it. Returns None if there is none."""
  base = release_dir() or UTILITIES_DIR
  try:
    zips = sorted((f for f in os.listdir(base)
                   if f.lower().startswith(LIBS_ZIP_PREFIX) and f.lower().endswith(".zip")
                   and os.path.isfile(os.path.join(base, f))), key=str.lower)
  except OSError:
    return None
  if base != UTILITIES_DIR:  # release: prx-windows / prx-linux, the one of the target goes first
    tag = RELEASE_LIBS_FOR.get(target, target)
    zips.sort(key=lambda n: tag in n.lower())  # the loop below reads from the end
  for wanted in ("%s-%s" % (LIBS_DIR_NAME, TARGETS[target]["suffix"]), LIBS_DIR_NAME):
    for name in reversed(zips):  # last in alphabetical order first (newest version)
      path = os.path.join(base, name)
      try:
        st = os.stat(path)
      except OSError:
        continue
      key = (path, st.st_mtime, st.st_size, wanted)
      if key not in _libs_cache:
        _libs_cache[key] = _scan_libs_zip(path, wanted)
      if _libs_cache[key]:
        return _libs_cache[key]
  return None


def extract_libs(info, dest, cancel):
  """Extract the libs folder of the zip (or copy the built libs folder) into `dest`.
  Returns the number of files."""
  os.makedirs(dest, exist_ok=True)
  root = os.path.realpath(dest)
  count = 0
  if info.get("dir"):
    for orig, rel, _size, _mode in info["entries"]:
      check_cancel(cancel)
      out = os.path.join(dest, *rel.split("/"))
      os.makedirs(os.path.dirname(out), exist_ok=True)
      shutil.copy2(orig, out)  # orig = full path of the file (libs-out or the MinGW bin folder)
      count += 1
    return count
  tmp = None
  with open_archive(info["zip"]) as arc:
    by_name = {e.name: e for e in arc.files}
    if not arc.streams:  # 7-Zip / py7zr extract the whole archive (the libs are small): copy from there
      base = os.path.join(UTILITIES_DIR, EXTRACT_DIR_NAME)
      os.makedirs(base, exist_ok=True)
      tmp = tempfile.mkdtemp(prefix="libs-", dir=base)
      try:
        arc.extract_to(tmp, arc.files, lambda _d: None, cancel, lambda _m: None)
      except ArchiveError:
        _rmtree_force(tmp)
        raise
    try:
      for orig, rel, _size, mode in info["entries"]:
        check_cancel(cancel)
        parts = [p for p in rel.split("/") if p]
        if not parts:
          continue
        out = os.path.join(dest, *parts)
        if not os.path.realpath(out).startswith(root + os.sep):
          continue  # unsafe path inside the archive
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if tmp:
          srcp = os.path.join(tmp, *orig.replace("\\", "/").split("/"))
          if not os.path.isfile(srcp):
            continue
          shutil.copy2(srcp, out)
        else:
          with arc.open(by_name[orig]) as src, open(out, "wb") as dst:
            shutil.copyfileobj(src, dst)
          if os.name != "nt" and mode:
            try:
              os.chmod(out, (mode & 0o777) | 0o600)
            except OSError:
              pass
        count += 1
    finally:
      if tmp:
        _rmtree_force(tmp)
  return count


def wants_built_libs(rl=None):
  """GitHub option active AND "build the libs from the repo" selected."""
  rl = rl if rl is not None else _active_rl
  return bool(rl) and rl.get("relinker_source") == "github" and yes(rl.get("repo_libs_from_repo", "yes"))


def libs_info(target, rl=None):
  """Libs to put in the package: the ones built from the repo (if that option is active and
  they were built), otherwise the prx-*.zip. None if not available (yet)."""
  rl = rl if rl is not None else _active_rl
  if wants_built_libs(rl):
    return read_built_libs(source_paths(rl), target)
  return libs_zip_info(target)


def libs_label(info):
  return os.path.basename(info["zip"]) if info.get("zip") else "built from the GitHub repo"


def game_problems(eboot, sce_sys, module_dirs):
  """What a PS5 game dump needs and does not have: eboot.bin, the sce_sys folder and exactly one module
  folder (sce_module / sce_modules / prx). Same rule for a game folder and for a game archive.
  Returns the list of missing things (empty = it is a game)."""
  problems = []
  if not eboot:
    problems.append("eboot.bin")
  if not sce_sys:
    problems.append("sce_sys folder")
  if not module_dirs:
    problems.append("module folder (sce_module, sce_modules or prx)")
  elif "sce_module" in module_dirs and "sce_modules" in module_dirs:
    problems.append("only one of sce_module / sce_modules (both are present)")
  return problems


def folder_game_problems(folder):
  if not folder or not os.path.isdir(folder):
    return ["game folder"]
  return game_problems(os.path.isfile(os.path.join(folder, EXECUTABLE_NAME)),
                       os.path.isdir(os.path.join(folder, "sce_sys")), existing_module_dirs(folder))


def archive_game_problems(z):
  return game_problems(z["eboot"], z["dirs"], z["module_dirs"])


def scan_game(folder, target="windows"):
  """State of the folder for the UI panel."""
  if not folder or not os.path.isdir(folder):
    return None
  mods = module_files(folder)
  states = [file_state(p) for _, p in mods]
  return {
    "eboot": file_state(os.path.join(folder, EXECUTABLE_NAME)),
    "modules_total": len(states),
    "modules_self": states.count("self"),
    "modules_unknown": states.count("unknown"),
    "module_error": check_module_dirs(folder),
    "module_dirs": existing_module_dirs(folder),
    "sce_sys": os.path.isdir(os.path.join(folder, "sce_sys")),
    "relinker": find_relinker(),
    "libs": libs_info(target) is not None or wants_built_libs(),
    "unwrapper": unwrapper_status(),
  }


def format_duration(seconds):
  seconds = int(seconds)
  if seconds >= 3600:
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
  if seconds >= 60:
    return f"{seconds // 60}m {seconds % 60:02d}s"
  return f"{seconds}s"


def cancellable_copy(cancel, log=None, total=0, sync=True, workers=0):
  """copy function for shutil.copytree: checks the cancel flag every 8 MiB (a large game can be
  interrupted in the middle of a file) and reports the progress through log("percent", value)
  and log("progress", text). The returned function has a .finish() to call at the end.
  sync=True forces flush + fsync after every file (needed when the source is removed afterwards);
  with sync=False (plain copy) it is skipped: fsync per file is very slow with many small files.
  workers>0 copies the files in parallel on that many threads (the per-file latency of games with
  tens of thousands of small files is hidden); the copy then finishes only in .finish(), which
  waits for the pool and re-raises the first error. Needs sync=False (the move mode stays serial)."""
  state = {"done": 0, "t0": time.time(), "last": 0.0}
  lock = threading.Lock()
  local = threading.local()  # one reusable read buffer per thread (no 8 MiB allocation per file)
  CHUNK = 4 * 1024 * 1024
  # diagnostics: where does the time go? (files, open/create, read, write, copystat, fsync)
  prof = {"files": 0, "open": 0.0, "read": 0.0, "write": 0.0, "stat": 0.0, "sync": 0.0,
          "slow": [], "last_dump": time.time()}

  def dump_profile(label):
    if log is None or not prof["files"]:
      return
    elapsed = max(time.time() - state["t0"], 0.001)
    log("detail", f"[diag] {label}: {prof['files']} files in {elapsed:.0f}s "
                  f"({prof['files'] / elapsed:.1f} files/s) · open/create {prof['open']:.1f}s · "
                  f"read {prof['read']:.1f}s · write {prof['write']:.1f}s · "
                  f"copystat {prof['stat']:.1f}s · fsync {prof['sync']:.1f}s")
    for t, name in sorted(prof["slow"], reverse=True)[:3]:
      log("detail", f"[diag] slowest file: {t:.2f}s  {name}")

  def report(force=False):
    if log is None:
      return
    now = time.time()
    if not force and now - state["last"] < 0.25:
      return
    state["last"] = now
    done = state["done"]
    pct = min(done * 100.0 / total, 100.0) if total else 0.0
    elapsed = max(now - state["t0"], 0.001)
    speed = done / elapsed
    text = f"Copying: {pct:.0f}% ({format_size(done)} / {format_size(total)}) · {format_size(speed)}/s"
    if elapsed > 2 and total > done and speed > 0:
      text += f" · {format_duration((total - done) / speed)} left"
    log("percent", pct)
    log("progress", text)

  def copy_one(src, dst):
    pc = time.perf_counter
    t_file = pc()
    buf = getattr(local, "buf", None)
    if buf is None:
      buf = local.buf = bytearray(CHUNK)
    mv = memoryview(buf)
    p_open = p_read = p_write = p_sync = 0.0
    t = pc()
    with open(src, "rb", buffering=0) as fi, open(dst, "wb") as fo:
      p_open += pc() - t
      st = os.fstat(fi.fileno())  # already-open handle: no extra stat of the source
      while True:
        check_cancel(cancel)
        t = pc()
        n = fi.readinto(buf)
        p_read += pc() - t
        if not n:
          break
        t = pc()
        fo.write(mv[:n])
        p_write += pc() - t
        with lock:
          state["done"] += n
        report()
      if sync:
        t = pc()
        fo.flush()
        try:
          os.fsync(fo.fileno())
        except OSError:
          pass
        p_sync += pc() - t
      t = pc()
    p_write += pc() - t  # closing the file is where the antivirus usually scans it
    t = pc()
    try:  # only the timestamps (one call): copystat reopens the destination several times on Windows
      os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    except OSError:
      pass
    p_stat = pc() - t
    with lock:
      prof["open"] += p_open
      prof["read"] += p_read
      prof["write"] += p_write
      prof["sync"] += p_sync
      prof["stat"] += p_stat
      prof["files"] += 1
      prof["slow"].append((pc() - t_file, os.path.basename(src)))
      if len(prof["slow"]) > 200:
        prof["slow"] = sorted(prof["slow"], reverse=True)[:5]
      dump = time.time() - prof["last_dump"] > 30
      if dump:
        prof["last_dump"] = time.time()
    if dump:
      dump_profile("progress")
    return dst

  pool = ThreadPoolExecutor(max_workers=workers) if workers > 0 else None
  pending = threading.BoundedSemaphore(workers * 64) if pool else None
  failed = []  # first error raised by a worker

  def worker(src, dst):
    try:
      if not failed:
        copy_one(src, dst)
    except BaseException as e:  # noqa: BLE001 - re-raised by the main thread
      failed.append(e)
    finally:
      pending.release()

  def copy(src, dst):
    if pool is None:
      return copy_one(src, dst)
    if failed:
      raise failed[0]
    check_cancel(cancel)
    pending.acquire()  # backpressure: at most workers*64 queued files
    pool.submit(worker, src, dst)
    return dst

  def close(abort=True):
    """abort=True drops the queued files (error/cancel); abort=False waits for all of them."""
    if pool is not None:
      pool.shutdown(wait=True, cancel_futures=abort)

  def add(n):
    with lock:
      state["done"] += n
    report()

  def finish():
    close(abort=False)
    if failed:
      raise failed[0]
    if log is not None:
      state["done"] = max(state["done"], total)
      report(True)
      dump_profile("total")
      log("percent", None)  # back to the indeterminate bar

  copy.finish = finish
  copy.add = add
  copy.close = close
  return copy


class Mover:
  """Safe 'move' of the game: every file is copied (or renamed, on the same drive), checked and
  only then removed from the source. The list of moved files lets the operation be rolled back
  if the relink fails or is cancelled."""

  def __init__(self, source_root, dest_root):
    self.source_root = source_root
    self.dest_root = dest_root
    self.moved = []        # relative paths already removed from the source
    self.leftovers = 0     # files that could not be removed from the source (kept in both places)

  def rel(self, src):
    return os.path.relpath(src, self.source_root)

  def _remove_source(self, src, log):
    try:
      os.remove(src)
    except OSError:
      try:
        os.chmod(src, stat.S_IWRITE | stat.S_IREAD)
        os.remove(src)
      except OSError as e:
        self.leftovers += 1
        if log:
          log("warn", f"Copied but not removed from the source ({e}): {src}")
        return False
    return True

  def rollback(self, backup_dir, log):
    """Puts every moved file back in the source folder. The originals saved before the unwrap
    (backup_dir) win over the unwrapped copies. Returns True if everything is back."""
    failed = 0
    for rel in self.moved:
      dst = os.path.join(self.source_root, rel)
      src = os.path.join(self.dest_root, rel)
      bak = os.path.join(backup_dir, *rel.split(os.sep)) if backup_dir else None
      try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):
          continue
        if bak and os.path.isfile(bak):
          shutil.copy2(bak, dst)
        else:
          try:
            os.rename(src, dst)
          except OSError:
            shutil.copy2(src, dst)
      except Exception as e:
        failed += 1
        log("err", f"Unable to restore {rel}: {e}")
    return failed == 0


def cancellable_move(mover, cancel, log=None, total=0):
  """copy function for shutil.copytree that MOVES the file: rename if possible, otherwise copy,
  check the size (after flush + fsync) and only then remove the source. Same interface as
  cancellable_copy."""
  inner = cancellable_copy(cancel, log, total)

  def move(src, dst):
    size = os.path.getsize(src)
    if not os.path.islink(src):
      try:
        os.rename(src, dst)  # same drive: instant and atomic
        mover.moved.append(mover.rel(src))
        inner.add(size)
        return dst
      except OSError:
        pass  # different drive (or locked): copy + verify + remove
    inner(src, dst)
    if os.path.getsize(dst) != size:
      raise OSError(f"size mismatch after copying: {src}")
    if mover._remove_source(src, log):
      mover.moved.append(mover.rel(src))
    return dst

  move.finish = inner.finish
  return move


def check_cancel(cancel):
  if cancel.is_set():
    raise Cancelled()


# status codes returned by unwrap_file (used by the logic and the summary)
ST_UNWRAPPED = "UNWRAPPED"
ST_ALREADY_OK = "ALREADY OK"
ST_NOT_FOUND = "NOT FOUND"
ST_ERROR = "ERROR"


def unwrap_file(file_path, label, backup_dir, log):
  """Unwrap one file with unwrap_self.py. backup_dir=None -> no backup.
  Returns (status, detail)."""
  name = label
  log("step", f"File · {name}")
  state = file_state(file_path)

  if state == "missing":
    log("warn", f"'{name}' does not exist at this path. Skipped.")
    return (ST_NOT_FOUND, "-")
  if state == "elf":
    log("info", "It is already an ELF: no change, no backup.")
    return (ST_ALREADY_OK, "-")
  if state == "unknown":
    log("warn", "Unrecognised format (neither ELF nor SELF): leaving it as is.")
    return (ST_ERROR, "unknown format")

  try:
    module = load_unwrapper()
  except Exception as e:
    log("err", f"Unable to load {UNWRAP_SCRIPT_NAME}: {e}")
    return (ST_ERROR, "unwrapper cannot be loaded")
  if module is None:
    where = ("the cloned GitHub repository (tools/) or " if github_mode()
             else "the downloaded release or " if release_mode() else "")
    log("err", f"'{UNWRAP_SCRIPT_NAME}' not found in {where}the {UTILITIES_DIR_NAME} folder ({UTILITIES_DIR}).")
    return (ST_ERROR, "unwrap_self.py missing")

  if backup_dir is not None:
    backup_path = os.path.join(backup_dir, *name.split("/"))
    try:
      os.makedirs(os.path.dirname(backup_path), exist_ok=True)
      if os.path.exists(backup_path):
        log("info", "A backup of the original already exists: leaving it untouched.")
      else:
        shutil.copy2(file_path, backup_path)
        log("ok", f"Backup created: {BACKUP_DIR_NAME}/{os.path.basename(backup_dir)}/{name}")
    except Exception as e:
      log("err", f"Unable to create the backup: {e}")
      return (ST_ERROR, "backup failed")

  base, ext = os.path.splitext(file_path)
  tmp_path = f"{base}_unwrapped{ext}"
  try:
    size = module.unwrap(file_path, tmp_path, False)
    os.replace(tmp_path, file_path)
  except module.Refused as e:
    log("err", f"{UNWRAP_SCRIPT_NAME} refuses the file: {e}")
    _remove_quiet(tmp_path)
    return (ST_ERROR, "refused")
  except Exception as e:
    log("err", f"Unwrap failed: {e}")
    _remove_quiet(tmp_path)
    return (ST_ERROR, "unwrap failed")

  log("ok", f"'{name}' replaced with the ELF ({size} bytes).")
  return (ST_UNWRAPPED, f"{size} bytes")


def move_to_trash(path):
  """Move a file/folder to the system trash. Returns True on success."""
  path = os.path.abspath(path)
  try:
    from send2trash import send2trash  # optional: pip install send2trash
    send2trash(path)
    return True
  except ImportError:
    pass
  except Exception:
    return False
  try:
    if os.name == "nt":
      import ctypes
      from ctypes import wintypes

      class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
      FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_NOERRORUI, FOF_SILENT = 3, 0x40, 0x10, 0x400, 0x4
      op = SHFILEOPSTRUCTW()
      op.wFunc = FO_DELETE
      op.pFrom = path + "\0\0"
      op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT
      return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0 and not op.fAnyOperationsAborted
    if sys.platform == "darwin":
      script = 'tell application "Finder" to delete POSIX file "%s"' % path.replace('"', '\\"')
      return subprocess.run(["osascript", "-e", script], capture_output=True).returncode == 0
    if shutil.which("gio"):
      if subprocess.run(["gio", "trash", path], capture_output=True).returncode == 0:
        return True
    base = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "Trash")
    files_dir, info_dir = os.path.join(base, "files"), os.path.join(base, "info")
    os.makedirs(files_dir, exist_ok=True)
    os.makedirs(info_dir, exist_ok=True)
    name, n = os.path.basename(path), 0
    target = name
    while os.path.exists(os.path.join(files_dir, target)):
      n += 1
      target = "%s.%d" % (name, n)
    with open(os.path.join(info_dir, target + ".trashinfo"), "w", encoding="utf-8") as f:
      f.write("[Trash Info]\nPath=%s\nDeletionDate=%s\n" % (path, time.strftime("%Y-%m-%dT%H:%M:%S")))
    shutil.move(path, os.path.join(files_dir, target))
    return True
  except Exception:
    return False


def storage_choice_to_dir(picked):
  """Folder picked in the dialog = the folder to use as is (validate_storage_target requires it to be
  empty, so nothing already there gets mixed up with the app's files)."""
  return os.path.abspath(picked)


def validate_storage_target(new):
  """Returns "" if Utilities/ can be moved to `new`, else the reason why not."""
  if not STORAGE_EDITABLE:
    return "The location can only be changed in the packaged app (not from source or in portable mode)."
  if not new or not os.path.isabs(new):
    return "Enter a full folder path."
  cur, new = os.path.realpath(UTILITIES_DIR), os.path.realpath(new)
  nc, cc = os.path.normcase(new), os.path.normcase(cur)
  if nc == cc:
    return "That is already the current folder."
  if nc.startswith(cc + os.sep):
    return "Choose a folder outside the current one."
  if cc.startswith(nc + os.sep):
    return "Choose a folder that does not contain the current one."
  if os.path.isfile(new):
    return "That path is a file."
  try:
    if os.path.isdir(new) and [n for n in os.listdir(new) if n != os.path.basename(LOCATION_FILE)]:
      return "That folder is not empty: choose an empty one (or a new one)."
    os.makedirs(new, exist_ok=True)
    probe = os.path.join(new, ".write-test")
    with open(probe, "w", encoding="utf-8") as fh:
      fh.write("x")
    os.remove(probe)
  except OSError as e:
    return "Cannot write in that folder: %s" % e
  return ""


def relocate_utilities(new, status):
  """Moves everything in Utilities/ to `new` (an empty folder), then makes it the new location.
  status(text, pct) reports progress. Raises RuntimeError with a readable message on failure; in that
  case the already moved items are put back. The app must be restarted afterwards."""
  problem = validate_storage_target(new)
  if problem:
    raise RuntimeError(problem)
  old = UTILITIES_DIR
  items = [n for n in sorted(os.listdir(old)) if n.lower() != "__pycache__" and n != os.path.basename(LOCATION_FILE)]
  if os.stat(old).st_dev != os.stat(new).st_dev:  # other drive: real copy, check the space first
    status("Checking the free space…", None)
    need, free = dir_size(old), shutil.disk_usage(existing_parent(new)).free
    if need + (64 << 20) > free:
      raise RuntimeError("Not enough space in the new folder: %s needed, %s free." % (format_size(need), format_size(free)))
  done, name = [], ""
  try:
    for i, name in enumerate(items):
      status("Moving %s… (%d/%d)" % (name, i + 1, len(items)), int(100 * i / max(1, len(items))))
      shutil.move(os.path.join(old, name), os.path.join(new, name))
      done.append(name)
  except Exception as e:
    status("Something went wrong: putting everything back…", None)
    partial = os.path.join(new, name)
    if name and os.path.exists(partial) and os.path.exists(os.path.join(old, name)):
      shutil.rmtree(partial, ignore_errors=True) if os.path.isdir(partial) else _remove_quiet(partial)
    for n in reversed(done):
      try:
        shutil.move(os.path.join(new, n), os.path.join(old, n))
      except Exception:
        pass
    raise RuntimeError("Could not move '%s': %s. The folder was not changed (if a file is open in another "
                       "program, close it and try again)." % (name, e))
  status("Saving the new location…", 100)
  try:
    os.makedirs(os.path.dirname(LOCATION_FILE), exist_ok=True)
    if os.path.normcase(os.path.realpath(new)) == os.path.normcase(os.path.realpath(DEFAULT_UTILITIES_DIR)):
      if os.path.isfile(LOCATION_FILE):
        os.remove(LOCATION_FILE)  # back to the default: no pointer needed
    else:
      with open(LOCATION_FILE + ".tmp", "w", encoding="utf-8") as fh:
        fh.write(os.path.abspath(new))
      os.replace(LOCATION_FILE + ".tmp", LOCATION_FILE)
  except OSError as e:
    for n in reversed(done):  # without the pointer the app would not find its data: undo
      try:
        shutil.move(os.path.join(new, n), os.path.join(old, n))
      except Exception:
        pass
    raise RuntimeError("Could not save the new location: %s. The folder was not changed." % e)
  shutil.rmtree(os.path.join(old, "__pycache__"), ignore_errors=True)
  if os.path.normcase(os.path.realpath(old)) != os.path.normcase(os.path.realpath(DEFAULT_UTILITIES_DIR)):
    if os.path.isdir(old) and not os.listdir(old) and not move_to_trash(old):  # old folder empty: to the trash
      try:
        os.rmdir(old)
      except OSError:
        pass


# what the program itself puts in Utilities/ (or looks for there): moved by migrate_legacy_utilities.
# Anything else the user left in the old folder stays where it is.
_MIGRATE_NAMES = {"backups", "release", "source", "toolchain", "extracted", "update", "anyps5-fonts", "tools",
                  "config.ini", "userelinker.ini", "anyps5-input.ini", "unwrap_self.py",
                  "7z", "7zz", "7za", "7z.exe", "7za.exe", "7z.dll"}
_MIGRATE_PREFIXES = ("relinker-", "prx-")
_MIGRATE_HEAVY = {"release", "source", "toolchain", "extracted", "update"}  # big, rebuilt on demand
_MIGRATE_JUNK = {"__pycache__", ".ds_store", "thumbs.db", "desktop.ini"}


def migrate_legacy_utilities(old=None):
  """Moves an old Utilities folder (default: <program folder>/Utilities) into UTILITIES_DIR, item by item.
  Items already present in the new folder are not touched. The old folder goes to the trash only if
  nothing else is left in it. Never raises. Returns (moved, left) lists of names."""
  moved, left = [], []
  old, new = old or LEGACY_UTILITIES_DIR, UTILITIES_DIR
  try:
    if os.path.abspath(old) == os.path.abspath(new) or not os.path.isdir(old):
      return moved, left
    os.makedirs(new, exist_ok=True)
    same_drive = os.stat(old).st_dev == os.stat(new).st_dev
    for name in sorted(os.listdir(old)):
      low = name.lower()
      if low in _MIGRATE_JUNK:
        continue
      known = low in _MIGRATE_NAMES or low.startswith(_MIGRATE_PREFIXES)
      src, dst = os.path.join(old, name), os.path.join(new, name)
      if not known or os.path.lexists(dst):
        left.append(name)
        continue
      if low in _MIGRATE_HEAVY and not same_drive:
        left.append(name)  # a copy across drives could take minutes with no window: leave it
        continue
      try:
        shutil.move(src, dst)  # same drive: instant rename
        moved.append(name)
      except OSError:
        left.append(name)
    if not left:  # empty (junk aside): to the trash
      for name in os.listdir(old):
        junk = os.path.join(old, name)
        if os.path.isdir(junk) and not os.path.islink(junk):
          shutil.rmtree(junk, ignore_errors=True)
        else:
          try:
            os.remove(junk)
          except OSError:
            pass
      if not os.listdir(old) and not move_to_trash(old):
        try:
          os.rmdir(old)
        except OSError:
          pass
  except Exception:
    pass
  return moved, left


def _remove_empty_dirs(root):
  """Removes the empty folders left in the source after a move (root included)."""
  for cur, _dirs, _files in os.walk(root, topdown=False):
    try:
      os.rmdir(cur)
    except OSError:
      pass


def do_relink(folder, cfg, log, cancel, holder):
  """Wrapper: if the relink fails or is cancelled, the created folder goes to the trash.
  With keep_game = no the game is moved: on failure/cancel the files are put back first."""
  created = []
  ok = False
  move = not yes(cfg["settings"].get("keep_game", "yes"))
  mover = None
  game_name = os.path.basename(os.path.normpath(folder))
  if move:
    s = cfg["settings"]
    out_base = s["output_dir"] or SCRIPT_DIR
    pkg = package_dir(out_base, game_name, cfg["relinker"]["target"])
    mover = Mover(folder, os.path.join(pkg, "app0"))
    log("info", "'Keep PS5 game' is OFF: the game will be MOVED (safe mode).")
  try:
    ok = _do_relink(folder, cfg, log, cancel, holder, created, mover)
    if ok and mover is not None:
      _remove_empty_dirs(folder)
      log("ok", "Source folder cleaned up." if not os.path.isdir(folder)
          else "Source folder kept (some files could not be removed).")
    return ok
  finally:
    if not ok:
      safe_to_trash = True
      if mover is not None and mover.moved:
        log("step", "Restoring the game to its original folder")
        if mover.rollback(get_backup_dir(game_name), log):
          log("ok", f"{len(mover.moved)} files put back in {folder}")
        else:
          safe_to_trash = False
          log("err", "Restore incomplete: the package folder was NOT deleted, "
                     "the missing files are still inside it.")
      for d in created:
        if safe_to_trash and os.path.isdir(d):
          if move_to_trash(d):
            log("info", f"Incomplete folder moved to the trash: {d}")
          else:
            log("warn", f"Unable to move it to the trash, delete it by hand: {d}")
        elif os.path.isdir(d):
          log("warn", f"Folder left in place: {d}")


def _remove_quiet(path):
  if os.path.exists(path):
    try:
      os.remove(path)
    except OSError:
      pass


def handle_esbak(file_path, label, backup_dir, log):
  """Removes any .esbak (AnyPS5 would see it as a duplicate).
  With backup_dir it moves it into the backups, otherwise it deletes it (working copy)."""
  esbak = file_path + ".esbak"
  if not os.path.exists(esbak):
    return
  name = label + ".esbak"
  try:
    if backup_dir is not None:
      dest = os.path.join(backup_dir, *name.split("/"))
      os.makedirs(os.path.dirname(dest), exist_ok=True)
      if os.path.exists(dest):
        os.remove(dest)
      shutil.move(esbak, dest)
      log("ok", f"'{name}' moved into the backups (AnyPS5 would see it as a duplicate).")
    else:
      os.remove(esbak)
      log("ok", f"'{name}' removed from the working copy.")
  except Exception as e:
    log("err", f"Unable to handle '{name}': {e}")


def unwrap_game(folder, backup_dir, log, cancel, with_modules=True, extra_dirs=(), executables=()):
  """Unwrap eboot.bin, extra executables and modules. Returns the list of results."""
  results = []
  for name, path in game_targets(folder, with_modules, extra_dirs, executables):
    check_cancel(cancel)
    if name != "eboot.bin" and file_state(path) in ("missing",):
      continue  # optional modules: no noise if they do not exist
    outcome = unwrap_file(path, name, backup_dir, log)
    results.append((name, outcome))
    if outcome[0] != ST_ERROR:
      handle_esbak(path, name, backup_dir, log)
  return results


def log_results(results, log):
  log("step", "Unwrap summary")
  for name, (status, detail) in results:
    kind = {ST_UNWRAPPED: "ok", ST_ALREADY_OK: "info", ST_NOT_FOUND: "warn", ST_ERROR: "err"}.get(status, "info")
    log(kind, f"{name:<28} {status:<12} {detail}")


def split_list(text):
  return [p.strip().strip('"') for p in re.split(r"[;,\n]", text) if p.strip()]


def do_unwrap(folder, cfg, log, cancel, holder=None):
  game_name = os.path.basename(os.path.normpath(folder))
  rl = cfg["relinker"]
  set_active_relinker_cfg(rl)
  if rl.get("relinker_source") == "github":
    if not ensure_repo_unwrapper(rl, log, cancel, holder if holder is not None else {}):
      return False
  elif rl.get("relinker_source") == "release":
    ensure_release_assets(rl, log, cancel, need=("unwrap",))  # falls back to Utilities/ if it fails
    if unwrapper_status() is None:
      log("err", f"'{UNWRAP_SCRIPT_NAME}' not available: not in the release and not in {UTILITIES_DIR_NAME}.")
      return False
  log("step", f"Unwrap · {game_name}")
  try:
    if not log_space(space_requirements(folder, "windows", "unwrap", UTILITIES_DIR), log):
      log("err", "Not enough disk space for the backups: free some space and retry.")
      return False
  except OSError as e:
    log("warn", f"Unable to check the disk space: {e}")
  results = unwrap_game(folder, get_backup_dir(game_name), log, cancel,
                        with_modules=yes(rl["unwrap_modules"]),
                        extra_dirs=effective_module_dirs(folder, rl, log),
                        executables=split_list(rl["executables"]))
  log_results(results, log)
  return all(r[1][0] != ST_ERROR for r in results)


def do_restore(folder, log, cancel):
  game_name = os.path.basename(os.path.normpath(folder))
  backup_dir = get_backup_dir(game_name)
  log("step", f"Restore · {game_name}")
  log("detail", f"Backup folder: {backup_dir}")

  found = []
  for root, _dirs, files in os.walk(backup_dir):
    for f in files:
      bak = os.path.join(root, f)
      rel = os.path.relpath(bak, backup_dir)
      found.append((rel.replace("\\", "/"), os.path.join(folder, rel), bak))
  if not found:
    log("err", "No backup to restore for this game.")
    return False

  ok = True
  for name, path, bak in found:
    check_cancel(cancel)
    try:
      os.makedirs(os.path.dirname(path), exist_ok=True)
      shutil.copy2(bak, path)
      log("ok", f"{name} restored from the backup.")
    except Exception as e:
      log("err", f"Restoring {name} failed: {e}")
      ok = False
  return ok


_ERROR_LINE_RE = re.compile(r"\berrors?\b|\berrore\b|\bfatal\b|\b(?:C|LNK|MSB)\d{4}\b|undefined reference|\*\*\*", re.I)
_NOT_ERROR_RE = re.compile(r"\bwarning\b|\bavviso\b|\b0 errors?\b|\b0 errori\b", re.I)


def is_error_line(text):
  """Compiler/linker error line (also localised MSVC output)."""
  return bool(_ERROR_LINE_RE.search(text)) and not _NOT_ERROR_RE.search(text)


def kill_tree(proc):
  """Kill a process AND all its children (cmake -> ninja -> glslang/cl/g++...).
  proc.terminate() only kills the direct child: ninja and its compilers/glslang survive as
  orphans, keep the output pipe open and can only be removed by rebooting."""
  if proc is None or proc.poll() is not None:
    return
  try:
    if os.name == "nt":
      subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                     timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
      import signal
      os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
  except Exception:
    pass
  try:
    proc.kill()
  except Exception:
    pass


def kill_stale_build_processes():
  """Windows: remove leftover glslang/ninja processes of a previous hung build."""
  if os.name != "nt":
    return
  for name in ("glslangValidator.exe", "glslang.exe", "ninja.exe"):
    try:
      subprocess.run(["taskkill", "/F", "/T", "/IM", name], capture_output=True, timeout=20,
                     creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception:
      pass


def build_parallel_jobs():
  """Parallel jobs for the build: not all cores (glslang/FFmpeg builds with every core can
  exhaust RAM and freeze the machine)."""
  n = os.cpu_count() or 2
  return max(1, min(8, n // 2 if n > 4 else n))


def stream_process(cmd, log, cancel, holder, label="Process", cwd=None, env=None,
                   errors=None, logfile=None, stdin=None, idle_timeout=None):
  """Run a process showing its output in real time. Returns the exit code.
  errors: list that collects the error lines of the whole run (they are also logged as errors);
  logfile: path where the complete output is written;
  idle_timeout: seconds without any output after which the whole process tree is killed."""
  import queue
  kwargs = {}
  if os.name == "nt":
    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
  else:
    kwargs["start_new_session"] = True  # own process group -> killpg kills the whole tree
  proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          bufsize=0, cwd=cwd, env=env,
                          stdin=stdin if stdin is not None else subprocess.DEVNULL, **kwargs)
  holder["proc"] = proc
  log("ok", f"{label} started (PID {proc.pid})")

  import codecs
  decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
  fd = proc.stdout.fileno()
  buf = ""
  tail = holder.setdefault("tail", [])
  del tail[:]
  lf = None
  if logfile:
    try:
      lf = open(logfile, "a", encoding="utf-8", errors="replace")
      lf.write("\n===== %s =====\n" % " ".join(cmd))
    except OSError:
      lf = None

  def emit(text, term):
    text = text.rstrip()
    tail.append(text)
    del tail[:-40]
    if lf:
      lf.write(text + "\n")
    bad = errors is not None and is_error_line(text)
    if bad and len(errors) < 300:
      errors.append(text)
    log("err" if bad else ("progress" if term == "\r" else "info"), text)

  def flush_segments(final=False):
    nonlocal buf
    pos = 0
    for m in re.finditer(r"([^\r\n]*)(\r\n|\n|\r)", buf):
      text, term = m.group(1), m.group(2)
      pos = m.end()
      if text.strip():
        emit(text, term)
    buf = buf[pos:]
    if final and buf.strip():
      emit(buf, "\n")
      buf = ""

  # os.read() blocks while a hung child keeps the pipe open: it is done in a thread so that the
  # main loop can always react to Cancel and to the idle timeout.
  q = queue.Queue()

  def reader():
    try:
      while True:
        chunk = os.read(fd, 4096)
        q.put(chunk)
        if not chunk:
          break
    except Exception:
      q.put(b"")

  threading.Thread(target=reader, daemon=True).start()
  last_out = time.time()
  try:
    while True:
      if cancel.is_set():
        kill_tree(proc)
        raise Cancelled()
      try:
        chunk = q.get(timeout=0.2)
      except queue.Empty:
        if idle_timeout and time.time() - last_out > idle_timeout:
          log("err", f"{label}: no output for {int(idle_timeout // 60)} minutes, the build looks "
                     "hung. Killing the whole process tree (cmake, ninja, glslang...).")
          kill_tree(proc)
          return -1
        continue
      if not chunk:
        break
      last_out = time.time()
      buf += decoder.decode(chunk)
      flush_segments()
    buf += decoder.decode(b"", final=True)
    flush_segments(final=True)
    return proc.wait()
  finally:
    if lf:
      lf.close()
    if proc.poll() is None:
      kill_tree(proc)
      try:
        proc.wait(timeout=10)  # released files can then be moved to the trash
      except subprocess.TimeoutExpired:
        proc.wait()
    holder["proc"] = None



# ─────────────────────────────────────────────────────────────
#  Files of a GitHub release (relinker, prx zips, docs, unwrap_self.py)
# ─────────────────────────────────────────────────────────────
_GITHUB_REPO_RE = re.compile(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", re.I)
_RELEASE_TAG_RE = re.compile(r"^[\w][\w./+-]*$")
RAW_URL = "https://raw.githubusercontent.com/%s/%s/%s/%s"
API_URL = "https://api.github.com/repos/%s/%s/releases/%s"


def github_owner_repo(url):
  """(owner, repo) of a github.com URL, or None."""
  m = _GITHUB_REPO_RE.search((normalize_repo_url(url) or "").strip())
  return (m.group(1), m.group(2)) if m else None


def release_paths(rl):
  """{'root', 'current'} of the release folder of the configured repository, or None."""
  url = normalize_repo_url(rl.get("repo_url", ""))
  if not url:
    return None
  root = os.path.join(UTILITIES_DIR, RELEASE_DIR_NAME, repo_slug(url, "release"))
  return {"url": url, "root": root, "current": os.path.join(root, RELEASE_CURRENT_NAME)}


def read_release_current(rl):
  """{'tag', 'dir'} of the downloaded release in use (the configured tag, else the last one
  downloaded), or None."""
  paths = release_paths(rl)
  if not paths:
    return None
  tag = rl.get("release_tag", "").strip()
  if not tag:
    cfg = configparser.ConfigParser(interpolation=None)
    try:
      cfg.read(paths["current"], encoding="utf-8")
    except Exception:
      return None
    tag = cfg.get("release", "tag", fallback="")
  if not tag or not _RELEASE_TAG_RE.match(tag):
    return None
  d = os.path.join(paths["root"], tag)
  return {"tag": tag, "dir": d} if os.path.isdir(d) else None


def release_dir(rl=None):
  """Folder of the downloaded release when the Release option is active, else None."""
  rl = rl if rl is not None else _active_rl
  if not rl or rl.get("relinker_source") != "release":
    return None
  cur = read_release_current(rl)
  return cur["dir"] if cur else None


def release_mode():
  return bool(_active_rl) and _active_rl.get("relinker_source") == "release"


def _http_get(url, accept=None, timeout=30):
  import urllib.request
  headers = {"User-Agent": "AnyPS5-GUI"}
  if accept:
    headers["Accept"] = accept
  token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
  if token and "api.github.com" in url:
    headers["Authorization"] = "Bearer " + token
  return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)


def fetch_release_info(owner, repo, tag):
  """Release description from the GitHub API: {'tag', 'assets': [{name, url, size, sha256}]}."""
  import json
  what = "tags/" + tag if tag else "latest"
  with _http_get(API_URL % (owner, repo, what), "application/vnd.github+json") as resp:
    data = json.loads(resp.read().decode("utf-8"))
  assets = []
  for a in data.get("assets", []):
    digest = a.get("digest") or ""
    assets.append({"name": a.get("name", ""), "url": a.get("browser_download_url", ""),
                   "size": int(a.get("size") or 0),
                   "sha256": digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") else ""})
  return {"tag": data.get("tag_name", ""), "notes": data.get("body") or "",
          "assets": [a for a in assets if a["name"] and a["url"]]}


def _download_file(url, dest, size, sha256, log, cancel, label, quiet=False):
  """Download `url` to `dest` (through .part, checks size and sha256). Returns True on success."""
  tmp = dest + ".part"
  try:
    h = hashlib.sha256()
    done, t0, last = 0, time.time(), 0.0
    with _http_get(url, timeout=60) as resp, open(tmp, "wb") as out:
      total = size or int(resp.headers.get("Content-Length") or 0)
      while True:
        check_cancel(cancel)
        chunk = resp.read(256 * 1024)
        if not chunk:
          break
        out.write(chunk)
        h.update(chunk)
        done += len(chunk)
        now = time.time()
        if now - last >= 0.25:
          last = now
          pct = min(done * 100.0 / total, 100.0) if total else 0.0
          speed = done / max(now - t0, 0.001)
          log("percent", pct if total else None)
          log("progress", f"Downloading {label}: {format_size(done)}"
              + (f" / {format_size(total)}" if total else "") + f" · {format_size(speed)}/s")
      out.flush()
      try:
        os.fsync(out.fileno())
      except OSError:
        pass
    if size and done != size:
      raise OSError(f"incomplete download ({done} of {size} bytes)")
    if sha256 and h.hexdigest() != sha256:
      raise OSError("sha256 does not match the one published by GitHub")
    os.replace(tmp, dest)
    return True
  except Cancelled:
    raise
  except Exception as e:
    log("detail" if quiet else "err", f"Download of {label} failed: {e}")
    return False
  finally:
    log("percent", None)
    log("progress", "")
    try:
      if os.path.exists(tmp):
        os.remove(tmp)
    except OSError:
      pass


def _file_ok(path, size, sha256):
  try:
    if not os.path.isfile(path) or (size and os.path.getsize(path) != size):
      return False
    if sha256:
      h = hashlib.sha256()
      with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
          h.update(chunk)
      return h.hexdigest() == sha256
    return os.path.getsize(path) > 0
  except OSError:
    return False


def _pick_release_assets(assets, target, need):
  """Chooses the assets to download. Returns ([(asset, required)], [missing descriptions])."""
  by_low = {a["name"].lower(): a for a in assets}
  picked, missing = [], []
  if "relinker" in need:
    cand = []
    for a in assets:
      low = a["name"].lower()
      if not low.startswith(RELINKER_PREFIX):
        continue
      if os.name == "nt":
        ok = low.endswith(".exe")
      else:
        ok = not low.endswith(".exe") and not low.endswith(NOT_BINARY_EXT)
      if ok:
        cand.append(a)
    if cand:
      picked.append((sorted(cand, key=lambda a: a["name"].lower())[-1], True))
    else:
      missing.append("%s*%s" % (RELINKER_PREFIX, ".exe" if os.name == "nt" else " (no extension)"))
  if "libs" in need:
    want = RELEASE_LIBS_FOR.get(target, target)
    zips = [a for a in assets if a["name"].lower().startswith(LIBS_ZIP_PREFIX) and a["name"].lower().endswith(".zip")]
    best = [a for a in zips if want in a["name"].lower()] or zips
    if best:
      picked.append((sorted(best, key=lambda a: a["name"].lower())[-1], True))
    else:
      missing.append("%s%s-*.zip" % (LIBS_ZIP_PREFIX, want))
  if "unwrap" in need and UNWRAP_SCRIPT_NAME in by_low:
    picked.append((by_low[UNWRAP_SCRIPT_NAME], True))
  if "docs" in need:
    for name in RELEASE_DOCS:
      if name.lower() in by_low:
        picked.append((by_low[name.lower()], False))
  return picked, missing


def ensure_release_assets(rl, log, cancel, target=None, need=("relinker", "libs", "unwrap", "docs")):
  """Release option: downloads from the GitHub release (latest, or the configured tag) the files
  that are not already in Utilities/release/<repo>/<tag>/, keeping the names used in the release.
  The release has no unwrap_self.py asset: it is taken from tools/ of the same tag. If GitHub
  cannot be reached, the release downloaded earlier is used. Returns True if everything required
  is available."""
  import json
  paths = release_paths(rl)
  gh = github_owner_repo(rl.get("repo_url", "")) if paths else None
  if not gh:
    log("err", "The Release option needs a github.com repository URL (https://github.com/owner/repo).")
    return False
  want_tag = rl.get("release_tag", "").strip()
  if want_tag and not _RELEASE_TAG_RE.match(want_tag):
    log("err", "Invalid release tag: %s" % want_tag)
    return False
  target = target or rl.get("target", "windows")
  owner, repo = gh
  log("step", f"Release from GitHub · {owner}/{repo}" + (f" · {want_tag}" if want_tag else " · latest"))

  # 1. which release: reuse the saved description when updates are off and the tag is known
  cur = read_release_current(rl)
  info = None
  if cur and not yes(rl.get("release_update", "yes")):
    try:
      with open(os.path.join(cur["dir"], RELEASE_INFO_NAME), encoding="utf-8") as f:
        info = json.load(f)
    except (OSError, ValueError):
      info = None
  if info is None:
    try:
      info = fetch_release_info(owner, repo, want_tag)
      if not info["tag"] or not _RELEASE_TAG_RE.match(info["tag"]):
        raise ValueError("release without a valid tag")
    except Exception as e:
      if cur:
        log("warn", f"Cannot read the release from GitHub ({e}): using the one already downloaded ({cur['tag']}).")
        try:
          with open(os.path.join(cur["dir"], RELEASE_INFO_NAME), encoding="utf-8") as f:
            info = json.load(f)
        except (OSError, ValueError):
          info = {"tag": cur["tag"], "assets": []}
      else:
        log("err", f"Cannot read the release from GitHub: {e}")
        log("detail", "Check the connection, the repository URL and the release tag "
                      "(GitHub allows 60 requests/hour without a token: set GITHUB_TOKEN to raise it).")
        return False
  tag = info["tag"]
  dest = os.path.join(paths["root"], tag)
  try:
    os.makedirs(dest, exist_ok=True)
    with open(os.path.join(dest, RELEASE_INFO_NAME), "w", encoding="utf-8") as f:
      json.dump(info, f, indent=1)
    cfgc = configparser.ConfigParser(interpolation=None)
    cfgc["release"] = {"tag": tag, "checked_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(paths["current"], "w", encoding="utf-8") as f:
      cfgc.write(f)
  except OSError as e:
    log("err", f"Cannot write in {paths['root']}: {e}")
    return False
  log("ok", f"Release: {tag} ({len(info['assets'])} files)")
  check_cancel(cancel)

  # 2. download what is missing
  picked, missing = _pick_release_assets(info["assets"], target, need)
  ok = True
  for name in missing:
    log("err", f"'{name}' not found among the files of release {tag}.")
    ok = False
  for a, required in picked:
    check_cancel(cancel)
    path = os.path.join(dest, a["name"])
    if _file_ok(path, a["size"], a["sha256"]):
      log("ok", f"{a['name']}: already downloaded")
    elif _download_file(a["url"], path, a["size"], a["sha256"], log, cancel, a["name"]):
      log("ok", f"{a['name']}: downloaded ({format_size(os.path.getsize(path))})")
    elif required:
      ok = False
    else:
      log("warn", f"{a['name']} skipped.")
    if os.name != "nt" and os.path.isfile(path) and a["name"].lower().startswith(RELINKER_PREFIX):
      try:
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
      except OSError:
        pass

  # 3. unwrap_self.py: not a release asset, taken from tools/ of the tag
  if "unwrap" in need and not os.path.isfile(os.path.join(dest, UNWRAP_SCRIPT_NAME)):
    check_cancel(cancel)
    rel = "/".join(REPO_UNWRAP_REL)
    out = os.path.join(dest, UNWRAP_SCRIPT_NAME)
    got = None
    # the tag may not contain tools/unwrap_self.py (404): fall back to the repository's default branch
    for ref, desc in ((tag, tag), ("HEAD", "the default branch")):
      if _download_file(RAW_URL % (owner, repo, ref, rel), out, 0, "", log, cancel,
                        rel + " @ " + ref, quiet=True):
        got = desc
        break
    if got:
      if got == tag:
        log("ok", f"{UNWRAP_SCRIPT_NAME}: taken from tools/ of {tag}")
      else:
        log("warn", f"tools/{UNWRAP_SCRIPT_NAME} is not in {tag}: taken from {got} of the repository.")
    elif unwrapper_status():
      log("warn", f"{UNWRAP_SCRIPT_NAME} not available in the repository: using the one in {UTILITIES_DIR_NAME}.")
    else:
      log("err", f"{UNWRAP_SCRIPT_NAME} not found in release {tag} nor in the repository: "
                 f"put it in the {UTILITIES_DIR_NAME} folder.")
      ok = False
  return ok


# ─────────────────────────────────────────────────────────────
#  Relinker built from a GitHub repository (clone + CMake)
# ─────────────────────────────────────────────────────────────
_REPO_URL_RE = re.compile(r"^(?:https?|ssh|git|file)://\S+$|^[\w.-]+@[\w.-]+:\S+$", re.I)
_REPO_REF_RE = re.compile(r"^[\w][\w./+-]*$")


def normalize_repo_url(text):
  """Repository URL to use, or None if it is not valid. Empty -> the official AnyPS5 repo.
  Accepts full URLs, 'github.com/owner/repo' and the 'owner/repo' shorthand."""
  url = (text or "").strip().strip('"')
  if not url:
    return DEFAULT_REPO_URL
  if re.match(r"^[\w.-]+/[\w.-]+$", url):
    url = "https://github.com/" + url
  elif re.match(r"^github\.com/\S+$", url, re.I):
    url = "https://" + url
  return url if _REPO_URL_RE.match(url) else None


def repo_slug(url, ref):
  base = re.sub(r"\.git$", "", url.rstrip("/"))
  tail = "_".join(p for p in re.split(r"[/:]", base)[-2:] if p)
  tail = re.sub(r"[^\w.-]", "_", tail)[:60] or "repo"
  return "%s-%s" % (tail, hashlib.sha1((url + "\0" + ref).encode("utf-8")).hexdigest()[:8])


def source_paths(rl):
  url = normalize_repo_url(rl.get("repo_url", ""))
  if not url:
    return None
  ref = rl.get("repo_ref", "").strip()
  root = os.path.join(UTILITIES_DIR, SOURCE_DIR_NAME, repo_slug(url, ref))
  return {"url": url, "ref": ref, "root": root, "repo": os.path.join(root, "repo"),
          "build": os.path.join(root, "build"), "info": os.path.join(root, SOURCE_INFO_NAME),
          "mark": os.path.join(root, SOURCE_CLONED_MARK)}


def read_source_build(paths):
  """{'commit', 'target', 'binary'} of the last successful build, or None."""
  cfg = configparser.ConfigParser(interpolation=None)
  try:
    cfg.read(paths["info"], encoding="utf-8")
  except Exception:
    return None
  commit = cfg.get("build", "commit", fallback="")
  rel = cfg.get("build", "binary", fallback="")
  if not commit or not rel:
    return None
  binary = os.path.join(paths["root"], *rel.split("/"))
  if not os.path.isfile(binary):
    return None
  return {"commit": commit, "target": cfg.get("build", "target", fallback=""), "binary": binary}


def write_source_build(paths, commit, target, binary):
  cfg = configparser.ConfigParser(interpolation=None)
  cfg["build"] = {"commit": commit, "target": target,
                  "binary": os.path.relpath(binary, paths["root"]).replace("\\", "/"),
                  "built_at": time.strftime("%Y-%m-%d %H:%M:%S")}
  try:
    with open(paths["info"], "w", encoding="utf-8") as f:
      cfg.write(f)
  except OSError:
    pass


def resolve_relinker(rl):
  """Relinker that would be used right now, without building anything (None if unavailable)."""
  if rl.get("relinker_source") == "github":
    paths = source_paths(rl)
    built = read_source_build(paths) if paths else None
    return built["binary"] if built else None
  return find_relinker()


def source_preflight(rl):
  """None if a source build can be attempted, otherwise the reason why not."""
  refresh_tool_paths()
  if not normalize_repo_url(rl.get("repo_url", "")):
    return ("Invalid repository URL. Use e.g. https://github.com/owner/repo, "
            "github.com/owner/repo or owner/repo.")
  ref = rl.get("repo_ref", "").strip()
  if ref and not _REPO_REF_RE.match(ref):
    return "Invalid branch/tag name: %s" % ref
  missing = [t for t in ("git", "cmake") if not shutil.which(t)]
  if missing:
    return ("%s not found in PATH. Building from source needs git, cmake and a C/C++ compiler "
            "(Visual Studio Build Tools or MinGW on Windows, build-essential on Linux, "
            "Xcode command line tools on macOS)." % " and ".join(missing))
  return None


def relinker_status(rl):
  """(text, level) for the status panel."""
  if rl.get("relinker_source") == "release":
    cur = read_release_current(rl)
    path = find_relinker()
    if path and cur:
      return f"{os.path.basename(path)} · release {cur['tag']}  ✔", "ok"
    return "will be downloaded from the GitHub release  ⚠", "warn"
  if rl.get("relinker_source") != "github":
    path = find_relinker()
    if path:
      return os.path.basename(path) + "  ✔", "ok"
    return f"not found in {UTILITIES_DIR_NAME}  ✖", "err"
  problem = source_preflight(rl)
  if problem:
    return ("invalid repository URL  ✖" if problem.startswith("Invalid") else
            "git / cmake not found  ✖"), "err"
  paths = source_paths(rl)
  repo = re.sub(r"^https?://(www\.)?", "", re.sub(r"\.git$", "", paths["url"]))
  built = read_source_build(paths)
  if built:
    return f"{repo} · built from {built['commit'][:7]}  ✔", "ok"
  return f"{repo} · will be built when needed  ⚠", "warn"


def _rmtree_force(path):
  def onerror(func, p, _exc):
    try:
      os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
      func(p)
    except OSError:
      pass
  shutil.rmtree(path, onerror=onerror)


def _capture(cmd, cwd=None, env=None):
  """Run a short command, returns (exit code, stdout). Exit code -1 if it cannot start."""
  kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
  try:
    res = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=120, **kwargs)
  except (OSError, subprocess.SubprocessError):
    return -1, ""
  return res.returncode, (res.stdout or "").strip()


def _log_tail(holder, log, count=20):
  log("step", "Last output")
  for line in holder.get("tail", [])[-count:]:
    log("err" if re.search(r"error|fatal|fail|cannot|unable|missing|undefined", line, re.I) else "info", line)


def _detect_mingw(env):
  """Windows only. The project supports ONLY WinLibs GCC 15.2.0 (see docs/dev/BUILD.md): the GUI
  uses the copy it installed in Utilities/toolchain. Returns (cmake arguments, note); arguments are []
  if it is not installed yet (the GUI then offers to download it)."""
  d = winlibs_bin()
  if not d:
    return [], "MinGW-w64 GCC 15.2.0 (WinLibs) not installed"
  env["PATH"] = d + os.pathsep + env.get("PATH", "")
  cert = _ca_bundle()
  if cert and not env.get("SSL_CERT_FILE"):
    env["SSL_CERT_FILE"] = cert
  gcc, gpp = os.path.join(d, "gcc.exe"), os.path.join(d, "g++.exe")
  gen = ["-G", "Ninja"] if os.path.isfile(os.path.join(d, "ninja.exe")) else ["-G", "MinGW Makefiles"]
  return gen + ["-DCMAKE_C_COMPILER=" + gcc, "-DCMAKE_CXX_COMPILER=" + gpp], "WinLibs GCC 15.2.0 in " + d


# ─────────────────────────────────────────────────────────────
#  Build tools: detection and automatic installation (Windows: winget for Git/CMake, WinLibs download for GCC)
# ─────────────────────────────────────────────────────────────
TOOL_NAMES = {"git": "Git", "cmake": "CMake", "mingw": "MinGW-w64 GCC 15.2.0 (WinLibs, with Ninja)"}
# docs/dev/BUILD.md: on Windows ONLY this toolchain is supported (newer GCC such as MSYS2's 16.x selects
# SjLj exceptions and the project's DWARF check fails).
WINLIBS_TAG = "15.2.0posix-14.0.0-ucrt-r7"
WINLIBS_ZIP = "winlibs-x86_64-posix-seh-gcc-15.2.0-mingw-w64ucrt-14.0.0-r7.zip"
WINLIBS_URL = "https://github.com/brechtsanders/winlibs_mingw/releases/download/%s/%s" % (WINLIBS_TAG, WINLIBS_ZIP)
WINLIBS_DIR = os.path.join(UTILITIES_DIR, "toolchain", "winlibs-" + WINLIBS_TAG)


def winlibs_bin():
  """mingw64/bin of the WinLibs GCC 15.2.0 installed by the GUI, or None."""
  d = os.path.join(WINLIBS_DIR, "mingw64", "bin")
  return d if os.path.isfile(os.path.join(d, "g++.exe")) else None


def _ca_bundle():
  """CA bundle for CMake's downloads (BUILD.md: the WinLibs CMake fails with status 60 without it)."""
  for d in (os.environ.get("ProgramFiles", r"C:\Program Files"), r"C:\Program Files"):
    p = os.path.join(d, "Git", "mingw64", "etc", "ssl", "certs", "ca-bundle.crt")
    if os.path.isfile(p):
      return p
  return None


def refresh_tool_paths():
  """Freshly installed tools are not in the PATH of this running process: add the usual
  install folders (at the end, so the user's own PATH keeps priority)."""
  if os.name != "nt":
    return
  local = os.environ.get("LOCALAPPDATA", "")
  for d in (r"C:\Program Files\Git\cmd", r"C:\Program Files\CMake\bin", r"C:\Program Files (x86)\CMake\bin",
            os.path.join(local, "Programs", "Git", "cmd") if local else ""):
    if d and os.path.isdir(d) and d.lower() not in os.environ.get("PATH", "").lower():
      os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + d


def missing_build_tools(rl):
  """Keys of TOOL_NAMES that a source build needs and that are not available (empty = all there)."""
  refresh_tool_paths()
  missing = [t for t in ("git", "cmake") if not shutil.which(t)]
  if os.name == "nt":
    try:
      extra = shlex.split(rl.get("repo_cmake_args", ""), posix=False)
    except ValueError:
      extra = []
    if "-G" not in extra and not any(a.startswith("-DCMAKE_CXX_COMPILER") for a in extra):
      if not _detect_mingw(dict(os.environ))[0]:
        missing.append("mingw")
  return missing


def install_winlibs(log, cancel):
  """Download the WinLibs GCC 15.2.0 zip (~270 MB) from GitHub and unpack it in Utilities/toolchain."""
  import urllib.request
  if winlibs_bin():
    return True
  os.makedirs(os.path.dirname(WINLIBS_DIR), exist_ok=True)
  zpath = WINLIBS_DIR + ".zip"
  tmp = WINLIBS_DIR + ".part"
  log("step", "Downloading MinGW-w64 GCC 15.2.0 (WinLibs, ~270 MB)")
  log("detail", WINLIBS_URL)
  try:
    req = urllib.request.Request(WINLIBS_URL, headers={"User-Agent": "AnyPS5-GUI"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(zpath, "wb") as out:
      total = int(resp.headers.get("Content-Length") or 0)
      done, last = 0, 0.0
      while True:
        check_cancel(cancel)
        chunk = resp.read(1 << 20)
        if not chunk:
          break
        out.write(chunk)
        done += len(chunk)
        if time.time() - last > 3:
          last = time.time()
          log("info", "Download: %d / %d MiB" % (done >> 20, total >> 20) if total else "Download: %d MiB" % (done >> 20))
  except (OSError, ValueError) as e:  # URLError/HTTPError/timeouts are OSError
    log("err", "Download failed: %s. Check your connection, or download %s by hand from "
               "github.com/brechtsanders/winlibs_mingw (release %s) and unzip it so that "
               "'mingw64' ends up in %s" % (e, WINLIBS_ZIP, WINLIBS_TAG, WINLIBS_DIR))
    return False
  log("step", "Unpacking the toolchain")
  try:
    if os.path.isdir(tmp):
      _rmtree_force(tmp)
    with zipfile.ZipFile(zpath) as zf:
      members = zf.namelist()
      for i, name in enumerate(members):
        if i % 500 == 0:
          check_cancel(cancel)
        zf.extract(name, tmp)
    if os.path.isdir(WINLIBS_DIR):
      _rmtree_force(WINLIBS_DIR)
    os.replace(tmp, WINLIBS_DIR)
  except (OSError, zipfile.BadZipFile) as e:
    log("err", "Unpacking failed: %s (the downloaded zip may be corrupt: it will be downloaded again)" % e)
    _remove_quiet(zpath)
    return False
  _remove_quiet(zpath)
  if not winlibs_bin():
    log("err", "g++.exe not found in the unpacked toolchain: %s" % WINLIBS_DIR)
    return False
  log("ok", "Toolchain installed in " + WINLIBS_DIR)
  return True


def install_build_tools(missing, log, cancel, holder):
  """Install the missing tools: git and cmake with winget, GCC 15.2.0 + Ninja (WinLibs) by direct download.
  Returns True if everything is available afterwards."""
  if os.name != "nt":
    log("err", "Automatic installation is only available on Windows: install git, cmake and a C/C++ "
               "compiler with your package manager (e.g. sudo apt install git cmake build-essential).")
    return False
  winget = shutil.which("winget")
  if not winget:
    log("err", "winget not found (it comes with 'App Installer' from the Microsoft Store on Windows 10/11). "
               "Install by hand Git (git-scm.com) and CMake (cmake.org); the GCC 15.2.0 toolchain is "
               "downloaded by the GUI itself.")
    return False

  def winget_install(pkg):
    cmd = [winget, "install", "--id", pkg, "-e", "--silent",
           "--accept-package-agreements", "--accept-source-agreements"]
    code = stream_process(cmd, log, cancel, holder, "winget " + pkg)
    if code != 0:  # also returned when it is already installed: the check below decides
      log("detail", f"winget exited with code {code} for {pkg}")

  log("step", "Installing the missing tools: " + ", ".join(TOOL_NAMES[t] for t in missing))
  log("info", "Windows may ask for administrator permission: accept it. This can take several minutes.")
  if "git" in missing:
    winget_install("Git.Git")
    check_cancel(cancel)
  if "cmake" in missing:
    winget_install("Kitware.CMake")
    check_cancel(cancel)
  if "mingw" in missing:
    if not install_winlibs(log, cancel):
      return False
    check_cancel(cancel)
  refresh_tool_paths()
  still = missing_build_tools({})
  if still:
    log("err", "Still missing: " + ", ".join(TOOL_NAMES[t] for t in still) +
               ". Close and reopen the GUI (so Windows refreshes the PATH) and retry.")
    return False
  log("ok", "All build tools are available.")
  return True


def _log_build_errors(errors, logfile, log):
  """Summary at the END of the log: the first distinct errors (the later ones are usually a
  cascade) and where the complete output is saved."""
  log("step", "Build errors")
  seen = []
  for line in errors:
    if line not in seen:
      seen.append(line)
  if not seen:
    log("warn", "No error line recognised: open the full build log below.")
  for line in seen[:12]:
    log("err", line)
  if len(seen) > 12:
    log("detail", f"... and {len(seen) - 12} more error lines")
  text = "\n".join(seen)
  if re.search(r"CVT1100|LNK1123|LNK2019|LNK2001", text):
    log("detail", "Hint: this project is written for MinGW-w64 (GCC): Visual Studio compiles it but cannot "
                  "link it. Install the MinGW-w64 GCC 15.2.0 (WinLibs) toolchain: the GUI "
                  "downloads it by itself when you run Build now again.")
  if re.search(r"SjLj|DWARF unwinding|ANYPS5_MINGW_DWARF", text):
    log("detail", "Hint: wrong MinGW-w64 version. docs/dev/BUILD.md supports only WinLibs GCC 15.2.0 on "
                  "Windows. Remove other MinGW folders from your PATH and run Build now again: the GUI "
                  "downloads and uses its own copy.")
  if re.search(r"C2589|C2059|C2062|C2143", text) and re.search(r"[(]\s*\d+\s*,\s*\d+\s*[)]|[(]\d+[)]", text):
    log("detail", "Hint: 'token ( ...' errors on Windows are usually the min/max macros of windows.h "
                  "(fixed by -DNOMINMAX, which the GUI already passes) or code written for "
                  "another compiler: try MinGW/clang (see 'Extra CMake arguments').")
  log("info", f"Full build log: {logfile}")


def find_built_relinker(search_dirs, hint, newer_than=0.0):
  """Executable whose name contains `hint` inside the given folders (newest, exact name first).
  Only files modified after `newer_than` count for the folders flagged as (dir, True)."""
  hint = hint.lower()
  best = None
  skip = {"CMakeFiles", "_deps", ".git", "3rdparty", "demo", "docs", "node_modules"}
  for folder, check_time in search_dirs:
    for root, dirs, files in os.walk(folder):
      dirs[:] = [d for d in dirs if d not in skip]
      for f in files:
        low = f.lower()
        if hint not in low or "test" in low:
          continue
        path = os.path.join(root, f)
        if os.name == "nt":
          if not low.endswith(".exe"):
            continue
        elif "." in f or not os.access(path, os.X_OK):
          continue
        try:
          mtime = os.path.getmtime(path)
        except OSError:
          continue
        if check_time and mtime < newer_than:
          continue
        stem = os.path.splitext(low)[0]
        key = (0 if stem == hint else 1, -mtime, len(path))
        if best is None or key < best[0]:
          best = (key, path)
    if best:
      return best[1]  # the first folder that yields something wins
  return None


def ensure_source_repo(rl, log, cancel, holder):
  """Clone/update ONLY the repository (no build). Used when the unwrapper is needed from the repo
  (tools/unwrap_self.py) without building the relinker. Returns True if the repo is ready."""
  refresh_tool_paths()
  if not normalize_repo_url(rl.get("repo_url", "")):
    log("err", "Invalid repository URL.")
    return False
  if not shutil.which("git"):
    log("err", "git not found in PATH: it is needed to download the repository.")
    return False
  p = source_paths(rl)
  repo = p["repo"]
  env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
  os.makedirs(p["root"], exist_ok=True)
  if not os.path.isfile(p["mark"]):
    if os.path.isdir(repo):
      _rmtree_force(repo)
    log("step", "Cloning the repository (to get %s)" % "/".join(REPO_UNWRAP_REL))
    cmd = ["git", "clone", "--recurse-submodules", "--shallow-submodules", "--depth", "1"]
    if p["ref"]:
      cmd += ["--branch", p["ref"]]
    cmd += ["--", p["url"], repo]
    code = stream_process(cmd, log, cancel, holder, "git clone", env=env)
    if code != 0:
      log("err", f"git clone failed (exit code {code}): check the URL, the branch/tag and your connection.")
      if os.path.isdir(repo):
        _rmtree_force(repo)
      return False
    try:
      open(p["mark"], "w").close()
    except OSError:
      pass
  elif yes(rl.get("repo_update", "yes")):
    log("step", "Updating the repository")
    for cmd, label in (([ "git", "fetch", "--depth", "1", "origin", p["ref"] or "HEAD"], "git fetch"),
                       (["git", "reset", "--hard", "FETCH_HEAD"], "git reset")):
      code = stream_process(cmd, log, cancel, holder, label, cwd=repo, env=env)
      if code != 0:
        log("warn", f"{label} failed (exit code {code}): using the existing checkout.")
        break
  return os.path.isdir(repo)


LIBS_BUILD_DIR_NAME = "build-libs"
LIBS_OUT_NAME = "libs-out"
LIBS_INFO_NAME = "build_libs.ini"
MINGW_RUNTIME_DLLS = ("libgcc_s_seh-1.dll", "libstdc++-6.dll", "libwinpthread-1.dll")


def read_built_libs(paths, target=None):
  """Info of the libs built from the repo (same shape as libs_zip_info, with 'dir'), or None.
  The .prx are the same for every platform; the MinGW runtime DLLs are added ONLY when the
  selected target is Windows (and the WinLibs toolchain installed by the GUI is present)."""
  if not paths:
    return None
  out = os.path.join(paths["root"], LIBS_OUT_NAME)
  if not os.path.isfile(os.path.join(paths["root"], LIBS_INFO_NAME)) or not os.path.isdir(out):
    return None
  entries = []
  for r, _d, fs in os.walk(out):
    for f in fs:
      if f.lower().endswith(".dll"):
        continue  # DLLs left by older builds: they are added below, per target
      full = os.path.join(r, f)
      rel = os.path.relpath(full, out).replace("\\", "/")
      entries.append((full, rel, os.path.getsize(full), 0))
  if not entries:
    return None
  if target == "windows":
    d = winlibs_bin()
    if d:
      for dll in MINGW_RUNTIME_DLLS:
        full = os.path.join(d, dll)
        if os.path.isfile(full):
          entries.append((full, dll, os.path.getsize(full), 0))
  return {"zip": None, "dir": out, "name": LIBS_DIR_NAME, "entries": entries,
          "size": sum(e[2] for e in entries)}


def predownload_ffmpeg(repo, build, env, log, cancel):
  """CMake downloads FFmpeg with its own HTTP client, which can fail ("Error in the HTTP2 framing
  layer"). The zip is fetched here with Python (with retries) into the exact place CMake looks
  for it (<build>/externals/<sha>-<name>): if it is there, CMake skips its download."""
  import platform
  machine = platform.machine().lower()
  arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
  if sys.platform.startswith("win"):
    osname = "mingw"  # the GUI builds with MinGW on Windows
  elif sys.platform == "darwin":
    osname = "macos"
  else:
    osname = "linux"
  name = "ffmpeg-%s-%s.zip" % (osname, arch)
  code, sha = _capture(["git", "rev-parse", "--short=12", "HEAD"],
                       cwd=os.path.join(repo, "3rdparty", "ffmpeg-core"), env=env)
  if code != 0 or not sha:
    log("warn", "FFmpeg commit not found in 3rdparty/ffmpeg-core: leaving the download to CMake.")
    return
  ext = os.path.join(build, "externals")
  dest = os.path.join(ext, "%s-%s" % (sha, name))
  if os.path.isfile(dest):
    return
  url = "https://github.com/KytyPS5/ext-ffmpeg-core/releases/download/%s/%s" % (sha, name)
  os.makedirs(ext, exist_ok=True)
  log("step", "Downloading FFmpeg (%s)" % name)
  log("detail", url)
  tmp = dest + ".part"
  last = None
  for attempt in range(1, 4):
    check_cancel(cancel)
    try:
      req = urllib.request.Request(url, headers={"User-Agent": "AnyPS5-GUI"})
      with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        while True:
          check_cancel(cancel)
          chunk = r.read(1 << 20)
          if not chunk:
            break
          f.write(chunk)
      os.replace(tmp, dest)
      log("ok", "FFmpeg downloaded (%.1f MiB)" % (os.path.getsize(dest) / 1048576.0))
      return
    except Cancelled:
      raise
    except Exception as e:
      last = e
      log("warn", "FFmpeg download, attempt %d/3 failed: %s" % (attempt, e))
      time.sleep(2 * attempt)
  try:
    os.remove(tmp)
  except OSError:
    pass
  log("warn", "Could not download FFmpeg (%s): CMake will try on its own." % last)


def ensure_source_libs(rl, log, cancel, holder, target=None):
  """Build the 'libs' CMake target of the cloned repo (full configuration: the relinker-only
  one does not include core/libs) and collect the .prx files (+ the MinGW runtime DLLs on
  Windows target) in Utilities/source/<repo>/libs-out. Reuses the previous build if the commit is
  the same. The .prx are platform-independent; `target` only decides whether the MinGW runtime
  DLLs are included in the returned info. Returns the libs info or None."""
  p = source_paths(rl)
  if not p or not os.path.isdir(p["repo"]):
    if not ensure_source_repo(rl, log, cancel, holder):
      return None
  repo, root = p["repo"], p["root"]
  build = os.path.join(root, LIBS_BUILD_DIR_NAME)
  info_path = os.path.join(root, LIBS_INFO_NAME)
  out = os.path.join(root, LIBS_OUT_NAME)
  env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
  code, commit = _capture(["git", "rev-parse", "HEAD"], cwd=repo, env=env)
  commit = commit if code == 0 and commit else "unknown"
  cfgp = configparser.ConfigParser(interpolation=None)
  try:
    cfgp.read(info_path, encoding="utf-8")
  except Exception:
    pass
  if cfgp.get("build", "commit", fallback="") == commit and read_built_libs(p):
    log("ok", f"Libs already built from commit {commit[:12]}")
    return read_built_libs(p, target)
  try:
    os.remove(info_path)
  except OSError:
    pass
  logfile = os.path.join(root, "build-libs.log")
  try:
    open(logfile, "w").close()
  except OSError:
    logfile = None
  if os.name == "nt":
    env["CXXFLAGS"] = (env.get("CXXFLAGS", "") + " -DNOMINMAX").strip()
  try:
    extra_args = shlex.split(rl.get("repo_cmake_args", ""), posix=(os.name != "nt"))
  except ValueError:
    log("err", "Invalid 'Extra CMake arguments' (unbalanced quotes).")
    return None
  extra_args = [a for a in extra_args if not a.startswith("-DANYPS5_RELINKER_ONLY")]
  if os.name == "nt" and "-G" not in extra_args and not any(a.startswith("-DCMAKE_CXX_COMPILER") for a in extra_args):
    mingw_args, note = _detect_mingw(env)
    if mingw_args:
      log("ok", "Toolchain: " + note)
      extra_args = mingw_args + extra_args
    else:
      log("warn", note + ": the build will probably fail.")
  errors = []
  log("step", "Configuring the full build (CMake, Release) - FFmpeg is downloaded here")
  env_key = env.get("CXXFLAGS", "") + "|" + " ".join(extra_args)
  env_mark = os.path.join(build, ".manager_env")
  try:
    old_env = open(env_mark, encoding="utf-8").read() if os.path.isfile(env_mark) else None
  except OSError:
    old_env = None
  if os.path.isdir(build) and old_env != env_key:
    _rmtree_force(build)
  os.makedirs(build, exist_ok=True)
  try:
    with open(env_mark, "w", encoding="utf-8") as fh:
      fh.write(env_key)
  except OSError:
    pass
  predownload_ffmpeg(repo, build, env, log, cancel)
  cfg_cmd = ["cmake", "-S", repo, "-B", build, "-DCMAKE_BUILD_TYPE=Release"] + extra_args
  for attempt in range(1, 4):
    del errors[:]
    code = stream_process(cfg_cmd, log, cancel, holder, "cmake", env=env, errors=errors, logfile=logfile)
    if code == 0:
      break
    text = "\n".join(errors) + "\n" + "\n".join(holder.get("tail", []))
    # Windows: ninja cannot rewrite its .ninja_deps/.ninja_log while an antivirus, a sync client
    # (OneDrive on Desktop) or a leftover process holds them. Drop them (they are only caches) and retry.
    if attempt < 3 and re.search(r"failed recompaction|Permission denied", text, re.I):
      log("warn", f"ninja could not update its cache files (attempt {attempt}/3): retrying.")
      for name in (".ninja_deps", ".ninja_log"):
        try:
          os.remove(os.path.join(build, name))
        except OSError:
          pass
      time.sleep(3 * attempt)
      check_cancel(cancel)
      continue
    break
  if code != 0:
    log("err", f"CMake configuration of the full build failed (exit code {code}).")
    _log_build_errors(errors, logfile, log)
    return None
  check_cancel(cancel)
  log("step", "Building target 'libs' - this can take a long time")
  kill_stale_build_processes()
  del errors[:]
  build_cmd = ["cmake", "--build", build, "--config", "Release", "--target", "libs",
               "--parallel", str(build_parallel_jobs())]
  for attempt in range(1, 4):
    del errors[:]
    code = stream_process(build_cmd, log, cancel, holder, "cmake --build", env=env, errors=errors,
                          logfile=logfile, idle_timeout=1200)
    if code == 0:
      break
    text = "\n".join(errors) + "\n" + "\n".join(holder.get("tail", []))
    if attempt < 3 and re.search(r"failed recompaction", text, re.I):
      log("warn", f"ninja could not update its cache files (attempt {attempt}/3): retrying.")
      for name in (".ninja_deps", ".ninja_log"):
        try:
          os.remove(os.path.join(build, name))
        except OSError:
          pass
      time.sleep(3 * attempt)
      check_cancel(cancel)
      continue
    break
  if code != 0:
    log("err", f"Build of the libs failed (exit code {code}).")
    _log_build_errors(errors, logfile, log)
    return None
  # output of core/libs/CMakeLists.txt: <build>/core/libs/libs/*.prx (PATCHED_DIR)
  src = os.path.join(build, "core", "libs", "libs")
  if not os.path.isdir(src):
    log("err", "Build finished but the folder core/libs/libs was not found in the build tree.")
    return None
  if os.path.isdir(out):
    _rmtree_force(out)
  os.makedirs(out, exist_ok=True)
  # same selection as the official tools/package_release.py: the .prx that match the folders of
  # core/libs/prx (nothing else from the build folder)
  prx_root = os.path.join(repo, "core", "libs", "prx")
  expected = {d + ".prx" for d in os.listdir(prx_root)
              if os.path.isdir(os.path.join(prx_root, d)) and not d.startswith(".")} \
    if os.path.isdir(prx_root) else None
  count = 0
  built = set()
  for name in os.listdir(src):
    full = os.path.join(src, name)
    if os.path.isfile(full) and name.lower().endswith(".prx") and (expected is None or name in expected):
      shutil.copy2(full, os.path.join(out, name))
      built.add(name)
      count += 1
  if expected:
    missing = sorted(expected - built)
    if missing:
      log("err", "Missing patched libraries after the build: " + ", ".join(missing[:10])
          + (" ..." if len(missing) > 10 else ""))
      return None
  if count == 0:
    log("err", "The libs folder is empty after the build.")
    return None
  cfgp = configparser.ConfigParser(interpolation=None)
  cfgp["build"] = {"commit": commit, "built_at": time.strftime("%Y-%m-%d %H:%M:%S")}
  try:
    with open(info_path, "w", encoding="utf-8") as fh:
      cfgp.write(fh)
  except OSError:
    pass
  log("ok", f"Libs built: {count} files in {out}")
  return read_built_libs(p, target)


def ensure_repo_unwrapper(rl, log, cancel, holder):
  """GitHub option active: make sure tools/unwrap_self.py of the repo is available (clone if
  needed). Returns True if an unwrapper can be used (repo copy, or Utilities/ as fallback)."""
  set_active_relinker_cfg(rl)
  if rl.get("relinker_source") != "github":
    return unwrapper_status() is not None
  if repo_unwrapper_path(rl) is None:
    ensure_source_repo(rl, log, cancel, holder)
  path = repo_unwrapper_path(rl)
  if path:
    log("ok", "%s taken from the repository: %s" % (UNWRAP_SCRIPT_NAME, "/".join(REPO_UNWRAP_REL)))
    return True
  if unwrapper_status():
    log("warn", "tools/%s not found in the repository: using the one in %s." % (UNWRAP_SCRIPT_NAME, UTILITIES_DIR_NAME))
    return True
  log("err", "%s not found in the repository (tools/) nor in the %s folder." % (UNWRAP_SCRIPT_NAME, UTILITIES_DIR_NAME))
  return False


def ensure_source_relinker(rl, log, cancel, holder):
  """Clone/update the repository and build the relinker with CMake. Reuses the previous build
  when the commit did not change. Returns the path of the executable, or None on failure."""
  problem = source_preflight(rl)
  if problem:
    log("err", problem)
    return None
  p = source_paths(rl)
  repo, build = p["repo"], p["build"]
  env = dict(os.environ, GIT_TERMINAL_PROMPT="0")  # never wait for credentials
  update = yes(rl.get("repo_update", "yes"))
  logfile = os.path.join(p["root"], "build.log")
  try:
    os.makedirs(p["root"], exist_ok=True)
    open(logfile, "w").close()
  except OSError:
    logfile = None
  if os.name == "nt":  # windows.h defines min/max macros that break std::min / numeric_limits::max
    env["CXXFLAGS"] = (env.get("CXXFLAGS", "") + " -DNOMINMAX").strip()
  try:
    extra_args = shlex.split(rl.get("repo_cmake_args", ""), posix=(os.name != "nt"))
  except ValueError:
    log("err", "Invalid 'Extra CMake arguments' (unbalanced quotes).")
    return None
  errors = []
  if os.name == "nt" and "-G" not in extra_args and not any(a.startswith("-DCMAKE_CXX_COMPILER") for a in extra_args):
    mingw_args, note = _detect_mingw(env)
    if mingw_args:
      log("ok", "Toolchain: " + note + " (generator " + mingw_args[1] + ")")
      extra_args = mingw_args + extra_args
    else:
      log("warn", note + ": CMake will use Visual Studio, which cannot link this project "
                  "(see the hint if the build fails).")
  user_target = rl.get("repo_target", "").strip()
  if not any(a.startswith("-DANYPS5_RELINKER_ONLY") for a in extra_args):
    extra_args = extra_args + ["-DANYPS5_RELINKER_ONLY=ON"]  # BUILD.md: relinker without SDL/Vulkan/FFmpeg
  log("info", f"Repository: {p['url']}" + (f"  (branch/tag {p['ref']})" if p["ref"] else ""))
  log("detail", f"Working folder: {p['root']}")
  os.makedirs(p["root"], exist_ok=True)
  previous = read_source_build(p)

  # 1. clone or update
  if not os.path.isfile(p["mark"]):
    if os.path.isdir(repo):
      _rmtree_force(repo)  # incomplete clone from a previous attempt
    log("step", "Cloning the repository (with submodules)")
    cmd = ["git", "clone", "--recurse-submodules", "--shallow-submodules", "--depth", "1"]
    if p["ref"]:
      cmd += ["--branch", p["ref"]]
    cmd += ["--", p["url"], repo]
    code = stream_process(cmd, log, cancel, holder, "git clone", env=env)
    if code != 0:
      log("err", f"git clone failed (exit code {code}): check the URL, the branch/tag and your connection.")
      _log_tail(holder, log)
      if os.path.isdir(repo):
        _rmtree_force(repo)
      return None
    try:
      open(p["mark"], "w").close()
    except OSError:
      pass
  elif update:
    log("step", "Updating the repository")
    fetch = ["git", "fetch", "--depth", "1", "origin", p["ref"] or "HEAD"]
    steps = [(fetch, "git fetch"), (["git", "reset", "--hard", "FETCH_HEAD"], "git reset"),
             (["git", "submodule", "sync", "--recursive"], "git submodule sync"),
             (["git", "submodule", "update", "--init", "--recursive", "--depth", "1"], "git submodule update")]
    for cmd, label in steps:
      code = stream_process(cmd, log, cancel, holder, label, cwd=repo, env=env)
      if code != 0:
        if previous:
          log("warn", f"{label} failed (exit code {code}): using the previous build.")
          return previous["binary"]
        log("err", f"{label} failed (exit code {code}).")
        _log_tail(holder, log)
        return None
  else:
    log("info", "Repository update disabled: using the existing checkout.")
  check_cancel(cancel)

  # 2. structure
  if not os.path.isfile(os.path.join(repo, "CMakeLists.txt")):
    log("err", "No CMakeLists.txt in the repository root: this is not an AnyPS5-style project.")
    return None
  for name in ("core", "3rdparty", "cmake", "tools"):
    if not os.path.isdir(os.path.join(repo, name)):
      log("warn", f"Folder '{name}' not found in the repository (different layout than AnyPS5?).")
  code, commit = _capture(["git", "rev-parse", "HEAD"], cwd=repo, env=env)
  commit = commit if code == 0 and commit else "unknown"
  log("ok", f"Source at commit {commit[:12]}")

  # 3. already built?
  if previous and previous["commit"] == commit and previous["target"] == user_target:
    log("ok", f"Relinker already built from this commit: {os.path.basename(previous['binary'])}")
    return previous["binary"]

  # 4. configure + build
  log("step", "Configuring (CMake, Release)")
  # CXXFLAGS is only read on the first configure: if it changed, start from a clean build folder
  env_mark = os.path.join(build, ".manager_env")
  try:
    old_env = open(env_mark, encoding="utf-8").read() if os.path.isfile(env_mark) else None
  except OSError:
    old_env = None
  env_key = env.get("CXXFLAGS", "") + "|" + " ".join(extra_args)
  if os.path.isdir(build) and old_env != env_key:
    log("info", "Build settings changed: starting from a clean build folder.")
    _rmtree_force(build)
  os.makedirs(build, exist_ok=True)
  try:
    with open(env_mark, "w", encoding="utf-8") as fh:
      fh.write(env_key)
  except OSError:
    pass
  code = stream_process(["cmake", "-S", repo, "-B", build, "-DCMAKE_BUILD_TYPE=Release"] + extra_args,
                        log, cancel, holder, "cmake", env=env, errors=errors, logfile=logfile)
  if code != 0:
    log("err", f"CMake configuration failed (exit code {code}).")
    _log_build_errors(errors, logfile, log)
    return None
  check_cancel(cancel)

  started = time.time()
  base = ["cmake", "--build", build, "--config", "Release", "--parallel", str(build_parallel_jobs())]
  attempts = [user_target] if user_target else [RELINKER_TARGET_GUESS, ""]
  built_ok = False
  for target in attempts:
    log("step", f"Building ({'target ' + target if target else 'all targets'}) - this can take a few minutes")
    del errors[:]
    code = stream_process(base + (["--target", target] if target else []), log, cancel, holder,
                          "cmake --build", env=env, errors=errors, logfile=logfile)
    if code == 0:
      built_ok = True
      break
    tail = "\n".join(holder.get("tail", []))
    if target and not user_target and re.search(
        r"no rule to make target|unknown target|does not exist|invalid target", tail, re.I):
      log("warn", f"Target '{target}' does not exist in this project: building everything instead.")
      continue
    log("err", f"Build failed (exit code {code}).")
    _log_build_errors(errors, logfile, log)
    return None
  if not built_ok:
    return None

  # 5. find the executable
  hint = user_target or RELINKER_TARGET_GUESS
  binary = find_built_relinker([(build, False), (repo, True)], hint, started - 2)
  if not binary:
    log("err", f"Build finished but no executable named '*{hint}*' was found.")
    log("detail", "Set the CMake target in AnyPS5 options → Source build, or check the project output.")
    return None
  if os.name != "nt":
    try:
      os.chmod(binary, os.stat(binary).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
      pass
  write_source_build(p, commit, user_target, binary)
  log("ok", f"Relinker built: {binary}")
  return binary


# ─────────────────────────────────────────────────────────────
#  Relinker options
# ─────────────────────────────────────────────────────────────
def parse_address(text):
  text = text.strip()
  return int(text, 16) if text.lower().startswith("0x") else int(text)


def validate_relink_options(intel, rl):
  """Check the combinations forbidden by USAGE.md. Returns None or a message."""
  target = rl["target"]
  rosetta = yes(rl["rosetta"])
  if rosetta and target == "windows":
    return "--to-rosetta conflicts with --windows."
  for key, flag in (("gui", "--windows-gui"), ("diagnostics", "--windows-diagnostics"),
                    ("trace_imports", "--trace-imports")):
    if yes(rl[key]) and target != "windows":
      return f"{flag} requires the Windows target."
  if rl["fixed_base"].strip():
    if target != "windows":
      return "--windows-fixed-base requires the Windows target."
    try:
      addr = parse_address(rl["fixed_base"])
    except ValueError:
      return "--windows-fixed-base: invalid address (decimal or 0x...)."
    if addr % 0x10000 or not (0x20000 <= addr <= 0x7FFFFFFF0000):
      return "--windows-fixed-base: needs a 64 KiB-aligned address between 0x20000 and 0x7fffffff0000."
  if rl["unused_filter"] not in ("0", "1", "2"):
    return "unused-filter must be 0, 1 or 2."
  rpath = rl["rpath"].strip()
  if rpath and target == "windows" and not rpath.isascii():
    return "--rpath on Windows requires an ASCII path."
  if rpath and target == "linux" and not (rpath.startswith("/") or rpath.startswith("$ORIGIN")):
    return "--rpath on Linux requires an absolute path or one starting with $ORIGIN."
  skip = yes(rl.get("skip_sce_module", "no"))
  smp = rl.get("sce_module_path", "").strip()
  excl = split_list(rl.get("exclude_sce_modules", ""))
  if skip:
    if smp:
      return "--skip-sce-module conflicts with --sce-module-path."
    if rl.get("module_dirs", "").strip():
      return "--skip-sce-module conflicts with --module-dir."
    if rl.get("executables", "").strip():
      return "--skip-sce-module conflicts with --executable."
    if excl:
      return "--skip-sce-module conflicts with --exclude-sce-module."
  if smp and not os.path.isdir(os.path.abspath(smp)):
    return f"--sce-module-path: '{smp}' is not an existing folder."
  for name in excl:
    if "/" in name or "\\" in name:
      return f"--exclude-sce-module takes a file name, not a path: '{name}'."
  if yes(rl.get("lazy_binding", "no")) and yes(rl["trace_imports"]):
    return "--trace-imports conflicts with --lazy-binding."
  if yes(rl.get("autorun", "no")) and target != host_target():
    return ("--autorun needs the target system to be the one of this computer ("
            + TARGETS[host_target()]["label"].split(" (")[0] + ").")
  return None


def build_relink_command(relinker, intel, rl, input_file, out_exe):
  target = rl["target"]
  cmd = [relinker]
  if target == "windows":
    cmd.append("--windows")
    if yes(rl["gui"]):
      cmd.append("--windows-gui")
    if yes(rl["diagnostics"]):
      cmd.append("--windows-diagnostics")
    if rl["fixed_base"].strip():
      cmd += ["--windows-fixed-base", rl["fixed_base"].strip()]
    if yes(rl["trace_imports"]):
      cmd.append("--trace-imports")
  elif target == "macos":
    cmd.append("--macos")
  if yes(rl["rosetta"]):
    cmd.append("--to-rosetta")  # implies --to-intel
  elif intel:
    cmd.append("--to-intel")
  if rl["unused_filter"] != "0":
    cmd.append("unused-filter=" + rl["unused_filter"])
  if yes(rl["registry"]):
    cmd.append("--registry")
  if rl["rpath"].strip():
    cmd += ["--rpath", rl["rpath"].strip()]
  for d in split_list(rl["module_dirs"]):
    cmd += ["--module-dir", d]
  for e in split_list(rl["executables"]):
    cmd += ["--executable", e]
  if rl.get("sce_module_path", "").strip():
    cmd += ["--sce-module-path", os.path.abspath(rl["sce_module_path"].strip())]
  if yes(rl.get("skip_sce_module", "no")):
    cmd.append("--skip-sce-module")
  for name in split_list(rl.get("exclude_sce_modules", "")):
    cmd += ["--exclude-sce-module", name]
  for key, flag in (("skip_syscall_check", "--skip-syscall-check"),
                    ("trap_syscall_stubs", "--trap-sdk-syscall-stubs"),
                    ("lazy_binding", "--lazy-binding"),
                    ("autorun", "--autorun")):
    if yes(rl.get(key, "no")):
      cmd.append(flag)
  cmd += [input_file, out_exe]
  return cmd


# ─────────────────────────────────────────────────────────────
#  Runtime: environment variables and game shortcut
# ─────────────────────────────────────────────────────────────
PAD_HELPER = os.path.join("tools", "pad_input.py")


def _whole(text):
  return re.fullmatch(r"[0-9]+", text.strip()) is not None


def validate_runtime_options(rt, rl=None):
  """Returns None if the Runtime settings are valid, otherwise a message.
  rl (relinker options) is optional: it enables the checks that depend on them."""
  pad = rt.get("pad_input_file", "").strip()
  if pad and not os.path.isabs(pad):
    return "APS5_PAD_INPUT_FILE must be an absolute path."
  lang = rt.get("language", "").strip()
  if lang and not (_whole(lang) and int(lang) <= 30):
    return "ANYPS5_LANGUAGE must be a console language number from 0 to 30."
  if rl is not None and rt.get("trace_calls", "").strip() and rl.get("target") == "windows" \
      and not yes(rl.get("trace_imports", "no")):
    return "APS5_TRACE_CALLS needs --trace-imports (Conversion options): without it nothing is recorded."
  prio = rt.get("thread_priority", "").strip()
  if prio and prio not in ("off", "high", "rt"):
    return "APS5_THREAD_PRIORITY must be off, high or rt."
  nice = rt.get("thread_nice", "").strip()
  if nice and not re.fullmatch(r"-?[0-9]+", nice):
    return "APS5_THREAD_NICE must be a whole number (default -10)."
  rtp = rt.get("thread_rt", "").strip()
  if rtp and not _whole(rtp):
    return "APS5_THREAD_RT must be a whole number (default 10)."
  frames = rt.get("trace_frames", "").strip()
  seconds = rt.get("trace_seconds", "").strip()
  if frames and seconds:
    return "APS5_TRACE_FRAMES and APS5_TRACE_SECONDS cannot be used together."
  if frames and not re.fullmatch(r"[0-9]+:[0-9]+", frames):
    return "APS5_TRACE_FRAMES must be <start>:<count>, whole numbers."
  if seconds:
    if not re.fullmatch(r"[0-9]+:[0-9]+", seconds) or int(seconds.split(":")[1]) < 1:
      return "APS5_TRACE_SECONDS must be <start>:<length>, whole numbers, length above 0."
  every = rt.get("trace_summary_every", "").strip()
  if every and not (_whole(every) and int(every) >= 1):
    return "APS5_TRACE_SUMMARY_EVERY must be a whole number above 0."
  events = rt.get("trace_events", "").strip()
  if events and not (_whole(events) and 1 <= int(events) <= 4194304):
    return "APS5_TRACE_EVENTS must be a whole number from 1 to 4194304."
  tdir = rt.get("trace_dir", "").strip()
  if tdir and not os.path.isabs(tdir):
    return "APS5_TRACE_DIR must be an absolute path."
  return None


def find_pad_helper(rl):
  """tools/pad_input.py in Utilities/tools or in the cloned repository, else None."""
  candidates = [os.path.join(UTILITIES_DIR, PAD_HELPER)]
  paths = source_paths(rl) if rl.get("relinker_source") == "github" else None
  if paths:
    candidates.append(os.path.join(paths["repo"], PAD_HELPER))
  for c in candidates:
    if os.path.isfile(c):
      return c
  return None


def sync_input_ini(pkg_dir, rt, log=None):
  """Copy Utilities/anyps5-input.ini over the ini next to the game executable (pkg_dir).
  Called at every launch, so the game always uses the CURRENT Input tab settings."""
  if not yes(rt.get("copy_input_ini", "yes")) or not os.path.isfile(INPUT_INI_FILE):
    return False
  try:
    with open(INPUT_INI_FILE, encoding="utf-8-sig") as f:
      errs = validate_input_ini(f.read())
    if errs:
      if log:
        log("warn", f"{INPUT_INI_NAME} not copied: line {errs[0][0]}: {errs[0][1]}")
      return False
    shutil.copy2(INPUT_INI_FILE, os.path.join(pkg_dir, INPUT_INI_NAME))
    if log:
      log("ok", f"{INPUT_INI_NAME} updated in {pkg_dir}")
    return True
  except Exception as e:
    if log:
      log("warn", f"Copying {INPUT_INI_NAME} failed: {e}")
    return False


def runtime_env(rt, target, pkg_dir):
  """Dictionary of the environment variables to set at launch."""
  env = {}
  if rt["language"].strip() != "":
    env["ANYPS5_LANGUAGE"] = rt["language"].strip()
  if rt["gpu"].strip():
    env["ANYPS5_GPU"] = rt["gpu"].strip()
  if rt["present_mode"].strip():
    env["ANYPS5_PRESENT_MODE"] = rt["present_mode"].strip()
  if rt["fonts_dir"].strip():
    env["ANYPS5_SYSTEM_FONTS"] = rt["fonts_dir"].strip()
  if yes(rt["store_at_flip"]):
    env["APS5_STORE_AT_FLIP"] = "1"
  for key, var in (("sdl_videodriver", "SDL_VIDEODRIVER"), ("thread_priority", "APS5_THREAD_PRIORITY"),
                   ("thread_nice", "APS5_THREAD_NICE"), ("thread_rt", "APS5_THREAD_RT"),
                   ("trace_frames", "APS5_TRACE_FRAMES"), ("trace_seconds", "APS5_TRACE_SECONDS"),
                   ("trace_summary_every", "APS5_TRACE_SUMMARY_EVERY"), ("trace_events", "APS5_TRACE_EVENTS"),
                   ("trace_dir", "APS5_TRACE_DIR")):
    if rt.get(key, "").strip():
      if key == "sdl_videodriver" and target == "windows":
        continue  # BUILD.md: SDL_VIDEODRIVER is about the Linux X11/Wayland backends
      env[var] = rt[key].strip()
  if yes(rt.get("pipeline_stats", "no")):
    env["APS5_PIPELINE_STATS"] = "1"
  if yes(rt.get("gpu_checkpoints", "no")):
    env["APS5_GPU_CHECKPOINTS"] = "1"
  if rt.get("pad_input_file", "").strip():
    env["APS5_PAD_INPUT_FILE"] = rt["pad_input_file"].strip()
  if rt["trace_calls"].strip() and target == "windows":
    env["APS5_TRACE_CALLS"] = rt["trace_calls"].strip()
  if yes(rt["disable_dozen"]) and target == "windows":
    env["VK_LOADER_DRIVERS_DISABLE"] = "*dzn*"
  if yes(rt["copy_input_ini"]) and os.path.isfile(os.path.join(pkg_dir, INPUT_INI_NAME)):
    env["ANYPS5_INPUT_CONFIG"] = os.path.join(pkg_dir, INPUT_INI_NAME)
  for item in re.split(r"[;\n]", rt.get("extra_env", "")):
    if "=" in item:
      k, v = item.split("=", 1)
      if k.strip():
        env[k.strip()] = v.strip()
  return env


def _safe_filename(name):
  return re.sub(r'[<>:"/\\|?*]', "_", name).strip(" .") or "game"


def windows_shortcut_spec(exe_path, env, sync=None):
  """(target, arguments) of the .lnk. A shortcut cannot carry environment variables, so when
  there are some it starts cmd.exe, which sets them and runs the exe (window stays open only if
  the game exits with an error, so the message can be read). Without variables: the exe itself."""
  if not env and not sync:
    return exe_path, ""
  parts = []
  if sync:  # refresh the input ini from Utilities every time the shortcut is used
    parts.append(f'(if exist "{sync[0]}" copy /y "{sync[0]}" "{sync[1]}" >nul)')
  parts += [f'set "{k}={v}"' for k, v in env.items()]
  parts.append(f'"{exe_path}"')
  target = os.environ.get("ComSpec") or r"C:\Windows\System32\cmd.exe"
  return target, '/c "' + " && ".join(parts) + ' || pause"'


def _desktop_quote(text):
  """Quoting of one argument in the Exec= key of a .desktop file."""
  return '"' + re.sub(r'([\\"`$])', r"\\\1", text) + '"'


def create_shortcut(pkg_dir, exe_name, target, rt, game_name, log, dest_dir=None):
  """Shortcut named after the game, next to the executable, with the Runtime settings baked in
  (Windows .lnk, Linux .desktop, macOS .command). Replaces the old launch.bat / launch.sh."""
  env = runtime_env(rt, target, pkg_dir)
  # the ini sits next to the executable: ANYPS5_INPUT_CONFIG is not needed
  env.pop("ANYPS5_INPUT_CONFIG", None)
  exe_path = os.path.abspath(os.path.join(pkg_dir, exe_name))
  sync = None
  if yes(rt.get("copy_input_ini", "yes")) and os.path.isfile(INPUT_INI_FILE):
    sync = (os.path.abspath(INPUT_INI_FILE), os.path.join(os.path.abspath(pkg_dir), INPUT_INI_NAME))
    sync_input_ini(pkg_dir, rt)  # also right now
  dest_dir = dest_dir or pkg_dir  # where the shortcut file is written (the exe stays in pkg_dir)
  base = _safe_filename(game_name)
  try:
    if target == "windows":
      if os.name != "nt":
        log("warn", "A Windows shortcut (.lnk) can only be created when the GUI runs on Windows.")
        return
      path = os.path.abspath(os.path.join(dest_dir, base + ".lnk"))
      tgt, args = windows_shortcut_spec(exe_path, env, sync)
      script = ("$s = New-Object -ComObject WScript.Shell; $l = $s.CreateShortcut($env:LNK_PATH); "
                "$l.TargetPath = $env:LNK_TARGET; $l.Arguments = $env:LNK_ARGS; "
                "$l.WorkingDirectory = $env:LNK_WD; $l.IconLocation = $env:LNK_ICON; "
                "$l.Description = $env:LNK_DESC; $l.Save()")
      penv = dict(os.environ, LNK_PATH=path, LNK_TARGET=tgt, LNK_ARGS=args,
                  LNK_WD=os.path.abspath(pkg_dir), LNK_ICON=exe_path + ",0", LNK_DESC=game_name)
      res = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        env=penv, capture_output=True, text=True, timeout=60,
        creationflags=subprocess.CREATE_NO_WINDOW)
      if res.returncode != 0 or not os.path.isfile(path):
        raise RuntimeError((res.stderr or res.stdout or "PowerShell failed").strip())
    else:
      if os.path.isfile(exe_path):
        os.chmod(exe_path, os.stat(exe_path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
      if target == "macos":
        path = os.path.join(dest_dir, base + ".command")
        lines = ["#!/bin/sh", f"cd {shlex.quote(os.path.abspath(pkg_dir))} || exit 1"]
        if sync:
          lines.append(f"[ -f {shlex.quote(sync[0])} ] && cp -f {shlex.quote(sync[0])} {shlex.quote(sync[1])}")
        lines += [f"export {k}='{v}'" for k, v in env.items()]
        lines += [f'exec {shlex.quote(exe_path)} "$@"']
        content = "\n".join(lines) + "\n"
      else:
        path = os.path.join(dest_dir, base + ".desktop")
        exec_line = "env " + " ".join(_desktop_quote(f"{k}={v}") for k, v in env.items())
        exec_line = (exec_line + " " if env else "") + _desktop_quote(exe_path)
        if sync:
          script = (f"[ -f {shlex.quote(sync[0])} ] && cp -f {shlex.quote(sync[0])} {shlex.quote(sync[1])}; "
                    f"exec {exec_line}")
          exec_line = "sh -c " + _desktop_quote(script.replace("%", "%%"))
        content = ("[Desktop Entry]\nType=Application\nName=%s\nExec=%s\nPath=%s\nTerminal=true\n"
                   % (game_name, exec_line, os.path.abspath(pkg_dir)))
      with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
      os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log("ok", f"Shortcut created: {path} ({len(env)} variables)")
  except Exception as e:
    log("warn", f"Shortcut not created: {e}")


# ─────────────────────────────────────────────────────────────
#  Game given as an archive (.zip .rar), extracted into Utilities/extracted/<name>
#    .zip / .tar*      Python standard library, nothing to install
#    .rar .7z .iso...  7-Zip (7z / 7zz in the Utilities folder or in PATH); fallback: the optional
#                      python modules `rarfile` (+ UnRAR/WinRAR) and `py7zr`
# ─────────────────────────────────────────────────────────────
EXTRACT_DIR_NAME = "extracted"

ARCHIVE_TAR_EXTS = (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".tbz", ".txz", ".tar")
ARCHIVE_7Z_EXTS = (".rar", ".7z", ".iso", ".cab", ".wim", ".arj", ".lzh", ".001")  # handled by 7-Zip
ARCHIVE_EXTS = (".zip",) + ARCHIVE_TAR_EXTS + ARCHIVE_7Z_EXTS  # every format the readers understand (libs archive)
GAME_ARCHIVE_EXTS = (".zip", ".rar")  # formats accepted for the GAME dump
ARCHIVE_DIALOG_FILTER = "Game archives (%s)" % ";".join("*" + e for e in GAME_ARCHIVE_EXTS)
ARCHIVE_HINT = ("To open .rar / .7z / .iso ... files install 7-Zip (https://www.7-zip.org: 7z or 7zz in PATH "
                "or in the %s folder), or `pip install rarfile py7zr` (rarfile also needs UnRAR/WinRAR)."
                % UTILITIES_DIR_NAME)


class ArchiveError(Exception):
  pass


class _Entry:
  __slots__ = ("name", "size", "mode", "ref")

  def __init__(self, name, size, mode=0, ref=None):
    self.name, self.size, self.mode, self.ref = name, int(size or 0), mode or 0, ref


def archive_ext(path):
  """Archive extension of a file name ('.tar.gz' counts as one), '' if it is not an archive."""
  low = (path or "").lower()
  return next((e for e in ARCHIVE_EXTS if low.endswith(e)), "")


def is_archive_source(path):
  """True if `path` is a game archive (.zip / .rar) file (instead of a game folder)."""
  try:
    return bool(path) and os.path.isfile(path) and archive_ext(path) in GAME_ARCHIVE_EXTS
  except OSError:
    return False


def find_7z():
  """Paths of the 7-Zip command line tools found (Utilities folder first, then PATH, then the usual
  Windows install folders)."""
  found = []
  search = UTILITIES_DIR + os.pathsep + os.environ.get("PATH", "")
  for n in ("7zz", "7z", "7za"):  # 7za cannot read rar: it is tried last
    p = shutil.which(n, path=search)
    if p and p not in found:
      found.append(p)
  if os.name == "nt":
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
      base = os.environ.get(var)
      p = os.path.join(base, "7-Zip", "7z.exe") if base else ""
      if p and os.path.isfile(p) and p not in found:
        found.append(p)
  return found


def _drop_unsafe_links(dest):
  """Symbolic links extracted by an external tool that point outside `dest` are removed."""
  root = os.path.realpath(dest)
  for base, dirs, names in os.walk(dest):
    for n in dirs + names:
      p = os.path.join(base, n)
      try:
        if os.path.islink(p) and not (os.path.realpath(p) + os.sep).startswith(root + os.sep):
          os.unlink(p)
      except OSError:
        pass


class _ArchiveBase:
  label = "archive"
  streams = True  # True: single files can be read with open(); False: only extract_to() works
  files = ()

  def __enter__(self):
    return self

  def __exit__(self, *exc):
    self.close()

  def close(self):
    pass

  def extract_to(self, dest, files, progress, cancel, warn):
    """Extracts `files` (list of _Entry) into dest, reading them one by one. Returns the bytes written."""
    try:
      return self._extract_streaming(dest, files, progress, cancel, warn)
    except (Cancelled, ArchiveError):
      raise
    except Exception as e:  # BadRarFile, tarfile errors, disk full...: let the next backend try
      raise ArchiveError("%s: %s" % (type(e).__name__, e))

  def _extract_streaming(self, dest, files, progress, cancel, warn):
    root = os.path.realpath(dest)
    done = 0
    for e in files:
      check_cancel(cancel)
      parts = [p for p in e.name.replace("\\", "/").split("/") if p and p != "."]
      out = os.path.join(dest, *parts) if parts else dest
      if not parts or ".." in parts or not os.path.realpath(out).startswith(root + os.sep):
        warn(f"Skipped unsafe path in the archive: {e.name}")
        continue
      os.makedirs(os.path.dirname(out), exist_ok=True)
      with self.open(e) as src, open(out, "wb") as dst:
        while True:
          check_cancel(cancel)
          chunk = src.read(8 * 1024 * 1024)
          if not chunk:
            break
          dst.write(chunk)
          done += len(chunk)
          progress(done)
      if os.name != "nt" and e.mode:
        try:
          os.chmod(out, (e.mode & 0o777) | 0o600)
        except OSError:
          pass
    return done


class _ZipArchive(_ArchiveBase):
  label = "zip"

  def __init__(self, path):
    try:
      self._zf = zipfile.ZipFile(path)
      self.files = [_Entry(i.filename, i.file_size, i.external_attr >> 16, i)
                    for i in self._zf.infolist() if not i.is_dir()]
      if any(i.flag_bits & 0x1 for i in self._zf.infolist()):
        raise ArchiveError("password-protected archives are not supported")
    except (zipfile.BadZipFile, OSError, NotImplementedError) as e:
      raise ArchiveError(str(e))

  def open(self, e):
    return self._zf.open(e.ref)

  def close(self):
    self._zf.close()


class _TarArchive(_ArchiveBase):
  label = "tar"

  def __init__(self, path):
    try:
      self._tf = tarfile.open(path, "r:*")
      self.files = [_Entry(m.name, m.size, m.mode, m) for m in self._tf.getmembers() if m.isfile()]
    except (tarfile.TarError, EOFError, OSError) as e:
      raise ArchiveError(str(e))

  def open(self, e):
    f = self._tf.extractfile(e.ref)
    if f is None:
      raise ArchiveError("cannot read " + e.name)
    return f

  def close(self):
    self._tf.close()


class _RarPyArchive(_ArchiveBase):
  label = "rarfile"

  def __init__(self, path):
    try:
      import rarfile
    except ImportError:
      raise ArchiveError("python module 'rarfile' not installed")
    if os.name == "nt":
      for var in ("ProgramFiles", "ProgramFiles(x86)"):
        for d in (os.path.join(os.environ.get(var) or "", "WinRAR"), UTILITIES_DIR):
          p = os.path.join(d, "UnRAR.exe")
          if os.path.isfile(p):
            rarfile.UNRAR_TOOL = p
    try:
      self._rf = rarfile.RarFile(path)
      infos = [i for i in self._rf.infolist() if not (i.is_dir() if hasattr(i, "is_dir") else i.isdir())]
      if self._rf.needs_password():
        raise ArchiveError("password-protected archives are not supported")
      self.files = [_Entry(i.filename, i.file_size, getattr(i, "mode", 0) if isinstance(getattr(i, "mode", 0), int) else 0, i)
                    for i in infos]
      first = next((e for e in self.files if e.size > 0), None)
      if first:  # fails here (not halfway through) if no unrar/unar/bsdtar tool is available
        with self._rf.open(first.ref) as f:
          f.read(1)
    except ArchiveError:
      raise
    except Exception as e:
      raise ArchiveError(str(e))

  def open(self, e):
    return self._rf.open(e.ref)

  def close(self):
    self._rf.close()


class _Py7zArchive(_ArchiveBase):
  label = "py7zr"
  streams = False

  def __init__(self, path):
    try:
      import py7zr
    except ImportError:
      raise ArchiveError("python module 'py7zr' not installed")
    self._path = path
    try:
      with py7zr.SevenZipFile(path) as z:
        if z.needs_password():
          raise ArchiveError("password-protected archives are not supported")
        self.files = [_Entry(i.filename, i.uncompressed, 0, i) for i in z.list() if not i.is_directory]
    except ArchiveError:
      raise
    except Exception as e:
      raise ArchiveError(str(e))

  def extract_to(self, dest, files, progress, cancel, warn):
    import py7zr
    from py7zr.callbacks import ExtractCallback

    class CB(ExtractCallback):
      def __init__(self):
        self.done = 0

      def report_start_preparation(self):
        pass

      def report_start(self, processing_file_path, processing_bytes):
        check_cancel(cancel)

      def report_update(self, decompressed_bytes):
        pass

      def report_end(self, processing_file_path, wrote_bytes):
        self.done += int(wrote_bytes)
        progress(self.done)

      def report_warning(self, message):
        warn(str(message))

      def report_postprocess(self):
        pass

    cb = CB()
    try:
      with py7zr.SevenZipFile(self._path) as z:
        z.extractall(path=dest, callback=cb)
    except Cancelled:
      raise
    except Exception as e:
      raise ArchiveError(str(e))
    check_cancel(cancel)
    _drop_unsafe_links(dest)
    return cb.done


class _SevenZipCli(_ArchiveBase):
  label = "7-Zip"
  streams = False

  def __init__(self, path):
    self._path = path
    exes = find_7z()
    if not exes:
      raise ArchiveError("7-Zip not found")
    last = None
    for exe in exes:
      try:
        self.files = self._list(exe, path)
      except ArchiveError as e:
        last = e
        continue
      self._exe = exe
      return
    raise last

  @staticmethod
  def _list(exe, path):
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
      res = subprocess.run([exe, "l", "-slt", "-ba", path], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=300, **kwargs)
    except (OSError, subprocess.SubprocessError) as e:
      raise ArchiveError(str(e))
    text = res.stdout.decode("oem" if os.name == "nt" else "utf-8", errors="replace")
    if res.returncode not in (0, 1):
      bad = [l.strip() for l in (text + res.stderr.decode("utf-8", errors="replace")).splitlines()
             if "error" in l.lower() or "password" in l.lower()]
      raise ArchiveError(bad[0] if bad else "%s: exit code %d" % (os.path.basename(exe), res.returncode))
    entries, cur = [], {}
    for line in text.splitlines() + [""]:
      if not line.strip():
        if "Path" in cur:
          entries.append(cur)
        cur = {}
      elif " = " in line:
        k, v = line.split(" = ", 1)
        cur[k.strip()] = v.strip()
    files = []
    for c in entries:
      if c.get("Folder") == "+" or c.get("Attributes", "").startswith("D"):
        continue
      if c.get("Encrypted") == "+":
        raise ArchiveError("password-protected archives are not supported")
      size = c.get("Size", "")
      files.append(_Entry(c["Path"], int(size) if size.isdigit() else 0))
    return files

  def extract_to(self, dest, files, progress, cancel, warn):
    total = sum(e.size for e in self.files) or 1

    def log(kind, text):
      if kind in ("progress", "info"):
        m = re.match(r"\s*(\d{1,3})%", str(text))
        if m:
          progress(min(int(m.group(1)), 100) * total // 100)

    holder = {}
    for flags in (["-bsp1", "-bso0"], []):  # very old p7zip builds do not know -bs
      code = stream_process([self._exe, "x", "-y", "-aoa"] + flags + ["-o" + dest, self._path],
                            log, cancel, holder, label="7-Zip")
      if code != 7:  # 7 = command line error
        break
    if code not in (0, 1):  # 1 = warning (e.g. a file could not be set to the original date)
      text = " ".join(holder.get("tail", [])).replace("\x08", "")
      msgs = []
      for m in re.findall(r"ERROR: (.+?)(?=\s{2,}|\s\d{1,3}% |ERROR:|$)", text):
        if m.strip() not in msgs:
          msgs.append(m.strip())
      raise ArchiveError("7-Zip exit code %d: %s" % (code, " | ".join(msgs[:3]) or "extraction failed"))
    _drop_unsafe_links(dest)
    return total


def iter_archives(path):
  """Yields, one at a time, every backend that can open the archive (the content decides, not only
  the extension). The caller stops at the first one that works; raises ArchiveError if none opens it."""
  ext = archive_ext(path)
  if ext == ".zip":
    chain = (_ZipArchive, _SevenZipCli)
  elif ext in ARCHIVE_TAR_EXTS:
    chain = (_TarArchive, _SevenZipCli)
  elif ext == ".rar":
    chain = (_SevenZipCli, _RarPyArchive)
  elif ext == ".7z":
    chain = (_SevenZipCli, _Py7zArchive)
  else:
    chain = (_SevenZipCli,)
  errors, opened = [], 0
  for cls in chain:
    try:
      arc = cls(path)
      if not arc.files:
        arc.close()
        raise ArchiveError("no files found inside")
    except ArchiveError as e:
      errors.append("%s: %s" % (cls.label, e))
      continue
    opened += 1
    yield arc
  if not opened:
    msg = " · ".join(errors)
    if ext not in (".zip",) + ARCHIVE_TAR_EXTS:
      msg += "  —  " + ARCHIVE_HINT
    raise ArchiveError(msg)


def open_archive(path):
  """The first backend that can open the archive."""
  for arc in iter_archives(path):
    return arc


_archive_cache = {}


def archive_listing(path):
  """[(name, size, mode)] of the files inside the archive (cached while the file does not change)."""
  try:
    st = os.stat(path)
  except OSError as e:
    raise ArchiveError(str(e))
  key = (path, st.st_mtime, st.st_size)
  hit = _archive_cache.get(key)
  if hit is None:
    with open_archive(path) as arc:
      hit = [(e.name, e.size, e.mode) for e in arc.files]
    if len(_archive_cache) >= 8:
      _archive_cache.clear()
    _archive_cache[key] = hit
  return hit


def zip_game_prefix(names):
  """Folder inside the archive that holds eboot.bin (shallowest one), as 'a/b/' or '' for the root.
  Returns None if there is no eboot.bin."""
  best = None
  for n in names:
    parts = [x for x in n.replace("\\", "/").split("/") if x != "."]  # tar: "./game/eboot.bin"
    if parts and parts[-1].lower() == EXECUTABLE_NAME and "__MACOSX" not in parts:
      depth = len(parts) - 1
      if best is None or depth < best[0]:
        best = (depth, "/".join(parts[:-1]) + "/" if depth else "")
  return None if best is None else best[1]


def _archive_stem(path):
  """File name without the archive extension ('game.tar.gz', 'game.part1.rar', 'game.7z.001' -> 'game')."""
  base = os.path.basename(path)
  ext = archive_ext(path)
  stem = base[:-len(ext)] if ext else os.path.splitext(base)[0]
  if ext == ".001":
    stem = os.path.splitext(stem)[0]
  return re.sub(r"\.part0*1$", "", stem, flags=re.I) or stem


def archive_game_name(path):
  """Name the game gets: its folder inside the archive, otherwise the archive file name."""
  try:
    prefix = zip_game_prefix([n for n, _s, _m in archive_listing(path)])
  except ArchiveError:
    return _archive_stem(path)
  return prefix.rstrip("/").split("/")[-1] if prefix else _archive_stem(path)


def scan_archive(path):
  """Light scan of a game archive for the status panel (nothing is extracted). On failure the
  result has an 'error' text."""
  try:
    listing = archive_listing(path)
  except ArchiveError as e:
    return {"eboot": False, "dirs": False, "module_dirs": [], "modules_total": 0, "size": 0, "error": str(e)}
  names = ["/".join(x for x in n.replace("\\", "/").split("/") if x != ".") for n, _s, _m in listing]
  size = sum(s for _n, s, _m in listing)
  prefix = zip_game_prefix(names)
  has_eboot = prefix is not None
  if prefix is None:  # no eboot.bin: still look for the other folders, to say exactly what is missing
    sys_dirs = sorted((n.split("/sce_sys/")[0] + "/" if "/sce_sys/" in n else "")
                      for n in names if n.startswith("sce_sys/") or "/sce_sys/" in n)
    prefix = sys_dirs[0] if sys_dirs else ""
    if prefix == "/":
      prefix = ""
  rel = [n[len(prefix):] for n in names if n.startswith(prefix)]
  mdirs = [d for d in MODULE_DIRS if any(r.startswith(d + "/") for r in rel)]
  return {"eboot": has_eboot, "dirs": any(r.startswith("sce_sys/") for r in rel), "module_dirs": mdirs,
          "modules_total": sum(1 for r in rel if r.split("/")[0] in MODULE_DIRS and r.count("/") == 1),
          "size": size}


def extract_game_archive(arc_path, log, cancel):
  """Extracts the archive into Utilities/extracted/<name> and returns the game folder
  (the one that contains eboot.bin), or None on failure. If a backend opens the archive but fails
  while extracting (e.g. 7-Zip without the RAR codec on a solid .rar), the next one is tried."""
  dest = os.path.join(UTILITIES_DIR, EXTRACT_DIR_NAME, _safe_filename(_archive_stem(arc_path)))
  log("step", f"Extracting the archive · {os.path.basename(arc_path)}")
  failures = []
  try:
    for arc in iter_archives(arc_path):
      with arc:
        res = _extract_with(arc, arc_path, dest, log, cancel)
      if res is not False:
        return res
      failures.append(arc.label)
  except ArchiveError as e:
    log("err", f"Cannot open the archive: {e}")
    return None
  if failures:
    ext = archive_ext(arc_path)
    log("err", f"Extraction failed with: {', '.join(failures)}."
               + (" " + ARCHIVE_HINT if ext not in (".zip",) + ARCHIVE_TAR_EXTS else ""))
  return None


def _extract_with(arc, arc_path, dest, log, cancel):
  """One extraction attempt with one backend. Returns the game folder, None for a definitive failure
  (nothing else is worth trying) or False when another backend may succeed."""
  files = [e for e in arc.files if "__MACOSX/" not in e.name.replace("\\", "/")]
  prefix = zip_game_prefix([e.name for e in files])
  if prefix is None:
    log("err", f"{EXECUTABLE_NAME} not found inside the archive: is this a PS5 game dump?")
    return None
  total = sum(e.size for e in files)
  try:
    free = shutil.disk_usage(existing_parent(UTILITIES_DIR)).free
    if free < total + total // 20:
      log("err", f"Not enough disk space to extract the archive: {format_size(total)} needed, "
                 f"{format_size(free)} free on the drive of {UTILITIES_DIR}.")
      return None
  except OSError:
    pass
  if os.path.isdir(dest):
    _rmtree_force(dest)
  os.makedirs(dest)
  t0, last = time.time(), [0.0]

  def progress(done):
    now = time.time()
    if now - last[0] < 0.25:
      return
    last[0] = now
    pct = min(done * 100.0 / total, 100.0) if total else 0.0
    log("percent", pct)
    log("progress", f"Extracting: {pct:.0f}% ({format_size(done)} / {format_size(total)})"
                    f" · {format_size(done / max(now - t0, 0.001))}/s")

  try:
    done = arc.extract_to(dest, files, progress, cancel, lambda m: log("warn", m))
  except ArchiveError as e:
    _rmtree_force(dest)
    log("warn", f"Extraction with {arc.label} failed: {e}")
    return False
  except BaseException:
    _rmtree_force(dest)  # incomplete extraction (cancel or error)
    raise
  finally:
    log("percent", None)
    log("progress", "")
  if os.path.isdir(os.path.join(dest, "__MACOSX")):
    _rmtree_force(os.path.join(dest, "__MACOSX"))
  game = (os.path.join(dest, *prefix.split("/")) if prefix else dest).rstrip("/\\")
  if not os.path.isdir(game):
    _rmtree_force(dest)
    log("warn", f"Extraction with {arc.label} did not produce the game folder: {game}")
    return False
  log("ok", f"Archive extracted: {format_size(done)} → {game}")
  return game


def cleanup_extracted(folder, log):
  """Removes the temporary extraction folder created for a zip."""
  base = os.path.join(UTILITIES_DIR, EXTRACT_DIR_NAME)
  try:
    rel = os.path.relpath(folder, base)
    if rel.startswith("..") or os.path.isabs(rel):
      return
    top = os.path.join(base, rel.split(os.sep)[0])
    if os.path.isdir(top):
      _rmtree_force(top)
      log("info", "Temporary extracted copy removed.")
  except Exception as e:
    log("warn", f"Could not remove the temporary folder {folder}: {e}")


def package_dir(output_base, game_name, target):
  return os.path.join(output_base, f"{game_name}-{TARGETS[target]['suffix']}")


def exe_target(path):
  """Target (windows/linux/macos) deduced from the chosen executable."""
  b = os.path.basename(path).lower()
  for t, d in TARGETS.items():
    if b == d["exe"]:
      return t
  if b.endswith(".exe"):
    return "windows"
  if b.endswith(".elf"):
    return "linux"
  return "macos" if sys.platform == "darwin" else "linux"


def game_name_from_exe(path):
  """Game name from the package folder that contains the executable (suffix -windows/-linux/-macos removed)."""
  name = os.path.basename(os.path.dirname(os.path.abspath(path)))
  for d in TARGETS.values():
    suf = "-" + d["suffix"]
    if name.lower().endswith(suf):
      return name[:-len(suf)] or name
  return name


def launch_game(pkg_dir, target, cfg, exe=None, log=None):
  exe = exe or os.path.join(pkg_dir, TARGETS[target]["exe"])
  if not os.path.isfile(exe):
    raise FileNotFoundError(exe)
  sync_input_ini(pkg_dir, cfg["runtime"], log)  # always start with the current input settings
  env = dict(os.environ)
  env.update(runtime_env(cfg["runtime"], target, pkg_dir))
  kwargs = {}
  if os.name == "nt":
    kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
  return subprocess.Popen([exe], cwd=pkg_dir, env=env, **kwargs)


# ─────────────────────────────────────────────────────────────
#  Disk space
# ─────────────────────────────────────────────────────────────
def dir_size(path):
  total = 0
  for root, _dirs, files in os.walk(path):
    for f in files:
      try:
        total += os.path.getsize(os.path.join(root, f))
      except OSError:
        pass
  return total


def format_size(n):
  n = float(n)
  for unit in ("B", "KiB", "MiB", "GiB"):
    if n < 1024 or unit == "GiB":
      return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
    n /= 1024


def existing_parent(path):
  path = os.path.abspath(path)
  while not os.path.exists(path):
    parent = os.path.dirname(path)
    if parent == path:
      break
    path = parent
  return path


def space_requirements(folder, target, mode, output_base, auto=True, move=False):
  """Estimate of the space the operation needs and of what is free on the destination drive."""
  parts = []
  extra = find_extra_module_dirs(folder)
  files = [p for _n, p in game_targets(folder, True, extra) if os.path.isfile(p)]
  if mode == "unwrap":
    sizes = [os.path.getsize(p) for p in files if file_state(p) == "self"]
    parts.append(("backups of the originals", sum(sizes)))
    parts.append(("temporary file", max(sizes, default=0)))
    where, reusable = UTILITIES_DIR, 0
  else:
    eboot = os.path.join(folder, EXECUTABLE_NAME)
    fonts = os.path.join(UTILITIES_DIR, FONTS_DIR_NAME)
    if move:
      # moved file by file: the peak is only the biggest file (same drive) or the whole game on the
      # destination drive while the source shrinks at the same time
      same = False
      try:
        same = os.stat(folder).st_dev == os.stat(existing_parent(output_base)).st_dev
      except OSError:
        pass
      biggest = max((os.path.getsize(os.path.join(r, f)) for r, _d, fs in os.walk(folder) for f in fs
                     if os.path.isfile(os.path.join(r, f))), default=0)
      parts.append(("game move (app0)", biggest if same else dir_size(folder)))
    else:
      parts.append(("game copy (app0)", dir_size(folder)))
    libs = libs_info(target)
    parts.append(("libs", libs["size"] if libs else 0))
    if os.path.isdir(fonts):
      parts.append(("fonts", dir_size(fonts)))
    parts.append(("new executable", os.path.getsize(eboot) if os.path.isfile(eboot) else 0))
    if auto:
      parts.append(("temporary file", max((os.path.getsize(p) for p in files), default=0)))
    where = output_base
    pkg = package_dir(output_base, os.path.basename(os.path.normpath(folder)), target)
    reusable = dir_size(pkg) if os.path.isdir(pkg) else 0  # freed when the old package is replaced
  subtotal = sum(b for _l, b in parts)
  margin = max(subtotal // 20, 64 * 1024 * 1024)
  parts.append(("safety margin", margin))
  free = shutil.disk_usage(existing_parent(where)).free
  return {"parts": parts, "needed": subtotal + margin, "free": free,
          "reusable": reusable, "where": where}


def build_notification(op, game, ok, elapsed, errors):
  """(title, text) of the end-of-operation notification."""
  op = op or "Operation"
  if ok:
    took = "in under 1s" if elapsed < 1 else "in " + format_duration(elapsed)
    return f"{op} completed", f"{game} · {took}" if game else took
  lines = [game]
  seen = []
  for e in reversed(errors):  # last distinct errors, skipping the generic summary line
    e = e.strip()
    if e and not e.startswith("Completed with errors") and e not in seen:
      seen.append(e)
    if len(seen) == 3:
      break
  lines += [(e if len(e) <= 160 else e[:157] + "...") for e in reversed(seen)]
  lines.append("See the log for details.")
  return f"{op} failed", "\n".join(lines)


def notify_desktop(title, text):
  """System notification (Windows balloon/toast, macOS Notification Center, Linux notify-send).
  Never raises and never blocks: the notification is sent from a background thread."""
  def send():
    try:
      if os.name == "nt":
        # Native Windows toast shown under its own app name ("AnyPS5 GUI"): an AppUserModelID
        # is registered under HKCU (no admin rights) so Windows does not label it "Windows PowerShell".
        toast = (
          "$id='AnyPS5.GUI';"
          "$k='HKCU:\\Software\\Classes\\AppUserModelId\\'+$id;"
          "if(!(Test-Path $k)){New-Item $k -Force | Out-Null};"
          "New-ItemProperty $k -Name DisplayName -Value 'AnyPS5 GUI' -PropertyType String -Force | Out-Null;"
          "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime] | Out-Null;"
          "[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime] | Out-Null;"
          "$x=New-Object Windows.Data.Xml.Dom.XmlDocument;"
          "$x.LoadXml('<toast><visual><binding template=\"ToastGeneric\"><text></text><text></text></binding></visual></toast>');"
          "$t=$x.GetElementsByTagName('text');"
          "$t.Item(0).InnerText=$env:NTF_TITLE; $t.Item(1).InnerText=$env:NTF_TEXT;"
          "$n=New-Object Windows.UI.Notifications.ToastNotification($x);"
          "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show($n)")
        balloon = (
          "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
          "$n = New-Object System.Windows.Forms.NotifyIcon;"
          "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true;"
          "$n.BalloonTipTitle = 'AnyPS5 GUI - ' + $env:NTF_TITLE; $n.BalloonTipText = $env:NTF_TEXT;"
          "$n.ShowBalloonTip(8000); Start-Sleep -Seconds 9; $n.Dispose()")
        penv = dict(os.environ, NTF_TITLE=title[:60], NTF_TEXT=text[:250])
        for script in (toast, balloon):  # the old balloon only if the toast fails
          res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                                "-ExecutionPolicy", "Bypass", "-Command", script],
                               env=penv, capture_output=True, timeout=30,
                               creationflags=subprocess.CREATE_NO_WINDOW)
          if res.returncode == 0:
            break
      elif sys.platform == "darwin":
        script = ('on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run')
        subprocess.run(["osascript", "-e", script, title, text], capture_output=True, timeout=15)
      else:
        # Linux: try the common notification front ends in order, stop at the first that works
        attempts = []
        if shutil.which("notify-send"):
          attempts.append(["notify-send", "-a", "AnyPS5 GUI", title, text])
        if shutil.which("gdbus"):  # talks to org.freedesktop.Notifications directly (no extra package)
          attempts.append(["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
                           "--object-path", "/org/freedesktop/Notifications",
                           "--method", "org.freedesktop.Notifications.Notify",
                           "AnyPS5 GUI", "0", "", title, text, "[]", "{}", "8000"])
        if shutil.which("kdialog"):
          attempts.append(["kdialog", "--title", title, "--passivepopup", text, "8"])
        if shutil.which("zenity"):
          attempts.append(["zenity", "--notification", "--text", title + "\n" + text])
        for cmd in attempts:
          if subprocess.run(cmd, capture_output=True, timeout=15).returncode == 0:
            break
    except Exception:
      pass
  threading.Thread(target=send, daemon=True).start()


def log_space(req, log):
  """Write the estimate in the log. Returns True if there is enough space."""
  detail = ", ".join(f"{label} {format_size(b)}" for label, b in req["parts"] if b)
  avail = req["free"] + req["reusable"]
  ok = avail >= req["needed"]
  log("detail", f"Space needed: ~{format_size(req['needed'])} ({detail})")
  note = f" (including {format_size(req['reusable'])} freed from the existing package)" if req["reusable"] else ""
  log("ok" if ok else "err",
      f"Space available on {req['where']}: {format_size(avail)}{note}"
      + ("" if ok else f" - missing {format_size(req['needed'] - avail)}"))
  return ok


# ─────────────────────────────────────────────────────────────
#  Input: anyps5-input.ini (INPUT_MAPPING.md)
# ─────────────────────────────────────────────────────────────
INPUT_ACTIONS = {a.lower() for a in (
  "Cross Circle Triangle Square L1 R1 L2 R2 L3 R3 Options Up Right Down Left "
  "LeftStickLeft LeftStickRight LeftStickUp LeftStickDown "
  "RightStickLeft RightStickRight RightStickUp RightStickDown "
  "TouchLeft TouchRight ToggleMouse ToggleFullscreen").split()}
MOUSE_BUTTONS = {"left", "middle", "right", "x1", "x2"}
# name: (minimum, maximum, minimum_excluded, integer)
INPUT_SETTINGS = {
  "leftstickdeadzone": (0, 90, False, True),
  "rightstickdeadzone": (0, 90, False, True),
  "mousesensitivity": (0, 10, True, False),
  "mouseverticalscale": (0.1, 10, False, False),
  "mousecurve": (0.2, 5, False, False),
  "mousedeadzoneoffset": (0, 0.9, False, False),
  "mousemaxspeed": (0.1, 10, False, False),
  "mousesmoothingms": (0, 500, False, False),
  "mousedecayms": (1, 1000, False, False),
}

INPUT_TEMPLATE = """# anyps5-input.ini - AnyPS5 keyboard and mouse mapping
# Format:  Action = Type:Value     (# or ; start a comment)
# The first line of an action replaces the default keys, later ones add to them.
# Actions that are not listed keep their default keys. Controllers are not affected.
#
# Example below (active): Cross on F or Space, left stick on IJKL
#
# Settings (each at most once):
"""


def validate_input_ini(text):
  """Check the file according to INPUT_MAPPING.md. Returns a list of (line, message)."""
  errors = []
  seen_settings = set()
  for number, raw in enumerate(text.lstrip("\ufeff").splitlines(), 1):
    line = re.split(r"[#;]", raw, 1)[0].strip()
    if not line:
      continue
    if "=" not in line:
      errors.append((number, "missing '=': expected 'Action = Type:Value'"))
      continue
    key, value = (p.strip() for p in line.split("=", 1))
    low = key.lower()
    if low in INPUT_SETTINGS:
      if low in seen_settings:
        errors.append((number, f"'{key}' appears more than once"))
        continue
      seen_settings.add(low)
      lo, hi, lo_excl, integer = INPUT_SETTINGS[low]
      try:
        num = int(value) if integer else float(value)
        if not math.isfinite(num):
          raise ValueError(value)
      except ValueError:
        errors.append((number, f"'{key}': non-numeric value{' (an integer is required)' if integer else ''}"))
        continue
      if num < lo or num > hi or (lo_excl and num <= lo):
        errors.append((number, f"'{key}': out of range ({'>' if lo_excl else ''}{lo} .. {hi})"))
      continue
    if low not in INPUT_ACTIONS:
      errors.append((number, f"unknown action or setting: '{key}'"))
      continue
    if ":" not in value:
      errors.append((number, "expected 'Type:Value' (KEY, MOUSE or WHEEL)"))
      continue
    kind, name = (p.strip() for p in value.split(":", 1))
    kind = kind.upper()
    if not name:
      errors.append((number, "empty value after the colon"))
    elif kind == "MOUSE" and name.lower() not in MOUSE_BUTTONS:
      errors.append((number, f"MOUSE:{name} is invalid (Left, Middle, Right, X1, X2)"))
    elif kind == "WHEEL" and name.lower() not in ("up", "down"):
      errors.append((number, f"WHEEL:{name} is invalid (Up or Down)"))
    elif kind not in ("KEY", "MOUSE", "WHEEL"):
      errors.append((number, f"unknown type '{kind}' (KEY, MOUSE, WHEEL)"))
  return errors


# ─────────────────────────────────────────────────────────────
#  Relink
# ─────────────────────────────────────────────────────────────
def _do_relink(folder, cfg, log, cancel, holder, created, mover=None):
  s, rl, rt = cfg["settings"], cfg["relinker"], cfg["runtime"]
  set_active_relinker_cfg(rl)
  game_name = os.path.basename(os.path.normpath(folder))
  auto = yes(s["auto_unwrap"])
  intel = yes(s["intel"])
  target = rl["target"]
  info = TARGETS[target]
  skip_modules = yes(rl.get("skip_sce_module", "no"))
  smp = rl.get("sce_module_path", "").strip()
  modbase = os.path.abspath(smp) if smp else folder  # parent of the module folders
  extra_dirs = effective_module_dirs(modbase, rl, log, explicit_base=bool(smp))
  rl = dict(rl, module_dirs=";".join(extra_dirs))  # the relinker command also uses these
  if extra_dirs:
    log("info", "Module folders passed to --module-dir: " + ", ".join(extra_dirs))
  conflicts = [] if skip_modules else find_module_conflicts(modbase, extra_dirs)
  if conflicts:
    log("err", "Modules with the same name in different folders (the relinker would stop with "
               "'Conflicting guest output filename'):")
    for name, ds in conflicts:
      log("err", f"  {name}: " + ", ".join(ds))
    log("info", "Remove the unwanted folder from '--module-dir' (Conversion options) or delete the duplicate "
                "from the game folder, then retry.")
    return False
  executables = split_list(rl["executables"])
  if yes(rl.get("lazy_binding", "no")) and not skip_modules and (existing_module_dirs(modbase) or extra_dirs):
    log("err", "--lazy-binding is incompatible with bundled ELF modules (USAGE.md), and this game has some. "
               "Turn off lazy binding (Conversion options).")
    return False

  # 1. Requirements
  log("step", "[1] Checking requirements")
  problem = validate_relink_options(intel, rl) or validate_runtime_options(rt, rl)
  if problem:
    log("err", f"Invalid options: {problem}")
    return False
  log("ok", f"Target system: {info['label']}")

  use_source = rl.get("relinker_source") == "github"
  if use_source:
    problem = source_preflight(rl)
    if problem:
      log("err", problem)
      return False
    log("ok", f"Relinker: will be built from {normalize_repo_url(rl['repo_url'])}")
    relinker = None  # built after the quick checks below
  else:
    if rl.get("relinker_source") == "release":
      log("step", "[1a] Downloading the files of the GitHub release")
      if not ensure_release_assets(rl, log, cancel, target, need=("relinker", "libs", "unwrap", "docs") if auto
                                   else ("relinker", "libs", "docs")):
        return False
      check_cancel(cancel)
    relinker = find_relinker()
    if not relinker:
      log("err", f"No '{RELINKER_PREFIX}*' found in the {UTILITIES_DIR_NAME} folder.")
      log("detail", f"Searched in: {UTILITIES_DIR}")
      return False
    log("ok", f"Relinker: {os.path.basename(relinker)}")

  build_libs = wants_built_libs(rl)
  libs = None
  if build_libs:
    log("ok", "Libraries: will be built from the GitHub repository (target 'libs')")
  else:
    libs = libs_zip_info(target)
    if not libs:
      log("err", f"No '{LIBS_ZIP_PREFIX}*.zip' containing the '{LIBS_DIR_NAME}' folder found in the {UTILITIES_DIR_NAME} folder.")
      log("detail", f"Put the AnyPS5 libs zip downloaded from GitHub (its name starts with '{LIBS_ZIP_PREFIX}', no need to extract it) in: {UTILITIES_DIR}")
      return False
    log("ok", f"Libraries: {os.path.basename(libs['zip'])} → {libs['name']}/ ({len(libs['entries'])} files)")

  state = file_state(os.path.join(folder, EXECUTABLE_NAME))
  if state == "missing":
    log("err", f"{EXECUTABLE_NAME} not found in the game folder.")
    return False
  if state == "self" and auto:
    log("info", f"{EXECUTABLE_NAME} is a SELF: it will be unwrapped on the copy.")
  elif state == "self":
    log("err", f"{EXECUTABLE_NAME} is a SELF, not an ELF.")
    log("info", "Enable 'Automatic unwrap' or run Unwrap mode first.")
    return False
  elif state != "elf":
    log("err", f"{EXECUTABLE_NAME} is not a valid ELF (still encrypted / protected?).")
    return False
  else:
    log("ok", f"{EXECUTABLE_NAME} is a valid ELF")

  if not os.path.isdir(os.path.join(folder, "sce_sys")):
    log("err", "Folder 'sce_sys' not found in the game folder.")
    return False
  log("ok", "Folder 'sce_sys' found")
  if skip_modules:
    log("warn", "--skip-sce-module: bundled modules and the module folder checks are disabled "
                "(debug only: the game will be extremely unstable).")
  else:
    problem = check_module_dirs(modbase)
    if problem:
      log("err", f"Modules: {problem}.")
      if smp:
        log("detail", f"Searched in: {modbase}")
      return False
    log("ok", "Module folders: " + ", ".join(existing_module_dirs(modbase)))
    if smp:
      log("info", f"--sce-module-path: the modules are read from {modbase}")
    if yes(rl.get("lazy_binding", "no")) and existing_module_dirs(modbase):
      log("err", "--lazy-binding is incompatible with bundled ELF modules: disable it.")
      return False
    excl = split_list(rl.get("exclude_sce_modules", ""))
    if excl:
      known = {os.path.basename(rel) for rel, _p in module_files(modbase, extra_dirs)}
      missing = [n for n in excl if n not in known]
      if missing:
        log("err", "--exclude-sce-module: file not found in the module folders: " + ", ".join(missing))
        return False
  for e in executables:
    if not os.path.isfile(os.path.join(modbase, e)):
      log("err", f"--executable: '{e}' does not exist in " + ("the module parent folder." if smp else "the game folder."))
      return False
  if auto and not use_source and unwrapper_status() is None:
    log("err", f"'{UNWRAP_SCRIPT_NAME}' not found in the {UTILITIES_DIR_NAME} folder: it is required for automatic unwrap.")
    return False
  check_cancel(cancel)

  if use_source:
    log("step", "[1b] Building the relinker from the GitHub source")
    relinker = ensure_source_relinker(rl, log, cancel, holder)
    if not relinker:
      return False
    log("ok", f"Relinker: {os.path.basename(relinker)} (from source)")
    check_cancel(cancel)
    if auto and not ensure_repo_unwrapper(rl, log, cancel, holder):
      return False
    if build_libs:
      log("step", "[1c] Building the libs from the GitHub source")
      libs = ensure_source_libs(rl, log, cancel, holder, target)
      if not libs:
        return False
      log("ok", f"Libraries: {len(libs['entries'])} files built")
      if target == "windows":
        if any(e[1].lower().endswith(".dll") for e in libs["entries"]):
          log("ok", "Windows target: MinGW runtime DLLs included in libs")
        else:
          log("warn", "Windows target: MinGW runtime DLLs (WinLibs) not available on this system: "
                      "they will be missing from the libs folder.")
      check_cancel(cancel)

  # 2. Output
  output_base = s["output_dir"] or SCRIPT_DIR
  if not os.path.isdir(output_base):
    log("err", f"Invalid output folder: {output_base}")
    return False
  pkg_dir = package_dir(output_base, game_name, target)
  app0_dir = os.path.join(pkg_dir, "app0")
  log("step", "[2] Preparing the output folder")
  log("detail", pkg_dir)
  try:
    if not log_space(space_requirements(folder, target, "relink", output_base, auto,
                                        move=mover is not None), log):
      log("err", "Not enough disk space: free some space or choose another output folder.")
      return False
  except OSError as e:
    log("warn", f"Unable to check the disk space: {e}")
  if os.path.exists(pkg_dir):
    log("info", "Removing the existing folder (confirmed)...")
    try:
      shutil.rmtree(pkg_dir)
    except Exception as e:
      log("err", f"Unable to remove the folder: {e}")
      return False
  os.makedirs(pkg_dir)
  created.append(pkg_dir)
  log("ok", "Output folder created.")
  check_cancel(cancel)

  # 3. Copy the game
  if mover is not None:
    log("step", "[3] Moving the game into app0 (each file is removed from the source only after it is verified)")
    copier = cancellable_move(mover, cancel, log, dir_size(folder))
  else:
    log("step", "[3] Copying the game into app0")
    copier = cancellable_copy(cancel, log, dir_size(folder), sync=False, workers=COPY_WORKERS)
  try:
    shutil.copytree(folder, app0_dir, copy_function=copier)
    copier.finish()
  except Cancelled:
    copier.close() if hasattr(copier, "close") else None
    raise
  except Exception as e:
    copier.close() if hasattr(copier, "close") else None
    log("err", f"Copy failed: {e}")
    return False
  if mover is not None:
    log("ok", f"Game moved into 'app0' ({len(mover.moved)} files)."
              + (f" {mover.leftovers} files could not be removed from the source." if mover.leftovers else ""))
  else:
    log("ok", "Game copied and renamed 'app0'.")
  check_cancel(cancel)

  # 4. Unwrap on the copy (optional)
  if auto:
    log("step", "[4] Automatic unwrap (on the copy in app0)")
    if mover is not None:
      log("info", f"Move mode: the original SELF files are saved in {BACKUP_DIR_NAME}/{game_name}.")
    if smp:
      log("warn", "--sce-module-path: the modules in that folder are not unwrapped automatically "
                  "(unwrap them first with Unwrap mode).")
    results = unwrap_game(app0_dir, get_backup_dir(game_name) if mover is not None else None, log, cancel,
                          with_modules=yes(rl["unwrap_modules"]) and not skip_modules and not smp,
                          extra_dirs=[] if smp else extra_dirs,
                          executables=[] if smp else executables)
    log_results(results, log)
    if any(r[1][0] == ST_ERROR for r in results):
      log("err", "Unwrap failed: relink cancelled.")
      return False
    if file_state(os.path.join(app0_dir, EXECUTABLE_NAME)) != "elf":
      log("err", f"After the unwrap {EXECUTABLE_NAME} is not an ELF: relink cancelled.")
      return False
  else:
    log("step", "[4] Automatic unwrap disabled: skipping")
  check_cancel(cancel)

  # 5. Libs, fonts, input
  log("step", f"[5] Copying '{LIBS_DIR_NAME}' and the optional files")
  try:
    n = extract_libs(libs, os.path.join(pkg_dir, LIBS_DIR_NAME), cancel)
  except Cancelled:
    raise
  except Exception as e:
    log("err", f"Extracting '{LIBS_DIR_NAME}' failed: {e}")
    return False
  log("ok", f"'{LIBS_DIR_NAME}' extracted from {libs_label(libs)} ({n} files).")
  fonts_src = os.path.join(UTILITIES_DIR, FONTS_DIR_NAME)
  if os.path.isdir(fonts_src):
    try:
      shutil.copytree(fonts_src, os.path.join(pkg_dir, FONTS_DIR_NAME))
      log("ok", f"'{FONTS_DIR_NAME}' copied.")
    except Exception as e:
      log("warn", f"Copying '{FONTS_DIR_NAME}' failed: {e}")
  else:
    log("detail", f"No '{FONTS_DIR_NAME}' folder: if the game uses system fonts, add it to {UTILITIES_DIR_NAME}.")
  if yes(rt["copy_input_ini"]) and os.path.isfile(INPUT_INI_FILE):
    try:
      with open(INPUT_INI_FILE, encoding="utf-8-sig") as f:
        text = f.read()
      errs = validate_input_ini(text)
      if errs:
        log("warn", f"{INPUT_INI_NAME} not copied: line {errs[0][0]}: {errs[0][1]}")
      else:
        shutil.copy2(INPUT_INI_FILE, os.path.join(pkg_dir, INPUT_INI_NAME))
        log("ok", f"{INPUT_INI_NAME} copied into the package.")
    except Exception as e:
      log("warn", f"Copying {INPUT_INI_NAME} failed: {e}")
  check_cancel(cancel)

  # 6. Relinker
  out_dir = pkg_dir if OUTPUT_NEXT_TO_APP0 else app0_dir
  out_exe = os.path.join(out_dir, info["exe"])
  input_file = os.path.join(app0_dir, EXECUTABLE_NAME)
  cmd = build_relink_command(relinker, intel, rl, input_file, out_exe)
  log("step", "[6] Running the relinker")
  log("detail", "Command: " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
  autorun = yes(rl.get("autorun", "no"))
  run_env = None
  if autorun:
    run_env = dict(os.environ)
    run_env.update(runtime_env(rt, target, pkg_dir))
    log("info", "--autorun: after the conversion the relinker starts the game (with the Runtime settings) "
                "and this step ends when the game exits.")
  try:
    code = stream_process(cmd, log, cancel, holder, "Relinker", env=run_env,
                          stdin=subprocess.DEVNULL if autorun else None)
  except Cancelled:
    raise
  except Exception as e:
    log("err", f"Unable to start the relinker: {e}")
    return False

  if code != 0 and autorun and os.path.isfile(out_exe):
    # with --autorun the exit code is the one of the game: the conversion itself worked
    log("warn", f"The game exited with code {code}.")
    code = 0
  if code != 0:
    meaning = {1: "invalid arguments", 2: "conversion failed (see below)"}.get(code, "")
    log("err", f"The relinker exited with code {code}" + (f": {meaning}." if meaning else "."))
    log("step", "Last relinker output")
    for line in holder.get("tail", [])[-25:]:
      log("err" if re.search(r"error|fail|cannot|unable|missing|unsupported|invalid", line, re.I) else "info", line)
    return False
  if not os.path.isfile(out_exe):
    log("warn", f"Relinker finished, but {info['exe']} was not found in the output path.")
    return False
  log("ok", f"{info['exe']} created.")
  check_cancel(cancel)

  # 7. Shortcut named after the game
  if yes(rl["make_launcher"]):
    log("step", "[7] Game shortcut")
    create_shortcut(out_dir, info["exe"], target, rt, game_name, log, SCRIPT_DIR)

  # system warnings
  if (target not in ("windows", "macos") and os.name != "nt"
      and os.path.exists("/dev/udmabuf")
      and not os.access("/dev/udmabuf", os.R_OK | os.W_OK)):
    log("warn", "/dev/udmabuf is not accessible: add your user to the kvm group and log in again "
                "(or: setfacl -m u:$USER:rw /dev/udmabuf). Not needed with the proprietary NVIDIA driver.")
  if target == "windows":
    log("detail", "Windows: direct memory (up to 13824 MiB) counts toward the commit limit. "
                  "If 'replace guest placeholder with resident direct memory' appears, enlarge the page file.")

  # 8. Import audit
  if yes(rl["import_audit"]):
    log("step", "[8] Import audit")
    run_import_audit(out_exe, app0_dir, pkg_dir, rl, log, cancel, holder)

  log("step", "Summary")
  log("detail", f"Game:       {game_name}")
  log("detail", f"Package:    {pkg_dir}")
  log("detail", f"Structure:  app0/  {LIBS_DIR_NAME}/")
  log("detail", f"Executable: {out_exe}")
  return True


def run_import_audit(out_exe, app0_dir, pkg_dir, rl, log, cancel, holder):
  script = os.path.join(UTILITIES_DIR, AUDIT_SCRIPT)
  if not os.path.isfile(script):
    log("warn", f"{UTILITIES_DIR_NAME}/{AUDIT_SCRIPT.replace(os.sep, '/')} not found: audit skipped.")
    return
  if not yes(rl["registry"]):
    log("warn", "The audit requires --registry: enable it in the options. Audit skipped.")
    return
  python = find_python()
  if not python:
    log("warn", "Python not found in PATH: audit skipped.")
    return
  stem = os.path.splitext(os.path.basename(out_exe))[0]
  out_dir = os.path.dirname(out_exe)
  registries = sorted(f for f in os.listdir(out_dir)
                      if f.startswith(stem) and f.endswith(".registry.json"))
  if not registries:
    log("warn", "No .registry.json file found: audit skipped.")
    return
  cmd = [python, script] + [os.path.join(out_dir, r) for r in registries]
  cmd += ["--libs", os.path.join(pkg_dir, LIBS_DIR_NAME)]
  smp = rl.get("sce_module_path", "").strip()
  mod_base = os.path.abspath(smp) if smp else app0_dir
  if not yes(rl.get("skip_sce_module", "no")):
    for d in existing_module_dirs(mod_base):
      cmd += ["--modules", os.path.join(mod_base, d)]
  names = rl.get("names_file", "").strip()
  if names:
    if os.path.isfile(names):
      cmd += ["--names", names]
    else:
      log("warn", f"Names file not found ({names}): audit without --names.")
  try:
    code = stream_process(cmd, log, cancel, holder, "Audit", cwd=UTILITIES_DIR)
  except Cancelled:
    raise
  except Exception as e:
    log("warn", f"Audit could not be run: {e}")
    return
  meaning = {0: "nothing blocks loading", 1: "there are 'absent' imports or missing libraries",
             2: "an input is not readable"}.get(code, "")
  log("ok" if code == 0 else "warn", f"Audit finished with code {code}: {meaning}")


# ─────────────────────────────────────────────────────────────
#  Front end (pywebview): the interface is index.html, this is only the bridge
# ─────────────────────────────────────────────────────────────
import json
import webview

L = sys.modules[__name__]   # the logic lives in this same file
HERE = SCRIPT_DIR

# ---------- GUI update check ----------
APP_VERSION = "1.0.6"
GUI_REPO_OWNER = "Lenny4250gh"
GUI_REPO_NAME = "AnyPS5-GUI"
GUI_RELEASES_URL = "https://github.com/%s/%s/releases/latest" % (GUI_REPO_OWNER, GUI_REPO_NAME)


def _version_tuple(v):
  return tuple(int(n) for n in re.findall(r"\d+", v or ""))


def check_gui_update():
  """{'version', 'current'} if a newer release exists on GitHub, else None. Never raises."""
  try:
    info = L.fetch_release_info(GUI_REPO_OWNER, GUI_REPO_NAME, "")
    tag = info["tag"]
    if _version_tuple(tag) > _version_tuple(APP_VERSION):
      return {"version": tag, "current": APP_VERSION, "packaged": bool(getattr(sys, "frozen", False)),
              "notes": (info.get("notes") or "")[:6000]}
  except Exception:
    pass  # offline, rate limit, no release yet: stay silent
  return None

# ---------- GUI self-update (download the release asset, replace the app, restart) ----------
_OS_TOKENS = {"windows": {"win", "windows", "win32", "win64"},
              "macos": {"mac", "macos", "osx", "darwin", "macosx"},
              "linux": {"linux", "appimage"}}
_ARCH_ARM = {"arm64", "aarch64", "armv8"}
_ARCH_X64 = {"x64", "x86_64", "amd64", "win64"}
_SKIP_EXT = (".sha256", ".sha512", ".sig", ".asc", ".txt", ".md", ".json", ".blockmap", ".msi",
             ".deb", ".rpm", ".yml", ".yaml", ".pem", ".sbom", ".spdx")
_OK_EXT = {"windows": (".exe", ".zip"),
           "macos": (".zip", ".dmg", ".tar.gz", ".tgz"),
           "linux": (".appimage", ".zip", ".tar.gz", ".tgz", "")}


class UpdateUnavailable(Exception):
  """Automatic update not possible here: the GUI falls back to the release page."""


def _host_arch():
  import platform
  m = platform.machine().lower()
  return "arm" if (m in _ARCH_ARM or m.startswith("arm")) else "x64"


def pick_gui_asset(assets, host=None, arch=None):
  """Asset of the release built for this system (None if there is none)."""
  host, arch = host or L.host_target(), arch or _host_arch()
  best, best_score = None, -1
  for a in assets:
    n = a["name"].lower()
    if n.endswith(_SKIP_EXT):
      continue
    if host == "windows" and re.search(r"setup|install", n):
      continue  # a wizard cannot replace the exe silently
    tokens = set(re.split(r"[^a-z0-9]+", n))
    oses = {k for k, v in _OS_TOKENS.items() if tokens & v}
    ext = next((e for e in (".tar.gz", ".tgz", ".appimage", ".exe", ".zip", ".dmg") if n.endswith(e)), "")
    if not ext and "." in n.rsplit("-", 1)[-1]:
      continue  # unknown extension
    score = 0
    if oses:
      if host not in oses:
        continue
      score += 10
    elif not ((host == "windows" and ext == ".exe") or (host == "linux" and ext == "")):
      continue
    if ext not in _OK_EXT[host]:
      continue
    a_arm, a_x64 = bool(tokens & _ARCH_ARM), bool(tokens & _ARCH_X64)
    if a_arm or a_x64:
      if (a_arm and arch == "arm") or (a_x64 and arch == "x64"):
        score += 3
      elif host != "macos":
        continue  # wrong CPU (macOS x64 still runs through Rosetta: kept, lower score)
    score += {".exe": 2, ".appimage": 2, ".zip": 1}.get(ext, 0)
    if score > best_score:
      best, best_score = a, score
  return best


def _safe_extract_zip(path, dest):
  base = os.path.realpath(dest)
  with zipfile.ZipFile(path) as z:
    for info in z.infolist():
      target = os.path.realpath(os.path.join(dest, info.filename))
      if target != base and not target.startswith(base + os.sep):
        raise OSError("unsafe path in the archive: " + info.filename)
    z.extractall(dest)
    for info in z.infolist():  # keep the executable bit
      mode = (info.external_attr >> 16) & 0o777
      if mode and not info.is_dir():
        try:
          os.chmod(os.path.join(dest, info.filename), mode | 0o600)
        except OSError:
          pass


def _safe_extract_tar(path, dest):
  import tarfile
  base = os.path.realpath(dest)
  with tarfile.open(path) as t:
    for m in t.getmembers():
      target = os.path.realpath(os.path.join(dest, m.name))
      if target != base and not target.startswith(base + os.sep):
        raise OSError("unsafe path in the archive: " + m.name)
    t.extractall(dest)


def _single_root(folder):
  """Descends while a folder contains only one folder."""
  while True:
    items = [i for i in os.listdir(folder) if not i.startswith("__MACOSX") and i != ".DS_Store"]
    if len(items) == 1 and os.path.isdir(os.path.join(folder, items[0])) and not items[0].endswith(".app"):
      folder = os.path.join(folder, items[0])
    else:
      return folder


def _find_main_exe(root, host, cur_name):
  files = [f for f in os.listdir(root) if os.path.isfile(os.path.join(root, f))]
  if host == "windows":
    files = [f for f in files if f.lower().endswith(".exe")]
  else:
    files = [f for f in files if not f.lower().endswith((".txt", ".md", ".so", ".json", ".png", ".ini"))]
  if not files:
    return None
  if cur_name in files:
    return cur_name
  return max(files, key=lambda f: os.path.getsize(os.path.join(root, f)))


def _run_detached(cmd, marker=None):
  env = dict(os.environ)
  if marker:
    env["ANYPS5_UPDATE_MARKER"] = marker  # inherited by the new app, which deletes it once its window is up
  env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"  # the new onefile exe must not reuse our temp folder
  for k in ("_MEIPASS2", "_PYI_APPLICATION_HOME_DIR", "_PYI_ARCHIVE_FILE", "_PYI_PARENT_PROCESS_LEVEL"):
    env.pop(k, None)
  if os.name == "nt":
    # NO_WINDOW + NEW_PROCESS_GROUP only. DETACHED_PROCESS must NOT be added: Windows then ignores
    # NO_WINDOW and cmd.exe opens a visible console (the black terminal during the update).
    flags = 0x00000200 | 0x08000000  # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE, in case a console is created anyway
    subprocess.Popen(cmd, creationflags=flags, startupinfo=si, close_fds=True, env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  else:
    subprocess.Popen(cmd, start_new_session=True, close_fds=True, env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


_BAT = """@echo off
chcp 65001 >nul
if exist "{splash}" start "" /b powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{splash}"
set "MAIN={main}"
set "DEST={dest}"
set "APPDIR={appdir}"
set "PAYLOAD={payload}"
set "STAGE={stage}"
set /a N=0
:retry
move /y "%MAIN%" "%DEST%" >nul 2>&1
if not errorlevel 1 goto moved
set /a N+=1
if %N% GEQ 120 goto fail
ping -n 2 127.0.0.1 >nul
goto retry
:moved
if exist "%PAYLOAD%\\_internal" if exist "%APPDIR%\\_internal" rmdir /s /q "%APPDIR%\\_internal"
xcopy "%PAYLOAD%\\*" "%APPDIR%\\" /e /y /i /q >nul 2>&1
:fail
rmdir /s /q "%STAGE%" >nul 2>&1
set "PYINSTALLER_RESET_ENVIRONMENT=1"
start "" /b "%DEST%"
(goto) 2>nul & del "%~f0"
"""

_PS_SPLASH = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
$marker = '@@MARKER@@'
$dest = '@@DEST@@'
$W = 420; $H = 132
$t0 = Get-Date
$f = New-Object System.Windows.Forms.Form
$f.Text = 'AnyPS5 GUI'
$f.FormBorderStyle = 'None'
$f.StartPosition = 'CenterScreen'
$f.TopMost = $true
$f.ShowInTaskbar = $false
$f.ClientSize = New-Object System.Drawing.Size($W, $H)
$f.BackColor = [System.Drawing.Color]::FromArgb(17, 19, 18)
$f.GetType().GetProperty('DoubleBuffered', [Reflection.BindingFlags]'Instance,NonPublic').SetValue($f, $true, $null)

function New-RoundPath($x, $y, $w, $h, $r) {
  $gp = New-Object System.Drawing.Drawing2D.GraphicsPath
  $d = $r * 2
  $gp.AddArc($x, $y, $d, $d, 180, 90)
  $gp.AddArc($x + $w - $d, $y, $d, $d, 270, 90)
  $gp.AddArc($x + $w - $d, $y + $h - $d, $d, $d, 0, 90)
  $gp.AddArc($x, $y + $h - $d, $d, $d, 90, 90)
  $gp.CloseFigure()
  return $gp
}
$f.Region = New-Object System.Drawing.Region((New-RoundPath 0 0 $W $H 18))

$white = [System.Drawing.Color]::FromArgb(240, 240, 240)
$dim = [System.Drawing.Color]::FromArgb(150, 154, 152)
$line = [System.Drawing.Color]::FromArgb(52, 56, 54)
$track = [System.Drawing.Color]::FromArgb(40, 43, 42)
$fTitle = New-Object System.Drawing.Font('Segoe UI Semibold', 13)
$fSub = New-Object System.Drawing.Font('Segoe UI', 9.5)
$bWhite = New-Object System.Drawing.SolidBrush($white)
$bDim = New-Object System.Drawing.SolidBrush($dim)
$bTrack = New-Object System.Drawing.SolidBrush($track)
$penLine = New-Object System.Drawing.Pen($line, 1)
$penIcon = New-Object System.Drawing.Pen($white, 2.4)
$penIcon.StartCap = 'Round'; $penIcon.EndCap = 'Round'; $penIcon.LineJoin = 'Round'
$penIconDim = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(70, 240, 240, 240), 2)

$f.Add_Paint({
  param($sender, $e)
  $g = $e.Graphics
  $g.SmoothingMode = 'AntiAlias'
  $g.TextRenderingHint = 'ClearTypeGridFit'
  $sec = ((Get-Date) - $t0).TotalSeconds
  # border
  $bp = New-RoundPath 0.5 0.5 ($W - 1) ($H - 1) 18
  $g.DrawPath($penLine, $bp)
  # icon: circle with a bobbing download arrow
  $g.DrawEllipse($penIconDim, 28, 30, 44, 44)
  $bob = [Math]::Sin($sec * 4) * 2.5
  $g.DrawLine($penIcon, 50, (41 + $bob), 50, (56 + $bob))
  $g.DrawLine($penIcon, 43, (50 + $bob), 50, (57 + $bob))
  $g.DrawLine($penIcon, 57, (50 + $bob), 50, (57 + $bob))
  $g.DrawLine($penIcon, 43, 64, 57, 64)
  # texts
  $g.DrawString('Updating AnyPS5 GUI', $fTitle, $bWhite, 88, 30)
  $g.DrawString('The app will restart by itself.', $fSub, $bDim, 89, 56)
  # indeterminate bar
  $tx = 28; $ty = 100; $tw = $W - 56; $th = 6; $sw = 120
  $tp = New-RoundPath $tx $ty $tw $th 3
  $g.FillPath($bTrack, $tp)
  $g.SetClip($tp)
  $pos = (($sec * 0.55) % 1.0)
  $ease = 0.5 - 0.5 * [Math]::Cos($pos * 2 * [Math]::PI)
  $sx = $tx - $sw + (($tw + $sw) * $pos)
  $sp = New-RoundPath $sx $ty $sw $th 3
  $g.FillPath($bWhite, $sp)
  $g.ResetClip()
})

$tm = New-Object System.Windows.Forms.Timer
$tm.Interval = 33
$tm.Add_Tick({
  $f.Invalidate()
  $el = ((Get-Date) - $t0).TotalSeconds
  $done = $false
  if ($el -gt 3 -and -not (Test-Path -LiteralPath $marker)) { $done = $true }
  if (-not $done -and $el -gt 2 -and ([int]($el * 30) % 15 -eq 0)) {
    # fallback for builds that do not clear the marker: a NEW process of the app with a visible window
    try {
      $np = Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.MainWindowHandle -ne 0 -and $_.StartTime -gt $t0 -and $_.Path -eq $dest }
      if ($np) { $done = $true }
    } catch {}
  }
  if ($done -or $el -gt 90) { $tm.Stop(); $f.Close() }
})
$tm.Start()
[void]$f.ShowDialog()
Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
"""

_SH_LINUX = """#!/bin/sh
MAIN="$1"; DEST="$2"; APPDIR="$3"; PAYLOAD="$4"; STAGE="$5"; shift 5
i=0
while [ $i -lt 90 ]; do
  alive=0
  for p in "$@"; do kill -0 "$p" 2>/dev/null && alive=1; done
  [ $alive -eq 0 ] && break
  sleep 1; i=$((i+1))
done
sleep 1
MARK="$ANYPS5_UPDATE_MARKER"
SPL=""
MSG="Updating AnyPS5 GUI... The app will restart by itself."
if command -v zenity >/dev/null 2>&1; then
  (sleep 120) | zenity --progress --pulsate --no-cancel --auto-close --title="AnyPS5 GUI" --text="$MSG" --width=380 >/dev/null 2>&1 &
  SPL=$!
elif command -v kdialog >/dev/null 2>&1; then
  kdialog --title "AnyPS5 GUI" --passivepopup "$MSG" 60 >/dev/null 2>&1 &
  SPL=$!
elif command -v xmessage >/dev/null 2>&1; then
  xmessage -center "$MSG" >/dev/null 2>&1 &
  SPL=$!
elif command -v notify-send >/dev/null 2>&1; then
  notify-send "AnyPS5 GUI" "$MSG" >/dev/null 2>&1
fi
if mv -f "$MAIN" "$DEST"; then
  chmod +x "$DEST"
  if [ -d "$PAYLOAD/_internal" ]; then rm -rf "$APPDIR/_internal"; fi
  cp -a "$PAYLOAD/." "$APPDIR/" 2>/dev/null
fi
rm -rf "$STAGE"
export PYINSTALLER_RESET_ENVIRONMENT=1
setsid "$DEST" >/dev/null 2>&1 &
if [ -n "$MARK" ]; then
  j=0
  while [ -e "$MARK" ] && [ $j -lt 90 ]; do sleep 1; j=$((j+1)); done
fi
[ -n "$SPL" ] && kill "$SPL" 2>/dev/null
rm -f "$MARK" "$0"
"""

_SH_MAC = """#!/bin/sh
NEWAPP="$1"; OLDAPP="$2"; KEEP="$3"; STAGE="$4"; shift 4
i=0
while [ $i -lt 90 ]; do
  alive=0
  for p in "$@"; do kill -0 "$p" 2>/dev/null && alive=1; done
  [ $alive -eq 0 ] && break
  sleep 1; i=$((i+1))
done
sleep 1
MARK="$ANYPS5_UPDATE_MARKER"
osascript -e 'display dialog "Updating AnyPS5 GUI..." & return & return & "The app will reopen by itself." with title "AnyPS5 GUI" buttons {} giving up after 120 with icon note' >/dev/null 2>&1 &
SPL=$!
SAVE="$STAGE/keep"
if [ -n "$KEEP" ] && [ -e "$OLDAPP/$KEEP" ]; then mkdir -p "$SAVE" && mv "$OLDAPP/$KEEP" "$SAVE/data"; fi
BACKUP="$OLDAPP.old"
rm -rf "$BACKUP"
if mv "$OLDAPP" "$BACKUP"; then
  if ditto "$NEWAPP" "$OLDAPP"; then
    rm -rf "$BACKUP"
  else
    rm -rf "$OLDAPP"; mv "$BACKUP" "$OLDAPP"
  fi
fi
if [ -n "$KEEP" ] && [ -e "$SAVE/data" ]; then rm -rf "$OLDAPP/$KEEP"; mv "$SAVE/data" "$OLDAPP/$KEEP"; fi
xattr -dr com.apple.quarantine "$OLDAPP" 2>/dev/null
rm -rf "$STAGE"
export PYINSTALLER_RESET_ENVIRONMENT=1
open -n "$OLDAPP"
if [ -n "$MARK" ]; then
  j=0
  while [ -e "$MARK" ] && [ $j -lt 90 ]; do sleep 1; j=$((j+1)); done
fi
kill "$SPL" 2>/dev/null
rm -f "$MARK" "$0"
"""


def _mac_bundle():
  """Path of the running .app bundle, or None."""
  p = os.path.realpath(sys.executable)
  while p and p != os.path.dirname(p):
    if p.endswith(".app"):
      return p
    p = os.path.dirname(p)
  return None


def _mac_extract_app(dl, stage):
  """Unpacks zip / tar.gz / dmg into stage and returns the path of the .app inside."""
  out = os.path.join(stage, "x")
  os.makedirs(out, exist_ok=True)
  n = dl.lower()
  if n.endswith(".zip"):
    subprocess.run(["ditto", "-x", "-k", dl, out], check=True)  # keeps symlinks and permissions
  elif n.endswith((".tar.gz", ".tgz")):
    _safe_extract_tar(dl, out)
  elif n.endswith(".dmg"):
    mnt = os.path.join(stage, "mnt")
    os.makedirs(mnt, exist_ok=True)
    subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noverify", "-mountpoint", mnt, dl],
                   check=True, stdout=subprocess.DEVNULL)
    try:
      apps = [a for a in os.listdir(mnt) if a.endswith(".app")]
      if not apps:
        raise OSError("no .app inside the dmg")
      subprocess.run(["ditto", os.path.join(mnt, apps[0]), os.path.join(out, apps[0])], check=True)
    finally:
      subprocess.run(["hdiutil", "detach", "-force", mnt], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  else:
    raise OSError("unsupported archive: " + dl)
  for root, dirs, _ in os.walk(out):
    for d in dirs:
      if d.endswith(".app"):
        return os.path.join(root, d)
    if root.count(os.sep) - out.count(os.sep) >= 2:
      dirs[:] = []
  raise OSError("no .app found in the downloaded file")


class _PausableCancel(threading.Event):
  """Cancel flag that blocks the downloading thread while `paused` is set (used by the GUI update)."""
  def __init__(self):
    super().__init__()
    self.paused = threading.Event()

  def is_set(self):
    while self.paused.is_set() and not super().is_set():
      time.sleep(0.1)
    return super().is_set()


def prepare_and_launch_update(asset, status, cancel):
  """Downloads `asset`, stages it and starts the external installer script. The caller must then
  quit the app. Raises UpdateUnavailable when the update cannot be applied automatically."""
  import tempfile
  host = L.host_target()
  if not getattr(sys, "frozen", False):
    raise UpdateUnavailable("not running from a packaged executable")
  pids = sorted({os.getpid(), os.getppid()} - {0, 1})

  if host == "macos":
    bundle = _mac_bundle()
    if not bundle:
      raise UpdateUnavailable("the app is not inside a .app bundle")
    if not os.access(os.path.dirname(bundle), os.W_OK):
      raise UpdateUnavailable("no write permission in " + os.path.dirname(bundle))
    stage = tempfile.mkdtemp(prefix="anyps5-update-")
  else:
    dest = os.environ.get("APPIMAGE") if host == "linux" and os.environ.get("APPIMAGE") else sys.executable
    dest = os.path.abspath(dest)
    appdir = os.path.dirname(dest)
    if not os.access(appdir, os.W_OK):
      raise UpdateUnavailable("no write permission in " + appdir)
    stage = os.path.join(L.UTILITIES_DIR, "update")
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage, exist_ok=True)

  errors = []

  def log(kind, msg):
    if kind == "percent":
      status(pct=msg if msg is not None else 0)
    elif kind == "progress" and msg:
      status(text=msg)
    elif kind == "err":
      errors.append(msg)
  dl = os.path.join(stage, "download-" + re.sub(r"[^\w.\-]", "_", asset["name"]))
  status(state="downloading", pct=0, text="Downloading " + asset["name"] + "…")
  if not L._download_file(asset["url"], dl, asset["size"], asset["sha256"], log, cancel, asset["name"]):
    shutil.rmtree(stage, ignore_errors=True)
    raise RuntimeError(errors[-1] if errors else "download failed")

  status(state="installing", pct=None, text="Preparing the installation…")
  mfd, marker = tempfile.mkstemp(prefix="anyps5-upd-", suffix=".run")  # splash stays up while this exists
  os.close(mfd)
  if host == "macos":
    newapp = _mac_extract_app(dl, stage)
    keep = ""
    ud = os.path.realpath(L.UTILITIES_DIR)
    if ud.startswith(os.path.realpath(bundle) + os.sep):  # settings live inside the bundle: carry them over
      keep = os.path.relpath(ud, os.path.realpath(bundle))
    fd, script = tempfile.mkstemp(prefix="anyps5-upd-", suffix=".sh")
    with os.fdopen(fd, "w") as f:
      f.write(_SH_MAC)
    os.chmod(script, 0o700)
    _run_detached(["/bin/sh", script, newapp, bundle, keep, stage] + [str(p) for p in pids], marker)
    return

  # Windows / Linux: single file, or an archive with the file (and maybe its folder)
  low = dl.lower()
  if low.endswith(".zip") or low.endswith((".tar.gz", ".tgz")):
    ex = os.path.join(stage, "payload")
    os.makedirs(ex, exist_ok=True)
    (_safe_extract_zip if low.endswith(".zip") else _safe_extract_tar)(dl, ex)
    payload = _single_root(ex)
    shutil.rmtree(os.path.join(payload, L.UTILITIES_DIR_NAME), ignore_errors=True)  # never touch user data
  else:
    payload = os.path.join(stage, "payload")
    os.makedirs(payload, exist_ok=True)
    shutil.move(dl, os.path.join(payload, os.path.basename(dest)))
  main = _find_main_exe(payload, host, os.path.basename(dest))
  if not main:
    shutil.rmtree(stage, ignore_errors=True)
    try:
      os.remove(marker)
    except OSError:
      pass
    raise RuntimeError("no executable found in the downloaded file")
  if host == "linux":
    os.chmod(os.path.join(payload, main), 0o755)
  main_path = os.path.join(payload, main)

  if host == "windows":
    fd, script = tempfile.mkstemp(prefix="anyps5-upd-", suffix=".bat")
    esc = lambda p: p.replace("%", "%%")
    sfd, splash = tempfile.mkstemp(prefix="anyps5-splash-", suffix=".ps1")
    with os.fdopen(sfd, "w", encoding="utf-8-sig") as f:  # BOM: PowerShell 5 reads UTF-8 paths correctly
      f.write(_PS_SPLASH.replace("@@MARKER@@", marker.replace("'", "''")).replace("@@DEST@@", dest.replace("'", "''")))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
      f.write(_BAT.format(main=esc(main_path), dest=esc(dest), appdir=esc(appdir),
                          payload=esc(payload), stage=esc(stage), splash=esc(splash)).replace("\n", "\r\n"))
    _run_detached(["cmd.exe", "/c", script], marker)
  else:
    fd, script = tempfile.mkstemp(prefix="anyps5-upd-", suffix=".sh")
    with os.fdopen(fd, "w") as f:
      f.write(_SH_LINUX)
    os.chmod(script, 0o700)
    _run_detached(["/bin/sh", script, main_path, dest, appdir, payload, stage] + [str(p) for p in pids], marker)


# Options schema (generated from the old SettingsDialog; if you add options, update it here)
SCHEMA = {'Conversion': [{'type': 'flag',
                 'section': 'relinker',
                 'key': 'rosetta',
                 'label': '--to-rosetta (RDSEED→RDRAND, CLWB→NOP, RDPID→0)',
                 'plat': 'nowin',
                 'hint': 'Implies --to-intel. Not compatible with Windows. For macOS on Apple silicon.'},
                {'type': 'flag',
                 'section': 'relinker',
                 'key': 'gui',
                 'label': '--windows-gui (GUI subsystem)',
                 'plat': 'win',
                 'hint': 'Output goes to anyps5.log next to the exe when there is no console.'},
                {'type': 'flag',
                 'section': 'relinker',
                 'key': 'diagnostics',
                 'label': '--windows-diagnostics',
                 'plat': 'win',
                 'hint': 'Dependency diagnostics at startup.'},
                {'type': 'flag',
                 'section': 'relinker',
                 'key': 'trace_imports',
                 'label': '--trace-imports',
                 'plat': 'win',
                 'hint': 'Use APS5_TRACE_CALLS (Runtime tab) to choose the libraries. Conflicts with '
                         '--lazy-binding.'},
                {'type': 'text',
                 'section': 'relinker',
                 'key': 'fixed_base',
                 'label': '--windows-fixed-base',
                 'plat': 'win',
                 'hint': 'Only if the executable needs an exact address, e.g. 0x400000 (multiple of 64 '
                         'KiB).'},
                {'type': 'choice',
                 'section': 'relinker',
                 'key': 'unused_filter',
                 'label': 'unused-filter',
                 'plat': 'all',
                 'values': ['0', '1', '2'],
                 'hint': None},
                {'type': 'flag',
                 'section': 'relinker',
                 'key': 'registry',
                 'label': '--registry (writes the .registry.json files)',
                 'plat': 'all',
                 'hint': None},
                {'type': 'text',
                 'section': 'relinker',
                 'key': 'rpath',
                 'label': '--rpath',
                 'plat': 'all',
                 'hint': 'Empty = default $ORIGIN/libs. Windows: ASCII path. Linux: absolute path or '
                         'starting with $ORIGIN.'},
                {'type': 'text',
                 'section': 'relinker',
                 'key': 'sce_module_path',
                 'label': '--sce-module-path',
                 'plat': 'all',
                 'hint': 'Parent folder of sce_module / sce_modules / prx (not the module folder itself). '
                         'Empty = the game folder. When set, --module-dir and --executable are relative to '
                         'it, and its modules are not unwrapped or auto-detected by the GUI.'},
                {'type': 'text',
                 'section': 'relinker',
                 'key': 'module_dirs',
                 'label': '--module-dir (separated by ;)',
                 'plat': 'all',
                 'hint': 'Other module folders, e.g. Media/Plugins (Unity plugins loaded by the title).'},
                {'type': 'flag',
                 'section': 'relinker',
                 'key': 'auto_module_dirs',
                 'label': 'Auto-detect extra module folders (--module-dir)',
                 'plat': 'all',
                 'hint': 'Finds folders with .prx files outside sce_module/prx (e.g. Media/Modules).'},
                {'type': 'text',
                 'section': 'relinker',
                 'key': 'executables',
                 'label': '--executable (separated by ;)',
                 'plat': 'all',
                 'hint': 'Other executables of the title, e.g. picker/picker.bin.'},
                {'type': 'flag',
                 'section': 'relinker',
                 'key': 'autorun',
                 'label': '--autorun (run the game after the conversion)',
                 'plat': 'host',
                 'hint': 'The target system must be the one of this computer. The game starts with the '
                         'Runtime settings and the relink ends when it exits.'},
                {'type': 'flag',
                 'section': 'relinker',
                 'key': 'trap_syscall_stubs',
                 'label': '--trap-sdk-syscall-stubs',
                 'plat': 'all',
                 'hint': 'Debug only: replaces the ps5-payload-dev syscall wrapper stubs with UD2, so unused '
                         'wrappers do not block relinking (a wrapper that runs raises an illegal-instruction '
                         'exception).'},
                {'type': 'flag',
                 'section': 'relinker',
                 'key': 'unwrap_modules',
                 'label': 'Also unwrap all modules (sce_module, prx...)',
                 'plat': 'all',
                 'hint': 'SELF modules are skipped by the relinker: they must be converted first.'},
                {'type': 'flag',
                 'section': 'relinker',
                 'key': 'make_launcher',
                 'label': 'Create a shortcut named after the game (with the Runtime settings)',
                 'plat': 'all',
                 'hint': 'Windows .lnk (only creatable when the GUI runs on Windows), Linux .desktop, '
                         'macOS .command.'},
                {'type': 'flag',
                 'section': 'relinker',
                 'key': 'import_audit',
                 'label': 'import_audit',
                 'plat': 'all',
                 'hint': None},
                {'type': 'text',
                 'section': 'relinker',
                 'key': 'names_file',
                 'label': 'Audit: names file (--names)',
                 'plat': 'all',
                 'hint': "Optional: aerolib.csv (from tools/nid_names.py) to give a name to 'absent' "
                         'imports.'},
                {'type': 'flag',
                 'section': 'relinker',
                 'key': 'lazy_binding',
                 'label': '--lazy-binding',
                 'plat': 'all',
                 'hint': 'Lazy symbol binding instead of eager. Incompatible with bundled ELF modules and '
                         '--trace-imports.'},
                {'type': 'flag',
                 'section': 'relinker',
                 'key': 'skip_sce_module',
                 'label': '--skip-sce-module',
                 'plat': 'all',
                 'hint': 'Disables bundled module processing and the module folder checks. Conflicts with '
                         '--sce-module-path, --module-dir, --executable and --exclude-sce-module.'},
                {'type': 'text',
                 'section': 'relinker',
                 'key': 'exclude_sce_modules',
                 'label': '--exclude-sce-module',
                 'plat': 'all',
                 'hint': 'File names (not paths) of bundled modules to leave out, separated by ;. A missing '
                         'name is an error.'},
                {'type': 'flag',
                 'section': 'relinker',
                 'key': 'skip_syscall_check',
                 'label': '--skip-syscall-check',
                 'plat': 'all',
                 'hint': 'Disables the syscall scan of the executable and of the bundled modules.'}],
 'Source build': [{'type': 'text',
                   'section': 'relinker',
                   'key': 'release_tag',
                   'label': 'Release tag (Release source)',
                   'plat': None,
                   'hint': 'Empty = the latest release of the repository (e.g. v0.1.2 for a fixed version). '
                           'Files are saved in Utilities/release/<repo>/<tag>/ with the names used in the release.'},
                  {'type': 'flag',
                   'section': 'relinker',
                   'key': 'release_update',
                   'label': 'Check for a newer release before every run (Release source)',
                   'plat': None,
                   'hint': 'ON = asks GitHub which release is current and downloads only the missing files. '
                           'OFF = reuse what was already downloaded (no network).'},
                  {'type': 'text',
                   'section': 'relinker',
                   'key': 'repo_ref',
                   'label': 'Branch or tag',
                   'plat': None,
                   'hint': "Empty = the repository's default branch."},
                  {'type': 'text',
                   'section': 'relinker',
                   'key': 'repo_target',
                   'label': 'CMake target',
                   'plat': None,
                   'hint': "Empty = try '…', and build everything if that target does not exist. Also used "
                           'as the name of the executable to pick up.'},
                  {'type': 'text',
                   'section': 'relinker',
                   'key': 'repo_cmake_args',
                   'label': 'Extra CMake arguments',
                   'plat': None,
                   'hint': 'Added to the configure step, e.g. -G Ninja -DCMAKE_C_COMPILER=clang '
                           '-DCMAKE_CXX_COMPILER=clang++ (use it to choose another compiler than Visual '
                           'Studio).'},
                  {'type': 'flag',
                   'section': 'relinker',
                   'key': 'repo_libs_from_repo',
                   'label': 'Build the libs (.prx) from the repository (instead of Utilities/prx-*.zip)',
                   'plat': None,
                   'hint': "ON = builds the 'libs' target (full CMake configuration: much slower, downloads "
                           'FFmpeg, needs the SDL/Vulkan build dependencies) and uses those libs instead of '
                           'Utilities/prx-*.zip.'},
                  {'type': 'flag',
                   'section': 'relinker',
                   'key': 'repo_update',
                   'label': 'Check for updates before every relink',
                   'plat': None,
                   'hint': 'ON = fetch the latest commit and rebuild only if it changed. OFF = reuse the '
                           'existing build.'}],
 'Runtime': [{'type': 'text',
              'section': 'runtime',
              'key': 'gpu',
              'label': 'GPU (ANYPS5_GPU)',
              'plat': 'all',
              'hint': "Part of the device name, lowercase. Names appear at startup ('Physical device "
                      "candidate')."},
             {'type': 'text',
              'section': 'runtime',
              'key': 'fonts_dir',
              'label': 'Fonts (ANYPS5_SYSTEM_FONTS)',
              'plat': 'all',
              'hint': 'Empty = anyps5-fonts/ folder next to the executable.'},
             {'type': 'flag',
              'section': 'runtime',
              'key': 'store_at_flip',
              'label': 'APS5_STORE_AT_FLIP=1',
              'plat': 'all',
              'hint': 'Writes GPU results at every flip: useful if graphics get corrupted, costs '
                      'performance.'},
             {'type': 'text',
              'section': 'runtime',
              'key': 'trace_calls',
              'label': 'APS5_TRACE_CALLS',
              'plat': 'win',
              'hint': 'Needs --trace-imports. E.g. libkernel,libSceSaveData or all.'},
             {'type': 'flag',
              'section': 'runtime',
              'key': 'disable_dozen',
              'label': 'Disable Mesa Dozen (VK_LOADER_DRIVERS_DISABLE=*dzn*)',
              'plat': 'win',
              'hint': 'Only if a D3D12 driver crashes and you have another Vulkan driver.'},
             {'type': 'flag',
              'section': 'runtime',
              'key': 'copy_input_ini',
              'label': 'Copy … into the package (Input tab)',
              'plat': 'all',
              'hint': None},
             {'type': 'text',
              'section': 'runtime',
              'key': 'pad_input_file',
              'label': 'APS5_PAD_INPUT_FILE',
              'plat': 'all',
              'hint': 'For test runs: absolute path of the command file that lets tools/pad_input.py send '
                      'controller pulses to the game.'},
             {'type': 'text',
              'section': 'runtime',
              'key': 'sdl_videodriver',
              'label': 'SDL_VIDEODRIVER',
              'plat': 'nowin',
              'hint': 'Linux only: x11 or wayland. Wayland is preferred when WAYLAND_DISPLAY is set; x11 selects XWayland. Some titles that bundle their own SDL fail with x11.'},
             {'type': 'text',
              'section': 'runtime',
              'key': 'thread_priority',
              'label': 'APS5_THREAD_PRIORITY',
              'plat': 'all',
              'hint': 'off, high or rt. Raises the AGC queue workers and the presenter above the title threads. A refusal by rtkit/SetThreadPriority stops the game.'},
             {'type': 'text',
              'section': 'runtime',
              'key': 'thread_nice',
              'label': 'APS5_THREAD_NICE',
              'plat': 'nowin',
              'hint': 'Linux, with priority high: nice value, default -10.'},
             {'type': 'text',
              'section': 'runtime',
              'key': 'thread_rt',
              'label': 'APS5_THREAD_RT',
              'plat': 'nowin',
              'hint': 'Linux, with priority rt: real-time priority, default 10.'},
             {'type': 'text',
              'section': 'runtime',
              'key': 'trace_frames',
              'label': 'APS5_TRACE_FRAMES',
              'plat': 'all',
              'hint': '<start>:<count>: records <count> presents from present <start> (Perfetto trace + aps5-summary.jsonl). Not together with APS5_TRACE_SECONDS.'},
             {'type': 'text',
              'section': 'runtime',
              'key': 'trace_seconds',
              'label': 'APS5_TRACE_SECONDS',
              'plat': 'all',
              'hint': '<start>:<length> in seconds, length above 0. Not together with APS5_TRACE_FRAMES.'},
             {'type': 'text',
              'section': 'runtime',
              'key': 'trace_summary_every',
              'label': 'APS5_TRACE_SUMMARY_EVERY',
              'plat': 'all',
              'hint': 'Seconds (whole number above 0): appends one summary line per interval to aps5-summary.jsonl for long sessions.'},
             {'type': 'text',
              'section': 'runtime',
              'key': 'trace_events',
              'label': 'APS5_TRACE_EVENTS',
              'plat': 'all',
              'hint': 'Events per trace buffer, 1 to 4194304 (default 262144, 32 bytes each, per thread).'},
             {'type': 'text',
              'section': 'runtime',
              'key': 'trace_dir',
              'label': 'APS5_TRACE_DIR',
              'plat': 'all',
              'hint': 'Absolute folder for the trace files. Empty = working directory.'},
             {'type': 'flag',
              'section': 'runtime',
              'key': 'pipeline_stats',
              'label': 'APS5_PIPELINE_STATS=1',
              'plat': 'all',
              'hint': 'Prints driver statistics for every new pipeline. Needs VK_KHR_pipeline_executable_properties; slows pipeline compilation.'},
             {'type': 'flag',
              'section': 'runtime',
              'key': 'gpu_checkpoints',
              'label': 'APS5_GPU_CHECKPOINTS=1',
              'plat': 'all',
              'hint': 'Device-loss reports list the last markers the GPU reached (VK_NV_device_diagnostic_checkpoints, NVIDIA).'},
             {'type': 'text',
              'section': 'runtime',
              'key': 'extra_env',
              'label': 'Extra variables (NAME=value;...)',
              'plat': None,
              'hint': "Added to the game shortcut and to the 'Launch game' button. Separate with ; ."}]}


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
  except Exception:
    pass


def clean(x):
  return json.loads(json.dumps(x, default=str))


class Api:
  def __init__(self):
    self.cfg = L.load_config()
    self.schema = SCHEMA
    self.folder = ""
    self.q = queue.Queue()
    self.running = False
    self.result = None
    self.cancel_ev = threading.Event()
    self.holder = {}
    self.game_proc = None
    self.quitting = False      # True once the user confirmed closing during an operation
    self._quit_asked = False   # a confirmation popup is already on screen
    self._upd = {"state": "idle"}  # progress of the GUI self-update
    self._mov = {"state": "idle"}  # progress of moving the Utilities folder
    self.relocated = False         # True once Utilities/ was moved: the app must be reopened

  # ---------- closing the window ----------
  def on_closing(self):
    """Window close event. Idle (or already confirmed) -> close. Operation running -> the close is
    vetoed and the in-app popup asks for confirmation."""
    if self.quitting or not self.running:
      return True
    if not self._quit_asked:
      self._quit_asked = True

      def ask():  # evaluate_js from a separate thread: calling it inside the handler could deadlock
        try:
          webview.windows[0].evaluate_js("window.confirmQuit && window.confirmQuit()")
        except Exception:
          self._quit_asked = False
      threading.Thread(target=ask, daemon=True).start()
    return False

  def quit_answered(self):
    self._quit_asked = False
    return True

  def force_quit(self):
    """The user confirmed: stop the operation, wait for its cleanup/rollback, then close the app."""
    self.quitting = True
    self._quit_asked = False
    self.cancel()

    def finish():
      deadline = time.time() + 300  # rollback of a big move can take a while
      while self.running and time.time() < deadline:
        time.sleep(0.2)
      try:
        webview.windows[0].destroy()
      except Exception:
        os._exit(0)
    threading.Thread(target=finish, daemon=True).start()
    return True

  # ---------- state ----------
  def get_state(self):
    inp = ""
    try:
      inp = open(L.INPUT_INI_FILE, encoding="utf-8-sig").read() if os.path.isfile(L.INPUT_INI_FILE) else L.INPUT_TEMPLATE
    except OSError:
      inp = L.INPUT_TEMPLATE
    return clean({"cfg": self.cfg, "schema": self.schema, "host": L.host_target(), "folder": self.folder,
                  "targets": {k: v["label"] for k, v in L.TARGETS.items()}, "languages": L.LANGUAGES,
                  "present_modes": L.PRESENT_MODES, "default_repo": L.DEFAULT_REPO_URL,
                  "utilities": L.UTILITIES_DIR_NAME, "defaults": L.DEFAULTS, "input": inp, "input_name": L.INPUT_INI_NAME,
                  "storage": {"path": L.UTILITIES_DIR, "default": L.DEFAULT_UTILITIES_DIR, "editable": L.STORAGE_EDITABLE,
                              "problem": L.STORAGE_PROBLEM},
                  "version": APP_VERSION})

  def set_cfg(self, values):
    if self.relocated:
      return False
    for s, items in values.items():
      self.cfg[s].update({k: str(v) for k, v in items.items()})
    L.save_config(self.cfg)
    return True

  def check_update(self):
    return clean(check_gui_update())

  def start_update(self):
    """Download the release asset for this system, replace the app and restart it."""
    if self.running:
      return {"ok": False, "error": "Finish or cancel the running operation first."}
    if self._upd.get("state") in ("downloading", "installing", "paused"):
      return {"ok": True}
    self._upd = {"state": "downloading", "pct": 0, "text": "Looking for the release…"}
    threading.Thread(target=self._update_worker, daemon=True).start()
    return {"ok": True}

  def update_status(self):
    return clean(self._upd)

  def pause_update(self):
    c = getattr(self, "_upd_cancel", None)
    if c is not None and self._upd.get("state") == "downloading":
      c.paused.set()
      self._upd["state"] = "paused"
    return {"ok": True}

  def resume_update(self):
    c = getattr(self, "_upd_cancel", None)
    if c is not None and self._upd.get("state") == "paused":
      self._upd.update(state="downloading", text="Resuming…")
      c.paused.clear()
    return {"ok": True}

  def _update_worker(self):
    def status(**k):
      self._upd.update(k)
    try:
      info = L.fetch_release_info(GUI_REPO_OWNER, GUI_REPO_NAME, "")
      asset = pick_gui_asset(info["assets"])
      if not asset:
        raise UpdateUnavailable("the release has no file for this system")
      self._upd_cancel = _PausableCancel()
      prepare_and_launch_update(asset, status, self._upd_cancel)
    except UpdateUnavailable as e:
      status(state="failed", fallback=True, text="Automatic update not available: %s." % e)
      return
    except Exception as e:
      status(state="failed", fallback=True, text="Update failed: %s" % e)
      return
    status(state="restarting", pct=100, text="Restarting…")
    self.quitting = True
    time.sleep(0.8)
    try:
      webview.windows[0].destroy()
    except Exception:
      pass
    time.sleep(1.5)
    os._exit(0)

  # ---------- storage folder (Utilities) ----------
  def pick_storage(self):
    """Folder dialog; returns the Utilities folder that would be used inside the picked folder."""
    res = webview.windows[0].create_file_dialog(getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
                                                or webview.FOLDER_DIALOG)
    return L.storage_choice_to_dir(res[0]) if res else None

  def default_storage(self):
    return L.DEFAULT_UTILITIES_DIR

  def move_storage(self, path):
    """Moves Utilities/ to `path` in a thread (progress: storage_status)."""
    if self.running:
      return {"ok": False, "error": "Finish or cancel the running operation first."}
    if self._mov.get("state") == "moving":
      return {"ok": True}
    path = os.path.abspath(str(path or "").strip()) if path else ""
    problem = L.validate_storage_target(path)
    if problem:
      return {"ok": False, "error": problem}
    self._mov = {"state": "moving", "pct": None, "text": "Starting…"}
    threading.Thread(target=self._move_worker, args=(path,), daemon=True).start()
    return {"ok": True}

  def storage_status(self):
    return clean(self._mov)

  def _move_worker(self, path):
    def status(text, pct):
      self._mov.update(text=text, pct=pct)
    try:
      L.relocate_utilities(path, status)
    except Exception as e:
      self._mov = {"state": "failed", "text": str(e)}
      return
    self.relocated = True
    self._mov = {"state": "done", "pct": 100, "text": "Done. Close and reopen the app to use the new folder.", "path": path}

  def open_release_page(self):
    import webbrowser
    webbrowser.open(GUI_RELEASES_URL)  # fixed URL: nothing coming from the UI is opened
    return True

  def open_link(self, url):
    """Opens a link of the release notes in the browser (https only)."""
    import webbrowser
    from urllib.parse import urlparse
    u = urlparse(str(url or ""))
    if u.scheme == "https" and u.netloc:
      webbrowser.open(u.geturl())
      return True
    return False

  def open_origin_page(self):
    import webbrowser
    webbrowser.open("https://github.com/Lenny4250gh/AnyPS5-GUI")  # fixed URL
    return True

  def pick_folder(self):
    res = webview.windows[0].create_file_dialog(getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
                                                or webview.FOLDER_DIALOG)
    if res:
      self.folder = res[0]
    return self.folder

  def pick_output(self):
    res = webview.windows[0].create_file_dialog(getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
                                                or webview.FOLDER_DIALOG)
    return res[0] if res else None

  def pick_archive(self):
    kind = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
    res = webview.windows[0].create_file_dialog(kind, file_types=(L.ARCHIVE_DIALOG_FILTER, "All files (*.*)"))
    if res:
      self.folder = res if isinstance(res, str) else res[0]
    return self.folder

  pick_zip = pick_archive  # old name

  def game_name(self):
    """Name of the game: folder name, or (for a zip) its internal folder / the zip name."""
    if L.is_archive_source(self.folder):
      return L.archive_game_name(self.folder)
    return os.path.basename(os.path.normpath(os.path.abspath(self.folder)))

  def set_folder(self, path):
    self.folder = (path or "").strip().strip('"')
    return self.folder

  def save_log(self, text):
    kind = getattr(getattr(webview, "FileDialog", None), "SAVE", None) or webview.SAVE_DIALOG
    res = webview.windows[0].create_file_dialog(kind, save_filename="anyps5-log.txt")
    if res:
      path = res if isinstance(res, str) else res[0]
      with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return True

  # ---------- status ----------
  def status(self):
    rl, target = self.cfg["relinker"], self.cfg["relinker"]["target"]
    L.set_active_relinker_cfg(rl)
    U = L.UTILITIES_DIR_NAME
    if L.repo_unwrapper_path(rl):
      unw = ("tools/" + L.UNWRAP_SCRIPT_NAME + " (GitHub repo)  ✔", "ok")
    elif L.unwrapper_status():
      unw = (os.path.basename(L.unwrapper_status()) + "  ✔", "ok")
    elif rl.get("relinker_source") == "github":
      unw = ("will be taken from the GitHub repo  ⚠", "warn")
    elif rl.get("relinker_source") == "release":
      unw = ("will be downloaded from the GitHub release  ⚠", "warn")
    else:
      unw = (f"not found in {U}  ✖", "err")
    li = L.libs_info(target, rl)
    libs = ((L.libs_label(li) + "  ✔", "ok") if li else
            (("will be built from the GitHub repo  ⚠", "warn") if L.wants_built_libs(rl)
             else ("will be downloaded from the GitHub release  ⚠", "warn") if rl.get("relinker_source") == "release"
             else (f"no {L.LIBS_ZIP_PREFIX}*.zip in {U}  ✖", "err")))
    rows = []
    zinfo = L.scan_archive(self.folder) if L.is_archive_source(self.folder) else None
    if zinfo and zinfo.get("error"):
      rows += [["eboot", "eboot.bin", "cannot read the archive  ✖  " + zinfo["error"], "err"],
               ["modules", "Modules (sce_module/prx)", "—", "muted"], ["dirs", "sce_sys", "—", "muted"]]
      zinfo = {"skip": True}
    info = L.scan_game(self.folder, target) if self.folder and not zinfo else None
    if zinfo and zinfo.get("skip"):
      pass
    elif zinfo:
      rows.append(["eboot", "eboot.bin", *(("inside the archive  ✔  extracted automatically", "ok") if zinfo["eboot"]
                                           else ("not found in the archive  ✖", "err"))])
      if zinfo["module_dirs"]:
        rows.append(["modules", "Modules (sce_module/prx)",
                     f"{', '.join(zinfo['module_dirs'])}: {zinfo['modules_total']} files (in the archive)  ✔", "ok"])
      else:
        rows.append(["modules", "Modules (sce_module/prx)", "none found in the archive  ✖", "err"])
      rows.append(["dirs", "sce_sys", "present  ✔" if zinfo["dirs"] else "not found  ✖",
                   "ok" if zinfo["dirs"] else "err"])
    elif info is None:
      for k, t in (("eboot", "eboot.bin"), ("modules", "Modules (sce_module/prx)"), ("dirs", "sce_sys")):
        rows.append([k, t, "—", "muted"])
    else:
      texts = {"elf": ("ELF  ✔  ready", "ok"), "self": ("SELF  ⚠  needs unwrapping", "warn"),
               "unknown": ("unknown format  ✖", "err"), "missing": ("not found  ✖", "err")}
      rows.append(["eboot", "eboot.bin", *texts[info["eboot"]]])
      if info["module_error"]:
        rows.append(["modules", "Modules (sce_module/prx)", info["module_error"] + "  ✖", "err"])
      else:
        d = f"{', '.join(info['module_dirs'])}: {info['modules_total']} files"
        if info["modules_self"]:
          rows.append(["modules", "Modules (sce_module/prx)", d + f", {info['modules_self']} SELF to unwrap  ⚠", "warn"])
        else:
          rows.append(["modules", "Modules (sce_module/prx)", d + "  ✔", "ok"])
      rows.append(["dirs", "sce_sys", "present  ✔" if info["sce_sys"] else "not found  ✖",
                   "ok" if info["sce_sys"] else "err"])
    rows += [["unwrapper", f"{U}/unwrap_self.py", *unw], ["relinker", "Relinker", *L.relinker_status(rl)],
             ["libs", f"{U}/prx-*.zip", *libs]]
    return clean(rows)

  def space(self):
    if L.is_archive_source(self.folder):
      z = L.scan_archive(self.folder)
      if not z:
        return None
      if z.get("error"):
        return {"text": z["error"], "level": "err"}
      free = shutil.disk_usage(L.existing_parent(L.UTILITIES_DIR)).free
      need = z["size"] * 2 + 64 * 1024 * 1024  # extracted copy + game copy (rough estimate)
      return {"text": f"~{L.format_size(need)} needed (archive extraction + package) · {L.format_size(free)} free"
                      + ("  ✔" if free >= need else "  ✖  not enough space"),
              "level": "ok" if free >= need else "err"}
    if not self.folder or not os.path.isdir(self.folder):
      return None
    s, rl = self.cfg["settings"], self.cfg["relinker"]
    try:
      r = L.space_requirements(self.folder, rl["target"], s["mode"], s["output_dir"] or L.SCRIPT_DIR,
                               L.yes(s["auto_unwrap"]), not L.yes(s["keep_game"]))
    except Exception:
      return {"text": "unable to calculate", "level": "warn"}
    avail = r["free"] + r["reusable"]
    t = f"~{L.format_size(r['needed'])} needed · {L.format_size(avail)} free"
    return {"text": t + ("  ✔" if avail >= r["needed"] else "  ✖  not enough space"),
            "level": "ok" if avail >= r["needed"] else "err"}

  # ---------- actions ----------
  def prepare(self, action):
    """Checks + the confirmations the Tk version asked with dialogs. -> {error, confirms[], tools[]}"""
    res = {"error": None, "confirms": [], "tools": []}
    if action in ("play", "download"):
      return res
    zipsrc = L.is_archive_source(self.folder)
    if action != "build" and not (self.folder and (os.path.isdir(self.folder) or zipsrc)):
      res["error"] = "Select the game folder (e.g. PPSAxxxxx) or an archive of the game (.zip or .rar)."
      return res
    if zipsrc and action != "build":
      z = L.scan_archive(self.folder)
      if z.get("error"):
        res["error"] = "Cannot read the archive: " + z["error"]
        return res
    if action not in ("build", "restore"):
      problems = L.archive_game_problems(L.scan_archive(self.folder)) if zipsrc else L.folder_game_problems(self.folder)
      if problems:
        res["error"] = (("The archive" if zipsrc else "The folder") + " does not look like a PS5 game, missing: "
                        + "; ".join(problems) + ".")
        return res
    cfg = self.cfg
    rl = cfg["relinker"]
    if action == "restore":
      if zipsrc:
        res["error"] = "Restore backup works on a game folder, not on an archive: select the folder."
        return res
      
      if not os.path.isdir(L.get_backup_dir(os.path.basename(os.path.normpath(self.folder)))):
        res["error"] = "There are no backups for this game."
      else:
        res["confirms"].append(["Restore backup", "The current files in the game (eboot.bin and modules) will be "
                                "OVERWRITTEN with the original backups. Proceed?"])
      return res
    if action == "build":
      p = L.source_preflight(dict(rl))
      if p and not p.startswith(("git", "cmake", "git and")):
        res["error"] = p
        return res
    elif cfg["settings"]["mode"] == "unwrap":
      return res
    if rl.get("relinker_source") == "github":
      missing = L.missing_build_tools(rl)
      if missing:
        names = ", ".join(L.TOOL_NAMES[t] for t in missing)
        if os.name != "nt" or not shutil.which("winget"):
          res["error"] = f"To build from source you need: {names}. Install them with your package manager / by hand."
          return res
        res["confirms"].append(["Install missing tools", f"Missing: {names}. Install them now, automatically "
                                "(winget)? Windows may ask for administrator permission."])
        res["tools"] = missing
    if action == "start":
      base = cfg["settings"]["output_dir"] or L.SCRIPT_DIR
      pkg = L.package_dir(base, self.game_name(), rl["target"])
      if os.path.exists(pkg):
        res["confirms"].append(["Folder already exists", f"'{os.path.basename(pkg)}' already exists in {base}. Overwrite it?"])
      if not L.yes(cfg["settings"]["keep_game"]):
        res["confirms"].append(["Move the game", "'Keep PS5 game' is OFF: the game files will be MOVED out of "
                                f"{self.folder}. Each file is deleted only after it has been copied and verified; "
                                "if the operation fails or is stopped, the files are put back. Continue?"])
    return res

  def run(self, action, tools=None):
    if self.running:
      return False
    folder = self.folder
    cfg = {s: dict(v) for s, v in self.cfg.items()}
    rl, mode = cfg["relinker"], cfg["settings"]["mode"]
    tools = tools or []
    errs = []

    def log0(k, m):
      if k == "err":
        errs.append(str(m))
      self.q.put((k, m))
    if action == "play":
      return self._play(cfg, log0)
    if action == "download":
      title = "Downloading the GitHub release"
      job = lambda log: (L.set_active_relinker_cfg(rl),
                         L.ensure_release_assets(rl, log, self.cancel_ev, rl["target"]))[1]
      mode_download = True
    else:
      mode_download = False
    if mode_download:
      pass
    elif action == "restore":
      title, job = "Restoring backup", lambda log: L.do_restore(folder, log, self.cancel_ev)
    elif action == "build":
      def job(log):
        if tools and not L.install_build_tools(tools, log, self.cancel_ev, self.holder):
          return False
        return L.ensure_source_relinker(rl, log, self.cancel_ev, self.holder) is not None
      title = "Building the relinker from source"
    elif mode == "unwrap":
      title, job = "Mode: Unwrap only", lambda log: L.do_unwrap(folder, cfg, log, self.cancel_ev, self.holder)
    else:
      def job(log):
        if tools and not L.install_build_tools(tools, log, self.cancel_ev, self.holder):
          return False
        return L.do_relink(folder, cfg, log, self.cancel_ev, self.holder)
      title = ("Mode: Convert PS5 game to PC + automatic unwrap" if L.yes(cfg["settings"]["auto_unwrap"])
               else "Mode: Convert PS5 game to PC") + " · " + L.TARGETS[rl["target"]]["label"].split(" (")[0]
    if action in ("start", "unwrap_only") and L.is_archive_source(folder):
      zip_path, inner = folder, job

      def job(log):
        nonlocal folder
        root = L.extract_game_archive(zip_path, log, self.cancel_ev)
        if not root:
          return False
        folder = root
        ok = False
        try:
          ok = inner(log)
          return ok
        finally:
          if mode == "unwrap":
            log("info", f"Unwrap-only on an archive works on the extracted copy, kept in: {root}")
          else:
            L.cleanup_extracted(root, log)
    self.running, self.result = True, None
    self.cancel_ev.clear()
    self.holder = {}
    log0("step", title)

    def work():
      t0, ok = time.time(), False
      try:
        ok = bool(job(log0))
      except L.Cancelled:
        log0("warn", "Operation cancelled.")
      except Exception as e:
        log0("err", f"Unexpected error: {e}")
      dt = time.time() - t0
      if self.cancel_ev.is_set():
        log0("warn", f"Cancelled after {dt:.0f}s.")
      else:
        log0("ok" if ok else "err", f"Operation completed ({dt:.0f}s)." if ok else f"Completed with errors ({dt:.0f}s).")
      if L.yes(cfg["settings"].get("notify", "yes")) and not self.cancel_ev.is_set():
        try:
          game = L.archive_game_name(folder) if L.is_archive_source(folder) else (
            os.path.basename(os.path.normpath(folder)) if folder else "")
        except Exception:
          game = ""
        ntitle, ntext = L.build_notification(title, game, ok, dt, errs)
        L.notify_desktop(ntitle if os.name == "nt" else "AnyPS5 · " + ntitle, ntext)
      self.result, self.running = ok, False

    threading.Thread(target=work, daemon=True).start()
    return True

  def _pick_exe_path(self):
    kind = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
    d = getattr(self, "last_exe_dir", "")
    if not (d and os.path.isdir(d)):
      d = self.cfg["settings"]["output_dir"] or L.SCRIPT_DIR
    res = webview.windows[0].create_file_dialog(
      kind, directory=d, file_types=("Game executable (*.exe;*.elf)", "All files (*.*)"))
    if not res:
      return None
    path = res if isinstance(res, str) else res[0]
    self.last_exe_dir = os.path.dirname(path)
    return path

  def play_exe(self):
    """Launch game: choose the executable and start it with the CURRENT Runtime settings."""
    path = self._pick_exe_path()
    if not path:
      return False
    if self.game_proc is not None and self.game_proc.poll() is None:
      self.q.put(("warn", "The game is already running."))
      return False
    cfg = {s: dict(v) for s, v in self.cfg.items()}
    target = L.exe_target(path)
    pkg = os.path.dirname(os.path.abspath(path))
    try:
      self.game_proc = L.launch_game(pkg, target, cfg, exe=path, log=lambda k, m: self.q.put((k, m)))
      self.q.put(("ok", f"Game started (PID {self.game_proc.pid}) from {pkg}"))
    except OSError as e:
      self.q.put(("err", f"Unable to launch the game: {e}"))
      return False
    return True

  def shortcut_exe(self):
    """Create a shortcut for the chosen executable with the CURRENT Runtime settings."""
    path = self._pick_exe_path()
    if not path:
      return False
    cfg = {s: dict(v) for s, v in self.cfg.items()}
    target = L.exe_target(path)
    pkg = os.path.dirname(os.path.abspath(path))
    L.create_shortcut(pkg, os.path.basename(path), target, cfg["runtime"],
                      L.game_name_from_exe(path), lambda k, m: self.q.put((k, m)), L.SCRIPT_DIR)
    return True

  def _play(self, cfg, log):
    if not self.folder:
      log("err", "Select the game folder first.")
      return False
    base = cfg["settings"]["output_dir"] or L.SCRIPT_DIR
    target = cfg["relinker"]["target"]
    pkg = L.package_dir(base, self.game_name(), target)
    if self.game_proc is not None and self.game_proc.poll() is None:
      log("warn", "The game is already running.")
      return False
    try:
      self.game_proc = L.launch_game(pkg, target, cfg, log=log)
      log("ok", f"Game started (PID {self.game_proc.pid}) from {pkg}")
    except FileNotFoundError:
      log("err", f"Run the Relink for this game first. Expected: {pkg}")
    except OSError as e:
      log("err", f"Unable to launch the game: {e}")
    return True

  def poll(self):
    lines, pct = [], "keep"
    while not self.q.empty():
      k, m = self.q.get()
      if k == "percent":
        pct = m
      else:
        lines.append([k, str(m)])
    return {"lines": lines, "pct": pct, "running": self.running, "ok": self.result}

  def cancel(self):
    self.cancel_ev.set()
    p = self.holder.get("proc")
    try:
      if p is not None and p.poll() is None:
        L.kill_tree(p)
    except Exception:
      pass
    self.q.put(("warn", "Cancellation requested…"))

  # ---------- settings ----------
  def check_input(self, text):
    return [f"Line {n}: {m}" for n, m in L.validate_input_ini(text)[:6]]

  def reset_input(self):
    return L.INPUT_TEMPLATE

  # ---------- import tools into Utilities (Local files option) ----------
  def _tool_dest(self, name):
    """(relative destination inside Utilities, None) for a file the program knows, else (None, reason)."""
    low = name.lower()
    if low == L.UNWRAP_SCRIPT_NAME:
      return L.UNWRAP_SCRIPT_NAME, None
    if low.startswith(L.RELINKER_PREFIX):
      if os.name == "nt" and not low.endswith(".exe"):
        return None, "not a Windows relinker (.exe)"
      if os.name != "nt" and (low.endswith(".exe") or low.endswith(L.NOT_BINARY_EXT)):
        return None, "not a relinker for this system"
      return name, None
    if low.startswith(L.LIBS_ZIP_PREFIX) and low.endswith(".zip"):
      return name, None
    if low in ("import_audit.py", "pad_input.py"):
      return os.path.join("tools", name), None
    return None, "not a file the program uses"

  def pick_tool_files(self):
    """Choose files to copy into Utilities/. Returns the plan: [{src, name, dest, exists, skip}]."""
    kind = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
    res = webview.windows[0].create_file_dialog(
      kind, allow_multiple=True,
      file_types=("Tool files (*.py;*.zip;*.exe)", "All files (*.*)"))
    if not res:
      return []
    paths = [res] if isinstance(res, str) else list(res)
    plan = []
    for src in paths:
      name = os.path.basename(src)
      rel, why = self._tool_dest(name)
      item = {"src": src, "name": name, "skip": why or "", "dest": rel or "", "exists": False}
      if rel:
        dest = os.path.join(L.UTILITIES_DIR, rel)
        item["exists"] = os.path.isfile(dest)
        try:
          if item["exists"] and os.path.samefile(src, dest):
            item["skip"] = "already in the Utilities folder"
        except OSError:
          pass
      plan.append(item)
    return plan

  def import_tool_files(self, plan, replace):
    """Copy the files of the plan into Utilities/. Existing ones are overwritten only if `replace`."""
    done, kept, errors = [], [], []
    for it in plan or []:
      if it.get("skip") or not it.get("dest"):
        continue
      rel, why = self._tool_dest(os.path.basename(it["src"]))  # never trust the destination coming from the page
      if not rel:
        continue
      dest = os.path.join(L.UTILITIES_DIR, rel)
      if os.path.isfile(dest) and not replace:
        kept.append(rel)
        continue
      try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".part"
        shutil.copyfile(it["src"], tmp)
        if os.name != "nt" and rel.lower().startswith(L.RELINKER_PREFIX):
          os.chmod(tmp, os.stat(tmp).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(tmp, dest)
        done.append(rel)
      except OSError as e:
        errors.append(f"{rel}: {e}")
    L._unwrapper = None  # unwrap_self.py may have been replaced: reload it on next use
    L._libs_cache.clear()
    for k, m in [("ok", f"Imported into {L.UTILITIES_DIR_NAME}: " + ", ".join(done)) if done else None,
                 ("info", "Kept existing: " + ", ".join(kept)) if kept else None,
                 ("err", "Import failed: " + "; ".join(errors)) if errors else None]:
      self.q.put((k, m))
    return {"done": done, "kept": kept, "errors": errors}

  def import_input(self):
    """Pick an input .ini made by someone else: its text goes into the editor (nothing is written yet)."""
    kind = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
    res = webview.windows[0].create_file_dialog(kind, file_types=("Input config (*.ini;*.txt)", "All files (*.*)"))
    if not res:
      return None
    path = res if isinstance(res, str) else res[0]
    try:
      if os.path.getsize(path) > 256 * 1024:
        return {"error": "The file is too big to be an input configuration."}
      with open(path, encoding="utf-8-sig") as fh:
        text = fh.read()
    except (OSError, UnicodeDecodeError) as e:
      return {"error": f"Unable to read the file: {e}"}
    errs = [f"Line {n}: {m}" for n, m in L.validate_input_ini(text)[:6]]
    return {"name": os.path.basename(path), "text": text.replace("\r\n", "\n"), "errors": errs}

  def export_input(self, text):
    """Save the input configuration currently shown in the editor to an .ini file chosen by the user."""
    kind = getattr(getattr(webview, "FileDialog", None), "SAVE", None) or webview.SAVE_DIALOG
    res = webview.windows[0].create_file_dialog(kind, save_filename=L.INPUT_INI_NAME,
                                                file_types=("Input config (*.ini)", "All files (*.*)"))
    if not res:
      return None
    path = res if isinstance(res, str) else res[0]
    try:
      with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text.rstrip() + "\n")
    except OSError as e:
      return {"error": f"Unable to save the file: {e}"}
    return {"name": os.path.basename(path)}

  def export_section(self, tab, values):
    """Save the options of one settings section (Conversion, Source build, Runtime) to a .json file."""
    kind = getattr(getattr(webview, "FileDialog", None), "SAVE", None) or webview.SAVE_DIALOG
    fname = "anyps5-" + re.sub(r"[^a-z0-9]+", "-", str(tab).lower()).strip("-") + ".json"
    res = webview.windows[0].create_file_dialog(kind, save_filename=fname,
                                                file_types=("Settings (*.json)", "All files (*.*)"))
    if not res:
      return None
    path = res if isinstance(res, str) else res[0]
    try:
      with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"anyps5_settings": tab, "values": values}, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    except OSError as e:
      return {"error": f"Unable to save the file: {e}"}
    return {"name": os.path.basename(path)}

  def import_section(self):
    """Pick a .json made by export_section: its content is returned to the UI (nothing is written yet)."""
    kind = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
    res = webview.windows[0].create_file_dialog(kind, file_types=("Settings (*.json)", "All files (*.*)"))
    if not res:
      return None
    path = res if isinstance(res, str) else res[0]
    try:
      if os.path.getsize(path) > 256 * 1024:
        return {"error": "The file is too big to be a settings file."}
      with open(path, encoding="utf-8-sig") as fh:
        data = json.load(fh)
    except (OSError, UnicodeDecodeError, ValueError) as e:
      return {"error": f"Unable to read the file: {e}"}
    if not isinstance(data, dict) or not isinstance(data.get("values"), dict) or "anyps5_settings" not in data:
      return {"error": "This is not an AnyPS5 settings file."}
    return {"name": os.path.basename(path), "tab": str(data["anyps5_settings"]), "values": data["values"]}

  def delete_input(self):
    try:
      if os.path.isfile(L.INPUT_INI_FILE):
        os.remove(L.INPUT_INI_FILE)
      return True
    except OSError as e:
      return str(e)

  def save_settings(self, values, input_text, write_input):
    if self.relocated:
      return {"error": "The storage folder was changed: close and reopen the app first."}
    new = {s: dict(v) for s, v in self.cfg.items()}
    for s, items in values.items():
      new[s].update({k: str(v) for k, v in items.items()})
    problem = (L.validate_relink_options(L.yes(new["settings"]["intel"]), new["relinker"])
               or L.validate_runtime_options(new["runtime"], new["relinker"]))
    if problem:
      return {"error": problem}
    if write_input:
      body = input_text.rstrip() + "\n"
      has = any(__import__("re").split(r"[#;]", l, 1)[0].strip() for l in body.splitlines())
      try:
        os.makedirs(L.UTILITIES_DIR, exist_ok=True)
        if has:
          with open(L.INPUT_INI_FILE, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        elif os.path.isfile(L.INPUT_INI_FILE):
          os.remove(L.INPUT_INI_FILE)
      except OSError as e:
        return {"error": f"Unable to save {L.INPUT_INI_NAME}: {e}"}
    self.cfg = new
    L.save_config(new)
    return {"error": None}


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
  except Exception:
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
  if UTILITIES_DIR != LEGACY_UTILITIES_DIR:  # before Api(): it reads config.ini
    migrate_legacy_utilities()  # <program folder>/Utilities (first versions)
    migrate_legacy_utilities(INTERIM_UTILITIES_DIR)  # <user data>/Utilities (previous build)
  _html = load_embedded_ui()
  _src = {"html": _html} if _html else {"url": find_ui()}
  api = Api()
  window = webview.create_window("AnyPS5 GUI - Unofficial | v" + APP_VERSION, js_api=api, width=1280, height=760,
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