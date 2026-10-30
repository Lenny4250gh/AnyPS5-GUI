"""AnyPS5 GUI

Originally part of https://github.com/Lenny4250gh/AnyPS5-GUI (GPL-2.0-only).

Graphical interface to use AnyPS5 without touching the command line:
  - Unwrap   : already-decrypted SELF/fSELF -> ELF, using the OFFICIAL unwrap_self.py
               (external file in the app data folder, not embedded here)
  - Relink   : builds the "<GAME>-<system>" package with app0 + libs + executable
  - Options  : all relinker options (Windows / Linux / macOS, --to-intel,
               --to-rosetta, --windows-gui, --windows-fixed-base, --linux-fixed-base,
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
"""
import base64
import configparser
import hashlib
import json
import importlib.util
import os
import queue
import re
import shlex
import shutil
import socket
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

# Default output folder (setting "output_dir" empty): "Converted games" next to the program
DEFAULT_OUTPUT_DIR = os.path.join(SCRIPT_DIR, "Converted games")

# Folder name used by the first versions (next to the program). Kept only to find and migrate the old
# data, and as the data folder of the portable / from-source mode (so existing data is not lost).
LEGACY_DIR_NAME = "Utilities"
LEGACY_DATA_DIR = os.path.join(SCRIPT_DIR, LEGACY_DIR_NAME)  # old location: next to the program
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


LOCATION_FILE = os.path.join(_user_data_dir(), "location.txt")  # tiny pointer: where the data folder was moved by the user
DEFAULT_DATA_DIR = _user_data_dir()  # the app's files go directly in here (location.txt is the only extra)
INTERIM_DATA_DIR = os.path.join(_user_data_dir(), LEGACY_DIR_NAME)  # layout of an earlier build: migrated
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


# Executable: the data folder is the user data folder, or the folder chosen in Settings → Storage
# (the old folder next to the program is moved there at startup, see migrate_legacy_data).
# From source or in portable mode: next to the script.
if STORAGE_EDITABLE:
  DATA_DIR = DEFAULT_DATA_DIR
  _custom = _read_location()
  if _custom:
    try:
      os.makedirs(_custom, exist_ok=True)
      DATA_DIR = _custom
    except OSError as _e:
      STORAGE_PROBLEM = "The folder you chose (%s) is not available (%s): using the default one." % (_custom, _e)
else:
  DATA_DIR = LEGACY_DATA_DIR
try:
  os.makedirs(DATA_DIR, exist_ok=True)
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
AUDIT_SCRIPT = os.path.join("tools", "import_audit.py")  # relative to the data folder
CONFIG_FILE = os.path.join(DATA_DIR, "config.ini")
LEGACY_CONFIG_FILE = os.path.join(DATA_DIR, "UseRelinker.ini")
INPUT_INI_FILE = os.path.join(DATA_DIR, INPUT_INI_NAME)

# relinker built from a GitHub repository (alternative to the prebuilt relinker-*.exe)
DEFAULT_REPO_URL = "https://github.com/boykopovar/AnyPS5"
SOURCE_DIR_NAME = "source"            # <data>/source/<repo>-<hash>/{repo,build}
SOURCE_INFO_NAME = "build_info.ini"
SOURCE_CLONED_MARK = ".cloned"        # written after a complete clone
RELINKER_TARGET_GUESS = "relinker"    # CMake target tried first when none is configured
RELINKER_SOURCES = ("prebuilt", "github", "release")
RELEASE_DIR_NAME = "release"          # <data>/release/<repo>/<tag>/ (files named as in the release)
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
OVERLAY_POSITIONS = ["", "tl", "tr", "bl", "br"]  # ANYPS5_OVERLAY_POS (USAGE.md, performance overlay)
SCREENSHOTS_DIR_NAME = "anyps5-screenshots"  # next to the executable unless ANYPS5_SCREENSHOTS is set

# default option values (all saved in config.ini as strings)
DEFAULTS = {
  "settings": {"output_dir": "", "intel": "no", "auto_unwrap": "yes", "mode": "relink", "notify": "yes", "open_trash": "yes",
               "keep_game": "yes", "window_size": "", "open_sections": ""},
  "relinker": {
    "target": "windows", "rosetta": "no", "gui": "no", "diagnostics": "no",
    "fixed_base": "", "linux_fixed_base": "", "macos_fixed_base": "", "unused_filter": "0", "registry": "yes", "rpath": "",
    "trace_imports": "no", "module_dirs": "", "executables": "",
    "unwrap_modules": "yes", "move_root_prx": "yes", "import_audit": "no", "make_launcher": "yes",
    "audit_strict_stubs": "no", "audit_strict_providers": "no", "audit_source": "",
    "patch_catalog": "", "patch_timeout": "", "nid_patcher_path": "", "nid_only_sce": "no",
    "nid_preserve_exports": "", "nid_preserve_list": "",
    "names_file": "",
    "auto_module_dirs": "yes",
    "relinker_source": "release", "repo_url": DEFAULT_REPO_URL, "repo_ref": "",
    "release_tag": "", "release_update": "yes",
    "repo_target": "", "repo_update": "yes", "repo_update_ask": "yes", "repo_cmake_args": "", "repo_build_jobs": "",
    "repo_libs_from_repo": "yes",
    "sce_module_path": "", "autorun": "no", "trap_syscall_stubs": "no",
    "lazy_binding": "no", "skip_sce_module": "no", "exclude_sce_modules": "",
    "skip_syscall_check": "no", "patches_file": "", "no_patches": "no", "extra_args": "",
    "frame_red_zone": "no",
  },
  "runtime": {
    "language": "", "gpu": "", "present_mode": "", "fonts_dir": "",
    "store_at_flip": "no", "trace_calls": "", "disable_dozen": "no",
    "copy_input_ini": "yes",
    "extra_env": "", "pad_input_file": "",
    "sdl_videodriver": "", "thread_priority": "", "thread_nice": "", "thread_rt": "",
    "trace_frames": "", "trace_seconds": "", "trace_summary_every": "", "trace_events": "",
    "trace_dir": "", "pipeline_stats": "no", "gpu_checkpoints": "no",
    "diagnostics": "no",
    "overlay": "no", "overlay_pos": "", "screenshots_dir": "", "shader_cache_dir": "", "no_shader_cache": "no",
    "fullscreen": "no", "display_index": "", "entitlements_file": "",
    "trace_bench": "no", "trace_calls_file": "", "trace_uds": "no", "videoout_telemetry_dir": "",
    "store_at_label": "no",
    "draw_pipeline": "no", "pipelined_draws": "", "pipeline_dispatch": "no", "verify_draw_pipeline": "no",
    "no_pipeline_lto": "no", "no_pipeline_journal": "no", "pipeline_journal_entries": "",
    "pipeline_journal_max_mib": "", "pipeline_journal_threads": "", "pipeline_store_entries": "",
    "address_copy_max_mib": "", "pm4_ring": "", "skip_refused_draws": "no", "mimalloc": "no",
    "gpu_wait_timeout_ms": "", "stall_watchdog_secs": "", "hang_dump_secs": "", "crash_stack_bytes": "",
    "no_discord_rpc": "no", "env_catalog": "", "host_report": "yes", "rumble": "100", "game_mode": "off", "pad_speaker_db": "0", "log_file": "",
    "sound_fx": "yes", "sound_volume": "60",
    "playgo_unlisted_chunks": "installed",
  },
}


class Cancelled(Exception):
  pass


# ─────────────────────────────────────────────────────────────
#  External unwrapper (official unwrap_self.py)
# ─────────────────────────────────────────────────────────────
_unwrapper = None
_unwrapper_path = None
UPDATE_PROMPT = None   # set by the UI: function(info, cancel) -> {"update": bool} or None (asks about a new commit)
_DECLINED_REPOS = set()  # repository folders for which the user chose "keep this build" in this run
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
  option), else the data folder."""
  path = release_unwrapper_path() or repo_unwrapper_path()
  if path:
    return path
  path = os.path.join(DATA_DIR, UNWRAP_SCRIPT_NAME)
  return path if os.path.isfile(path) else None


def github_mode():
  return bool(_active_rl) and _active_rl.get("relinker_source") == "github"


def load_unwrapper():
  """Load unwrap_self.py (from the cloned repo if the GitHub option is active, else from the
  data folder). Returns the module or None."""
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
def first_run_defaults():
  """(target, intel, rosetta) that fit this computer, used only when there is no config file yet:
  target = the host system · intel on for Intel CPUs · rosetta on for Apple silicon (implies intel)."""
  target, intel, rosetta = host_target(), "no", "no"
  try:
    c = cpu_info()
    if c["arch"] == "apple":  # Apple silicon (or an x86 process under Rosetta)
      target, rosetta = "macos", "yes"
    elif c["vendor"] == "intel":
      intel = "yes"
  except Exception:
    pass
  return target, intel, rosetta


def load_config():
  cfg = configparser.ConfigParser(interpolation=None)
  source = CONFIG_FILE
  if not os.path.isfile(CONFIG_FILE) and os.path.isfile(LEGACY_CONFIG_FILE):
    source = LEGACY_CONFIG_FILE  # migrate settings from the old name
  first_run = not os.path.isfile(source)  # no config yet: pick the options that fit this computer
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
  if first_run:
    values["relinker"]["target"], values["settings"]["intel"], values["relinker"]["rosetta"] = first_run_defaults()
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
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
      f.write("# settings: output_dir (default = 'Converted games' folder next to the program), intel yes/no,\n"
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


# ── CPU compatibility check ──
# AnyPS5 runs the game natively and relies on what the PS5's Zen 2 CPU offers without checking.
CPU_REQUIRED = (("avx2", "AVX2"), ("fma", "FMA"), ("f16c", "F16C"), ("bmi1", "BMI1"),
                ("bmi2", "BMI2"), ("lzcnt", "LZCNT"), ("movbe", "MOVBE"))
_CPU_INFO = None


def _sysctl(name):
  try:
    return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=5).stdout.strip()
  except Exception:
    return ""


def cpu_info():
  """{arch, vendor ('amd'/'intel'/'other'), name, features:set of CPU_REQUIRED keys or None if unknown, rosetta}."""
  global _CPU_INFO
  if _CPU_INFO is not None:
    return _CPU_INFO
  import platform
  machine = platform.machine().lower()
  info = {"arch": "x64" if machine in ("x86_64", "amd64", "x64") else machine,
          "vendor": "other", "name": "", "features": None, "rosetta": False}
  try:
    if sys.platform == "darwin":
      info["rosetta"] = _sysctl("sysctl.proc_translated") == "1"
      if info["rosetta"] or machine.startswith("arm"):
        info["arch"] = "apple"
        info["name"] = _sysctl("machdep.cpu.brand_string") or "Apple silicon"
      else:
        text = " ".join(_sysctl(k) for k in ("machdep.cpu.features", "machdep.cpu.leaf7_features",
                                             "machdep.cpu.extfeatures")).lower().split()
        info["features"] = {k for k, _ in CPU_REQUIRED if k in text}
        info["name"] = _sysctl("machdep.cpu.brand_string")
        info["vendor"] = "intel" if "intel" in info["name"].lower() else "other"
    elif os.name == "nt":
      import ctypes
      import winreg
      with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0") as k:
        v = winreg.QueryValueEx(k, "VendorIdentifier")[0]
        info["name"] = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
      info["vendor"] = "amd" if "AMD" in v else "intel" if "Intel" in v else "other"
      # Windows only reports AVX2 (feature 40). Every x86 CPU with AVX2 from Haswell/Excavator on also has
      # FMA, F16C, BMI1/2, LZCNT and MOVBE, so AVX2 stands for the whole set.
      if ctypes.windll.kernel32.IsProcessorFeaturePresent(40):
        info["features"] = {k for k, _ in CPU_REQUIRED}
      else:
        info["features"] = set()
    else:
      flags, vendor, name = set(), "", ""
      with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
        for line in f:
          key, _, val = line.partition(":")
          key, val = key.strip(), val.strip()
          if key == "flags" and not flags:
            flags = set(val.split())
          elif key == "vendor_id" and not vendor:
            vendor = val
          elif key == "model name" and not name:
            name = val
      if flags:
        flags.add("lzcnt") if "abm" in flags else None  # lzcnt is reported as abm
        info["features"] = {k for k, _ in CPU_REQUIRED if k in flags}
      info["vendor"] = "amd" if "AMD" in vendor else "intel" if "Intel" in vendor else "other"
      info["name"] = name
  except Exception:
    pass
  _CPU_INFO = info
  return info


def cpu_status(target, intel, rosetta):
  """(text, level) for the requirements list. level: ok / warn / err / muted."""
  c = cpu_info()
  name = c["name"] or c["arch"]
  if c["arch"] == "apple":
    if target == "macos" and rosetta:
      return (f"{name}  ✔  Rosetta (--to-rosetta on)", "ok")
    return (f"{name}  ⚠  Apple silicon: use the macOS target with --to-rosetta", "warn")
  if c["arch"] != "x64":
    return (f"{c['arch']}  ✖  not supported: AnyPS5 needs an x86-64 CPU", "err")
  if c["features"] is None:
    return (f"{name}  (extensions could not be read)", "muted")
  missing = [t for k, t in CPU_REQUIRED if k not in c["features"]]
  if missing:
    return (f"{name}  ✖  missing {', '.join(missing)}: AnyPS5 stops at start", "err")
  if c["vendor"] == "intel":
    if intel or rosetta:
      return (f"{name}  ✔  Intel, --to-intel on (titles that generate AMD-only code at runtime may still fail)", "ok")
    return (f"{name}  ⚠  Intel: turn on --to-intel (titles that generate AMD-only code at runtime may still fail)", "warn")
  return (f"{name}  ✔  {', '.join(t for _, t in CPU_REQUIRED)}", "ok")


_GPU_INFO = None
_SOFT_GPU = re.compile(r"llvmpipe|lavapipe|swiftshader|softpipe|software rasterizer|microsoft basic render", re.I)


def _run_text(cmd, timeout=15):
  try:
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace", **kw).stdout
  except Exception:
    return ""


def gpu_info():
  """[{name, software, vendor ('amd'/'nvidia'/'intel'/'other')}] from Vulkan; falls back to the OS list."""
  global _GPU_INFO
  if _GPU_INFO is not None:
    return _GPU_INFO
  names = []
  if shutil.which("vulkaninfo"):
    out = _run_text(["vulkaninfo", "--summary"])
    names = [m.strip() for m in re.findall(r"deviceName\s*=\s*(.+)", out)]
  vulkan = bool(names)
  if not names:
    if os.name == "nt":
      out = _run_text(["powershell", "-NoProfile", "-Command",
                       "(Get-CimInstance Win32_VideoController).Name"])
      names = [x.strip() for x in out.splitlines() if x.strip()]
    elif sys.platform == "darwin":
      out = _run_text(["system_profiler", "SPDisplaysDataType"])
      names = [m.strip() for m in re.findall(r"Chipset Model:\s*(.+)", out)]
    else:
      out = _run_text(["lspci"])
      names = [l.split(": ", 1)[1].strip() for l in out.splitlines()
               if re.search(r"VGA|3D|Display", l) and ": " in l]
  gpus, seen = [], set()
  for n in names:
    if n in seen:
      continue
    seen.add(n)
    low = n.lower()
    vendor = ("amd" if re.search(r"amd|radeon|advanced micro", low) else
              "nvidia" if re.search(r"nvidia|geforce|rtx|gtx|quadro", low) else
              "intel" if "intel" in low else "other")
    gpus.append({"name": n, "software": bool(_SOFT_GPU.search(n)), "vendor": vendor})
  _GPU_INFO = {"gpus": gpus, "vulkan": vulkan}
  return _GPU_INFO


def gpu_status(selected=""):
  """(text, level) for the requirements list. `selected` is the ANYPS5_GPU filter (part of the device name)."""
  info = gpu_info()
  gpus = info["gpus"]
  if sys.platform.startswith("linux"):
    import ctypes.util
    if not ctypes.util.find_library("vulkan"):
      return ("Vulkan loader (libvulkan.so.1) not found  ✖  install your distribution's Vulkan loader and GPU driver", "err")
  if not gpus:
    return ("no GPU found  (install the Vulkan driver / vulkan-tools to detect it)", "warn")
  hw = [g for g in gpus if not g["software"]]
  sel = selected.strip().lower()
  if sel:
    hit = [g for g in gpus if sel in g["name"].lower()]
    if not hit:
      return (f"ANYPS5_GPU “{selected.strip()}” matches no device: {'; '.join(g['name'] for g in gpus)}", "err")
    if hit[0]["software"]:
      return (f"{hit[0]['name']}  ⚠  software renderer selected: the title runs, but very slowly", "warn")
    return (f"{hit[0]['name']}  ✔  (selected with ANYPS5_GPU)", "ok")
  if not hw:
    return (f"{gpus[0]['name']}  ⚠  software renderer (no GPU driver): the title runs, but very slowly", "warn")
  names = "; ".join(g["name"] for g in hw)
  text = names + ("" if info["vulkan"] else "  (Vulkan not verified)")
  if len(hw) > 1:
    return (text + "  ⚠  several GPUs: set ANYPS5_GPU to pick one", "warn")
  tip = ""
  if os.name == "nt" and hw[0]["vendor"] == "amd":
    tip = "  · if the first run ends in DEVICE_LOST, run again (driver cache: %LOCALAPPDATA%\\AMD\\VkCache)"
  return (text + "  ✔" + tip, "ok" if info["vulkan"] else "muted")


DIRECT_MEMORY_MIB = 13824  # USAGE.md: up to this much direct memory is committed per title on Windows


def memory_status():
  """(text, level) for Windows: free commit (RAM + page file) against the title's direct memory. None elsewhere."""
  if os.name != "nt":
    return None
  try:
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
      _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                  ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                  ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                  ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                  ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(m)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
      return None
  except Exception:
    return None
  mib = 1024 * 1024
  limit, free, ram = m.ullTotalPageFile // mib, m.ullAvailPageFile // mib, m.ullTotalPhys // mib
  base = f"RAM {ram} MiB · commit limit {limit} MiB · {free} MiB free to commit"
  if limit < DIRECT_MEMORY_MIB:
    return (base + f"  ✖  below the {DIRECT_MEMORY_MIB} MiB direct memory: increase the page file", "err")
  if free < DIRECT_MEMORY_MIB:
    return (base + f"  ⚠  less than {DIRECT_MEMORY_MIB} MiB: close other programs or increase the page file", "warn")
  return (base + "  ✔", "ok")


BAD_NVIDIA_DRIVERS = {"610.47": "known crash in nvgpucomp64.dll (see USAGE.md)"}


def nvidia_status():
  """(text, level) for the NVIDIA driver, or None when there is no NVIDIA GPU / nvidia-smi."""
  if not any(g["vendor"] == "nvidia" for g in gpu_info()["gpus"]):
    return None
  exe = shutil.which("nvidia-smi")
  if not exe:
    return ("NVIDIA GPU found, driver version could not be read (nvidia-smi missing)", "muted")
  ver = _run_text([exe, "--query-gpu=driver_version", "--format=csv,noheader"]).strip().splitlines()
  ver = ver[0].strip() if ver else ""
  if not ver:
    return ("NVIDIA driver version could not be read", "muted")
  if ver in BAD_NVIDIA_DRIVERS:
    return (f"driver {ver}  ✖  {BAD_NVIDIA_DRIVERS[ver]}: install another driver version", "err")
  return (f"driver {ver}  ✔", "ok")


def udmabuf_status():
  """(text, level) for /dev/udmabuf on Linux (not needed with the proprietary NVIDIA driver). None elsewhere."""
  if not sys.platform.startswith("linux"):
    return None
  path = "/dev/udmabuf"
  if not os.path.exists(path):
    return ("/dev/udmabuf not present  (only needed with Mesa drivers: sudo modprobe udmabuf)", "warn")
  if os.access(path, os.R_OK | os.W_OK):
    return ("/dev/udmabuf accessible  ✔", "ok")
  return ("/dev/udmabuf not accessible  ✖  run: sudo usermod -aG kvm $USER  (then log in again) "
          "or: sudo setfacl -m u:$USER:rw /dev/udmabuf", "err")


# ─────────────────────────────────────────────────────────────
#  Logic (independent of the UI: communicates through the log callback)
#  log(kind, msg) with kind in: info ok warn err step detail progress
# ─────────────────────────────────────────────────────────────
def find_relinker():
  base = release_dir() or DATA_DIR
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


def move_root_prx(app0_dir, log):
  """On the COPY of the game (app0): move the .prx files that sit next to eboot.bin (the game root)
  into the module folder (sce_module / sce_modules / prx), except libc.prx. The relinker and the
  unwrap only look inside the module folders, so a module left in the root is never converted and
  the game then fails with 'Failed to load module ... libs\\<name>.prx'. Never overwrites a file
  that already exists in the module folder. Returns the number of files moved."""
  try:
    names = sorted(n for n in os.listdir(app0_dir)
                   if n.lower().endswith(".prx") and n.lower() != "libc.prx"
                   and os.path.isfile(os.path.join(app0_dir, n)))
  except OSError as e:
    log("warn", f"Cannot list the game root to find .prx files: {e}")
    return 0
  if not names:
    log("info", "No .prx files next to eboot.bin: nothing to move.")
    return 0
  found = existing_module_dirs(app0_dir)
  dest_name = found[0] if found else "sce_module"
  dest = os.path.join(app0_dir, dest_name)
  try:
    os.makedirs(dest, exist_ok=True)
  except OSError as e:
    log("warn", f"Cannot create '{dest_name}': {e}")
    return 0
  moved = 0
  for name in names:
    src, dst = os.path.join(app0_dir, name), os.path.join(dest, name)
    if os.path.exists(dst):
      log("warn", f"{name}: a file with the same name is already in '{dest_name}': left in the game root.")
      continue
    try:
      shutil.move(src, dst)
    except OSError as e:
      log("warn", f"{name}: move failed ({e}): left in the game root.")
      continue
    moved += 1
    log("detail", f"{name} → {dest_name}/")
  log("ok", f"{moved} .prx moved from the game root into '{dest_name}' (libc.prx stays where it is).")
  return moved


def get_backup_dir(game_name):
  return os.path.join(DATA_DIR, BACKUP_DIR_NAME, game_name)


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
  """A prx-*.zip in the data folder that contains the libs folder (libs-<system> preferred over libs).
  The zip is read as it is: no need to extract it. Returns None if there is none."""
  base = release_dir() or DATA_DIR
  try:
    zips = sorted((f for f in os.listdir(base)
                   if f.lower().startswith(LIBS_ZIP_PREFIX) and f.lower().endswith(".zip")
                   and os.path.isfile(os.path.join(base, f))), key=str.lower)
  except OSError:
    return None
  if base != DATA_DIR:  # release: prx-windows / prx-linux, the one of the target goes first
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
      base = os.path.join(DATA_DIR, EXTRACT_DIR_NAME)
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


def cancellable_copy(cancel, log=None, total=0, sync=True, workers=0, label="Copying"):
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
    text = f"{label}: {pct:.0f}% - {format_size(done)} of {format_size(total)} · speed {format_size(speed)}/s"
    if elapsed > 2 and total > done and speed > 0:
      text += f" · about {format_duration((total - done) / speed)} remaining"
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
  inner = cancellable_copy(cancel, log, total, label="Moving")

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
    log("err", f"'{UNWRAP_SCRIPT_NAME}' not found in {where}the app data folder ({DATA_DIR}).")
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


def open_trash():
  """Opens the system trash folder in the file manager. Returns True if it was launched."""
  try:
    if os.name == "nt":
      subprocess.Popen(["explorer.exe", "shell:RecycleBinFolder"])
      return True
    if sys.platform == "darwin":
      subprocess.Popen(["open", os.path.expanduser("~/.Trash")])
      return True
    for cmd in (["gio", "open", "trash:///"], ["xdg-open", "trash:///"]):
      if shutil.which(cmd[0]):
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    if shutil.which("xdg-open"):
      subprocess.Popen(["xdg-open", os.path.join(base, "Trash", "files")])
      return True
  except Exception:
    pass
  return False


def screenshots_dir(pkg_dir, rt):
  """Folder where F12 screenshots land: ANYPS5_SCREENSHOTS if set, else anyps5-screenshots/ next to the executable."""
  custom = rt.get("screenshots_dir", "").strip()
  if custom:
    return custom
  return os.path.join(pkg_dir, SCREENSHOTS_DIR_NAME) if pkg_dir else ""


def open_path(path):
  """Opens a folder in the file manager. Returns True if it was launched."""
  try:
    if os.name == "nt":
      os.startfile(path)  # noqa: S606 - a folder path that the app itself resolved
      return True
    cmd = ["open", path] if sys.platform == "darwin" else ["xdg-open", path]
    if shutil.which(cmd[0]):
      subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
      return True
  except Exception:
    pass
  return False


def storage_choice_to_dir(picked):
  """Folder picked in the dialog = the folder to use as is (validate_storage_target requires it to be
  empty, so nothing already there gets mixed up with the app's files)."""
  return os.path.abspath(picked)


def validate_storage_target(new):
  """Returns "" if the data folder can be moved to `new`, else the reason why not."""
  if not STORAGE_EDITABLE:
    return "The location can only be changed in the packaged app (not from source or in portable mode)."
  if not new or not os.path.isabs(new):
    return "Enter a full folder path."
  cur, new = os.path.realpath(DATA_DIR), os.path.realpath(new)
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


def relocate_data_dir(new, status):
  """Moves everything in the data folder to `new` (an empty folder), then makes it the new location.
  status(text, pct) reports progress. Raises RuntimeError with a readable message on failure; in that
  case the already moved items are put back. The app must be restarted afterwards."""
  problem = validate_storage_target(new)
  if problem:
    raise RuntimeError(problem)
  old = DATA_DIR
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
    if os.path.normcase(os.path.realpath(new)) == os.path.normcase(os.path.realpath(DEFAULT_DATA_DIR)):
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
  if os.path.normcase(os.path.realpath(old)) != os.path.normcase(os.path.realpath(DEFAULT_DATA_DIR)):
    if os.path.isdir(old) and not os.listdir(old) and not move_to_trash(old):  # old folder empty: to the trash
      try:
        os.rmdir(old)
      except OSError:
        pass


# what the program itself puts in the data folder (or looks for there): moved by migrate_legacy_data.
# Anything else the user left in the old folder stays where it is.
_MIGRATE_NAMES = {"backups", "release", "source", "toolchain", "extracted", "update", "anyps5-fonts", "tools",
                  "config.ini", "userelinker.ini", "anyps5-input.ini", "unwrap_self.py",
                  "7z", "7zz", "7za", "7z.exe", "7za.exe", "7z.dll"}
_MIGRATE_PREFIXES = ("relinker-", "prx-")
_MIGRATE_HEAVY = {"release", "source", "toolchain", "extracted", "update"}  # big, rebuilt on demand
_MIGRATE_JUNK = {"__pycache__", ".ds_store", "thumbs.db", "desktop.ini"}


def migrate_legacy_data(old=None):
  """Moves an old data folder (default: <program folder>/<LEGACY_DIR_NAME>, first versions) into DATA_DIR, item by item.
  Items already present in the new folder are not touched. The old folder goes to the trash only if
  nothing else is left in it. Never raises. Returns (moved, left) lists of names."""
  moved, left = [], []
  old, new = old or LEGACY_DATA_DIR, DATA_DIR
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
    out_base = s["output_dir"] or DEFAULT_OUTPUT_DIR
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
      trashed = False
      for d in created:
        if safe_to_trash and os.path.isdir(d):
          if move_to_trash(d):
            trashed = True
            log("info", f"Incomplete folder moved to the trash: {d}")
          else:
            log("warn", f"Unable to move it to the trash, delete it by hand: {d}")
        elif os.path.isdir(d):
          log("warn", f"Folder left in place: {d}")
      if trashed and yes(cfg["settings"].get("open_trash", "yes")):
        if open_trash():
          log("info", "Trash folder opened (you can restore or empty it from there).")


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
    ensure_release_assets(rl, log, cancel, need=("unwrap",))  # falls back to the data folder if it fails
    if unwrapper_status() is None:
      log("err", f"'{UNWRAP_SCRIPT_NAME}' not available: not in the release and not in the app data folder.")
      return False
  log("step", f"Unwrap · {game_name}")
  try:
    if not log_space(space_requirements(folder, "windows", "unwrap", DATA_DIR), log):
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


def _total_ram_gib():
  """Installed RAM in GiB, or 0 if it cannot be read."""
  try:
    if os.name == "nt":
      import ctypes

      class _MS(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
      m = _MS()
      m.dwLength = ctypes.sizeof(m)
      if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
        return m.ullTotalPhys / 2**30
    elif sys.platform == "darwin":
      out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout
      return int(out.strip()) / 2**30
    else:
      with open("/proc/meminfo", encoding="utf-8") as f:
        for line in f:
          if line.startswith("MemTotal:"):
            return int(line.split()[1]) / 2**20
  except Exception:
    pass
  return 0


def build_parallel_jobs(rl=None):
  """Parallel jobs for the build. Never fewer than the old conservative value (half the cores, max 8:
  glslang/FFmpeg builds with every core can exhaust RAM and freeze the machine), but on machines with
  plenty of RAM (~2 GiB per job, 4 GiB left free) it uses more cores. The 'Build jobs' setting
  (repo_build_jobs) or ANYPS5_BUILD_JOBS overrides it."""
  for raw in ((rl or {}).get("repo_build_jobs", ""), os.environ.get("ANYPS5_BUILD_JOBS", "")):
    try:
      forced = int(str(raw).strip() or "0")
    except ValueError:
      continue
    if forced > 0:
      return min(forced, 128)
  n = os.cpu_count() or 2
  safe = max(1, min(8, n // 2 if n > 4 else n))
  ram = _total_ram_gib()
  by_ram = int((ram - 4) // 2) if ram else 0
  return max(safe, min(n, by_ram))


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
  root = os.path.join(DATA_DIR, RELEASE_DIR_NAME, repo_slug(url, "release"))
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
  that are not already in <data>/release/<repo>/<tag>/, keeping the names used in the release.
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
      log("warn", f"{UNWRAP_SCRIPT_NAME} not available in the repository: using the one in the app data folder.")
    else:
      log("err", f"{UNWRAP_SCRIPT_NAME} not found in release {tag} nor in the repository: "
                 f"put it in the app data folder.")
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
  root = os.path.join(DATA_DIR, SOURCE_DIR_NAME, repo_slug(url, ref))
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
    return "not found in the app data folder  ✖", "err"
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
  uses the copy it installed in <data>/toolchain. Returns (cmake arguments, note); arguments are []
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
WINLIBS_DIR = os.path.join(DATA_DIR, "toolchain", "winlibs-" + WINLIBS_TAG)


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
  """Download the WinLibs GCC 15.2.0 zip (~270 MB) from GitHub and unpack it in <data>/toolchain."""
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
  elif yes(rl.get("repo_update", "yes")) and p["root"] not in _DECLINED_REPOS:
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


def quote_cmake_include_paths(repo, log):
  """The repository's CMake adds "SHELL:-include ${path}/HostStandardStreams.hpp" WITHOUT quoting the path:
  when the path contains a space the compiler sees two files ("cannot specify '-o' with '-c' ... multiple
  files"). Quote it in the cloned copy (the update step resets the files, so this runs before every
  configure). Returns how many lines were fixed."""
  fixed = 0
  core = os.path.join(repo, "core")
  pat = re.compile(r'"SHELL:-include (?!\\")([^"\n]*)"')
  for root, dirs, files in os.walk(core):
    if "CMakeFiles" in dirs:
      dirs.remove("CMakeFiles")
    if "CMakeLists.txt" not in files:
      continue
    fp = os.path.join(root, "CMakeLists.txt")
    try:
      with open(fp, encoding="utf-8", newline="") as f:
        text = f.read()
      new, n = pat.subn(lambda m: '"SHELL:-include \\"' + m.group(1) + '\\""', text)
      if n:
        with open(fp, "w", encoding="utf-8", newline="") as f:
          f.write(new)
        fixed += n
    except OSError as e:
      log("warn", f"Could not patch {fp}: {e}")
  if fixed:
    log("info", f"Quoted {fixed} '-include' path(s) in the repository's CMake files (the folder path has a space).")
  return fixed


def ensure_source_libs(rl, log, cancel, holder, target=None):
  """Build the 'libs' CMake target of the cloned repo (full configuration: the relinker-only
  one does not include core/libs) and collect the .prx files (+ the MinGW runtime DLLs on
  Windows target) in <data>/source/<repo>/libs-out. Reuses the previous build if the commit is
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
  if " " in repo:
    quote_cmake_include_paths(repo, log)
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
               "--parallel", str(build_parallel_jobs(rl))]
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
    if " " in repo and re.search(r"cannot specify '-o' with '-c'", text):
      log("err", "Cause: the folder path contains a SPACE and the repository's CMake passes "
                 "'-include <path>' to the compiler without quotes, so the compiler sees the path as two files.")
      log("info", f"Fix: put the program in a folder whose whole path has no spaces and is short "
                  f"(e.g. rename 'AnyPS5 GUI' to 'AnyPS5_GUI', or use C:\\AnyPS5), then build again. Now: {repo}")
      log("info", "Alternative: turn OFF 'Build the libs from the repository' and use the prx-*.zip libs.")
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
  needed). Returns True if an unwrapper can be used (repo copy, or the data folder as fallback)."""
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
    log("warn", "tools/%s not found in the repository: using the one in the app data folder." % UNWRAP_SCRIPT_NAME)
    return True
  log("err", "%s not found in the repository (tools/) nor in the app data folder." % UNWRAP_SCRIPT_NAME)
  return False


def _compare_url(url, old, new):
  """Link to the 'what changed' page of the repository (GitHub only), '' for other hosts."""
  m = re.match(r"^(?:https://github\.com/|git@github\.com:)([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", (url or "").strip())
  if not m:
    return ""
  return "https://github.com/%s/%s/compare/%s...%s" % (m.group(1), m.group(2), old, new)


def repo_changes(url, repo, old, new, ref, env, limit=12):
  """What changed between the commit already built and the remote one, for the update popup.
  Reads at most `limit` commit titles (hash, date, title) with a shallow `git fetch` that only
  fills FETCH_HEAD (the working tree is not touched). Everything is best effort: on any failure
  the popup simply shows the two hashes and the link."""
  info = {"commits": [], "partial": False, "old_date": "", "new_date": "", "compare": _compare_url(url, old, new)}
  if not os.path.isdir(repo):
    return info
  code, _ = _capture(["git", "fetch", "--depth", "50", "--no-tags", "--no-recurse-submodules", "origin", ref or "HEAD"],
                     cwd=repo, env=env)
  if code != 0:
    return info
  _, d = _capture(["git", "log", "-1", "--date=format-local:%Y-%m-%d %H:%M", "--format=%cd", old], cwd=repo, env=env)
  info["old_date"] = d.strip()
  _, d = _capture(["git", "log", "-1", "--date=format-local:%Y-%m-%d %H:%M", "--format=%cd", "FETCH_HEAD"], cwd=repo, env=env)
  info["new_date"] = d.strip()
  # the old commit is reachable from the fetched history only if fewer than ~50 commits are new
  reach, _ = _capture(["git", "merge-base", "--is-ancestor", old, "FETCH_HEAD"], cwd=repo, env=env)
  rng = (old + "..FETCH_HEAD") if reach == 0 else "FETCH_HEAD"
  code, out = _capture(["git", "log", "-n", str(limit + 1), "--no-merges", "--date=format-local:%Y-%m-%d %H:%M", "--format=%h%x1f%cd%x1f%s", rng],
                       cwd=repo, env=env)
  if code != 0:
    return info
  rows = [l.split("\x1f") for l in out.splitlines() if l.count("\x1f") == 2]
  info["partial"] = reach != 0 or len(rows) > limit  # "and more": the list is cut
  info["commits"] = [{"h": r[0], "d": r[1], "s": r[2][:140]} for r in rows[:limit]]
  return info


def remote_commit(url, ref, env):
  """Commit the remote branch/tag points to, read with `git ls-remote` (no download). '' if unknown
  (offline, ref given as a raw commit hash, ...): the caller then goes through the normal update."""
  patterns = [ref, ref + "^{}"] if ref else ["HEAD"]  # "^{}" gives the commit of an annotated tag
  code, out = _capture(["git", "ls-remote", "--", url] + patterns, env=env)
  if code != 0 or not out:
    return ""
  found = {}
  for line in out.splitlines():
    parts = line.split()
    if len(parts) == 2:
      found[parts[1]] = parts[0]
  if not ref:
    return found.get("HEAD", "")
  for name in ("refs/tags/%s^{}" % ref, "refs/heads/" + ref, "refs/tags/" + ref, ref):
    if name in found:
      return found[name]
  return ""


def fast_build_args(extra_args, log):
  """CMake arguments that speed the build up, added only when the user did not choose them:
  the Ninja generator (Linux/macOS; on Windows the toolchain detection already picks it) and a
  compiler cache (ccache or sccache) when installed."""
  args = []
  if (os.name != "nt" and not any(a.startswith("-G") for a in extra_args)
      and not os.environ.get("CMAKE_GENERATOR") and shutil.which("ninja")):
    args += ["-G", "Ninja"]
    log("info", "Build speed: Ninja generator")
  launcher = shutil.which("ccache") or shutil.which("sccache")
  if launcher and not any("COMPILER_LAUNCHER" in a for a in extra_args):
    args += ["-DCMAKE_C_COMPILER_LAUNCHER=" + launcher, "-DCMAKE_CXX_COMPILER_LAUNCHER=" + launcher]
    log("info", "Build speed: compiler cache " + os.path.basename(launcher))
  return args


def make_build_log(log):
  """Wraps log(): turns the '[12/345]' (Ninja) and '[ 42%]' (Makefiles) lines of the build into a
  progress bar with elapsed and estimated remaining time. Other lines pass through unchanged."""
  t0 = time.time()
  state = {"last": 0.0}
  ninja = re.compile(r"^\[\s*(\d+)/(\d+)\]")
  make = re.compile(r"^\[\s*(\d+)%\]")

  def update(pct, done, total):
    now = time.time()
    if now - state["last"] < 0.25 and pct < 100:
      return
    state["last"] = now
    elapsed = now - t0
    text = "Building: %.0f%%" % pct
    if total:
      text += " (%d/%d steps)" % (done, total)
    text += " · elapsed " + format_duration(elapsed)
    if elapsed > 5 and 1 <= pct < 100:
      text += " · about %s remaining" % format_duration(elapsed * (100 - pct) / pct)
    log("percent", pct)
    log("progress", text)

  def blog(kind, msg):
    if kind in ("info", "progress") and isinstance(msg, str):
      m = ninja.match(msg)
      if m and int(m.group(2)):
        update(int(m.group(1)) * 100.0 / int(m.group(2)), int(m.group(1)), int(m.group(2)))
      else:
        m = make.match(msg)
        if m:
          update(float(m.group(1)), 0, 0)
        else:
          log(kind, msg)
          return
      if kind == "progress":
        return  # the bar line above replaces it
    log(kind, msg)
  return blog


def ensure_source_relinker(rl, log, cancel, holder):
  """Clone/update the repository and build the relinker with CMake. Reuses the previous build
  when the commit did not change. Returns the path of the executable, or None on failure."""
  problem = source_preflight(rl)
  if problem:
    log("err", problem)
    return None
  p = source_paths(rl)
  repo, build = p["repo"], p["build"]
  t_start = time.time()
  _DECLINED_REPOS.discard(p["root"])
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
  extra_args = fast_build_args(extra_args, log) + extra_args
  log("info", f"Repository: {p['url']}" + (f"  (branch/tag {p['ref']})" if p["ref"] else ""))
  log("detail", f"Working folder: {p['root']}")
  os.makedirs(p["root"], exist_ok=True)
  previous = read_source_build(p)
  env_key = env.get("CXXFLAGS", "") + "|" + " ".join(extra_args)
  env_mark = os.path.join(build, ".manager_env")

  # 0. nothing to do if the remote commit is the one already built (no fetch, no submodules, no CMake)
  if previous and update and os.path.isfile(p["mark"]) and previous["target"] == user_target:
    log("step", "(1/3) Checking for a new commit")
    try:
      same_settings = open(env_mark, encoding="utf-8").read() == env_key
    except OSError:
      same_settings = False
    remote = remote_commit(p["url"], p["ref"], env)
    if remote and remote == previous["commit"] and same_settings:
      log("ok", f"Relinker already up to date (commit {remote[:12]}): nothing to download or build.")
      return previous["binary"]
    if remote and remote != previous["commit"]:
      log("info", f"New commit on the remote: {remote[:12]}")
      # a build already exists and the repository changed: let the user decide (only when the
      # settings are unchanged, otherwise a rebuild is needed anyway)
      if same_settings and UPDATE_PROMPT and yes(rl.get("repo_update_ask", "yes")):
        log("step", "A new version of the repository is available: waiting for your choice")
        prompt_info = {"url": p["url"], "old": previous["commit"][:12], "new": remote[:12]}
        try:
          prompt_info.update(repo_changes(p["url"], repo, previous["commit"], remote, p["ref"], env))
        except Exception:
          pass  # the list of changes is only a courtesy
        check_cancel(cancel)
        answer = UPDATE_PROMPT(prompt_info, cancel)
        check_cancel(cancel)
        if answer and not answer.get("update", True):
          _DECLINED_REPOS.add(p["root"])
          log("ok", f"Keeping the current build (commit {previous['commit'][:12]}).")
          return previous["binary"]
        log("info", "Updating and rebuilding.")

  # 1. clone or update
  if not os.path.isfile(p["mark"]):
    if os.path.isdir(repo):
      _rmtree_force(repo)  # incomplete clone from a previous attempt
    log("step", "(1/3) Cloning the repository (shallow, with submodules)")
    cmd = ["git", "clone", "--recurse-submodules", "--shallow-submodules", "--depth", "1",
           "--no-tags", "--jobs", "4"]
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
    log("step", "(1/3) Updating the repository")
    fetch = ["git", "fetch", "--depth", "1", "--no-tags", "origin", p["ref"] or "HEAD"]
    steps = [(fetch, "git fetch"), (["git", "reset", "--hard", "FETCH_HEAD"], "git reset"),
             (["git", "submodule", "sync", "--recursive"], "git submodule sync"),
             (["git", "submodule", "update", "--init", "--recursive", "--depth", "1", "--jobs", "4"],
              "git submodule update")]
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
  log("step", "(2/3) Configuring (CMake, Release)")
  # CXXFLAGS is only read on the first configure: if it changed, start from a clean build folder
  try:
    old_env = open(env_mark, encoding="utf-8").read() if os.path.isfile(env_mark) else None
  except OSError:
    old_env = None
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
  blog = make_build_log(log)
  base = ["cmake", "--build", build, "--config", "Release", "--parallel", str(build_parallel_jobs(rl))]
  attempts = [user_target] if user_target else [RELINKER_TARGET_GUESS, ""]
  built_ok = False
  for target in attempts:
    log("step", f"(3/3) Building ({'target ' + target if target else 'all targets'})")
    del errors[:]
    log("percent", None)
    code = stream_process(base + (["--target", target] if target else []), blog, cancel, holder,
                          "cmake --build", env=env, errors=errors, logfile=logfile)
    log("percent", None)
    log("progress", "")
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
  log("ok", f"Relinker built in {format_duration(time.time() - t_start)}: {binary}")
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
  if rl.get("linux_fixed_base", "").strip():
    if target != "linux":
      return "--linux-fixed-base requires the Linux target."
    try:
      addr = parse_address(rl["linux_fixed_base"])
    except ValueError:
      return "--linux-fixed-base: invalid address (decimal or 0x...)."
    if addr % 0x10000 or not (0x10000 <= addr <= 0x7FFFFFFF0000):
      return "--linux-fixed-base: needs a 64 KiB-aligned address between 0x10000 and 0x7fffffff0000."
    if yes(rl.get("lazy_binding", "no")):
      return "--linux-fixed-base conflicts with --lazy-binding."
  if rl.get("macos_fixed_base", "").strip():
    if target != "macos":
      return "--macos-fixed-base requires the macOS target."
    try:
      addr = parse_address(rl["macos_fixed_base"])
    except ValueError:
      return "--macos-fixed-base: invalid address (decimal or 0x...)."
    if addr % 0x10000 or not (0x20000 <= addr <= 0x7FFFFFFF0000):
      return "--macos-fixed-base: needs a 64 KiB-aligned address between 0x20000 and 0x7fffffff0000."
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
  pf = rl.get("patches_file", "").strip()
  if pf and yes(rl.get("no_patches", "no")):
    return "--patches conflicts with --no-patches."
  if pf and not os.path.isfile(os.path.abspath(pf)):
    return f"--patches: '{pf}' is not an existing file."
  extra, err = parse_extra_args(rl)
  if err:
    return err
  if extra:
    managed = {a for a in build_relink_command("relinker", intel, dict(rl, extra_args=""), "in", "out")
               if a.startswith("--")}
    for a in extra:
      flag = a.split("=", 1)[0]
      if flag.startswith("--") and flag in managed:
        return f"Extra arguments: {flag} is already set by the options above; remove one of them."
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
    if yes(rl.get("frame_red_zone", "no")):
      cmd.append("--frame-red-zone")  # Windows only: frame for leaf functions that use the red zone
    if rl["fixed_base"].strip():
      cmd += ["--windows-fixed-base", rl["fixed_base"].strip()]
    if yes(rl["trace_imports"]):
      cmd.append("--trace-imports")
  elif target == "macos":
    cmd.append("--macos")
    if rl.get("macos_fixed_base", "").strip():
      cmd += ["--macos-fixed-base", rl["macos_fixed_base"].strip()]
  elif target == "linux":
    if rl.get("linux_fixed_base", "").strip():
      cmd += ["--linux-fixed-base", rl["linux_fixed_base"].strip()]
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
  if yes(rl.get("no_patches", "no")):
    cmd.append("--no-patches")
  elif rl.get("patches_file", "").strip():
    cmd += ["--patches", os.path.abspath(rl["patches_file"].strip())]
  extra, _err = parse_extra_args(rl)
  cmd += extra
  cmd += [input_file, out_exe]
  return cmd


def parse_extra_args(rl):
  """Free-form relinker arguments (field "extra_args"). Returns (list, error or None)."""
  text = (rl.get("extra_args", "") or "").strip()
  if not text:
    return [], None
  try:
    return shlex.split(text, posix=(os.name != "nt")), None
  except ValueError as e:
    return [], f"Extra arguments: {e}"


def verify_patches(rl, folder):
  """Run `relinker --verify-patches <file> <eboot>` (writes nothing). Returns (level, text)."""
  pf = rl.get("patches_file", "").strip()
  if not pf:
    auto = os.path.join(folder, "relinker-patches.toml") if folder else ""
    if auto and os.path.isfile(auto):
      pf = auto
    else:
      return "warn", "Choose a patch file first (or put relinker-patches.toml next to eboot.bin)."
  pf = os.path.abspath(pf)
  if not os.path.isfile(pf):
    return "err", f"Patch file not found: {pf}"
  if not folder or not os.path.isdir(folder):
    return "warn", "Choose the game folder first."
  eboot = os.path.join(folder, EXECUTABLE_NAME)
  state = file_state(eboot)
  if state == "self":
    return "warn", f"{EXECUTABLE_NAME} is still a SELF: unwrap the game first (Unwrap mode), then verify."
  if state != "elf":
    return "err", f"{EXECUTABLE_NAME} not found or not a valid ELF in the game folder."
  set_active_relinker_cfg(rl)
  relinker = resolve_relinker(rl)
  if not relinker or not os.path.isfile(relinker):
    return "err", "Relinker not available yet: run a conversion once (or build it) so the app has it."
  cmd = [relinker, "--verify-patches", pf]
  smp = rl.get("sce_module_path", "").strip()
  if smp:
    cmd += ["--sce-module-path", os.path.abspath(smp)]
  for d in split_list(rl.get("module_dirs", "")):
    cmd += ["--module-dir", d]
  for e in split_list(rl.get("executables", "")):
    cmd += ["--executable", e]
  cmd.append(eboot)
  kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
  try:
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                         timeout=120, **kwargs)
  except (OSError, subprocess.SubprocessError) as e:
    return "err", f"Could not run the relinker: {e}"
  out = "\n".join(x for x in ((res.stdout or "").strip(), (res.stderr or "").strip()) if x)
  if res.returncode == 0:
    return "ok", "Patches OK: no mismatch.\n" + out if out else "Patches OK: no mismatch."
  if res.returncode == 1:
    return "err", "Patch mismatch (these patches do not fit this game version):\n" + out
  return "err", "Verification failed:\n" + out


def find_repo_tool(rl, rel):
  """tools/<script> in <data>/tools or in the cloned repository, else None."""
  candidates = [os.path.join(DATA_DIR, rel)]
  paths = source_paths(rl) if rl.get("relinker_source") == "github" else None
  if paths:
    candidates.append(os.path.join(paths["repo"], rel))
  for c in candidates:
    if os.path.isfile(c):
      return c
  return None


def suggest_patches(rl, folder, list_all=False):
  """Run tools/suggest_patches.py on the selected unwrapped game. Returns (level, text, patch_file)."""
  catalog = rl.get("patch_catalog", "").strip()
  if not catalog:
    return "warn", "Choose the patch catalog folder first (the one with index.json).", ""
  catalog = os.path.abspath(catalog)
  if not os.path.isfile(os.path.join(catalog, "index.json")):
    return "err", f"No index.json in {catalog}.", ""
  if not folder or not os.path.isdir(folder):
    return "warn", "Choose the game folder first.", ""
  eboot = os.path.join(folder, EXECUTABLE_NAME)
  state = file_state(eboot)
  if state == "self":
    return "warn", f"{EXECUTABLE_NAME} is still a SELF: unwrap the game first (Unwrap mode).", ""
  if state != "elf":
    return "err", f"{EXECUTABLE_NAME} not found or not a valid ELF in the game folder.", ""
  param = os.path.join(folder, "sce_sys", "param.json")
  if not os.path.isfile(param):
    return "err", "sce_sys/param.json not found in the game folder (it holds the title id and version).", ""
  timeout = rl.get("patch_timeout", "").strip()
  if timeout and not (_whole(timeout) and int(timeout) >= 1):
    return "err", "Advisor timeout must be a whole number of seconds, 1 or more.", ""
  script = find_repo_tool(rl, os.path.join("tools", "suggest_patches.py"))
  if not script:
    return "err", "tools/suggest_patches.py not found: clone the repository (Source build) or copy it into the app data folder under tools/.", ""
  python = find_python()
  if not python:
    return "err", "Python was not found in PATH (the advisor needs Python 3.11 or newer).", ""
  kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
  try:
    ok = subprocess.run([python, "-c", "import sys;print(int(sys.version_info>=(3,11)))"], capture_output=True,
                        text=True, timeout=15, **kw).stdout.strip()
  except (OSError, subprocess.SubprocessError):
    ok = ""
  if ok != "1":
    return "err", "The patch advisor needs Python 3.11 or newer.", ""
  set_active_relinker_cfg(rl)
  relinker = resolve_relinker(rl)
  if not relinker or not os.path.isfile(relinker):
    return "err", "Relinker not available yet: run a conversion once (or build it) so the app has it.", ""
  cmd = [python, script, "--catalog", catalog, "--param-json", param, "--input", eboot, "--relinker", relinker]
  smp = rl.get("sce_module_path", "").strip()
  if smp:
    cmd += ["--sce-module-path", os.path.abspath(smp)]
  elif yes(rl.get("skip_sce_module", "no")):
    cmd.append("--skip-sce-module")
  for d in split_list(rl.get("module_dirs", "")):
    cmd += ["--module-dir", d]
  for e in split_list(rl.get("executables", "")):
    cmd += ["--executable", e]
  if timeout:
    cmd += ["--timeout-seconds", timeout]
  if list_all:
    cmd.append("--list-verified")
  try:
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                         timeout=900, **kw)
  except (OSError, subprocess.SubprocessError) as e:
    return "err", f"Could not run the advisor: {e}", ""
  out = "\n".join(x for x in ((res.stdout or "").strip(), (res.stderr or "").strip()) if x)
  patch = ""
  if res.returncode == 0 and not list_all:
    for line in out.splitlines():  # "  --patches <file> [--module-dir ...]" printed under "Use these options"
      if line.strip().startswith("--patches"):
        try:
          parts = shlex.split(line.strip(), posix=(os.name != "nt"))
        except ValueError:
          break
        if len(parts) >= 2:
          patch = parts[1]
          if not os.path.isabs(patch):
            patch = os.path.join(catalog, patch)
        break
  if res.returncode == 0:
    return "ok", out or "Verified.", patch
  if res.returncode == 1:
    return "warn", "No single verified patch for this game:\n" + out, ""
  return "err", "The advisor could not finish:\n" + out, ""


def find_nid_patcher(rl):
  p = rl.get("nid_patcher_path", "").strip()
  if p:
    return os.path.abspath(p) if os.path.isfile(p) else None
  paths = source_paths(rl) if rl.get("relinker_source") == "github" else None
  if paths and os.path.isdir(paths["build"]):
    found = find_built_relinker([(paths["build"], False)], "nid_patcher")
    if found:
      return found
  local = os.path.join(DATA_DIR, "nid_patcher.exe" if os.name == "nt" else "nid_patcher")
  if os.path.isfile(local):  # imported with Local files
    return local
  return shutil.which("nid_patcher")


def run_nid_patcher(rl, files):
  """`nid_patcher --exec [--only-sce] [--preserve-*] <files>`: patches the imports of the files IN PLACE."""
  exe = find_nid_patcher(rl)
  if not exe:
    return "err", "nid_patcher not found: set its path above, or build the repository (Source build)."
  files = [f for f in files if f]
  if not files:
    return "warn", "No file chosen."
  missing = [f for f in files if not os.path.isfile(f)]
  if missing:
    return "err", f"File not found: {missing[0]}"
  pe, pl = rl.get("nid_preserve_exports", "").strip(), rl.get("nid_preserve_list", "").strip()
  if pe and pl:
    return "err", "--preserve-exports and --preserve-export-list cannot be used together."
  if pl and not os.path.isfile(pl):
    return "err", f"Export list not found: {pl}"
  cmd = [exe, "--exec"]
  if yes(rl.get("nid_only_sce", "no")):
    cmd.append("--only-sce")
  if pe:
    cmd += ["--preserve-exports", os.path.abspath(pe) if os.path.exists(pe) else pe]
  elif pl:
    cmd += ["--preserve-export-list", os.path.abspath(pl)]
  cmd += files
  kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
  try:
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, **kw)
  except (OSError, subprocess.SubprocessError) as e:
    return "err", f"Could not run nid_patcher: {e}"
  out = "\n".join(x for x in ((res.stdout or "").strip(), (res.stderr or "").strip()) if x)
  if res.returncode == 0:
    return "ok", f"Imports patched in {len(files)} file(s).\n" + out if out else f"Imports patched in {len(files)} file(s)."
  return "err", f"nid_patcher failed (code {res.returncode}). Unsupported layouts fail before the file is written.\n" + out


def verify_output(rl, output_path):
  """Run `relinker --verify-patches <manifest> --verify-output [--executable ..] <output>` (writes nothing).
  output_path = a converted Linux ELF; the manifest is the <output-stem>.patches.toml written beside it.
  Returns (level, text)."""
  if not output_path or not os.path.isfile(output_path):
    return "warn", "Choose the converted Linux executable first."
  output_path = os.path.abspath(output_path)
  if file_state(output_path) != "elf":
    return "err", "This is not a converted Linux ELF (PE and Mach-O outputs cannot be verified)."
  stem = os.path.splitext(output_path)[0]
  manifest = next((m for m in (stem + ".patches.toml", output_path + ".patches.toml") if os.path.isfile(m)), "")
  if not manifest:
    return "err", (f"No manifest found: expected {os.path.basename(stem)}.patches.toml next to the output. "
                   "It is written only when at least one patch was applied during the conversion.")
  set_active_relinker_cfg(rl)
  relinker = resolve_relinker(rl)
  if not relinker or not os.path.isfile(relinker):
    return "err", "Relinker not available yet: run a conversion once (or build it) so the app has it."
  cmd = [relinker, "--verify-patches", manifest, "--verify-output"]
  for e in split_list(rl.get("executables", "")):
    cmd += ["--executable", e]
  cmd.append(output_path)
  kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
  try:
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                         timeout=120, **kwargs)
  except (OSError, subprocess.SubprocessError) as e:
    return "err", f"Could not run the relinker: {e}"
  out = "\n".join(x for x in ((res.stdout or "").strip(), (res.stderr or "").strip()) if x)
  if res.returncode == 0:
    return "ok", "Output OK: no mismatch.\n" + out if out else "Output OK: no mismatch."
  if res.returncode == 1:
    return "err", "Output mismatch (the converted file does not hold the patches):\n" + out
  return "err", "Verification failed:\n" + out


def anyps5_build_info(rl):
  """Which AnyPS5 build the relinker comes from: release tag or source commit (for conversion_info.json)."""
  info = {"anyps5_source": rl.get("relinker_source", "") or "custom"}
  try:
    if rl.get("relinker_source") == "release":
      cur = read_release_current(rl)
      if cur:
        info["anyps5_release_tag"] = cur["tag"]
      info["anyps5_repo"] = normalize_repo_url(rl.get("repo_url", ""))
    elif rl.get("relinker_source") == "github":
      paths = source_paths(rl)
      built = read_source_build(paths) if paths else None
      if built:
        info["anyps5_commit"] = built["commit"]
      if paths:
        info["anyps5_repo"] = paths["url"]
        info["anyps5_ref"] = paths["ref"]
  except Exception:
    pass
  return info


def relinker_version(rl):
  """Run `relinker --version`. Returns (level, text)."""
  set_active_relinker_cfg(rl)
  relinker = resolve_relinker(rl)
  if not relinker or not os.path.isfile(relinker):
    return "warn", "Relinker not available yet: run a conversion once (or build it) so the app has it."
  kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
  try:
    res = subprocess.run([relinker, "--version"], capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=30, **kwargs)
  except (OSError, subprocess.SubprocessError) as e:
    return "err", f"Could not run the relinker: {e}"
  out = "\n".join(x for x in ((res.stdout or "").strip(), (res.stderr or "").strip()) if x)
  if res.returncode != 0:
    return "err", "This relinker does not answer --version (it may be an older build):\n" + out
  return "ok", out or "(no version printed)"


# ─────────────────────────────────────────────────────────────
#  Runtime: environment variables and game shortcut
# ─────────────────────────────────────────────────────────────
PAD_HELPER = os.path.join("tools", "pad_input.py")


def _whole(text):
  return re.fullmatch(r"[0-9]+", text.strip()) is not None


# Advanced runtime variables (docs/dev/ENVIRONMENT.md): key -> (variable, minimum, maximum, windows only)
ADVANCED_INTS = {
  "pipelined_draws": ("APS5_PIPELINED_DRAWS", 0, None, False),
  "pipeline_journal_entries": ("APS5_PIPELINE_JOURNAL_ENTRIES", 1, None, False),
  "pipeline_journal_max_mib": ("APS5_PIPELINE_JOURNAL_MAX_MIB", 1, None, False),
  "pipeline_journal_threads": ("APS5_PIPELINE_JOURNAL_THREADS", 0, None, False),
  "pipeline_store_entries": ("APS5_PIPELINE_STORE_ENTRIES", 1, None, False),
  "address_copy_max_mib": ("APS5_ADDRESS_COPY_MAX_MIB", 1, None, False),
  "pm4_ring": ("APS5_PM4_RING", 0, 4096, False),
  "gpu_wait_timeout_ms": ("APS5_GPU_WAIT_TIMEOUT_MS", 1, None, False),
  "stall_watchdog_secs": ("APS5_STALL_WATCHDOG_SECS", 0, 86400, False),
  "hang_dump_secs": ("APS5_HANG_DUMP_SECS", 1, None, True),
}
ADVANCED_FLAGS = (  # key -> variable (set to 1); the runtime reads their presence
  ("draw_pipeline", "APS5_DRAW_PIPELINE", False), ("pipeline_dispatch", "APS5_PIPELINE_DISPATCH", False),
  ("verify_draw_pipeline", "APS5_VERIFY_DRAW_PIPELINE", False), ("no_pipeline_lto", "APS5_NO_PIPELINE_LTO", False),
  ("no_pipeline_journal", "APS5_NO_PIPELINE_JOURNAL", False), ("skip_refused_draws", "APS5_SKIP_REFUSED_DRAWS", False),
  ("mimalloc", "APS5_MIMALLOC", True),
)


def _crash_stack_value(text):
  """Integer value of APS5_CRASH_STACK_BYTES (decimal or 0x hexadecimal), or None if malformed."""
  t = text.strip()
  if re.fullmatch(r"[0-9]+", t):
    return int(t)
  if re.fullmatch(r"0[xX][0-9a-fA-F]+", t):
    return int(t, 16)
  return None


def validate_advanced_runtime(rt):
  for key, (var, lo, hi, _win) in ADVANCED_INTS.items():
    v = rt.get(key, "").strip()
    if not v:
      continue
    if not _whole(v) or int(v) < lo or (hi is not None and int(v) > hi):
      return f"{var} must be a whole number" + (f" from {lo} to {hi}." if hi is not None else f" of {lo} or more.")
  cs = rt.get("crash_stack_bytes", "").strip()
  if cs:
    n = _crash_stack_value(cs)
    if n is None or n > 1048576:
      return "APS5_CRASH_STACK_BYTES must be a decimal or 0x hexadecimal number, at most 1048576."
  if yes(rt.get("verify_draw_pipeline", "no")) and not yes(rt.get("draw_pipeline", "no")):
    return "APS5_VERIFY_DRAW_PIPELINE needs APS5_DRAW_PIPELINE (it checks that pipeline)."
  return None


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
  opos = rt.get("overlay_pos", "").strip()
  if opos and opos not in OVERLAY_POSITIONS:
    return "ANYPS5_OVERLAY_POS must be tl, tr, bl or br."
  sdir = rt.get("screenshots_dir", "").strip()
  if sdir and not os.path.isabs(sdir):
    return "ANYPS5_SCREENSHOTS must be an absolute path."
  spk = str(rt.get("pad_speaker_db", "0")).strip().lower()
  if spk not in ("", "mute", "off", "-inf"):
    try:
      if not -60 <= float(spk) <= 0:
        raise ValueError
    except ValueError:
      return "APS5_PAD_SPEAKER_FALLBACK_DB must be a number from -60 to 0, or mute."
  lf = rt.get("log_file", "").strip()
  if lf and (not os.path.isabs(lf) or os.path.isdir(lf)):
    return "ANYPS5_LOG_FILE must be an absolute path to a file, not a folder."
  adv_err = validate_advanced_runtime(rt)
  if adv_err:
    return adv_err
  if yes(rt.get("trace_bench", "no")) and not every:
    return "APS5_TRACE_BENCH=1 needs APS5_TRACE_SUMMARY_EVERY (e.g. 30): set the interval too."
  tcf = rt.get("trace_calls_file", "").strip()
  if tcf and not os.path.isabs(tcf):
    return "APS5_TRACE_CALLS_FILE must be an absolute path to a file."
  if tcf and not rt.get("trace_calls", "").strip():
    return "APS5_TRACE_CALLS_FILE needs APS5_TRACE_CALLS (and --trace-imports) to record anything."
  tel = rt.get("videoout_telemetry_dir", "").strip()
  if tel and not os.path.isabs(tel):
    return "APS5_VIDEOOUT_TELEMETRY_DIR must be an absolute path."
  disp = rt.get("display_index", "").strip()
  if disp and not _whole(disp):
    return "ANYPS5_DISPLAY_INDEX must be a whole number from 0 up (0 = first display)."
  ent = rt.get("entitlements_file", "").strip()
  if ent and not os.path.isfile(ent):
    return f"ANYPS5_ENTITLEMENTS: '{ent}' is not an existing file."
  return validate_catalog_env(rt)


# Environment variables documented in docs/dev/ENVIRONMENT.md that have no dedicated option: (name, kind, default, description)
ENV_CATALOG = [
  ('ANYPS5_JOB_MANAGER_CASE', 'string', '', 'selects the CPU fixture run by tools/run_job_manager_reference.py'),
  ('ANYPS5_JOB_MANAGER_HOST', 'presence', '', 'runs the reference adapter against the host fallback instead of the loaded guest module'),
  ('ANYPS5_LOADEXEC_PARENT', 'string', '', 'variable the Windows entry stub reads; its wide name is built in core/relinker/elfpatcher/src/windows/WindowsEntryStubBuilder.cpp'),
  ('ANYPS5_LOADEXEC_TEST_ROOT', 'string', '', 'directory the load-exec host test reads its guest executable from'),
  ('ANYPS5_POSIX_SPAWN_ROLE', 'string', '', "role of the test's own copy spawned as a posix_spawn child, which exits or sleeps on it and otherwise fails"),
  ('ANYPS5_TEST_QUOTED_ARGV0', 'presence', '', "makes the load-exec test's first argument one with quotes, a backslash and non-ASCII text"),
  ('APS5_EBOOT', 'string', '', 'host path of the executable launched for /app0/eboot.bin; unset or empty means the running executable'),
  ('APS5_SKIP_TIMING_TESTS', 'string', '', "treats the timing tests' runner as preempted, so they skip without probing the clock; an empty value or one starting with 0 leaves the probe on"),
  ('APS5_USBD_DEVICE', 'string', '', 'selects the USB device a test drives, as vendor:product in hex'),
  ('ANYPS5_GUEST_MODULE_ARGS', 'string', '', 'path of the file the module-argument fixture appends its events to'),
  ('APS5_BDA_BYTE_READS', 'presence', '', 'forces one address lookup and byte load per byte for BDA reads instead of a wide dword load'),
  ('APS5_BDA_DWORD_STORES', 'presence', '', 'reverts BDA writes to one address store per dword instead of a wide multi-dword store'),
  ('APS5_DUMP_IR', 'string', '', 'hex code address (or all) of the programs whose IR is printed after each front-end pass'),
  ('APS5_EXACT_RCP', 'presence', '', "emits v_rcp_f32 and v_rcp_iflag_f32 as the RDNA hardware reciprocal (bit-exact for normal inputs and results) instead of the driver's division; adds about 13k SPIR-V words to each shader that uses them"),
  ('APS5_GATHER_CONST', 'integer', '', 'integer every non-depth gather returns in all four texels instead of the sampled value'),
  ('APS5_IMAGE_TABLE_KEY_SCAN', 'integer', '256', 'number of image-table entries the key scan examines'),
  ('APS5_IMAGE_TABLE_STRICT', 'string', '', 'sets image-table materialization to its strict form; 0 leaves it off'),
  ('APS5_NO_ATOMIC_BARRIER', 'presence', '', 'drops the device-scope acquire/release memory barrier emitted after atomics'),
  ('APS5_NO_ATOMIC_ZERO_SKIP', 'presence', '', 'restores plain buffer atomics instead of skipping identity-operand atomics'),
  ('APS5_NO_CODE_HASH_KEY', 'presence', '', 'appends every code word to the cache key instead of hashing the code'),
  ('APS5_NO_FAILURE_MEMO', 'presence', '', 'rebuilds a program whose compilation failed instead of reusing the memoized failure'),
  ('APS5_NO_IMAGE_TABLES', 'presence', '', 'disables image tables, leaving table-loaded descriptors unplanned'),
  ('APS5_NO_MASKED_RESULT_MEMO', 'presence', '', 'materializes every result memo miss instead of patching the buffer descriptors, user data and flattened SRT of a cached result whose snapshot differs only in those words'),
  ('APS5_NO_PERF_DISK_PLANS', 'presence', '', 'disables the disk cache of planning results'),
  ('APS5_NO_PERF_DOMINATOR_TREE', 'presence', '', "computes the structurizer's dominator and post-dominator sets with the set fixpoint instead of a dominator tree, walks every selection construct instead of testing its members by dominance, and tests every block against each…"),
  ('APS5_NO_PERF_DOM_INTERSECTION', 'presence', '', 'computes dominator intersections into a fresh vector instead of intersecting in place'),
  ('APS5_NO_PERF_ITERATION_SELECTION', 'presence', '', "recomputes the post-dominator sets when finding a loop's iteration-selection merge instead of reusing them"),
  ('APS5_NO_RESULT_MEMO', 'presence', '', 'materializes a fresh result for every recompile instead of reusing the result memo'),
  ('APS5_NO_SRT_TAPE', 'presence', '', 'runs the runtime-source walk of a plan in the IR evaluator instead of the compiled tape'),
  ('APS5_NO_TABLE_RESOLUTION_REUSE', 'presence', '', "stops a result memo miss from reusing an unchanged image table's resolution, so the table is resolved again"),
  ('APS5_NO_TABLE_SNAPSHOT_REUSE', 'presence', '', 'turns off the image table snapshot reuse, so every capture reads and compares each table column'),
  ('APS5_NO_WINDOW_NARROWING', 'presence', '', "indexes every slot of the shader's vector window instead of only the slots the program uses"),
  ('APS5_PROBE', 'string', '', 'pc:vgpr[:shift] probe that copies that VGPR into v255 after the instruction so image stores write it as a per-lane trace'),
  ('APS5_RAYTRACING', 'string', '', 'selects the image_bvh_intersect_ray behaviour: strict throws and miss writes 0xffffffff'),
  ('APS5_RESULT_MEMO_MB', 'integer', '256', 'MiB the result memos of all shader sources may hold before the least recently used results are dropped (0 keeps them until their set evicts them)'),
  ('APS5_SAVEEXEC_WRITE_FIRST', 'presence', '', 'writes the saved exec to the destination before reading the source in s_*_saveexec'),
  ('APS5_SINGLE_LANE', 'string', '', 'hex code addresses (comma separated, or all) of wave64 programs kept at one guest lane per invocation'),
  ('APS5_SRT_DEBUG', 'presence', '', 'prints extra SRT descriptor-walk debug output, such as guest reads outside the request memory'),
  ('APS5_STRICT_DESCRIPTOR_WEBS', 'string', '', "makes a T# or S# read in a descriptor web throw when a path's descriptor dwords depend on the program counter, EXEC or a lane mask, instead of selecting the first descriptor and recording an InvalidDescriptor fault when a lane…"),
  ('APS5_TRACE_BDA', 'presence', '', 'prints the first few accesses that make a program take the address-based path'),
  ('APS5_TRACE_BUFFER_WRITTEN', 'presence', '', 'prints how many guest buffer elements a program may store to and how many it only loads'),
  ('APS5_TRACE_IMAGE_TABLE', 'presence', '', 'traces image-table resolution'),
  ('APS5_VERIFY_MASKED_RESULT_MEMO', 'presence', '', 'also materializes every patched result memo hit, prints the first twenty whose patched result differs and counts them in the result memo report of APS5_PROFILE_DRAW'),
  ('APS5_VERIFY_SRT_COMPACT', 'presence', '', 'walks every SRT descriptor a second time through the IR evaluator and throws when the two walks differ'),
  ('APS5_VERIFY_SRT_TAPE', 'presence', '', "walks a plan's runtime sources a second time through the evaluator, adopts its answers where the two differ and prints the first twenty differences"),
  ('APS5_WAVE_LDS_SUBGROUP', 'presence', '', 'restores subgroup-scope LDS barriers for single-wave workgroups instead of workgroup scope'),
  ('APS5_TRACE_LABEL', 'integer', '0', 'hex guest address; logs a matching RELEASE_MEM when recorded, and nearby RELEASE_MEM, WRITE_DATA, COPY_DATA, DMA_DATA and WAIT_REG_MEM packets when executed'),
  ('ANYPS5_REQUIRE_DESCRIPTOR_BINDING_FLAGS', 'presence', '', 'fails the replay test instead of accepting a device that refuses descriptor binding flags'),
  ('ANYPS5_REQUIRE_ROBUST2', 'presence', '', 'fails the LDS lock-budget test when the device does not enable robustBufferAccess2, instead of only printing it'),
  ('ANYPS5_REQUIRE_VULKAN', 'presence', '', 'fails a test instead of skipping when no usable Vulkan device is present'),
  ('APS5_AB', 'string', '', '<seconds>:<switch>[+<switch>...]: alternates phase A (switches as the environment sets them) and phase B (the listed runtime switches toggled) at flip packets, each phase at least <seconds> long; prints [ab] lines; a name that is…'),
  ('APS5_AB_SETTLE', 'string', '2', "seconds into a phase after which APS5_AB prints the phase's settled line; shorter than the phase"),
  ('APS5_AB_START', 'string', '0', 'flip packet count (from 1) at which APS5_AB starts, or <n>s for seconds after the first flip'),
  ('APS5_ALL_BUFFERS_WRITTEN', 'presence', '', 'notes every shader buffer element as written, as before bufferWritten existed'),
  ('APS5_ASSERT_GPU_LOCK', 'presence', '', 'aborts when a guarded path is reached without holding GuestMemory::GpuMutex'),
  ('APS5_ATOMIC_STAGE_MAX_KIB', 'integer', '1024', 'size cap in KiB on an atomic shader element covered by device-local staging'),
  ('APS5_BARRIER_VALIDATE', 'presence', '', 'counts the barriers a hazard-tracking validator would emit and skip, by class and hazard kind'),
  ('APS5_BATCH_CAP', 'integer', '64', 'recorded work since the last submit at which a label batch, flush or reap is forced (0 disables the cap)'),
  ('APS5_BDA_FLUSH', 'presence', '', 'also flushes pending storage results over address-based shader regions, which are otherwise exempt'),
  ('APS5_BDA_STAGING_MIN_THREADS', 'integer', '512', 'smallest dispatch, in threads, whose address-based build stages its written elements device-local; an indirect dispatch always does'),
  ('APS5_BDA_TABLE_CACHE_ENTRIES', 'integer', '8', 'number of recently used BDA page tables kept in the cache, clamped to 1..64 (1 is the previous behaviour)'),
  ('APS5_BLOCK_WRITEBACK_EACH', 'presence', '', 'stores the touched units only on every write-back instead of widening repeated partial stores'),
  ('APS5_BLOCK_WRITEBACK_FRAMES', 'integer', '8', 'window of presents within which repeated partial stores trigger the whole-image widening'),
  ('APS5_BLOCK_WRITEBACK_PIECES', 'integer', '4', 'number of partial stores within the window that triggers the whole-image widening'),
  ('APS5_BLOCK_WRITEBACK_WIDEN', 'presence', '', 'enables the widening that stores a whole image when it keeps being asked for in pieces'),
  ('APS5_BUFFER_POOL_SHARED', 'presence', '', 'uses one buffer tier for every allocation size, as before the tier split'),
  ('APS5_BUFFER_POOL_SLOTS', 'integer', '4096', 'bounds the buffers each pool tier retains (0 keeps the default of 4096)'),
  ('APS5_CAPTURE_GPU_COPIES', 'presence', '', 'records GPU buffer copies in the capture trace (requires APS5_CAPTURE_TRACE)'),
  ('APS5_CAPTURE_INPUTS', 'presence', '', "records each draw's inputs in the capture trace (requires APS5_CAPTURE_TRACE)"),
  ('APS5_CAPTURE_TRACE', 'presence', '', 'writes capture events to capture-trace.log'),
  ('APS5_CAPTURE_TRACE_PATH', 'string', 'capture-trace.log', 'file path for the capture events log'),
  ('APS5_CHECK_INDIRECT_ARGS', 'presence', '', 'reads GPU-side indirect draw records back on the CPU and prints the first ones'),
  ('APS5_COMPLETION_STORE_SYNC', 'presence', '', 'makes a store made by a completion action wait for later batches over its range'),
  ('APS5_COPY_DESTINATION_STRICT', 'presence', '', 'refuses a CPU copy whose destination has any pending writer, not only an unsettled one'),
  ('APS5_COPY_HLE_CPU_MAX', 'integer', '262144', 'largest copy in bytes the CPU side of the copy HLE handles'),
  ('APS5_COPY_HLE_GPU_MAX', 'integer', '', 'largest copy in bytes the GPU side of the copy HLE handles; unset means no limit'),
  ('APS5_COPY_READ_TRACKING', 'string', '', 'notes in-place reads of a range; set to 0 to disable, making the CPU copy path require an idle recorder'),
  ('APS5_COPY_SYNC', 'presence', '', "treats any pending writer over a copy's range as needing a wait"),
  ('APS5_COPY_VERIFY', 'presence', '', "verifies a CPU copy's source and destination bytes against the pre-copy values after a sync"),
  ('APS5_COPY_WAIT_SOURCE', 'presence', '', "waits for the source's producer batch when it is the only one in flight, on non-zero queues"),
  ('APS5_COUNT_ALL_COMPLETION_LABELS', 'presence', '', 'counts a completion label also stored on the GPU as pending from its registration'),
  ('APS5_CPU_COPIES', 'presence', '', 'copies misaligned descriptor sub-ranges on the CPU instead of with a GPU buffer copy'),
  ('APS5_CPU_DCC_KEYS', 'presence', '', 'always stores DCC keys on the CPU'),
  ('APS5_CPU_FLIP_IN_ORDER', 'presence', '', 'orders every CPU flip behind queued work instead of presenting unqueued flips at once'),
  ('APS5_CPU_STORES', 'presence', '', 'writes packet stores on the CPU instead of recording them on the GPU'),
  ('APS5_DCC_KEYS_EACH', 'presence', '', 'records every DCC key store at once instead of batching them'),
  ('APS5_DEPTH_COPY_MIN_KIB', 'integer', '1024', 'minimum surface size in KiB for a depth target to take the depth-copy path'),
  ('APS5_DEPTH_PLANE_COPY_MIB', 'integer', '256', 'budget in MiB of the sampled depth-plane copies kept for reuse (least recently used copies leave first; copies sampled since the previous present stay while all copies hold at most twice the budget); 0 keeps every copy'),
  ('APS5_DEPTH_PLANE_WHOLE_STORE', 'presence', '', 'stores a stale depth plane whole when memory over it is read, instead of only the 64 KiB units the read overlaps'),
  ('APS5_DISPATCH_CACHE_BYTES', 'integer', '268435456', 'size of the dispatch cache in bytes (0 keeps the default of 256 MiB)'),
  ('APS5_DISPATCH_CACHE_ENTRIES', 'integer', '8192', 'number of dispatch cache entries; 0 picks a default from the variant count: 8192 with the default 4 variants, 16384 with 2 or fewer, 4096 under APS5_DISPATCH_STAMP_VALIDATE'),
  ('APS5_DISPATCH_STAMP_VALIDATE', 'presence', '', 'validates dispatch cache hits by write stamps, forcing one variant and a 4096-entry cache'),
  ('APS5_DISPATCH_VARIANTS', 'integer', '4', 'number of dispatch variants kept per cache entry, clamped to the maximum'),
  ('APS5_DISPMOVE_MAX_KIB', 'integer', '1024', 'upper bound in KiB on the read-only buffers a dispatch-cache hit moves into its template, above which the dispatch is prepared again'),
  ('APS5_DRAIN_ALL', 'presence', '', 'drains every packet, disabling the batched recording path'),
  ('APS5_DRAIN_COMPLETION_LABELS', 'presence', '', 'records completion labels immediately instead of deferring them with the batch, and drains a label recorded while a copied write-back is pending instead of storing it behind the write-backs'),
  ('APS5_DRAW_CACHE_BYTES', 'integer', '268435456', 'size of the draw cache in bytes, evicting least-recently-used entries past it (0 keeps the default of 256 MiB)'),
  ('APS5_DRAW_CACHE_ENTRIES', 'integer', '4096', 'number of entries in the draw cache; 0 keeps the default 4096'),
  ('APS5_DRAW_DRAIN', 'presence', '', 'treats draw packets as drains, waiting for them instead of recording ahead'),
  ('APS5_DRAW_INPUTS_IN_PLACE', 'presence', '', "binds draw vertex and index ranges in place from host imports also with NVIDIA's Windows driver, which copies them by default"),
  ('APS5_DRAW_KEY_ALL_USER_WORDS', 'presence', '', "includes all 32 user words in a draw's register key instead of the used count"),
  ('APS5_DRAW_SNAPSHOT_MAX_MIB', 'integer', '64', "upper bound in MiB on the snapshot bytes a draw's bindings may take"),
  ('APS5_DRAW_TRANSITIONS', 'presence', '', 'keeps the per-draw upload and download barriers and layout transitions instead of the lean recorded path'),
  ('APS5_DRAW_UPLOAD_MAX', 'integer', '4096', 'largest draw snapshot in bytes copied into the per-recorder upload chunk; 0 turns the chunk off'),
  ('APS5_DUMP_COMPUTE_MIN_WORDS', 'integer', '', 'only captures compute pipelines whose SPIR-V has at least this many words (an integer from 0 through 8388608; any other value fails)'),
  ('APS5_DUMP_COMPUTE_PIPELINES', 'string', '', 'directory in which captured compute pipelines are written'),
  ('APS5_DUMP_DRAW_SHADERS', 'integer', '0', "hex color-target or slot-0 address, or a program's code address, whose draw's shaders are dumped"),
  ('APS5_DUMP_DRAW_SLOT1', 'integer', '0', "hex slot-1 address whose draw's shaders and register state are dumped"),
  ('APS5_DUMP_FRAMES', 'integer', '0', 'saves the first n presented frames as frame_<index>.bmp'),
  ('APS5_DUMP_FRAMES_EVERY', 'integer', '1', 'dumps every n-th presented frame'),
  ('APS5_DUMP_QUEUE', 'integer', '', 'dumps the first submissions of the queue with this hex id'),
  ('APS5_DUMP_REJECTED', 'presence', '', 'writes a rejected submission to rejected_submission_<n>.bin'),
  ('APS5_DUMP_SCALE', 'integer', '4', 'keeps every n-th pixel in each direction of a dumped frame (1 to 16384; any other value fails)'),
  ('APS5_DUMP_SHADERS', 'presence', '', "writes each prepared shader's request to shader_<address>.req"),
  ('APS5_DUMP_STORAGE', 'string', '', 'selects storage textures to dump by hex address or WxHxL[:format] extent as storage_<address>_<n>.raw'),
  ('APS5_DUMP_SUBMITS', 'string', '', 'directory in which every submission is captured for replay'),
  ('APS5_DUMP_TARGETS', 'integer', '0', 'dumps the first n renders of each non-resident color target as target_<address>_<n>.raw in the working directory; a non-zero value also runs every draw synchronously, and in practice it needs APS5_NO_RESIDENT_TARGETS=1'),
  ('APS5_DUMP_TEXTURE', 'string', '', 'hex addresses or WxH extents whose sampled textures are dumped on each of their first 8 uploads as texture_<address>_<n>.raw'),
  ('APS5_EAGER_WRITEBACK', 'presence', '', 'writes back pending storage layers eagerly'),
  ('APS5_EXACT_DRAW_WRITES', 'string', '', "sizes a color or depth target's draw write ranges from its own bytes and slices (the default when unset); 0 uses the slack and unknown-byte approximation instead"),
  ('APS5_FILL_CHAIN', 'presence', '', 'seeds and doubles the fill pattern in place in the import'),
  ('APS5_FILL_CLEAR_EXACT_ONLY', 'presence', '', 'turns a fill into a clear only when it covers the target exactly and nothing else'),
  ('APS5_FILL_SYNC', 'presence', '', "waits for every recorded store over the fill's range"),
  ('APS5_FLIP_INFLIGHT', 'integer', '1', 'number of presentations that may trail on the GPU (0, 1 or 2)'),
  ('APS5_FLIP_READ_CHECK', 'presence', '', "records the in-place reads of completed batches and checks them before the present blit's fence"),
  ('APS5_FORCE_SDR', 'presence', 'unset', 'suppresses advertised HDR availability while retaining detected host capabilities; uses the existing SDR presentation path'),
  ('APS5_FORCE_STRUCTURIZER_FALLBACK', 'presence', '', 'makes the structurizer use the reach-flag fallback for every graph that supports it instead of trying cloning first; the control-flow tests then expect that fallback'),
  ('APS5_FRAME_CAPTURE', 'string', '', "directory in which the driver records a few frames of the title's driver calls and guest memory for agc_frame_replay"),
  ('APS5_FRAME_CAPTURE_AT_FLIP', 'integer', '0', 'frame boundary (CPU flip or submission that flips) at which the frame capture starts when no trigger file is set; 0 and 1 both start at the first boundary; a value that is not a number fails'),
  ('APS5_FRAME_CAPTURE_FRAMES', 'integer', '10', 'number of frame boundaries the frame capture records'),
  ('APS5_FRAME_CAPTURE_TRIGGER', 'string', '', 'file whose appearance starts the frame capture at the next frame boundary; a number in it replaces the frame count'),
  ('APS5_FULL_BARRIERS', 'presence', '', 'records every barrier instead of merging redundant ones'),
  ('APS5_GPU_COPY_MAX_KIB', 'integer', '1024', 'size cap in KiB on a descriptor sub-range copied through the GPU path, larger ones staying on the CPU'),
  ('APS5_GPU_NO_WRITEBACK', 'presence', '', 'skips writing GPU results back into guest memory'),
  ('APS5_GROUP_CAPTURE_ALL_QUEUES', 'presence', '', 'resolves group-ahead capture on queue 0 as well'),
  ('APS5_HANDOFF_SPIN_US', 'integer', '', "microseconds a thread spins for a handed-off draw's work or for the GPU mutex before blocking, clamped to 0..10000; unset spins 150 us when the process has more than 8 usable processors and not at all otherwise"),
  ('APS5_HEAP_MIRROR_MIB', 'integer', '24576', 'budget in MiB for the heap mirrors held for registered allocations'),
  ('APS5_HOOK_FLUSH_CPU_BLOCKS', 'string', '', 'set to 0 flushes pending images whose every 64 KiB block the CPU wrote, instead of skipping them'),
  ('APS5_HOOK_FULL_SYNC', 'presence', '', "makes the flush hook's recorded-store wait a full Sync under the GpuMutex"),
  ('APS5_HOOK_LOCKED_WAIT', 'presence', '', "keeps the flush hook's pending-write wait under the GpuMutex"),
  ('APS5_IGNORE_DEPTH_TEST', 'presence', '', 'renders depth- and stencil-tested draws without a depth target as if the tests always passed'),
  ('APS5_IMAGE_POOL_MIB', 'integer', '', "size in MiB of the released image memory the buffer pool keeps for reuse by memory type and size while the device-local heap is under its reported budget, a quarter of the device tier's budget by default; 0 frees image memory at…"),
  ('APS5_INDIRECT_VERTEX_MIB', 'integer', '64', 'cap in MiB on a vertex attribute extent, above which a GPU-side indirect draw resolves on the CPU'),
  ('APS5_INSERT_COMPARE', 'presence', '', "compares a fresh capture's regions against the recorder before inserting it into the cache"),
  ('APS5_KEPT_DESCRIPTOR_SETS', 'integer', '8', 'number of freed dedicated image table descriptor sets kept for a later build with the same layout and sizes; 0 keeps none'),
  ('APS5_KEYFLIP_DROP', 'presence', '', "drops a pending unit's results on a clear-code to uncompressed key flip, as before"),
  ('APS5_KEYS_FILL_CLEAR', 'string', '', 'clears an image from DCC keys whose fill pattern it matches (only when set to 1)'),
  ('APS5_LABEL_FLUSH_US', 'integer', '', 'age in microseconds at which a pending label batch is recorded or submitted; unset is 1000 with batched label submission and 250 when APS5_NO_LABEL_BATCH_SUBMIT or APS5_NO_LABEL_BATCHING turns it off'),
  ('APS5_LABEL_GATE_ALL', 'presence', '', 'gates a label store on any pending completion, not only one overlapping the range'),
  ('APS5_LABEL_LOCK_EACH', 'presence', '', 'records each label under the GpuMutex instead of deferring it'),
  ('APS5_LABEL_OWN_LOCK', 'presence', '', 'makes a label flush take the GpuMutex itself instead of relying on a self-locking packet'),
  ('APS5_LABEL_REAP_ALL_QUEUES', 'presence', '', 'reaps completion labels on every queue instead of only queue 0'),
  ('APS5_LABEL_RUNS_INLINE', 'presence', '', 'records store runs at once and closes each run at the next command'),
  ('APS5_LABEL_STORE_ALWAYS', 'presence', '', 'writes back every label store even when a later store already covered its bytes'),
  ('APS5_LABEL_SUBMIT_NOW', 'presence', '', 'submits every label immediately instead of waiting for the flush deadline'),
  ('APS5_LABEL_TRUST_LATE', 'string', '', "trusts cache entries with a stamp at or below the wait's stamp under the late rule (off unless set to a non-zero value)"),
  ('APS5_LABEL_TRY_EACH_PACKET', 'presence', '', 'attempts to record deferred labels at every self-locking packet instead of deferring to its lock'),
  ('APS5_LAND_WAITS_ALL_QUEUES', 'presence', '', 'notes pending waits on queue 0 as well'),
  ('APS5_LAZY_SHADER_PREPARE', 'presence', '', 'prepares a registered shader on its first use instead of while registering it'),
  ('APS5_LDS_SLACK', 'integer', '0', "dwords added to every dispatch's LDS allocation"),
  ('APS5_LOCKED_BUILD', 'presence', '', 'builds the whole dispatch preparation under the GpuMutex, skipping the split path'),
  ('APS5_LOCKED_DRAW_PREPARE', 'presence', '', 'takes the GpuMutex before preparing a draw'),
  ('APS5_LOG_STATS', 'string', '', "prints the AGC log's line, byte and coalescing statistics every 10 s window by default, when unset or set to anything but 0; 0 turns them off"),
  ('APS5_LOG_SYNC', 'presence', '', 'writes AGC log lines on the calling thread instead of through the asynchronous sink thread'),
  ('APS5_LOOP_GUARD', 'presence', '', 'enables the fault buffer that guards shader loops running past their bound'),
  ('APS5_MIRROR_REFRESH_THREADS', 'integer', '3', 'number of helper threads that refresh heap mirrors, clamped to 0 through 16'),
  ('APS5_NO_ADDRESS_READ_PASSES', 'presence', '', "stops a draw that only reads through its buffer descriptors from counting as read-only, so it breaks the pass instead of continuing the producer's"),
  ('APS5_NO_ADDRESS_SPACE_CACHE', 'presence', '', 'disables the address-space cache so every build rebuilds its space'),
  ('APS5_NO_ADJACENT_GENERATION', 'presence', '', 'leaves adjacent pending images at their generation during a write-back, as before'),
  ('APS5_NO_ALIAS_BORROW', 'presence', '', 'disables borrowing a pending aliased storage image'),
  ('APS5_NO_ATOMIC_STAGING', 'presence', '', 'binds atomic shader elements in place instead of staging them through a device-local buffer'),
  ('APS5_NO_BARRIER_BATCH', 'presence', '', 'records the staging, write-back and label-run leading barriers that a covering barrier already orders, instead of leaving them out; APS5_AB can toggle it'),
  ('APS5_NO_BARRIER_ELISION', 'presence', '', 'records every leading barrier instead of merging it into the following submission'),
  ('APS5_NO_BDA_STAGING', 'presence', '', 'binds the written elements of address-based compute builds in place in the host imports instead of staging them device-local'),
  ('APS5_NO_BDA_TABLE_CACHE', 'presence', '', 'builds every BDA descriptor table instead of reusing the recent-table cache'),
  ('APS5_NO_BLOCK_TRACKING', 'presence', '', 'tracks array layers, or the whole surface, instead of 64 KiB blocks'),
  ('APS5_NO_BUFFER_CLASSES', 'presence', '', 'matches exact sizes in the buffer pool instead of rounding up to size classes'),
  ('APS5_NO_CACHED_PRECOLLECT', 'presence', '', "leaves the write-watch walks of a cached object's Revalidate under the GPU mutex"),
  ('APS5_NO_CLEAR_HAND_OVER', 'presence', '', "writes other images' pending results back at a clear-key storage refresh instead of handing them over to the refreshed image"),
  ('APS5_NO_CLEARED_VIEW', 'presence', '', 'keeps fast-cleared surfaces as CPU snapshots instead of viewing the cleared storage image'),
  ('APS5_NO_COLLECT_MEMO', 'presence', '', 'disables the per-thread memo of write-watch collects'),
  ('APS5_NO_CONTEXT_CACHE', 'presence', '', 'rebuilds the Graphics::Context on every call instead of returning the cached one'),
  ('APS5_NO_COPY_ALIAS', 'presence', '', 'keeps every device copy a transfer instead of aliasing the source storage image'),
  ('APS5_NO_COPY_HLE', 'presence', '', 'disables the copy-kernel HLE, so matching copies run as compute'),
  ('APS5_NO_COPY_KNOWN_VALUES', 'presence', '', "disables serving a copy's destination from source values the driver already knows"),
  ('APS5_NO_CPU_COPY', 'presence', '', 'disables the CPU copy path of the copy HLE'),
  ('APS5_NO_DATA_HITS', 'presence', '', 'disables dispatch-cache hits that differ only in ShaderData or FlattenedSrt words'),
  ('APS5_NO_DCC_PRESYNC', 'presence', '', 'leaves the DCC key ranges out of the surfaces a build pre-syncs, so only the surface itself waits for its producer'),
  ('APS5_NO_DEFERRED_DESTRUCTION', 'presence', '', 'on MoltenVK, destroys buffers and frees device memory as soon as the driver releases them, instead of while the graphics queue is idle (KhronosGroup/MoltenVK#2876)'),
  ('APS5_NO_DCC_WORD_SCAN', 'presence', '', 'restores the byte-by-byte DCC key scan instead of the word-at-a-time compare'),
  ('APS5_NO_DEFERRED_EVICTION', 'presence', '', 'destroys displaced resource-cache entries under the mutex instead of deferring them to the open batch'),
  ('APS5_NO_DEFERRED_SETTLES', 'presence', '', 'settles watched private host imports as they open instead of through the deferred block-settling path (Windows only)'),
  ('APS5_NO_DIRECT_DEPTH_COPY', 'presence', '', "keeps every copy between a depth plane and a color storage image on the staging buffer instead of one vkCmdCopyImage (used when VK_KHR_maintenance8 is enabled and the storage format is a single component of the aspect's size)"),
  ('APS5_NO_DISPATCH_CACHE', 'presence', '', 'disables the dispatch cache so every dispatch takes the uncached path'),
  ('APS5_NO_DISPATCH_KEY_HYGIENE', 'presence', '', 'mixes every shader register into the dispatch key instead of only the registers that affect the dispatch'),
  ('APS5_NO_DISPATCH_LRU', 'presence', '', 'clears the whole dispatch cache on overflow instead of evicting the least recently used entry'),
  ('APS5_NO_DISPATCH_MOVE', 'presence', '', 'leaves a dispatch-cache hit from moving its read-only buffers into the template, so it prepares the dispatch again; also off when APS5_NO_TEMPLATE_DATA_REFRESH is set'),
  ('APS5_NO_DISPATCH_RECIPE', 'presence', '', 'builds no dispatch recipes, so dispatch-cache hits repeat the recorded work'),
  ('APS5_NO_DRAW_DATA_HITS', 'presence', '', 'disables draw-cache hits that differ only in ShaderData words'),
  ('APS5_NO_DRAW_INPUTS_IN_PLACE', 'presence', '', 'copies draw vertex and index ranges instead of binding them in place from host imports, on any device; wins over APS5_DRAW_INPUTS_IN_PLACE'),
  ('APS5_NO_DRAW_KEY', 'presence', '', 'registers no draw key, so every draw decodes its programs instead of hitting the draw cache'),
  ('APS5_NO_DRAW_KEY_TRIM', 'presence', '', 'keys the render target and index ranges into the draw resource key instead of repeating the alias checks on a hit'),
  ('APS5_NO_DRAW_PRECHECK', 'presence', '', 'disables the draw precheck that rejects empty or invalid draws early'),
  ('APS5_NO_DRAW_RECIPE', 'presence', '', 'builds no draw recipes, so draw-cache hits repeat the recorded stages'),
  ('APS5_NO_DRAW_RELOCATION', 'presence', '', "disables draw relocation, which rewrites a draw's moved user words from a learned variant instead of preparing the draw again"),
  ('APS5_NO_DRAW_RESOURCE_CACHE', 'presence', '', "builds every draw's shader resources instead of reusing a cached identical build"),
  ('APS5_NO_DRAW_SRT_ENTRIES', 'presence', '', "disables the draw cache's SRT entries, so every draw decodes its programs"),
  ('APS5_NO_DRAW_STAGE_DATA_HITS', 'presence', '', "disables draw-cache hits that differ only in a stage's ShaderData words"),
  ('APS5_NO_EMPTY_INDIRECT_SKIP', 'presence', '', 'builds the stages of an indirect draw whose front program last took the CPU path even when none of its records draws anything, instead of reading its records first and skipping it'),
  ('APS5_NO_EPOCH_COLLECT_MAP', 'presence', '', 'drops the per-epoch map of completed collects, leaving only the fixed-size thread memo to answer a repeated collect'),
  ('APS5_NO_EPOCH_REVALIDATE', 'presence', '', "runs every Revalidate's per-element and per-region checks instead of the epoch gate"),
  ('APS5_NO_EXACT_ROBUST_LOADS', 'presence', '', 'keeps the descriptor-range comparison in direct buffer loads on a device that zeroes past the range'),
  ('APS5_NO_FAST_REVALIDATE', 'presence', '', 'always repeats the resource lookups instead of using the fast proof'),
  ('APS5_NO_FILL_CLEAR', 'presence', '', 'disables the clear-through-DCC-keys path in a fill'),
  ('APS5_NO_FILL_HLE', 'presence', '', 'disables the fill-kernel HLE, so matching fills run as compute'),
  ('APS5_NO_FLUSH_PRETEST', 'presence', '', 'lists every pending image on FlushPending instead of testing whether a pending unit lies inside the range'),
  ('APS5_NO_FOREIGN_WRITERS', 'presence', '', 'records no draw-written ranges as foreign writers for capture validation'),
  ('APS5_NO_GATE_RETRY', 'presence', '', 'retries no validation whose entry moved, leaving the dispatch or draw unvalidated'),
  ('APS5_NO_GPL', 'presence', '', 'draws without graphics pipeline libraries; the device still enables their extensions, so the driver builds its pipelines as for a default device'),
  ('APS5_NO_GPU_DUMP', 'presence', '', 'decodes dump frames from the tiled guest buffer on the CPU'),
  ('APS5_NO_GPU_INDIRECT', 'presence', '', 'reads indirect dispatch arguments on the CPU instead of letting the GPU count them'),
  ('APS5_NO_GPU_INDIRECT_DRAW', 'presence', '', 'reads indirect draw arguments through the flush hook on the CPU instead of from the GPU'),
  ('APS5_NO_GPU_SAMPLE_DUMPS', 'presence', '', 'dumps occlusion-counter samples on the CPU instead of through the GPU copy path'),
  ('APS5_NO_GPU_THREAD_DIMENSIONS', 'presence', '', 'reads the thread counts of an indirect dispatch with USE_THREAD_DIMENSIONS on the CPU instead of turning them into group counts and a thread limit on the GPU'),
  ('APS5_NO_HOLD_PROFILE', 'presence', '', 'turns off GpuMutex hold-time profiling, keeping only the wait timing'),
  ('APS5_NO_HOOKSYNC_PROFILE', 'presence', '', 'leaves only the plain hook-sync counts under APS5_PROFILE_DRAW'),
  ('APS5_NO_HOOK_COMPLETION_GUARD', 'presence', '', "lets the flush hook record the accessing worker's queued labels even from inside a completion action"),
  ('APS5_NO_HOOK_SNAPSHOT', 'presence', '', 'takes the GpuMutex on every access instead of using the pending-write snapshot'),
  ('APS5_NO_HOST_IMPORT', 'presence', '', 'disables importing guest memory, so address-based shaders copy instead of using it in place'),
  ('APS5_NO_HW_F32_FLUSH', 'presence', '', 'skips the startup f32 denormal flush probe and keeps the emulated flush in every shader'),
  ('APS5_NO_HW_F32_PRESERVE', 'presence', '', 'stops requesting DenormPreserve for 32-bit floats, so the device keeps its default f32 denormal handling'),
  ('APS5_NO_IMAGE_PAGE_CACHE', 'presence', '', "leaves the main guest image's page-protection table uncached"),
  ('APS5_NO_INDIRECT_DRAW', 'presence', '', 'rejects indirect draw packets at submission validation'),
  ('APS5_NO_JOIN_WAW_CHECK', 'presence', '', 'lets a contiguous store join the pending one without the write-after-write check'),
  ('APS5_NO_KEEP_NEW_STORAGE', 'presence', '', 'does not keep a newly created storage image alive with the open batch; such an image evicted before the batch completes can hand its memory to another image through the buffer pool while the batch still writes it'),
  ('APS5_NO_KEYS_FILL_MEMO', 'presence', '', 'records no memo of GPU-side DCC key fills'),
  ('APS5_NO_KEYS_REFILL', 'presence', '', "keeps a covered image's uploaded keys as they were instead of marking them uncompressed after a matching key fill"),
  ('APS5_NO_KEYS_RESCAN', 'presence', '', "reuses the DCC keys scanned before the lookup's refresh instead of discarding them for a rescan"),
  ('APS5_NO_KEY_FAST_PATH', 'presence', '', "scans a surface's DCC keys on every call instead of using and storing a proof"),
  ('APS5_NO_LABEL_BATCHING', 'presence', '', 'turns off every label-batching behaviour, the label runs, queued labels and batched submit'),
  ('APS5_NO_LABEL_BATCH_SUBMIT', 'presence', '', 'disables batched label submission'),
  ('APS5_NO_LABEL_PACKET_FLUSH', 'presence', '', 'disables the label flush between packets, which runs only when batched label submission is off (APS5_NO_LABEL_BATCH_SUBMIT or APS5_NO_LABEL_BATCHING)'),
  ('APS5_NO_LABEL_READBACK_STORE', 'presence', '', 'leaves pending linear and small storage images on the GPU at a label instead of storing them before it'),
  ('APS5_NO_LABEL_RUNS', 'presence', '', 'records every store in a run of its own between a barrier pair'),
  ('APS5_NO_LABEL_SHORTCUT', 'presence', '', 'disables the pending-label table shortcut in a memory wait'),
  ('APS5_NO_LAYER_CLEAR', 'presence', '', 'tracks every surface as one layer instead of per array layer'),
  ('APS5_NO_LAYER_REFUSAL_MEMO', 'presence', '', 're-checks a layer whose surface check failed at its current generation on every fill'),
  ('APS5_NO_LAYOUT_CACHE', 'presence', '', 'builds descriptor set layouts fresh instead of through the descriptor cache'),
  ('APS5_NO_LEASE_MIRROR', 'presence', '', 'restores copies instead of image mirrors for address-based builds'),
  ('APS5_NO_LIBRARY_PREBUILD', 'presence', '', 'disables prebuilding registration-form shader libraries (only 1 disables it)'),
  ('APS5_NO_LOCK_PROFILE', 'presence', '', 'turns off GpuMutex wait profiling under APS5_PROFILE_DRAW'),
  ('APS5_NO_MOVED_BUFFER_TEMPLATES', 'presence', '', 'keys the full descriptor words of read-only guest buffers instead of allowing a hit when they move'),
  ('APS5_NO_OBSERVATION_GUARD', 'presence', '', 'observes every validated range, not only the ranges whose flush hook waited'),
  ('APS5_NO_OPPORTUNISTIC_REAP', 'presence', '', 'skips retiring already-finished batches before a submit'),
  ('APS5_NO_OWN_IMAGE_REFRESH', 'presence', '', 'lets a Pending failure of the fast proof take the full walk instead of the own-object refresh'),
  ('APS5_NO_PARALLEL_MIRROR_REFRESH', 'presence', '', 'disables the mirror-refresh thread pool, so mirror refreshes run on the calling thread'),
  ('APS5_NO_PATTERN_FILL_HLE', 'presence', '', 'disables the dword-pattern fill HLE'),
  ('APS5_NO_PAUSE_SPIN', 'presence', '', 'disables the CPU pause-spin in a blocked memory wait'),
  ('APS5_NO_PENDING_HTILE_FILLS', 'presence', '', 'reads the HTILE words of a depth target bind even when recorded fills are pending over them, instead of working the words out from the fills'),
  ('APS5_NO_PERF_FRONTEND_PAIR', 'presence', '', "disables the recompiler's frontend-pair reuse; the shader disk cache treats it as output-neutral"),
  ('APS5_NO_PERF_LAYOUT_PAIR', 'presence', '', "disables the recompiler's layout-pair reuse; the shader disk cache treats it as output-neutral"),
  ('APS5_NO_PIPELINED_INDIRECT', 'presence', '', 'drains the draw pipeline at every CPU-path indirect draw instead of queuing it behind the pipeline'),
  ('APS5_NO_PIPELINE_CACHE', 'presence', '', 'builds a pipeline per draw instead of reusing cached ones'),
  ('APS5_NO_POOL_CACHE', 'presence', '', 'allocates descriptor pools fresh instead of through the descriptor cache'),
  ('APS5_NO_PRECOLLECT', 'presence', '', 'disables the stage-A precollect pass'),
  ('APS5_NO_PREPARED_FIND', 'presence', '', 'looks the resource-cache key up again instead of using the object stage A found'),
  ('APS5_NO_PRESENT_EPOCH', 'presence', '', 'keeps the presenter at collect epoch 0 instead of adopting the newest epoch a worker started'),
  ('APS5_NO_PRESYNC', 'presence', '', 'disables the stage-A pre-sync of pending writes'),
  ('APS5_NO_PROC_TABLE', 'presence', '', "resolves the device's Vulkan entry points per call instead of filling the function table once"),
  ('APS5_NO_QUEUED_DRAW_READ_ORDER', 'presence', '', 'stops queued draws and DMA copies from holding back write-backs over the guest memory they read at commit'),
  ('APS5_NO_QUEUED_LABELS', 'presence', '', 'disables the queued-label table'),
  ('APS5_NO_RECENT_IMPORTS', 'presence', '', 'looks every host import up in the import map instead of first checking the last four found while the map is unchanged'),
  ('APS5_NO_RECORDED_CLEAR', 'presence', '', "waits for a storage image's fast clear in a batch of its own instead of recording it"),
  ('APS5_NO_RECORDER_SYNC_DRAWS', 'presence', '', 'gives the draws the recorder waits for their own batch instead of recording them with the wait'),
  ('APS5_NO_RECORD_COPIED_DRAWS', 'presence', '', 'waits for copied-write draws at once instead of listing their write-back for later'),
  ('APS5_NO_RECT_INDIRECT', 'presence', '', 'sends an indirect rectangle-list draw down the CPU path instead of the GPU-side path'),
  ('APS5_NO_RESIDENT_LINEAR', 'presence', '', 'drops the residency of linear color targets, so a target whose address has a host import, or whose import a draw would make, is copied in and out again'),
  ('APS5_NO_RESIDENT_PRESENT', 'presence', '', 'always presents through guest memory instead of the resident image'),
  ('APS5_NO_RESIDENT_TARGETS', 'presence', '', 'copies every render target in and out again instead of keeping it resident'),
  ('APS5_NO_RESOURCE_CACHE', 'presence', '', 'disables the shared resource cache, so every dispatch builds its resources fresh'),
  ('APS5_NO_RUN_MEMO', 'presence', '', "walks the lease for a host import's run of ranges on every lookup instead of reusing the bounds found for the same lease snapshot and import window block"),
  ('APS5_NO_SAMPLED_FROM_STORAGE', 'presence', '', "keeps every sampled texture a CPU snapshot instead of viewing the surface's storage image"),
  ('APS5_NO_SAMPLER_CACHE', 'presence', '', 'creates a sampler per binding instead of caching them'),
  ('APS5_NO_SEPARATE_QUEUED_LABELS', 'presence', '', 'lets a queued label enter the recorded table and replace a recorded entry of its dword'),
  ('APS5_NO_SHAPE_MEMO', 'presence', '', "looks up every draw's resident targets and pipeline anew instead of repeating the ones an earlier draw of the same register shape used"),
  ('APS5_NO_SINGLE_PASS_COLLECT', 'presence', '', 'walks the written pages once without clearing them and, when any were written, a second time clearing them, instead of one cleared pass'),
  ('APS5_NO_SITE_SAMPLING', 'presence', '', 'disables per-site sampling of write evidence'),
  ('APS5_NO_SNAPSHOT_CHECK', 'presence', '', 'skips the live compare that proves a captured snapshot still equals the guest bytes'),
  ('APS5_NO_SORTED_SNAPSHOT_LOOKUP', 'presence', '', "scans the snapshot's regions in AddSnapshot instead of searching them"),
  ('APS5_NO_SPACE_STAMP_ONCE', 'presence', '', 'stamps every direct import of a cached address space on each hit instead of once per space'),
  ('APS5_NO_SPARSE_HEAP_MIRROR', 'presence', '', 'disables sparse heap mirrors, mirroring only whole heap ranges'),
  ('APS5_NO_STAGE_A_IMAGES', 'presence', '', 'leaves every image element to the stage-B full lookup instead of a stage-A record'),
  ('APS5_NO_STORAGE_DEDUPE', 'presence', '', 'looks each identical consecutive storage descriptor up, and refreshes its image, separately'),
  ('APS5_NO_SYNC_SITES', 'presence', '', 'leaves only the per-source and per-thread sync counts, with no per-site attribution'),
  ('APS5_NO_SYNC_THROUGH', 'presence', '', 'makes every pending-write sync a full Sync instead of the targeted unlocked wait'),
  ('APS5_NO_TABLE_REPLAY', 'presence', '', "makes a reused bound image table rerun each element's checks instead of replaying its recorded bind outcomes"),
  ('APS5_NO_TABLE_REUSE', 'presence', '', 'disables reusing a materialized image table'),
  ('APS5_NO_TARGET_WRITEBACK', 'presence', '', 'skips writing render targets back to guest memory after the draw'),
  ('APS5_NO_TEMPLATE_DATA_REFRESH', 'presence', '', "keys the ShaderData and FlattenedSrt words into compute keys instead of refreshing a template's data buffers"),
  ('APS5_NO_TEXTURE_CACHE', 'presence', '', 'builds every texture and storage-image object fresh instead of caching it'),
  ('APS5_NO_TEXTURE_HASH', 'presence', '', 'finds texture-cache entries by scanning the list instead of the hash index'),
  ('APS5_NO_TIMELINE', 'presence', '', 'leaves timeline semaphores off, so drains wait under the mutex'),
  ('APS5_NO_UNIT_SHADOW', 'presence', '', 'makes every unit shadow primitive inert'),
  ('APS5_NO_UNLOCKED_DEVICE', 'presence', '', 'loads the device and runs a dispatch under the GPU mutex instead of using an unlocked device'),
  ('APS5_NO_UNLOCKED_DRAIN', 'presence', '', 'keeps the GPU mutex held while draining a packet instead of the unlocked drain'),
  ('APS5_NO_UNLOCKED_VALIDATE', 'presence', '', 'keeps the dispatch-cache mutex held while validating a cache entry'),
  ('APS5_NO_UNREGISTERED_DROP', 'presence', '', "stores a storage image's pending results even when its memory is no longer registered, instead of dropping them"),
  ('APS5_NO_VALIDATE_CACHE', 'presence', '', 'validates every draw instead of serving the validation memo'),
  ('APS5_NO_VALIDATE_SKIP', 'presence', '', 'syncs every pending write instead of skipping validation on written-element evidence'),
  ('APS5_NO_VOUCHED_VALIDATION', 'presence', '', "validates a cached draw or dispatch through its per-run checks instead of its table block's vouch"),
  ('APS5_NO_WAIT_OVERLAP_SUBMIT', 'presence', '', 'disables the unlocked wait that submits recorded work while overlapping writes are open'),
  ('APS5_NO_WORDWISE_CAPTURE', 'presence', '', 'fetches every captured page whole instead of reading pending pages word by word'),
  ('APS5_NO_WORKER_AFFINITY', 'presence', '', 'leaves the queue workers and presenter unpinned (affinity mask 0)'),
  ('APS5_NO_WRITER_KEYED_EVIDENCE', 'presence', '', 'records written-element evidence without keying it by writer'),
  ('APS5_NO_WRITE_EVIDENCE', 'presence', '', 'disables the written-element evidence that lets a read skip the flush hook'),
  ('APS5_NO_WRITE_ONLY_PASSES', 'presence', '', 'counts a buffer a draw only stores to as read and written, so the next draw storing to it begins a new render pass'),
  ('APS5_NO_WRITTEN_SHADOW', 'presence', '', 'binds written staging elements in place instead of through the written shadow'),
  ('APS5_NV_DIAGNOSTICS', 'presence', '', 'creates the device with VK_NV_device_diagnostics_config and VK_NV_device_diagnostic_checkpoints where supported, for Nsight Aftermath crash dumps'),
  ('APS5_PACKET_EPOCH', 'presence', '', 'makes queue workers bump the collect epoch before every packet'),
  ('APS5_PIPELINE_DRAIN_FILLS', 'presence', '', 'runs queue-0 buffer fills through the draw pipeline to keep their order; any value stores them in place instead'),
  ('APS5_PIPELINE_DRAIN_LABELS', 'presence', '', "answers a queue-0 wait from the draw pipeline's own pending labels while it is busy; any value drains the pipeline at the wait"),
  ('APS5_PIPELINE_DRAIN_SUBMISSIONS', 'presence', '', "lets a submission end without submitting its recorded work, so it joins a later submit with the pipeline's queue; any value drains the pipeline and submits at the end of the submission"),
  ('APS5_POLL_REAP', 'presence', '', 'reaps completions on every poll tick'),
  ('APS5_POLL_TRY_EACH', 'presence', '', 'tries the poll service on every tick instead of at the poll interval'),
  ('APS5_PREBUILD_THREADS', 'integer', '', 'threads in the pool that prebuilds registration-form shader libraries; unset uses half the hardware threads, at least two'),
  ('APS5_PRESENT_POLL_FENCE', 'presence', '', 'polls the present fence every 50 us instead of blocking on it'),
  ('APS5_PROBE_DISPATCH', 'string', '', 'probes one dispatch at a hex program address (optionally :index) with debug output'),
  ('APS5_PROFILE_GPU_DRAWS', 'presence', '', "with APS5_PROFILE_GPU, records the GPU time of each recorded draw by its fragment program's variant id and prints a [drawvariant] line with the code address behind each variant id once (draws that do not use a recipe only; the…"),
  ('APS5_REAP_EACH_PACKET', 'presence', '', 'reaps deferred labels after every packet'),
  ('APS5_RELEASE_ON_UNLOCK', 'presence', '', 'destroys deferred batches on the unlocking thread instead of a release thread'),
  ('APS5_RELEASE_QUEUE_MAX', 'integer', '256', 'maximum deferred batches handed to the release thread before a thread destroys its own'),
  ('APS5_RELEASE_UNDER_LOCK', 'presence', '', 'destroys deferred batches in finish() under the lock instead of on a release thread'),
  ('APS5_RESOURCE_CACHE_ENTRIES', 'integer', 'adaptive', 'fixes the bound on the entries of the per-device shader-resource cache, which otherwise grows while evicted keys keep coming back and shrinks back to its base when few keys are in use; 0 keeps the adaptive bound'),
  ('APS5_RESOURCE_CACHE_TABLE_ELEMENTS', 'integer', '65536', 'number of image-table elements the shader-resource cache keeps (0 keeps the default)'),
  ('APS5_SAMPLE_COMMITTER', 'string', '', "samples queue 0's draw committer every millisecond and writes its stacks to a file (Windows only)"),
  ('APS5_SAMPLE_THREADS', 'string', '', "samples every thread of the process every 2 ms and writes each busy thread's hottest frames to a file (Windows only)"),
  ('APS5_SAMPLE_WORKER', 'string', '', 'samples the calling queue worker every millisecond and writes its stacks to a file (Windows only)'),
  ('APS5_SHADOW_VERIFY', 'presence', '', 'verifies that the shadow-published bytes equal the guest bytes'),
  ('APS5_SHARED_COLLECT_MEMO', 'presence', '', "uses the tracker's shared collect-memo ring under its mutex instead of the per-thread ring"),
  ('APS5_SHARED_MEMORY_COUNTERS', 'presence', '', 'accumulates the memory counters into one shared set with atomic RMWs'),
  ('APS5_SKIP_INVALID_INDIRECT_REGISTERS', 'presence', '', 'skips the entries whose offset dword is no register offset in a SET_*_REG_INDIRECT list read again where its packet executes, and writes its valid entries, instead of stopping'),
  ('APS5_SKIP_REQUEST_DUMPS', 'integer', '8', 'caps RecompileRequest dumps per shader-skip reason'),
  ('APS5_SKIP_SHADER', 'integer', '0', 'hex program address of a shader whose draws and dispatches are dropped'),
  ('APS5_SKIP_SHADER_FILE', 'path', '', 'file of whitespace-separated hex program addresses (# starts a comment) whose draws and dispatches are dropped; re-read within 500 ms of a change; a token that is not a whole 64-bit hex number is reported and ignored'),
  ('APS5_SNAPSHOT_CHECK_FATAL', 'presence', '', 'throws on the first snapshot that differs from registered memory instead of only counting them'),
  ('APS5_SPANNED_WAVE', 'presence', '', 'spans a single-wave wave64 compute program over two full 32-lane subgroups'),
  ('APS5_SPIRV_OPT', 'string', '', 'selects the SPIR-V optimization pipeline: full restores the performance pipeline, none skips optimization'),
  ('APS5_SRGB_ATTACHMENT_PROXY', 'string', '', 'forces the sRGB color-attachment proxy format; 1 forces it'),
  ('APS5_SRGB_SHADER_DECODE', 'string', '', '1 forces shader sRGB decode for the sRGB 8-bit formats; copy does the same, except that 8 and 8_8 sRGB sample an RGBA8 sRGB copy when the device can blit to RGBA8; any other value has no effect'),
  ('APS5_STAGING_POOL_MIB', 'integer', '', 'size in MiB of the device-local staging buffer pool; 0 disables the device tier'),
  ('APS5_STALE_EVICT', 'presence', '', "also evicts a proved-present dead image's pending layers on an access"),
  ('APS5_SUBMISSION_EPOCH_EACH', 'presence', '', 'bumps the guest-memory collect epoch on every submission instead of only when the submission is newer than the last bump'),
  ('APS5_SUBMIT_AT_END', 'presence', '', 'submits recorded work at the end of a submission even when a worker is queued'),
  ('APS5_SUBMIT_PROGRESS', 'presence', '', 'enables opt-in pacing for eligible queue-zero submission prefixes within a shared nominal two-millisecond acknowledgement/timeline budget; timeout retains asynchronous acceptance'),
  ('APS5_SUSPEND_DRAIN', 'presence', '', 'drains the device at a suspend point'),
  ('APS5_SYNC_COMPLETION_DRAWS', 'presence', '', 'keeps every draw with completion work synchronous'),
  ('APS5_SYNC_DISPATCH', 'presence', '', 'waits for every dispatch instead of batching it'),
  ('APS5_SYNC_DRAWS', 'presence', '', 'makes every draw synchronous'),
  ('APS5_SYNC_FLIP', 'presence', '', 'makes flips synchronous (no frames in flight)'),
  ('APS5_SYNC_LEASE_DISPATCH', 'presence', '', 'waits for leased draws and dispatches at once instead of deferring them'),
  ('APS5_SYNC_SAMPLE_DUMPS', 'presence', '', 'takes the CPU path for occlusion-counter sample dumps, as APS5_NO_GPU_SAMPLE_DUMPS does'),
  ('APS5_SYNC_SHADER_PREPARE', 'presence', '', 'prepares registered shaders on the registering thread instead of in the background'),
  ('APS5_SYNC_TEXTURE_UPLOAD', 'presence', '', 'restores the waiting batch for every texture upload'),
  ('APS5_TEMPLATE_AUDIT_MS', 'integer', '1000', 'milliseconds after which a cached shader-resource object takes the full walk instead of the fast proof; 0 turns the audit off'),
  ('APS5_THREAD_RTTIME_US', 'integer', '200000', 'hard RLIMIT_RTTIME the realtime plan lowers to, in microseconds'),
  ('APS5_TRACE_BARRIERS', 'integer', '', 'prints the first n accesses for which the hazard validator would skip a barrier; needs APS5_BARRIER_VALIDATE'),
  ('APS5_TRACE_BIGBUF', 'presence', '', 'lists every copied guest region of at least 1 MiB'),
  ('APS5_TRACE_CAPSYNC', 'presence', '', 'prints every unlocked hook wait'),
  ('APS5_TRACE_CONTEXT_STATE', 'presence', '', 'logs context-state operations and the context registers written'),
  ('APS5_TRACE_COPY_HLE', 'presence', '', "traces the CPU copy HLE's refusals"),
  ('APS5_TRACE_DCC_KEYS', 'presence', '', 'traces stores of uncompressed DCC keys'),
  ('APS5_TRACE_DISPATCH_CACHE', 'presence', '', 'reports dispatch-cache entries whose key changed'),
  ('APS5_TRACE_DISPATCH_IO', 'string', '', "prints every dispatch's resources; 2 also lists the small-buffer words"),
  ('APS5_TRACE_DRAWS', 'presence', '', 'prints one line per draw with its phases and inputs'),
  ('APS5_TRACE_DRAW_CACHE', 'presence', '', 'reports draw-cache entries whose key changed'),
  ('APS5_TRACE_DRAW_PROGRAMS', 'presence', '', 'prints one line per recorded draw with the stage, role and code address of each program'),
  ('APS5_TRACE_FILL_COVER', 'presence', '', 'prints the first fills that meet images without covering them'),
  ('APS5_TRACE_FLUSH', 'presence', '', 'names what forces pending storage-image results into guest memory'),
  ('APS5_TRACE_GPU', 'presence', '', 'prints GPU wait, submission and scanout events, the execute lines, and waits longer than 200 ms (read in DisplayBuffer, PacketExecution, QueueWorker, Submission and WaitMemory)'),
  ('APS5_TRACE_HOST_IMPORT', 'presence', '', 'traces host-import mapping failures'),
  ('APS5_TRACE_INDIRECT_DRAWS', 'presence', '', 'prints indirect draw packets that reach the precheck'),
  ('APS5_TRACE_LATE_LABELS', 'presence', '', 'prints how late labels were trusted or refused'),
  ('APS5_TRACE_PREPARATION_WAIT', 'presence', '', 'prints where a demanded shader preparation sat in the queue'),
  ('APS5_TRACE_QUERY', 'presence', '', 'on Windows, prints every 16th page query with its cost'),
  ('APS5_TRACE_RESOURCE_KEY_MISSES', 'presence', '', 'reports shader-resource cache key misses'),
  ('APS5_TRACE_SHADER_CORRELATION', 'presence', '', 'traces shader selection and preparation failure tokens through draw batches (requires APS5_CAPTURE_TRACE)'),
  ('APS5_TRACE_SHADER_REGS', 'string', '', "prints a shader's registers for one hex program address, or all"),
  ('APS5_TRACE_SHADOW', 'integer', '', 'prints the first n shadow publishes, the first 64 seeds, every evict and retire'),
  ('APS5_TRACE_SKIPS', 'presence', '', 'reports each distinct shader skip once with or without it; set, a repeat of an already reported skip is also logged as again, cut to 100 characters'),
  ('APS5_TRACE_STAGING', 'presence', '', 'prints every distinct staged range once, with its size and whether an atomic element lies in it'),
  ('APS5_TRACE_TEXTURES', 'presence', '', "prints a sampled texture's bytes and how many are nonzero"),
  ('APS5_TRACE_UNREADABLE', 'presence', '', 'names the code that checked an inaccessible range'),
  ('APS5_TRACE_UPLOAD', 'presence', '', 'names why a storage image is uploaded again'),
  ('APS5_UNIT_SHADOW_MIB', 'integer', '1024', "budget in MiB of the unit shadow's slabs"),
  ('APS5_UNIT_SHADOW_SLAB_MIB', 'integer', '8', 'size in MiB of one unit-shadow slab'),
  ('APS5_VALIDATE_LEGACY', 'presence', '', 'uses the legacy snapshot and full-compare path for pending-write validation'),
  ('APS5_VALIDATE_SYNC_SKIP', 'presence', '', 'reads skipped dwords through the hook too and counts a mismatch'),
  ('APS5_VERIFY_BDA_TABLES', 'presence', '', 'checks a reused BDA page table against the one just built'),
  ('APS5_VERIFY_DATA_HITS', 'presence', '', 'recomputes a dispatch data hit and aborts on a disagreement'),
  ('APS5_VERIFY_DRAW_DATA_HITS', 'presence', '', 'recomputes a draw data hit and aborts on a disagreement'),
  ('APS5_VERIFY_DRAW_ENTRIES', 'presence', '', 're-decodes every draw-cache hit through the full path and checks it'),
  ('APS5_VERIFY_DRAW_RECIPE', 'presence', '', 're-decodes a draw recipe instead of using the cache and checks the result'),
  ('APS5_VERIFY_DRAW_SNAPSHOTS', 'presence', '', 're-reads a reused draw snapshot and counts the ones that differ'),
  ('APS5_VERIFY_KNOWN_VALUES', 'presence', '', 'serves known values through the hook too and counts mismatches'),
  ('APS5_VERIFY_PROOFS', 'presence', '', 'runs the full walk beside every own-object refresh proof and aborts on a different decision'),
  ('APS5_VERIFY_RECIPE', 'presence', '', 'makes the ordinary prepare path beside a recipe hit and compares the answers'),
  ('APS5_VERIFY_REGISTER_KEYS', 'presence', '', 'checks the incremental register fingerprint against the full state'),
  ('APS5_VERIFY_SET_LAYOUTS', 'presence', '', "checks the draw's set layouts against those described from the shaders"),
  ('APS5_VERIFY_SHAPE_MEMO', 'presence', '', 'looks up the resident targets and pipeline of every shape memo hit anew as well, throws when they differ and prints [shape-memo] counts of the checked hits every 10 s'),
  ('APS5_WAIT_LOCK', 'presence', '', 'always takes the GPU mutex on a wait (no unlocked label lookup)'),
  ('APS5_WAIT_LOCK_ENTRY', 'presence', '', 'takes the GPU mutex blocking on the entry instead of trying it'),
  ('APS5_WATCH_MEMORY', 'string', '', 'watches named guest ranges (<hex address>:<hex bytes>[,...]) across dispatches; needs APS5_SYNC_DISPATCH=1'),
  ('APS5_WORKER_AFFINITY', 'presence', '', 'pins the queue workers and presenter to the performant cores of a hybrid CPU (needed to enable it on non-Linux hosts)'),
  ('APS5_WORKER_AFFINITY_MASK', 'integer', '', 'explicit hex CPU affinity mask for the queue workers and presenter'),
  ('APS5_WORKING_SET_MIB', 'integer', '', 'sets the process working-set minimum to this many MiB (Windows only)'),
  ('APS5_WRITE_EVIDENCE_AFTER', 'integer', '4', "consecutive unchanged observations before a dword's written-element evidence is trusted"),
  ('APS5_WRITE_EVIDENCE_MAX_KIB', 'integer', '16', 'largest written range in KiB that the evidence may serve'),
  ('APS5_WRITE_EVIDENCE_SAMPLE', 'integer', '16', 'reads one in this many pending writes to sample them'),
  ('APS5_WRITE_WATCH_IMPORTS', 'string', 'probe', 'how host imports are write-tracked, not on Windows: probe, watch or unwatch; any other value throws'),
  ('APS5_WRITTEN_SHADOW_MAX_KIB', 'integer', '2048 (8192 on a discrete GPU)', 'upper bound in KiB of the written staging window; a set value is used as given on both device types'),
  ('APS5_WRITTEN_SHADOW_MIN_KIB', 'integer', '16', 'lower bound in KiB of the written staging window'),
  ('APS5_TRACE_AJM', 'presence', '', 'logs every job the title submits and what the library writes back'),
  ('APS5_TRACE_AUDIOOUT', 'presence', '', 'logs each audio-out port opened with its type, sample count, frequency and format'),
  ('APS5_TRACE_AUDIOOUT2', 'presence', '', "prints the title's AudioOut2 context and port parameters and stream activity"),
  ('APS5_TRACE_FIBER', 'presence', '', 'logs fiber initialization, switches and returns to the thread'),
  ('APS5_SAVEDATA_TRACE', 'presence', '3', 'logs every save-data operation instead of the first three at each logging site'),
  ('APS5_USB_STORAGE_PASSTHROUGH', 'presence', '', 'passes the FAT and exFAT volumes the host mounted from USB mass-storage devices through to the guest, read-only where the host mounted them read-only; unset, no host volume is scanned and the guest sees no USB storage device'),
  ('ANYPS5_REQUIRE_DISPLAY', 'presence', '', 'makes video-out tests fail instead of skipping when no display or Vulkan device is available'),
  ('APS5_NO_PAD_INPUT', 'presence', '', 'ignores keyboard and mouse input so a measurement run is not advanced by a stray press'),
  ('APS5_NO_WGI', 'presence', '', "turns off SDL's Windows.Gaming.Input joystick backend at default hint priority, on Windows hosts where it faults a few seconds into every run"),
  ('APS5_OUTPUT_RESOLUTION', 'string', '', "<width>x<height> in pixels that replaces the host display's size when sceVideoOutGetOutputStatus and sceVideoOutGetOutputStatusWithVrr report the output resolution: 1 for a size below 3840 wide and 2160 high, 2 otherwise, as for…"),
  ('APS5_TRACE_VIDEODEC', 'presence', '', 'logs decoder memory queries, queue allocation and the first few decodes'),
  ('APS5_HEAP_CACHE_MIB', 'integer', '4096', 'size in MiB of the cache of freed large blocks before the oldest are decommitted'),
  ('APS5_HELPER_AFFINITY_MASK', 'integer', '', 'hex core mask that helper threads (deferred release, mirror refresh) are pinned to'),
  ('APS5_NO_PROCMAP_QUERY', 'presence', '', 'skips the /proc/self/maps PROCMAP_QUERY ioctl and scans the maps file instead'),
  ('APS5_NO_SSE4A_EMULATION', 'presence', '', 'disables emulation of AMD-only SSE4a instructions, making such a fault fatal'),
  ('APS5_NO_WRITE_WATCH', 'presence', '', 'disables write watching so guest buffers are fully re-read on every use'),
  ('APS5_PIN_WAIT_MS', 'integer', '60000', 'milliseconds to wait for a pinned allocation lease before failing'),
  ('APS5_TRACE_AFFINITY', 'presence', '', 'prints a line naming the role, tid and mask of every thread pinning'),
  ('APS5_TRACE_EXIT', 'presence', '', 'prints a backtrace of the caller of exit, _Exit and the process-exit path'),
  ('APS5_TRACE_SSE4A', 'presence', '', 'prints each emulated SSE4a instruction once per rip'),
  ('APS5_VERIFY_DIRECT_COLLECT', 'presence', '', 'queries the regions of each private-memory collect read from the allocation map and throws when that collect succeeded over memory that is no longer committed private'),
  ('APS5_WATCH_ADDR', 'integer', '', 'absolute hex address whose writes are reported after its page is committed'),
  ('APS5_WATCH_WRITE', 'integer', '', 'guest ELF vaddr whose 8-byte writes are reported by single-stepping the protected page'),
  ('ANYPS5_LOADEXEC_ARGV_SKIP', 'string', '', 'on Windows, set to 1 to drop the first process argument and clear the variable'),
  ('APS5_COARSE_TIMED_WAITS', 'presence', '', 'on Windows, rounds timed waits up to the scheduler tick'),
  ('APS5_JOB_AFFINITY', 'presence', '', "on Windows, pins the title's job worker threads to the efficiency cores of a hybrid CPU"),
  ('APS5_JOB_AFFINITY_MASK', 'integer', '', 'on Windows, hex core mask that job worker threads are pinned to'),
  ('APS5_NO_DIRECT_MEMORY_SECTION_POOL', 'presence', '', 'on Windows, creates a section for every direct-memory allocation and closes it at release instead of reusing released sections that never held guest data'),
  ('APS5_NO_JOB_AFFINITY', 'presence', '', 'on Windows, disables pinning of job worker threads'),
  ('APS5_NO_SLEEP_WAKE', 'presence', '', 'keeps every sleep of 1 ms or less in full instead of ending it at a GPU batch, batch completion action or flip completion'),
  ('APS5_NO_TIMER_RESOLUTION', 'presence', '', 'keeps the default 15.6 ms scheduler tick instead of raising it to 0.5 ms on Windows'),
  ('APS5_TIME_SCALE', 'string', '1', 'factor applied to the guest monotonic clocks and TSC; sleeps and host JobManager worker spin intervals still take real time; fractional factors work, and a value that is not above 0 uses 1'),
  ('APS5_TRACE_APR', 'presence', '', 'logs every APR file read with its path, offset, size and destination'),
  ('APS5_TRACE_MEMORY', 'presence', '', 'logs direct-memory map, remap, protect, unmap and physical allocation operations'),
  ('APS5_TRACE_SYNC', 'presence', '', 'logs event-flag and semaphore operations'),
  ('APS5_TRACE_TIMERS', 'presence', '', 'on Windows, runs a timed-wait selftest at startup and prints mean, p50 and max latencies'),
  ('APS5_TRACE_USLEEP', 'presence', '', 'reports every 2000 usleep calls the guest call sites that sleep most'),
  ('APS5_TRACE_WAITS', 'presence', '', 'aggregates blocking waits per guest call site and prints the longest-waiting sites every 3 s'),
]
ENV_CATALOG_KINDS = {n: k for n, k, _d, _t in ENV_CATALOG}


def catalog_env_values(rt):
  """Dict {VARIABLE: value} chosen in the 'All environment variables' panel (rt['env_catalog'] is JSON)."""
  raw = rt.get("env_catalog", "").strip()
  if not raw:
    return {}
  try:
    data = json.loads(raw)
  except ValueError:
    return None
  if not isinstance(data, dict):
    return None
  return {str(k): str(v) for k, v in data.items()}


def validate_catalog_env(rt):
  data = catalog_env_values(rt)
  if data is None:
    return "The list of extra environment variables is damaged: clear it in Runtime > All environment variables."
  for name, value in data.items():
    kind = ENV_CATALOG_KINDS.get(name)
    if kind is None:
      return f"{name} is not a documented environment variable."
    value = value.strip()
    if kind == "integer" and value and not re.fullmatch(r"-?[0-9]+|0[xX][0-9a-fA-F]+", value):
      return f"{name} must be a whole number (decimal or 0x hexadecimal)."
    if kind == "path" and value and not os.path.isabs(value):
      return f"{name} must be an absolute path."
  return None


def find_pad_helper(rl):
  """tools/pad_input.py in <data>/tools or in the cloned repository, else None."""
  candidates = [os.path.join(DATA_DIR, PAD_HELPER)]
  paths = source_paths(rl) if rl.get("relinker_source") == "github" else None
  if paths:
    candidates.append(os.path.join(paths["repo"], PAD_HELPER))
  for c in candidates:
    if os.path.isfile(c):
      return c
  return None


INPUT_MAPPER_REL = os.path.join("tools", "input-mapper")
INPUT_MAPPER_PORT = 4173


def find_input_mapper(rl):
  """tools/input-mapper in <data>/tools or in the cloned repository, else None. Returns (folder_to_run_from, mapper_dir)."""
  candidates = [DATA_DIR]
  paths = source_paths(rl) if rl.get("relinker_source") == "github" else None
  if paths:
    candidates.append(paths["repo"])
  for base in candidates:
    if os.path.isfile(os.path.join(base, INPUT_MAPPER_REL, "package.json")):
      return base, os.path.join(base, INPUT_MAPPER_REL)
  return None


def download_input_mapper(rl, log, cancel):
  """Release option: tools/input-mapper is not a release asset, so it is taken from the source
  archive of the release tag (else of the default branch) and unpacked into <data>/tools/input-mapper.
  Returns '' on success, otherwise the error text."""
  paths = release_paths(rl)
  gh = github_owner_repo(rl.get("repo_url", "")) if paths else None
  if not gh:
    return "The Release option needs a github.com repository URL to download the input mapper."
  owner, repo = gh
  cur = read_release_current(rl)
  refs = []
  if cur:
    refs.append((cur["tag"], "refs/tags/" + cur["tag"], cur["tag"]))
  refs.append(("HEAD", "HEAD", "the default branch"))
  prefix = "tools/input-mapper/"
  dest = os.path.join(DATA_DIR, INPUT_MAPPER_REL)
  tmp_dir = dest + ".part"
  os.makedirs(os.path.dirname(dest), exist_ok=True)
  for ref, path_ref, desc in refs:
    check_cancel(cancel)
    archive = os.path.join(DATA_DIR, "input-mapper-source.zip.part")
    url = "https://github.com/%s/%s/archive/%s.zip" % (owner, repo, path_ref)
    if not _download_file(url, archive, 0, "", log, cancel, "input-mapper (%s)" % desc, quiet=True):
      continue
    try:
      if os.path.isdir(tmp_dir):
        _rmtree_force(tmp_dir)
      found = False
      with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
          name = info.filename
          top, _, rest = name.partition("/")  # "<repo>-<tag>/tools/input-mapper/..."
          if not rest.startswith(prefix) or info.is_dir():
            continue
          rel = rest[len(prefix):]
          parts = rel.split("/")
          if not rel or any(x in ("", ".", "..") for x in parts) or ":" in rel or rel.startswith("/"):
            continue  # never write outside the mapper folder
          target = os.path.join(tmp_dir, *parts)
          os.makedirs(os.path.dirname(target), exist_ok=True)
          with z.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
          found = True
      if not found or not os.path.isfile(os.path.join(tmp_dir, "package.json")):
        continue  # this ref has no input mapper: try the default branch
      if os.path.isdir(dest):
        _rmtree_force(dest)
      os.replace(tmp_dir, dest)
      return ""
    except (OSError, zipfile.BadZipFile) as e:
      return "Unable to unpack the input mapper: %s" % e
    finally:
      try:
        os.remove(archive)
      except OSError:
        pass
      if os.path.isdir(tmp_dir):
        _rmtree_force(tmp_dir)
  return "tools/input-mapper was not found in the repository (release or default branch), or the download failed. Check the connection."


def install_input_mapper_deps(mapper_dir, npm):
  """npm install for the mapper if its package.json lists dependencies and node_modules is missing."""
  try:
    with open(os.path.join(mapper_dir, "package.json"), encoding="utf-8") as f:
      deps = (json.load(f) or {}).get("dependencies") or {}
  except (OSError, ValueError):
    return ""
  if not deps or os.path.isdir(os.path.join(mapper_dir, "node_modules")):
    return ""
  kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
  try:
    res = subprocess.run([npm, "install", "--omit=dev", "--no-audit", "--no-fund"], cwd=mapper_dir,
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, **kw)
  except (OSError, subprocess.SubprocessError) as e:
    return "npm install of the input mapper failed: %s" % e
  return "" if res.returncode == 0 else "npm install of the input mapper failed (exit code %d)." % res.returncode


def node_status():
  """(path, major, text): node executable, its major version and an error text ('' when Node 22+ is available)."""
  node = shutil.which("node")
  if not node:
    return None, 0, "Node.js is not installed. Install Node.js 22 or newer from nodejs.org, then try again."
  try:
    kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10, **kw).stdout.strip()
    major = int(re.match(r"v?(\d+)", out).group(1))
  except Exception:
    return node, 0, "Unable to read the Node.js version (node --version failed)."
  if major < 22:
    return node, major, f"Node.js {out} is too old: the input mapper needs Node.js 22 or newer."
  return node, major, ""


def sync_input_ini(pkg_dir, rt, log=None):
  """Copy <data>/anyps5-input.ini over the ini next to the game executable (pkg_dir).
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
  if yes(rt.get("overlay", "no")):
    env["ANYPS5_OVERLAY"] = "1"  # shown from the first frame (F10 toggles it)
  if rt.get("overlay_pos", "").strip():
    env["ANYPS5_OVERLAY_POS"] = rt["overlay_pos"].strip()
  if rt.get("screenshots_dir", "").strip():
    env["ANYPS5_SCREENSHOTS"] = rt["screenshots_dir"].strip()
  for key, var, win_only in ADVANCED_FLAGS:
    if yes(rt.get(key, "no")) and not (win_only and target != "windows"):
      env[var] = "1"
  for key, (var, _lo, _hi, win_only) in ADVANCED_INTS.items():
    if rt.get(key, "").strip() and not (win_only and target != "windows"):
      env[var] = str(int(rt[key].strip()))
  if rt.get("crash_stack_bytes", "").strip() and target == "windows":
    env["APS5_CRASH_STACK_BYTES"] = rt["crash_stack_bytes"].strip()
  if yes(rt.get("trace_bench", "no")):
    env["APS5_TRACE_BENCH"] = "1"  # the runtime refuses any other value
  if yes(rt.get("trace_uds", "no")):
    env["APS5_TRACE_UDS"] = "1"
  if yes(rt.get("store_at_label", "no")):
    env["APS5_STORE_AT_LABEL"] = "1"
  if rt.get("videoout_telemetry_dir", "").strip():
    env["APS5_VIDEOOUT_TELEMETRY_DIR"] = rt["videoout_telemetry_dir"].strip()
  if rt.get("trace_calls_file", "").strip() and rt["trace_calls"].strip() and target == "windows":
    env["APS5_TRACE_CALLS_FILE"] = rt["trace_calls_file"].strip()
  if yes(rt.get("fullscreen", "no")):
    env["ANYPS5_FULLSCREEN"] = "1"  # any non-empty value other than 0; F11 still toggles in game
  if rt.get("display_index", "").strip():
    env["ANYPS5_DISPLAY_INDEX"] = str(int(rt["display_index"].strip()))
  if rt.get("entitlements_file", "").strip():
    env["ANYPS5_ENTITLEMENTS"] = os.path.abspath(rt["entitlements_file"].strip())
  if rt.get("shader_cache_dir", "").strip():
    env["ANYPS5_SHADER_CACHE_DIR"] = rt["shader_cache_dir"].strip()
  if yes(rt.get("no_shader_cache", "no")):
    env["ANYPS5_NO_SHADER_CACHE"] = "1"
  playgo_unlisted = str(rt.get("playgo_unlisted_chunks", "installed")).strip().lower()
  if playgo_unlisted in ("installed", "absent"):
    env["APS5_PLAYGO_UNLISTED_CHUNKS"] = playgo_unlisted
  if yes(rt.get("pipeline_stats", "no")):
    env["APS5_PIPELINE_STATS"] = "1"
  if yes(rt.get("gpu_checkpoints", "no")):
    env["APS5_GPU_CHECKPOINTS"] = "1"
  if yes(rt.get("diagnostics", "no")):
    # GAME_TESTING.md: [present] lines every 10 s, GPU timings, timestamped log
    env["APS5_PROFILE_DRAW"] = "1"
    env["APS5_PROFILE_GPU"] = "1"
    env["APS5_LOG_TIMESTAMPS"] = "1"
    if target == "windows":
      env["APS5_CRASH_INDIRECT_MEMORY"] = "1"  # crash report: bytes behind each register pointer
  rumble = str(rt.get("rumble", "100")).strip()
  if rumble.isdigit() and 0 <= int(rumble) < 100:
    env["ANYPS5_RUMBLE"] = rumble  # USAGE/ENVIRONMENT.md: 0-100 %, 100 = as the title asks
  spk = str(rt.get("pad_speaker_db", "0")).strip().lower()
  if spk in ("mute", "off", "-inf"):
    env["APS5_PAD_SPEAKER_FALLBACK_DB"] = "mute"
  elif spk not in ("", "0", "-0", "0.0"):  # 0 dB = default, variable left unset
    env["APS5_PAD_SPEAKER_FALLBACK_DB"] = spk
  if rt.get("log_file", "").strip():
    env["ANYPS5_LOG_FILE"] = rt["log_file"].strip()
  if rt.get("game_mode", "off") in ("on", "auto"):
    env["APS5_GAME_MODE"] = rt["game_mode"]  # Feral GameMode on Linux, activity on macOS
  if yes(rt.get("host_report", "yes")):
    env["ANYPS5_HOST_REPORT"] = "1"  # USAGE.md: host report on stderr, once, for bug reports
  if rt.get("pad_input_file", "").strip():
    env["APS5_PAD_INPUT_FILE"] = rt["pad_input_file"].strip()
  if rt["trace_calls"].strip() and target == "windows":
    env["APS5_TRACE_CALLS"] = rt["trace_calls"].strip()
  if yes(rt["disable_dozen"]) and target == "windows":
    env["VK_LOADER_DRIVERS_DISABLE"] = "*dzn*"
  if yes(rt["copy_input_ini"]) and os.path.isfile(os.path.join(pkg_dir, INPUT_INI_NAME)):
    env["ANYPS5_INPUT_CONFIG"] = os.path.join(pkg_dir, INPUT_INI_NAME)
  for name, value in (catalog_env_values(rt) or {}).items():  # Runtime > All environment variables
    kind = ENV_CATALOG_KINDS.get(name)
    if kind == "presence":
      env[name] = "1"
    elif kind and value.strip():
      env[name] = value.strip()
  for item in re.split(r"[;\n]", rt.get("extra_env", "")):
    if "=" in item:
      k, v = item.split("=", 1)
      if k.strip():
        env[k.strip()] = v.strip()
  return env


def _safe_filename(name):
  return re.sub(r'[<>:"/\\|?*]', "_", name).strip(" .") or "game"


def title_name(pkg_dir):
  """Real game title (e.g. "Dreaming Sarah") from <pkg>/app0/sce_sys/param.json; "" if unreadable."""
  try:
    with open(os.path.join(pkg_dir, "app0", "sce_sys", "param.json"), encoding="utf-8-sig") as f:
      j = json.load(f)
    loc = j.get("localizedParameters") or {}
    langs = [loc.get("defaultLanguage"), "en-US", "en-GB"] + [k for k in loc if isinstance(loc.get(k), dict)]
    for lang in langs:
      t = (loc.get(lang) or {}).get("titleName") if lang else None
      if isinstance(t, str) and t.strip():
        return t.strip()
    t = j.get("titleName")
    return t.strip() if isinstance(t, str) else ""
  except Exception:
    return ""


_DISCORD_SUBSTATES = (
  (r"^\[1b\]", "Building relinker"), (r"^\[1c\]", "Building libs"),
  (r"^\[2\]", "Preparing output folder"), (r"^\[3\] Moving", "Moving game files"),
  (r"^\[3\] Copying", "Copying game files"), (r"^\[3b\]", "Moving .prx files"),
  (r"^\[4\] Automatic unwrap \(", "Unwrapping game files"), (r"^\[5\]", "Copying libs"),
  (r"^\[6\]", "Relinking"), (r"^\[7\]", "Creating shortcut"), (r"^\[8\]", "Checking imports"),
  (r"^Summary", "Finishing"),
)


def discord_substate(step_msg):
  """Short Discord status for a conversion step line (\"[5] Copying 'libs'...\" -> \"Copying libs\"), or \"\"."""
  for pat, text in _DISCORD_SUBSTATES:
    if re.search(pat, step_msg):
      return text
  return ""


def windows_shortcut_spec(exe_path, env, sync=None):
  """(target, arguments) of the .lnk. A shortcut cannot carry environment variables, so when
  there are some it starts cmd.exe, which sets them and runs the exe (window stays open only if
  the game exits with an error, so the message can be read). Without variables: the exe itself."""
  if not env and not sync:
    return exe_path, ""
  parts = []
  if sync:  # refresh the input ini from the data folder every time the shortcut is used
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
  game_name = title_name(pkg_dir) or game_name  # real title (e.g. "Dreaming Sarah") instead of the title ID
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
#  Game given as an archive (.zip .rar), extracted into <data>/extracted/<name>
#    .zip / .tar*      Python standard library, nothing to install
#    .rar .7z .iso...  7-Zip (7z / 7zz in the app data folder or in PATH); fallback: the optional
#                      python modules `rarfile` (+ UnRAR/WinRAR) and `py7zr`
# ─────────────────────────────────────────────────────────────
EXTRACT_DIR_NAME = "extracted"

ARCHIVE_TAR_EXTS = (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".tbz", ".txz", ".tar")
ARCHIVE_7Z_EXTS = (".rar", ".7z", ".iso", ".cab", ".wim", ".arj", ".lzh", ".001")  # handled by 7-Zip
ARCHIVE_EXTS = (".zip",) + ARCHIVE_TAR_EXTS + ARCHIVE_7Z_EXTS  # every format the readers understand (libs archive)
GAME_ARCHIVE_EXTS = (".zip", ".rar")  # formats accepted for the GAME dump
ARCHIVE_DIALOG_FILTER = "Game archives (%s)" % ";".join("*" + e for e in GAME_ARCHIVE_EXTS)
ARCHIVE_HINT = ("To open .rar / .7z / .iso ... files install 7-Zip (https://www.7-zip.org: 7z or 7zz in PATH "
                "or in the app data folder), or `pip install rarfile py7zr` (rarfile also needs UnRAR/WinRAR).")


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
  """Paths of the 7-Zip command line tools found (data folder first, then PATH, then the usual
  Windows install folders)."""
  found = []
  search = DATA_DIR + os.pathsep + os.environ.get("PATH", "")
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
        for d in (os.path.join(os.environ.get(var) or "", "WinRAR"), DATA_DIR):
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
  """Extracts the archive into <data>/extracted/<name> and returns the game folder
  (the one that contains eboot.bin), or None on failure. If a backend opens the archive but fails
  while extracting (e.g. 7-Zip without the RAR codec on a solid .rar), the next one is tried."""
  dest = os.path.join(DATA_DIR, EXTRACT_DIR_NAME, _safe_filename(_archive_stem(arc_path)))
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
    free = shutil.disk_usage(existing_parent(DATA_DIR)).free
    if free < total + total // 20:
      log("err", f"Not enough disk space to extract the archive: {format_size(total)} needed, "
                 f"{format_size(free)} free on the drive of {DATA_DIR}.")
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
  base = os.path.join(DATA_DIR, EXTRACT_DIR_NAME)
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


def is_inside(path, parent):
  """True if `path` is `parent` or lives inside it (real paths, case-insensitive on Windows)."""
  try:
    a = os.path.normcase(os.path.realpath(path))
    b = os.path.normcase(os.path.realpath(parent))
    return os.path.commonpath([a, b]) == b
  except ValueError:  # different drives
    return False


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


def missing_runtime_dlls(pkg_dir, target):
  """MinGW runtime DLLs absent from <pkg>/libs on Windows (error 126 at start). [] if fine or not applicable."""
  libs = os.path.join(pkg_dir, LIBS_DIR_NAME)
  if target != "windows" or not os.path.isdir(libs):
    return []
  names = {n.lower() for n in os.listdir(libs)}
  if not any(n.endswith(".prx") for n in names):
    return []
  return [d for d in MINGW_RUNTIME_DLLS if d.lower() not in names]


def preflight_checks(pkg_dir, target, exe):
  """Checks before starting. Returns (errors, warnings): plain texts instead of cryptic start-up errors."""
  errors, warnings = [], []
  app0 = os.path.join(pkg_dir, "app0")
  if not os.path.isfile(exe):
    errors.append(f"{os.path.basename(exe)} was not found in the package: convert the game again.")
  pj = os.path.join(app0, "sce_sys", "param.json")
  if not os.path.isfile(pj):
    warnings.append("app0/sce_sys/param.json is missing: the title name and id cannot be read.")
  else:
    try:
      with open(pj, encoding="utf-8-sig") as f:
        j = json.load(f)
      loc = j.get("localizedParameters") or {}
      if not j.get("titleId") and not j.get("contentId"):
        warnings.append("param.json has no titleId.")
      if not any(isinstance(v, dict) and v.get("titleName") for v in loc.values()):
        warnings.append("param.json has no titleName.")
    except (OSError, ValueError):
      warnings.append("app0/sce_sys/param.json cannot be read (damaged file?).")
  if not os.path.isfile(os.path.join(app0, "eboot.bin")) and not os.path.isdir(os.path.join(pkg_dir, "app0")):
    errors.append("the app0 folder is missing from the package.")
  elif not os.path.isfile(os.path.join(app0, "eboot.bin")):
    warnings.append("app0/eboot.bin is missing: the title may not start.")
  libs = os.path.join(pkg_dir, LIBS_DIR_NAME)
  if target == "windows" and os.path.isdir(libs):
    prx = [n for n in os.listdir(libs) if n.lower().endswith(".prx")]
    if prx:
      errors.append(f"{LIBS_DIR_NAME}/ contains original .prx files ({', '.join(prx[:3])}"
                    f"{'...' if len(prx) > 3 else ''}): on Windows they cause error 193. Remove them or convert again.")
  return errors, warnings


def fix_exec_permissions(pkg_dir, target, exe, log=None):
  """Linux/macOS: give back the execute bit lost after a zip or a download. Returns the fixed files."""
  fixed = []
  if os.name == "nt" or target == "windows":
    return fixed
  cands = [exe]
  for d in (pkg_dir, os.path.join(pkg_dir, "app0")):
    try:
      cands += [os.path.join(d, n) for n in os.listdir(d) if n.endswith((".sh", ".elf", ".so"))]
    except OSError:
      pass
  for f in cands:
    if os.path.isfile(f) and not os.access(f, os.X_OK) and (f == exe or not f.endswith(".so")):
      try:
        os.chmod(f, os.stat(f).st_mode | 0o111)
        fixed.append(f)
        if log:
          log("warn", f"{os.path.basename(f)} had no execute permission: fixed (chmod +x).")
      except OSError:
        pass
  return fixed


def launch_game(pkg_dir, target, cfg, exe=None, log=None):
  exe = exe or os.path.join(pkg_dir, TARGETS[target]["exe"])
  fix_exec_permissions(pkg_dir, target, exe, log)
  errors, warnings = preflight_checks(pkg_dir, target, exe)
  if log:
    for w in warnings:
      log("warn", "Check before start: " + w)
  if errors:
    raise OSError("Check before start: " + " ".join(errors))
  if not os.path.isfile(exe):
    raise FileNotFoundError(exe)
  missing = missing_runtime_dlls(pkg_dir, target)
  if missing:
    raise OSError(f"{LIBS_DIR_NAME}/ lacks {', '.join(missing)} (every library would fail with error 126): "
                  "copy them from the mingw64/bin of the toolchain that built the libs, or use the release libs.")
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
    where, reusable = DATA_DIR, 0
  else:
    eboot = os.path.join(folder, EXECUTABLE_NAME)
    fonts = os.path.join(DATA_DIR, FONTS_DIR_NAME)
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
      parts.append(("game files moved on the same drive: only the biggest file counts" if same
                    else "game files moved to another drive: full size", biggest if same else dir_size(folder)))
    else:
      parts.append(("game files copy: full size", dir_size(folder)))
    libs = libs_info(target)
    parts.append(("libs folder", libs["size"] if libs else 0))
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
  log("detail", f"Disk space required: about {format_size(req['needed'])} ({detail})")
  note = f" (includes {format_size(req['reusable'])} that will be freed by replacing the old output folder)" if req["reusable"] else ""
  log("ok" if ok else "err",
      f"Free space at {req['where']}: {format_size(avail)}{note}"
      + ("" if ok else f" - NOT ENOUGH, {format_size(req['needed'] - avail)} more needed"))
  return ok


# ─────────────────────────────────────────────────────────────
#  Input: anyps5-input.ini (INPUT_MAPPING.md)
# ─────────────────────────────────────────────────────────────
INPUT_ACTIONS = {a.lower() for a in (
  "Cross Circle Triangle Square L1 R1 L2 R2 L3 R3 Options Up Right Down Left "
  "LeftStickLeft LeftStickRight LeftStickUp LeftStickDown "
  "RightStickLeft RightStickRight RightStickUp RightStickDown "
  "TouchLeft TouchRight StickModifier Shake ToggleMouse ToggleFullscreen "
  "TogglePerformanceOverlay Screenshot").split()}
KEY_ONLY_ACTIONS = {"togglefullscreen", "toggleperformanceoverlay", "screenshot"}  # INPUT_MAPPING.md: keyboard keys only
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
  "stickmodifierscale": (0.05, 1, False, False),
  "keyboardrampms": (0, 2000, False, False),
  "triggerrampms": (0, 2000, False, False),
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
    elif low in KEY_ONLY_ACTIONS and kind != "KEY":
      errors.append((number, f"'{key}' accepts only keyboard keys (KEY:...)"))
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
      log("err", f"No '{RELINKER_PREFIX}*' found in the app data folder.")
      log("detail", f"Searched in: {DATA_DIR}")
      return False
    log("ok", f"Relinker: {os.path.basename(relinker)}")

  build_libs = wants_built_libs(rl)
  libs = None
  if build_libs:
    log("ok", "Libraries: will be built from the GitHub repository (target 'libs')")
  else:
    libs = libs_zip_info(target)
    if not libs:
      log("err", f"No '{LIBS_ZIP_PREFIX}*.zip' containing the '{LIBS_DIR_NAME}' folder found in the app data folder.")
      log("detail", f"Put the AnyPS5 libs zip downloaded from GitHub (its name starts with '{LIBS_ZIP_PREFIX}', no need to extract it) in: {DATA_DIR}")
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
    log("err", f"'{UNWRAP_SCRIPT_NAME}' not found in the app data folder: it is required for automatic unwrap.")
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
  output_base = s["output_dir"] or DEFAULT_OUTPUT_DIR
  if not s["output_dir"]:
    try:
      os.makedirs(output_base, exist_ok=True)  # default "Converted games" folder
    except OSError:
      pass
  if not os.path.isdir(output_base):
    log("err", f"Invalid output folder: {output_base}")
    return False
  pkg_dir = package_dir(output_base, game_name, target)
  app0_dir = os.path.join(pkg_dir, "app0")
  log("step", "[2] Creating the output folder (the game goes into its 'app0' subfolder)")
  log("detail", pkg_dir)
  if is_inside(pkg_dir, folder):
    log("err", "The output folder is INSIDE the game folder, so the tool would keep copying "
               "its own output forever (the size would grow past 100% and never finish).")
    log("info", "Fix: choose an output folder OUTSIDE the game folder, then start again.")
    log("detail", f"Game folder:   {folder}")
    log("detail", f"Output folder: {output_base}")
    return False
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
    log("step", "[3] Moving the game files into 'app0' (each original is deleted only after its new copy is checked)")
    copier = cancellable_move(mover, cancel, log, dir_size(folder))
  else:
    log("step", "[3] Copying the game files into 'app0' (your original game folder is not touched)")
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

  # 3b. .prx files next to eboot.bin -> module folder (on the copy only)
  if yes(rl.get("move_root_prx", "yes")) and not skip_modules and not smp:
    if mover is not None:
      log("info", "Move mode: the .prx files in the game root are left where they are "
                  "(they are moved only when the game is copied, so the original is never changed).")
    else:
      log("step", "[3b] Moving the .prx files next to eboot.bin into the module folder (on the copy)")
      move_root_prx(app0_dir, log)
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
  fonts_src = os.path.join(DATA_DIR, FONTS_DIR_NAME)
  if os.path.isdir(fonts_src):
    try:
      shutil.copytree(fonts_src, os.path.join(pkg_dir, FONTS_DIR_NAME))
      log("ok", f"'{FONTS_DIR_NAME}' copied.")
    except Exception as e:
      log("warn", f"Copying '{FONTS_DIR_NAME}' failed: {e}")
  else:
    log("detail", f"No '{FONTS_DIR_NAME}' folder: if the game uses system fonts, add it to the app data folder.")
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

  # Relinker version used for this conversion: logged and saved in the package (useful for support)
  try:
    _lv, _vt = relinker_version(cfg["relinker"])
    _vt = (_vt or "").strip()
    log("info" if _lv == "ok" else "warn", "Relinker version: " + (_vt.splitlines()[0] if _vt else "unknown"))
    with open(os.path.join(pkg_dir, "conversion_info.json"), "w", encoding="utf-8") as _f:
      json.dump({"relinker_version": _vt, "relinker_version_ok": _lv == "ok", "target": target,
                 **anyps5_build_info(cfg["relinker"]),
                 "game": game_name, "converted_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                _f, indent=2, ensure_ascii=False)
  except Exception as _e:
    log("warn", f"Could not save the relinker version: {_e}")

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
  script = os.path.join(DATA_DIR, AUDIT_SCRIPT)
  if not os.path.isfile(script):
    log("warn", f"{AUDIT_SCRIPT.replace(os.sep, '/')} not found in the app data folder: audit skipped.")
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
  if yes(rl.get("audit_strict_stubs", "no")):
    cmd.append("--strict-stubs")
  if yes(rl.get("audit_strict_providers", "no")):
    cmd.append("--strict-providers")
  src = rl.get("audit_source", "").strip()
  if src:
    if os.path.isdir(src):
      cmd += ["--source", src]
    else:
      log("warn", f"Audit source folder not found ({src}): audit without --source.")
  json_path = os.path.join(out_dir, stem + ".audit.json")
  _remove_quiet(json_path)
  cmd += ["--json", json_path]
  try:
    code = stream_process(cmd, log, cancel, holder, "Audit", cwd=DATA_DIR)
  except Cancelled:
    raise
  except Exception as e:
    log("warn", f"Audit could not be run: {e}")
    return
  meaning = {0: "nothing blocks loading", 1: "there are 'absent' imports or missing libraries",
             2: "an input is not readable"}.get(code, "")
  if code == 1 and (yes(rl.get("audit_strict_stubs", "no")) or yes(rl.get("audit_strict_providers", "no"))):
    meaning += " (or stub / other-library imports, because of the strict options)"
  log("ok" if code == 0 else "warn", f"Audit finished with code {code}: {meaning}")
  summary = summarize_audit(json_path)
  if summary:
    log("ok" if code == 0 else "warn", summary)


def _audit_find(node, key):
  """First value stored under `key` anywhere in the audit JSON (case-insensitive)."""
  if isinstance(node, dict):
    for k, v in node.items():
      if str(k).lower() == key:
        return v
    for v in node.values():
      r = _audit_find(v, key)
      if r is not None:
        return r
  elif isinstance(node, list):
    for v in node:
      r = _audit_find(v, key)
      if r is not None:
        return r
  return None


def summarize_audit(json_path):
  """One readable line from import_audit's --json: implemented / stub / absent / module. '' if unreadable."""
  try:
    with open(json_path, encoding="utf-8") as f:
      data = json.load(f)
  except Exception:
    return ""
  counts = {}
  for key in ("implemented", "stub", "absent", "module"):
    v = _audit_find(data, key)
    if isinstance(v, (list, dict)):
      v = len(v)
    if isinstance(v, int) and not isinstance(v, bool):
      counts[key] = v
  if not counts:
    return ""
  text = (f"Audit: {counts.get('implemented', 0)} implemented, {counts.get('stub', 0)} stub "
          f"(load, but throw when called), {counts.get('absent', 0)} absent "
          f"(the game will not load), {counts.get('module', 0)} from the title's own modules.")
  if counts.get("absent"):
    names = _audit_find(data, "absent")
    if isinstance(names, list):
      shown = [str(n.get("name") or n.get("nid") or n) if isinstance(n, dict) else str(n) for n in names[:8]]
      text += " Absent: " + ", ".join(shown) + (" …" if len(names) > 8 else "")
  return text


# ─────────────────────────────────────────────────────────────
#  Exit codes, log reading and the test report (GAME_TESTING.md, "Reporting a result")
# ─────────────────────────────────────────────────────────────
EXIT_CODES = {
  0: "the game closed normally",
  124: "the time cap ended the run (timeout)",
  126: "a library could not be loaded (a MinGW DLL or a .prx is missing)",
  127: "a required file was not found",
  134: "uncaught exception or abort() in the title",
  137: "the process was killed (SIGKILL, often out of memory)",
  139: "segmentation fault: the title touched memory it cannot access",
  216: "Windows refused to start the file (an ELF output was built without --windows)",
  193: "Windows tried to load a PS5 module as a DLL (a game .prx is in libs/)",
  0xC0000005: "access violation: the title or AnyPS5 touched invalid memory",
  0xC0000409: "uncaught exception or abort() (the log ends with FATAL: abort())",
  0xC000013A: "the process was closed with Ctrl+C or its console was closed",
  0xC0000142: "a DLL failed to initialize (error 126/193 territory: check libs/)",
  0xC0000135: "a required DLL was not found",
}


def normalize_exit_code(code):
  """Popen reports a signal as -N on POSIX (shells show 128+N) and a signed value on Windows."""
  if code is None:
    return None
  if code < 0:
    return (code & 0xFFFFFFFF) if os.name == "nt" else 128 - code
  return code


def explain_exit_code(code):
  """Plain-language text for an exit code of the title."""
  code = normalize_exit_code(code)
  if code is None:
    return "still running"
  text = EXIT_CODES.get(code)
  shown = f"0x{code:08x}" if code > 0xFFFF else str(code)
  if text is None:
    text = "the title stopped with an error: check the last log lines" if code else "ok"
  return f"{shown}: {text}"


def battery_status():
  """'AC', 'battery' or '' when unknown (GAME_TESTING.md: a run on battery can look like a regression)."""
  try:
    if os.name == "nt":
      import ctypes

      class SPS(ctypes.Structure):
        _fields_ = [("AC", ctypes.c_ubyte), ("Flag", ctypes.c_ubyte), ("Pct", ctypes.c_ubyte),
                    ("Sav", ctypes.c_ubyte), ("Life", ctypes.c_ulong), ("Full", ctypes.c_ulong)]
      s = SPS()
      if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
        return {0: "battery", 1: "AC"}.get(s.AC, "")
    elif sys.platform == "darwin":
      out = _run_text(["pmset", "-g", "batt"])
      return "AC" if "AC Power" in out else "battery" if "Battery Power" in out else ""
    else:
      base = "/sys/class/power_supply"
      online, has_bat = None, False
      for n in os.listdir(base):
        p = os.path.join(base, n)
        kind = open(os.path.join(p, "type")).read().strip()
        if kind == "Mains":
          online = online or open(os.path.join(p, "online")).read().strip() == "1"
        elif kind == "Battery":
          has_bat = True
      if online:
        return "AC"
      return "battery" if has_bat else "AC"  # no battery at all: a desktop on mains
  except Exception:
    pass
  return ""


# Known errors: (pattern in the log, what to do). Shown in the reading of the report.
KNOWN_ERRORS = [
  (r"nvgpucomp64\.dll", "crash in nvgpucomp64.dll: known NVIDIA driver problem (e.g. 610.47); install another driver version"),
  (r"/dev/udmabuf", "/dev/udmabuf is not usable: on Linux add your user to the kvm group (sudo usermod -aG kvm $USER) and log in again"),
  (r"Ran out of memory|std::bad_alloc|out of memory", "out of memory: close other programs and raise the Windows paging file (direct memory needs up to 13.8 GiB)"),
  (r"Conflicting guest output filename", "two files would be written with the same name: check the output folder and the unwrapped game for duplicate names"),
  (r"(?:error|code)\s*:?\s*126\b|LoadLibrary.*126", "error 126: DLLs missing in libs/ (copy them from mingw64/bin or use the release libs)"),
  (r"(?:error|code)\s*:?\s*193\b|LoadLibrary.*193", "error 193: a wrong-architecture or original .prx file is in libs/; remove it and convert again"),
  (r"execv failed: Permission denied", "execute permission lost: run chmod +x on the executable (the GUI does it before start)"),
]


def known_error_notes(text):
  return [msg for pat, msg in KNOWN_ERRORS if re.search(pat, text, re.IGNORECASE)]


def analyze_log(text):
  """Facts read from a run log: GPU, presents per 10 s, milestones and what stopped the title."""
  gpu = re.findall(r"Selected GPU name=(.+)", text)
  presents = [int(n) for n in re.findall(r"\[present\]\s+(\d+)\s+presents over 10 s", text)]
  stop = [l.strip() for l in text.splitlines()
          if re.search(r"FATAL:|terminate called|what\(\)|presentation failed|ASSERT|VK_ERROR_DEVICE_LOST|"
                       r"vkAllocateMemory.*-2|sceSystemServiceLoadExec: execv failed", l)]
  levels = re.findall(r"Level has started:\s*(.+)", text)
  notes = []
  if gpu and _SOFT_GPU.search(gpu[-1]):
    notes.append("a software renderer was selected: very slow, set ANYPS5_GPU to the real GPU")
  if presents and presents[-1] == 0:
    notes.append("no frames in the last 10 s: the title stalled (try one input, then check again)")
  if any("DEVICE_LOST" in l for l in stop):
    notes.append("device lost: on AMD the first run can fail while shaders compile, run again with warm caches")
  if any("vkAllocateMemory" in l for l in stop):
    notes.append("the GPU ran out of memory")
  if any("execv failed" in l for l in stop):
    notes.append("an executable lost its permission bits: run chmod +x on it")
  if re.search(r"^\[gpu\] host import budget\b", text, re.MULTILINE):
    notes.append("[gpu] host import budget detected: APS5_HOST_IMPORT_MIB affects pinned host-import memory budget; change it only for controlled comparisons, not as a general tuning setting")
  if re.search(r"^\[texture-cache\]", text, re.MULTILINE):
    notes.append("[texture-cache] diagnostics detected: APS5_TEXTURE_CACHE_MIB controls the texture-cache budget; change it only for controlled comparisons, not as a general tuning setting")
  notes += [n for n in known_error_notes(text) if n not in notes and not (
    "chmod" in n and any("chmod" in x for x in notes))]
  return {"gpu": gpu[-1].strip() if gpu else "", "presents": presents, "stop": stop[-3:],
          "level": levels[-1].strip() if levels else "", "notes": notes}


def title_info(pkg_dir):
  """(title ID, content version) from <pkg>/app0/sce_sys/param.json; empty strings if unreadable."""
  try:
    with open(os.path.join(pkg_dir, "app0", "sce_sys", "param.json"), encoding="utf-8-sig") as f:
      j = json.load(f)
    return str(j.get("titleId", "")), str(j.get("contentVersion", ""))
  except Exception:
    return "", ""


def runtime_vars_text(cfg, target, pkg_dir):
  env = runtime_env(cfg["runtime"], target, pkg_dir)
  return "\n".join(f"{k}={v}" for k, v in sorted(env.items())) or "(none)"


def build_report(cfg, target, game, pkg_dir, log_text, exit_code=None, tail=None):
  """Plain text with the points 1-8 of GAME_TESTING.md, filled in as far as the GUI knows them.
  Unknown fields stay empty. Measured data and the automatic reading are kept apart."""
  import platform
  import datetime
  c = cpu_info()
  g = gpu_info()
  tid, ver = title_info(pkg_dir) if pkg_dir else ("", "")
  guest_log = ""
  for name in ("anyps5.log", "run.log"):
    p = os.path.join(pkg_dir or "", name)
    if os.path.isfile(p):
      try:
        with open(p, encoding="utf-8", errors="replace") as f:
          guest_log = f.read()
        break
      except OSError:
        pass
  source = guest_log or log_text
  facts = analyze_log(source)
  gpu_line = facts["gpu"] or "; ".join(x["name"] for x in g["gpus"] if not x["software"]) or ""
  lines = source.splitlines()[-tail:] if tail else source.splitlines()
  mem = memory_status()
  rt = cfg["runtime"]
  bar = "=" * 60
  def head(t):
    return [bar, t.upper(), bar]
  out = [
    "AnyPS5 TEST REPORT",
    "",
    *head("Measured data"),
    f"1. Build: GUI {APP_VERSION}; relinker commit/PR: (fill in)",
    f"2. Title: {tid or '(fill in)'}  content version {ver or '(fill in)'}  ({game or 'unknown'})",
    f"3. Host: CPU {c['name'] or c['arch']} ({c['vendor']}); OS {platform.platform()}; "
    f"GPU {gpu_line or '(fill in)'}; driver (fill in); power {battery_status() or '(fill in)'}; "
    f"display refresh rate (fill in)",
    f"   Target: {target}" + (f"; {mem[0]}" if mem and os.name == 'nt' else ""),
    "4. Command and variables:",
    runtime_vars_text(cfg, target, pkg_dir or ""),
    f"   --to-intel: {yn(yes(cfg['settings']['intel']))}; caches: (empty / warm)",
    f"5. How far it got: {facts['level'] or '(fill in)'}",
    f"   Exit code: {explain_exit_code(exit_code) if exit_code is not None else '(fill in)'}",
    f"6. Presents per 10 s: {', '.join(map(str, facts['presents'][-6:])) or '(none in the log)'}",
    "7. Runs / duration: (fill in)",
    f"8. Date: {datetime.date.today().isoformat()}",
    "",
    *head(f"Full log ({len(lines)} lines)" + ("" if guest_log else " - GUI log, the title's own output is not captured")),
    *lines,
    "",
  ]
  if guest_log and log_text.strip():
    out += [*head(f"GUI log ({len(log_text.splitlines())} lines)"), *log_text.splitlines(), ""]
  out += head("Automatic reading (generated, not measured)")
  out += [f"- {n}" for n in facts["notes"]] or ["- nothing unusual found in the log"]
  out += [f"- stop line: {s}" for s in facts["stop"]]
  return "\n".join(out) + "\n"


def redact_personal(text):
  """Hide user name, computer name and home folder in text meant for a public issue."""
  names = {os.environ.get(k, "") for k in ("USERNAME", "USER", "COMPUTERNAME", "HOSTNAME")}
  home = os.path.expanduser("~")
  if home and home != "~":
    text = text.replace(home, "~").replace(home.replace("\\", "/"), "~")
  for n in names:
    if len(n) >= 3:
      text = re.sub(re.escape(n), "<redacted>", text, flags=re.I)
  return text


def build_bug_report(cfg, target, game, pkg_dir, log_text, exit_code=None, tail=60):
  """Short report to paste in a GitHub issue: header, host report block (if any), last log lines."""
  full = build_report(cfg, target, game, pkg_dir, log_text, exit_code, tail=tail)
  lines = (log_text or "").splitlines()
  start = next((i for i, l in enumerate(lines) if "host report" in l.lower()), None)
  host = []
  if start is not None:
    for l in lines[start:start + 40]:
      if host and not l.strip():
        break
      host.append(l)
  head = full.split("=" * 60)[0] + "".join(full.split("=" * 60)[1:3])
  parts = [head.rstrip(), "", "Host report:" if host else "Host report: (not in the log)"] + host
  parts += ["", f"Last {min(tail, len(lines))} log lines:", *lines[-tail:]]
  return redact_personal("\n".join(parts) + "\n")


def trophy_summary(pkg):
  """Counts the unlocked trophies stored in <pkg>/_trophy (formats from AnyPS5 USAGE.md and libSceNpTrophy):
  PS5 `<user>_<label>.txt` has one `<trophy id> <unlock time>` line per trophy, `.stats.txt` holds statistics
  (not counted), PS4 `trophyNN.<user>` has one trophy id per line. Damaged files are listed, not guessed."""
  res = {"exists": False, "unlocked": 0, "files": [], "stats_files": 0, "damaged": []}
  d = os.path.join(pkg, "_trophy") if pkg else ""
  if not d or not os.path.isdir(d):
    return res
  res["exists"] = True
  try:
    names = sorted(os.listdir(d))
  except OSError:
    return res
  for name in names:
    path = os.path.join(d, name)
    if not os.path.isfile(path) or name.endswith(".tmp"):
      continue
    if name.endswith(".stats.txt"):
      res["stats_files"] += 1
      continue
    ps5 = name.endswith(".txt")
    ps4 = re.fullmatch(r"trophy\d+\.-?\d+", name) is not None
    if not (ps5 or ps4):
      continue
    ids, bad = set(), False
    try:
      with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
          parts = line.split()
          if not parts and ps4:
            continue  # the PS4 reader skips whitespace; for PS5 a blank line is damage (USAGE.md)
          if ps5 and len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            ids.add(int(parts[0]))
          elif ps4 and len(parts) == 1 and parts[0].isdigit():
            ids.add(int(parts[0]))
          else:
            bad = True
    except OSError:
      bad = True
    if bad:
      res["damaged"].append(name)
    res["files"].append({"name": name, "unlocked": len(ids)})
    res["unlocked"] += len(ids)
  return res


def build_debug_zip(path, cfg, target, game, pkg_dir, log_text, exit_code=None):
  """Writes a zip for support: bug report, full report, settings and .registry.json files.
  User name, computer name and home folder are replaced in every text file. Returns the list of names added."""
  added = []
  with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
    def put(name, text):
      z.writestr(name, redact_personal(text))
      added.append(name)
    put("bug-report.txt", build_bug_report(cfg, target, game, pkg_dir, log_text or "", exit_code))
    put("report.txt", build_report(cfg, target, game, pkg_dir, log_text or "", exit_code, tail=0))
    try:
      with open(CONFIG_FILE, encoding="utf-8", errors="replace") as f:
        put("config.ini", f.read())
    except OSError:
      pass
    total = 0
    if pkg_dir and os.path.isdir(pkg_dir):
      for n in sorted(os.listdir(pkg_dir)):
        p = os.path.join(pkg_dir, n)
        if n.endswith(".registry.json") and os.path.isfile(p):
          size = os.path.getsize(p)
          if total + size > 20 * 1024 * 1024:
            put("registry-skipped.txt", f"{n} skipped: the registry files exceed 20 MB in total.\n")
            continue
          total += size
          with open(p, encoding="utf-8", errors="replace") as f:
            put("registry/" + n, f.read())
  return added


# ─────────────────────────────────────────────────────────────
#  Front end (pywebview): the interface is index.html, this is only the bridge
# ─────────────────────────────────────────────────────────────
import json
import webview

L = sys.modules[__name__]   # the logic lives in this same file
HERE = SCRIPT_DIR

# ---------- GUI update check ----------
APP_VERSION = "1.1.4"
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
    stage = os.path.join(L.DATA_DIR, "update")
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
    ud = os.path.realpath(L.DATA_DIR)
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
    shutil.rmtree(os.path.join(payload, L.LEGACY_DIR_NAME), ignore_errors=True)  # never touch user data
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
                 'key': 'frame_red_zone',
                 'label': '--frame-red-zone',
                 'plat': 'win',
                 'hint': 'Gives leaf functions that keep values below RSP (the red zone) a frame, so an '
                         'exception Windows delivers on a guest thread cannot overwrite them. Try it if a '
                         'game crashes randomly on Windows. It changes the code more than the other options.'},
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
                {'type': 'text',
                 'section': 'relinker',
                 'key': 'linux_fixed_base',
                 'label': '--linux-fixed-base',
                 'plat': 'linux',
                 'hint': 'Only if the executable needs an exact address, e.g. 0x400000 for titles that read '
                         'imports.txt (64 KiB-aligned, 0x10000 to 0x7fffffff0000). Default is 0x400000. '
                         'Conflicts with --lazy-binding.'},
                {'type': 'text',
                 'section': 'relinker',
                 'key': 'macos_fixed_base',
                 'label': '--macos-fixed-base',
                 'plat': 'macos',
                 'hint': 'Only if the executable needs an exact address, e.g. 0x400000 for titles that read '
                         'imports.txt (64 KiB-aligned, 0x20000 to 0x7fffffff0000). Default is 0x140010000.'},
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
                 'key': 'move_root_prx',
                 'label': 'Move the .prx next to eboot.bin into sce_module (copy only)',
                 'plat': 'all',
                 'hint': 'Modules left in the game root are ignored by the relinker and the unwrap. On the copy '
                         'of the game they are moved into the module folder (libc.prx excluded). Not done when '
                         'the game is moved instead of copied: the original is never changed.'},
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
                {'type': 'flag',
                 'section': 'relinker',
                 'key': 'audit_strict_stubs',
                 'label': 'Audit: --strict-stubs',
                 'plat': 'all',
                 'hint': 'Also fail the audit (exit code 1) when an import is only a throwing stub.'},
                {'type': 'flag',
                 'section': 'relinker',
                 'key': 'audit_strict_providers',
                 'label': 'Audit: --strict-providers',
                 'plat': 'all',
                 'hint': 'Also fail the audit when an import is exported only by a library other than the one '
                         'it names (it resolves on Linux but can fail on Windows).'},
                {'type': 'text',
                 'section': 'relinker',
                 'key': 'audit_source',
                 'label': 'Audit: source folder (--source)',
                 'plat': 'all',
                 'hint': 'core/libs/prx tree searched for throwing stubs. Empty = the audit script default.'},
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
                 'hint': 'Disables the syscall scan of the executable and of the bundled modules.'},
                {'type': 'text',
                 'section': 'relinker',
                 'key': 'patches_file',
                 'label': '--patches (patch file)',
                 'plat': 'all',
                 'hint': 'Byte patches applied during conversion. Empty = relinker-patches.toml next to '
                         'eboot.bin is used if present. Use Verify patches to check the file against the '
                         'selected game (unwrapped) without writing anything.'},
                {'type': 'text',
                 'section': 'relinker',
                 'key': 'extra_args',
                 'label': 'Extra arguments (passed as they are)',
                 'plat': 'all',
                 'hint': 'Options of newer relinker versions that this GUI does not know yet, e.g. '
                         '--new-option 1. Split like a shell command line (quotes allowed). An option already '
                         'set by the GUI is refused. If the relinker rejects an option, its message is shown in the log.'},
                {'type': 'flag',
                 'section': 'relinker',
                 'key': 'no_patches',
                 'label': '--no-patches',
                 'plat': 'all',
                 'hint': 'Apply no patches and ignore relinker-patches.toml. Conflicts with --patches.'},
                {'type': 'text',
                 'section': 'relinker',
                 'key': 'patch_catalog',
                 'label': 'Patch advisor: catalog folder (suggest_patches.py)',
                 'plat': 'all',
                 'hint': 'Folder with index.json and the patch files. The advisor checks title, version and file '
                         'hashes of the selected unwrapped game, then verifies with the relinker. It never '
                         'applies anything: the recommended file is only put in the --patches field.'},
                {'type': 'text',
                 'section': 'relinker',
                 'key': 'patch_timeout',
                 'label': 'Patch advisor: --timeout-seconds',
                 'plat': 'all',
                 'hint': 'Seconds per relinker verification. Empty = 120.'},
                {'type': 'text',
                 'section': 'relinker',
                 'key': 'nid_patcher_path',
                 'label': 'nid_patcher executable',
                 'plat': 'all',
                 'hint': 'Empty = the one built by Source build, else nid_patcher from PATH. It patches the '
                         'imports of native executables or libraries IN PLACE: work on a copy.'},
                {'type': 'flag',
                 'section': 'relinker',
                 'key': 'nid_only_sce',
                 'label': 'nid_patcher: --only-sce',
                 'plat': 'all',
                 'hint': 'Only rename names starting with sce (any case) or ending with _nid_postfix.'},
                {'type': 'text',
                 'section': 'relinker',
                 'key': 'nid_preserve_exports',
                 'label': 'nid_patcher: --preserve-exports (unpatched library)',
                 'plat': 'all',
                 'hint': 'Library whose exports stay unpatched. Conflicts with the export list below.'},
                {'type': 'text',
                 'section': 'relinker',
                 'key': 'nid_preserve_list',
                 'label': 'nid_patcher: --preserve-export-list (file)',
                 'plat': 'all',
                 'hint': 'File with the export names to leave unchanged. Conflicts with --preserve-exports.'}],
 'Source build': [{'type': 'text',
                   'section': 'relinker',
                   'key': 'release_tag',
                   'label': 'Release tag (Release source)',
                   'plat': None,
                   'hint': 'Empty = the latest release of the repository (e.g. v0.1.2 for a fixed version). '
                           'Files are saved in the app data folder, under release/<repo>/<tag>/, with the names used in the release.'},
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
                   'label': 'Build the libs (.prx) from the repository (instead of the prx-*.zip in the app data folder)',
                   'plat': None,
                   'hint': "ON = builds the 'libs' target (full CMake configuration: much slower, downloads "
                           'FFmpeg, needs the SDL/Vulkan build dependencies) and uses those libs instead of '
                           'the prx-*.zip in the app data folder.'},
                  {'type': 'flag',
                   'section': 'relinker',
                   'key': 'repo_update',
                   'label': 'Check for updates before every relink',
                   'plat': None,
                   'hint': 'ON = fetch the latest commit and rebuild only if it changed. OFF = reuse the '
                           'existing build.'},
                  {'type': 'flag',
                   'section': 'relinker',
                   'key': 'repo_update_ask',
                   'label': 'Ask before updating and rebuilding',
                   'plat': None,
                   'hint': 'ON = when a new version of the repository is available and a build already exists, '
                           'a popup asks whether to update and rebuild or keep the current build. '
                           'OFF = the choice is not asked: it follows "Check for updates before every relink".'},
                  {'type': 'text',
                   'section': 'relinker',
                   'key': 'repo_build_jobs',
                   'label': 'Build jobs (parallel compilers)',
                   'plat': None,
                   'hint': 'Empty = automatic (about half of your CPU threads, more if you have a lot of RAM). '
                           'A higher number (e.g. 12 or 16 on a 16-thread CPU) builds faster but uses more RAM: '
                           'if the PC slows down or freezes, lower it. An interrupted build resumes where it stopped.'}],
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
              'key': 'overlay',
              'label': 'Performance overlay from the first frame (ANYPS5_OVERLAY=1)',
              'plat': 'all',
              'hint': 'FPS, frame times and memory over the game image. F10 shows or hides it while playing. '
                      'The corner is chosen below (ANYPS5_OVERLAY_POS).'},
             {'type': 'flag',
              'section': 'runtime',
              'key': 'fullscreen',
              'label': 'Start fullscreen (ANYPS5_FULLSCREEN=1)',
              'plat': 'all',
              'hint': 'Opens the game window fullscreen at the desktop resolution. F11 switches between window '
                      'and fullscreen while playing. The game keeps its own render resolution.'},
             {'type': 'text',
              'section': 'runtime',
              'key': 'display_index',
              'label': 'Display index (ANYPS5_DISPLAY_INDEX)',
              'plat': 'all',
              'hint': 'Monitor where the game window opens (0 = first display). Empty = default. '
                      'A number that does not exist makes the window fail to open.'},
             {'type': 'text',
              'section': 'runtime',
              'key': 'entitlements_file',
              'label': 'Entitlements file (ANYPS5_ENTITLEMENTS)',
              'plat': 'all',
              'hint': 'Path of the ini that lists the add-ons (DLC) you own. '
                      'Empty = anyps5-entitlements.ini next to the executable.'},
             {'type': 'text',
              'section': 'runtime',
              'key': 'screenshots_dir',
              'label': 'Screenshots folder (ANYPS5_SCREENSHOTS)',
              'plat': 'all',
              'hint': 'Absolute folder for the F12 screenshots (PNG, game resolution, no overlay). '
                      'Empty = anyps5-screenshots/ folder next to the executable.'},
             {'type': 'text',
              'section': 'runtime',
              'key': 'shader_cache_dir',
              'label': 'Shader cache folder (ANYPS5_SHADER_CACHE_DIR)',
              'plat': 'all',
              'hint': 'Absolute folder for the on-disk shader cache. Empty = shader_cache/ next to the executable. Can grow by several GB.'},
             {'type': 'flag',
              'section': 'runtime',
              'key': 'no_shader_cache',
              'label': 'Disable shader cache (ANYPS5_NO_SHADER_CACHE=1)',
              'plat': 'all',
              'hint': 'Disables AnyPS5 on-disk shader cache; shaders may need to be compiled again on later runs.'},
             {'type': 'select',
              'section': 'runtime',
              'key': 'playgo_unlisted_chunks',
              'label': 'PlayGo chunks without metadata (APS5_PLAYGO_UNLISTED_CHUNKS)',
              'plat': 'all',
              'values': ['installed', 'absent'],
              'hint': 'Some titles may not start unless this is set. installed = treat unlisted chunks as installed; absent = treat them as absent.'},
             {'type': 'flag',
              'section': 'runtime',
              'key': 'store_at_flip',
              'label': 'APS5_STORE_AT_FLIP=1',
              'plat': 'all',
              'hint': 'Writes GPU results at every flip: useful if graphics get corrupted, costs '
                      'performance.'},
             {'type': 'flag',
              'section': 'runtime',
              'key': 'store_at_label',
              'label': 'APS5_STORE_AT_LABEL=1',
              'plat': 'all',
              'hint': 'Writes GPU results before every label the GPU writes: for games that wait for a GPU label '
                      'and then read stale data. Costs more performance than the flip option.'},
             {'type': 'text',
              'section': 'runtime',
              'key': 'trace_calls',
              'label': 'APS5_TRACE_CALLS',
              'plat': 'win',
              'hint': 'Needs --trace-imports. E.g. libkernel,libSceSaveData or all.'},
             {'type': 'text',
              'section': 'runtime',
              'key': 'trace_calls_file',
              'label': 'APS5_TRACE_CALLS_FILE',
              'plat': 'win',
              'hint': 'Absolute path of a file. Every 10 seconds it is rewritten with every import the game '
                      'called so far, most called first. Needs APS5_TRACE_CALLS (above) and --trace-imports.'},
             {'type': 'flag',
              'section': 'runtime',
              'key': 'disable_dozen',
              'label': 'Disable Dozen (Vulkan over D3D12)',
              'plat': 'win',
              'hint': 'Use only if the game crashes while Vulkan enumerates adapters and another Vulkan driver '
                      'is installed. Do not enable it if Dozen is your only Vulkan driver. '
                      'Sets VK_LOADER_DRIVERS_DISABLE=*dzn*.'},
             {'type': 'flag',
              'section': 'runtime',
              'key': 'copy_input_ini',
              'label': 'Copy … into the package (Input tab)',
              'plat': 'all',
              'hint': None},
             {'type': 'select',
              'section': 'runtime',
              'key': 'rumble',
              'label': 'Controller vibration, % (ANYPS5_RUMBLE)',
              'plat': 'all',
              'values': ['100', '75', '50', '25', '0'],
              'hint': '100 = as the title asks (variable not set). 0 turns off motor rumble, impulse triggers '
                      'and controller-speaker haptics. DualSense adaptive triggers are not scaled.'},
             {'type': 'select',
              'section': 'runtime',
              'key': 'pad_speaker_db',
              'label': 'Controller speaker gain, dB (APS5_PAD_SPEAKER_FALLBACK_DB)',
              'plat': 'all',
              'values': ['0', '-6', '-12', '-24', '-40', '-60', 'mute'],
              'hint': 'Only for sound a title sends to the controller speaker when no controller speaker is '
                      'present (it is mixed into the main audio). 0 = unchanged (variable not set), '
                      'mute = silence that copy. Main audio is not affected.'},
             {'type': 'text',
              'section': 'runtime',
              'key': 'log_file',
              'label': 'Log file (ANYPS5_LOG_FILE)',
              'plat': 'all',
              'hint': 'Absolute path of a file where AnyPS5 saves the log of the run. Empty = not set.'},
             {'type': 'select',
              'section': 'runtime',
              'key': 'game_mode',
              'label': 'Host game mode (APS5_GAME_MODE)',
              'plat': 'host',
              'values': ['off', 'on', 'auto'],
              'hint': 'Asks the system for its performance mode while the title runs: Feral GameMode on Linux '
                      '(libgamemode.so.0), a latency-critical activity on macOS. on logs a missing library; '
                      'auto stays silent. Not used on Windows.'},
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
              'key': 'trace_bench',
              'label': 'APS5_TRACE_BENCH=1 (benchmark report)',
              'plat': 'all',
              'hint': 'Needs APS5_TRACE_SUMMARY_EVERY (e.g. 30). Every interval it rewrites '
                      'aps5-bench-<commit>-<pid>.json.gz in the trace folder with the time between presents. '
                      'Play the scene for a few minutes, then quit. Compare later intervals: the first one includes '
                      'shader compilation.'},
             {'type': 'flag',
              'section': 'runtime',
              'key': 'trace_uds',
              'label': 'APS5_TRACE_UDS=1 (trophy events)',
              'plat': 'all',
              'hint': 'Prints every posted trophy/statistics event and every statistic it sets. Useful when a '
                      'trophy does not unlock.'},
             {'type': 'text',
              'section': 'runtime',
              'key': 'videoout_telemetry_dir',
              'label': 'APS5_VIDEOOUT_TELEMETRY_DIR',
              'plat': 'all',
              'hint': 'Absolute folder where the game writes videoout.jsonl: presents (fps) and controller changes, '
                      'one JSON line each, appended between runs.'},
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
             {'type': 'flag',
              'section': 'runtime',
              'key': 'diagnostics',
              'label': 'Test diagnostics (for bug reports)',
              'plat': 'all',
              'hint': 'Sets APS5_PROFILE_DRAW, APS5_PROFILE_GPU and APS5_LOG_TIMESTAMPS (on Windows also '
                      'APS5_CRASH_INDIRECT_MEMORY). Needed for the "presents per 10 s" lines of the report. '
                      'Slows the game a little: do not use it for performance comparisons.'},
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


try:
  from pypresence import Presence
except ImportError:
  Presence = None

DISCORD_CLIENT_ID = "1563249221094875246"  # discord.com/developers/applications


class DiscordRPC:
  def __init__(self):
    self.rpc = None
    self.start_ts = None
    self.lock = threading.Lock()  # updates come from several threads: one at a time
    self.enabled = lambda: True  # set by Api from the "no_discord_rpc" setting

  def _drop(self):
    """Remove the presence and close the connection (a fresh one is opened on the next update)."""
    r, self.rpc = self.rpc, None
    self.start_ts = None
    if r is not None:
      try:
        r.clear()
      except Exception:
        pass
      try:
        r.close()
      except Exception:
        pass

  def _connect(self):
    if Presence is None or DISCORD_CLIENT_ID.startswith("YOUR_"):
      return False
    if self.rpc is None:
      try:
        r = Presence(DISCORD_CLIENT_ID)
        r.connect()
        self.rpc = r
      except Exception:
        self.rpc = None  # Discord not running: ignore
        return False
    return True

  def update(self, details, state=None, timer=False):
    with self.lock:
      if not self.enabled():
        self._drop()
        return
      for _ in range(2):  # a second try reconnects from scratch if the old connection went stale
        try:
          if not self._connect():
            return
          if timer and not self.start_ts:
            self.start_ts = int(time.time())
          elif not timer:
            self.start_ts = None
          self.rpc.update(details=details, state=state, start=self.start_ts,
                          large_image="logo", large_text="AnyPS5 GUI")
          return
        except Exception:
          self._drop()

  def clear(self):
    with self.lock:
      self._drop()


def _advanced_runtime_items():
  """Runtime > Advanced options (debugging and tuning). The UI shows them in a closed section, below the
  normal Runtime options. Values and defaults: docs/user/USAGE.md and docs/dev/ENVIRONMENT.md."""
  def flag(key, label, hint, plat='all'):
    return {'type': 'flag', 'section': 'runtime', 'key': key, 'label': label, 'plat': plat, 'hint': hint,
            'advanced': True}

  def text(key, label, hint, plat='all'):
    return {'type': 'text', 'section': 'runtime', 'key': key, 'label': label, 'plat': plat, 'hint': hint,
            'advanced': True}

  return [
    flag('draw_pipeline', 'Draw pipeline (APS5_DRAW_PIPELINE=1)',
         'Off by default. Finishes each draw on a second thread and reuses work that did not change. The picture '
         'should not change: if it does, turn it off. Meant to be used with the two options below '
         '(APS5_PIPELINED_DRAWS=32, APS5_PIPELINE_DISPATCH=1). APS5_PROFILE_DRAW (Test diagnostics) '
         'finishes every draw on the reading thread, so do not measure this option with it.'),
    text('pipelined_draws', 'Draw queue depth (APS5_PIPELINED_DRAWS)',
         'Whole number. 0 = record draws inline, 1 = default depth (8), any other value = that depth, '
         'at most 256. The docs suggest 32 together with the draw pipeline. Empty = default.'),
    flag('pipeline_dispatch', 'Pipeline dispatches (APS5_PIPELINE_DISPATCH=1)',
         'Records eligible compute dispatches as items of the draw pipeline instead of running them inline.'),
    flag('verify_draw_pipeline', 'Verify the draw pipeline (APS5_VERIFY_DRAW_PIPELINE=1)',
         'Needs the draw pipeline. Runs the full checks next to the reused ones, costs speed, and prints '
         '[epoch-proofs] lines: any nonzero count of differences is a bug worth reporting.'),
    flag('no_pipeline_lto', 'No pipeline LTO (APS5_NO_PIPELINE_LTO=1)',
         'Turns off the background link-time optimization of graphics pipeline libraries. Try it if the game '
         'crashes while pipelines are created (some NVIDIA driver versions crash in their shader compiler).'),
    flag('no_pipeline_journal', 'No pipeline journal (APS5_NO_PIPELINE_JOURNAL=1)',
         'Does not record or replay pipelines.journal, so the game does not get the head start from the '
         'previous run. The journal is also off while the shader cache is disabled.'),
    text('pipeline_journal_entries', 'Journal entries replayed (APS5_PIPELINE_JOURNAL_ENTRIES)',
         'Whole number, 1 or more. How many pipelines of the journal are replayed at most (the last recorded ones). '
         'Empty = 4096.'),
    text('pipeline_journal_max_mib', 'Journal size limit, MiB (APS5_PIPELINE_JOURNAL_MAX_MIB)',
         'Whole number, 1 or more. When the file reaches it, it is rewritten down to three quarters of the limit, '
         'keeping the newest pipelines. Empty = 1024.'),
    text('pipeline_journal_threads', 'Journal replay threads (APS5_PIPELINE_JOURNAL_THREADS)',
         'Whole number, 0 or more. 0 records the journal without replaying it. Empty = half of the CPU threads, '
         'at least one.'),
    text('pipeline_store_entries', 'Resident graphics pipelines (APS5_PIPELINE_STORE_ENTRIES)',
         'Whole number, 1 or more. Fixes how many graphics pipelines stay in memory. Empty = adaptive: it starts '
         'at 1024 and grows up to 4096 when needed.'),
    text('address_copy_max_mib', 'Address copy limit, MiB (APS5_ADDRESS_COPY_MAX_MIB)',
         'Whole number, 1 or more. Maximum guest memory that one address-based build copies; going over it '
         'aborts the game. Empty = 64.'),
    text('pm4_ring', 'PM4 ring size (APS5_PM4_RING)',
         'Whole number from 0 to 4096: how many command buffer submissions are kept for dumps. 0 turns the ring '
         'off. Empty = 16.'),
    flag('skip_refused_draws', 'Skip refused draws (APS5_SKIP_REFUSED_DRAWS=1)',
         'Skips draws whose shader the recompiler refuses (each one is reported once) instead of stopping the '
         'game. The picture may miss those objects. Only for testing.'),
    flag('mimalloc', 'Use mimalloc (APS5_MIMALLOC=1)',
         'Windows only. Loads mimalloc.dll at start so the game allocates through mimalloc. The DLL must be '
         'available next to the game, or the game may not start.', plat='win'),
    text('gpu_wait_timeout_ms', 'GPU wait warning, ms (APS5_GPU_WAIT_TIMEOUT_MS)',
         'Whole number, 1 or more. A GPU wait longer than this prints a warning. Empty = 1000.'),
    text('stall_watchdog_secs', 'Stall watchdog, seconds (APS5_STALL_WATCHDOG_SECS)',
         'Whole number from 0 to 86400. If a GPU queue makes no progress for this long, the submission rings '
         'are dumped. Empty or 0 = off.'),
    text('hang_dump_secs', 'Hang dump, seconds (APS5_HANG_DUMP_SECS)',
         'Windows only. After this many seconds a watchdog dumps all threads and aborts the game: use it only '
         'to catch a hang, and set it longer than a normal play session.', plat='win'),
    text('crash_stack_bytes', 'Crash report stack bytes (APS5_CRASH_STACK_BYTES)',
         'Windows only. Bytes of the crashing thread stack printed in the crash report: decimal or 0x '
         'hexadecimal, at most 1048576. Empty or 0 = none.', plat='win'),
  ]


SCHEMA['Runtime'] += _advanced_runtime_items()


SCHEMA['Runtime'] = SCHEMA.get('Runtime', []) + [{'type': 'flag',
                         'section': 'runtime',
                         'key': 'host_report',
                         'label': 'ANYPS5_HOST_REPORT=1',
                         'plat': 'all',
                         'hint': 'Prints OS, CPU, GPU, driver and Vulkan support once at start. '
                                 'Used by the "Copy for bug" button.'}]

SCHEMA['Appearance'] = [{'type': 'flag',
                         'section': 'runtime',
                         'key': 'no_discord_rpc',
                         'label': 'Disable Discord Rich Presence',
                         'plat': 'all',
                         'hint': 'Stops showing that you are using AnyPS5 GUI on your Discord profile.'}]


class Api:
  def __init__(self):
    self.cfg = L.load_config()
    self.rpc = DiscordRPC()
    self.rpc.enabled = lambda: not L.yes(self.cfg["runtime"].get("no_discord_rpc", "no"))
    threading.Thread(target=self.rpc.update, args=("In the launcher", "Idle"), daemon=True).start()
    self.schema = SCHEMA
    self.folder = ""
    self.play_folder = ""  # converted PC game package selected for Play
    self.q = queue.Queue()
    self.running = False
    self.result = None
    self.cancel_ev = threading.Event()
    self.holder = {}
    self.game_proc = None
    self.game_exit = None      # exit code of the last game that ended
    self.game_pkg = ""         # folder of the last started game
    self.quitting = False      # True once the user confirmed closing during an operation
    self.mapper_proc = None    # input mapper web server (node), started from the Input tab
    self._quit_asked = False   # a confirmation popup is already on screen
    self._upd = {"state": "idle"}  # progress of the GUI self-update
    self._mov = {"state": "idle"}  # progress of moving the data folder
    self.relocated = False         # True once the data folder was moved: the app must be reopened
    self._ru_ev = None             # waiting for the answer of the "new repository version" popup
    self._ru_answer = None
    L.UPDATE_PROMPT = self._ask_repo_update

  # ---------- closing the window ----------
  def on_closing(self):
    """Window close event. Idle (or already confirmed) -> close. Operation running -> the close is
    vetoed and the in-app popup asks for confirmation."""
    if self.quitting or not self.running:
      self.rpc.clear()
      self._stop_mapper()
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

  # ---------- "new version of the repository" popup ----------
  def _ask_repo_update(self, info, cancel):
    """Called by the build thread: shows the popup in the page and waits for the answer.
    Returns {"update": bool} or None if the popup cannot be shown (then the update goes ahead)."""
    ev = threading.Event()
    self._ru_ev, self._ru_answer = ev, None
    try:
      shown = webview.windows[0].evaluate_js(
          "(window.askRepoUpdate ? (window.askRepoUpdate(%s), true) : false)" % json.dumps(info))
    except Exception:
      shown = False
    if shown is not True:
      return None
    while not ev.wait(0.2):
      if cancel.is_set():
        return None
    return self._ru_answer

  def repo_update_answer(self, update, remember):
    """Answer of the popup. With 'remember' (do not show again) the choice becomes the setting:
    update -> 'Check for updates' ON, keep -> OFF; the question is not asked again."""
    update, remember = bool(update), bool(remember)
    if remember and not self.relocated:
      self.cfg["relinker"]["repo_update"] = "yes" if update else "no"
      self.cfg["relinker"]["repo_update_ask"] = "no"
      L.save_config(self.cfg)
    self._ru_answer = {"update": update, "remember": remember}
    if self._ru_ev is not None:
      self._ru_ev.set()
    return True

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
      self._stop_mapper()
      try:
        webview.windows[0].destroy()
      except Exception:
        os._exit(0)
    threading.Thread(target=finish, daemon=True).start()
    return True

  # ---------- input mapper web (tools/input-mapper) ----------
  def _mapper_running(self):
    return self.mapper_proc is not None and self.mapper_proc.poll() is None

  def _stop_mapper(self):
    p, self.mapper_proc = self.mapper_proc, None
    if p is not None and p.poll() is None:
      try:
        L.kill_tree(p)
      except Exception:
        try:
          p.terminate()
        except Exception:
          pass

  def input_mapper_status(self):
    return {"running": self._mapper_running(), "url": f"http://127.0.0.1:{L.INPUT_MAPPER_PORT}"}

  def start_input_mapper(self):
    """Starts `npm --prefix tools/input-mapper start` in the background and opens it in the browser."""
    import webbrowser
    url = f"http://127.0.0.1:{L.INPUT_MAPPER_PORT}"
    if self._mapper_running():
      webbrowser.open(url)
      return {"ok": True, "running": True, "url": url, "text": "The input mapper is already running."}
    node, _major, err = L.node_status()
    if err:
      return {"ok": False, "running": False, "text": err}
    npm = shutil.which("npm")
    if not npm:
      return {"ok": False, "running": False, "text": "npm was not found (it comes with Node.js). Reinstall Node.js 22 or newer."}
    found = L.find_input_mapper(self.cfg["relinker"])
    if not found and self.cfg["relinker"].get("relinker_source") == "release":
      # Release option: tools/input-mapper is not in the release files, download it like unwrap_self.py
      err = L.download_input_mapper(self.cfg["relinker"], lambda *a, **k: None, threading.Event())
      if err:
        return {"ok": False, "running": False, "text": err}
      found = L.find_input_mapper(self.cfg["relinker"])
    if not found:
      return {"ok": False, "running": False,
              "text": "tools/input-mapper was not found. Clone the repository (Source build), choose Release "
                      "to download it, or copy the tools/input-mapper folder into the app data folder under tools/."}
    base, mapper_dir = found
    err = L.install_input_mapper_deps(mapper_dir, npm)
    if err:
      return {"ok": False, "running": False, "text": err}
    kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    env = dict(os.environ)
    env["PORT"] = str(L.INPUT_MAPPER_PORT)
    try:
      self.mapper_proc = subprocess.Popen([npm, "--prefix", L.INPUT_MAPPER_REL, "start"], cwd=base, env=env,
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, **kw)
    except OSError as e:
      return {"ok": False, "running": False, "text": f"Unable to start the input mapper: {e}"}
    for _ in range(40):  # wait up to ~8 s for the server to answer
      if self.mapper_proc.poll() is not None:
        self.mapper_proc = None
        return {"ok": False, "running": False,
                "text": f"The input mapper stopped right away (is port {L.INPUT_MAPPER_PORT} already in use?)."}
      try:
        with socket.create_connection(("127.0.0.1", L.INPUT_MAPPER_PORT), timeout=0.4):
          break
      except OSError:
        time.sleep(0.2)
    webbrowser.open(url)
    return {"ok": True, "running": True, "url": url,
            "text": "Input mapper opened in your browser. Export the .ini there, then use Import settings here."}

  def stop_input_mapper(self):
    self._stop_mapper()
    return {"ok": True, "running": False, "text": "Input mapper stopped."}

  # ---------- state ----------
  def get_state(self):
    inp = ""
    try:
      inp = open(L.INPUT_INI_FILE, encoding="utf-8-sig").read() if os.path.isfile(L.INPUT_INI_FILE) else L.INPUT_TEMPLATE
    except OSError:
      inp = L.INPUT_TEMPLATE
    return clean({"cfg": self.cfg, "schema": self.schema, "host": L.host_target(), "folder": self.folder,
                  "targets": {k: v["label"] for k, v in L.TARGETS.items()}, "languages": L.LANGUAGES,
                  "present_modes": L.PRESENT_MODES, "overlay_positions": L.OVERLAY_POSITIONS, "default_repo": L.DEFAULT_REPO_URL,
                  "defaults": L.DEFAULTS, "input": inp, "input_name": L.INPUT_INI_NAME,
                  "storage": {"path": L.DATA_DIR, "default": L.DEFAULT_DATA_DIR, "editable": L.STORAGE_EDITABLE,
                              "problem": L.STORAGE_PROBLEM},
                  "version": APP_VERSION})

  def _rpc_set(self, details, state=None, timer=False):
    """Update Discord Rich Presence without blocking the GUI."""
    # only the most recent request is sent: a slow older thread can no longer overwrite a newer status
    self._rpc_seq = getattr(self, "_rpc_seq", 0) + 1
    seq = self._rpc_seq

    def run():
      if seq == self._rpc_seq:
        self.rpc.update(details, state, timer)
    threading.Thread(target=run, daemon=True).start()

  def _rpc_idle(self):
    if not self.quitting:
      self._rpc_set("In the launcher", "Idle")

  def _rpc_refresh(self):
    """Apply the Discord Rich Presence setting right away (no restart needed)."""
    if L.yes(self.cfg["runtime"].get("no_discord_rpc", "no")):
      threading.Thread(target=self.rpc.clear, daemon=True).start()
    else:
      self._rpc_idle()

  def set_cfg(self, values):
    if self.relocated:
      return False
    for s, items in values.items():
      self.cfg[s].update({k: str(v) for k, v in items.items()})
    L.save_config(self.cfg)
    self._rpc_refresh()
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

  # ---------- storage folder (app data) ----------
  def pick_storage(self):
    """Folder dialog; returns the data folder that would be used inside the picked folder."""
    res = webview.windows[0].create_file_dialog(getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
                                                or webview.FOLDER_DIALOG)
    return L.storage_choice_to_dir(res[0]) if res else None

  def default_storage(self):
    return L.DEFAULT_DATA_DIR

  def move_storage(self, path):
    """Moves the data folder to `path` in a thread (progress: storage_status)."""
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
      L.relocate_data_dir(path, status)
    except Exception as e:
      self._mov = {"state": "failed", "text": str(e)}
      return
    self.relocated = True
    self._mov = {"state": "done", "pct": 100, "text": "Done. Close and reopen the app to use the new folder.", "path": path}

  def shader_cache_info(self):
    """Return the effective AnyPS5 shader-cache path and its current size."""
    rt = dict(self.cfg.get("runtime", {}))
    pkg = self.game_pkg
    if not pkg and self.folder:
      try:
        pkg = L.package_dir(self.cfg["settings"]["output_dir"] or L.DEFAULT_OUTPUT_DIR,
                            self.game_name(), self.cfg["relinker"]["target"])
      except Exception:
        pkg = ""
    path = rt.get("shader_cache_dir", "").strip() or (os.path.join(pkg, "shader_cache") if pkg else "")
    total = 0
    if path and os.path.isdir(path):
      for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for name in files:
          fp = os.path.join(root, name)
          try:
            if not os.path.islink(fp):
              total += os.path.getsize(fp)
          except OSError:
            pass
    return {"path": path, "bytes": total, "size": ("%s B" % total if total < 1024 else "%.1f KiB" % (total / 1024) if total < 1024**2 else "%.1f MiB" % (total / 1024**2) if total < 1024**3 else "%.2f GiB" % (total / 1024**3))}

  def clear_shader_cache(self):
    """Remove only AnyPS5's shader_cache directory, never the driver's cache."""
    info = self.shader_cache_info()
    path = info["path"]
    if not path:
      return {"ok": False, "error": "Select a game package or set ANYPS5_SHADER_CACHE_DIR first."}
    if not os.path.isabs(path):
      return {"ok": False, "error": "ANYPS5_SHADER_CACHE_DIR must be an absolute path."}
    if os.path.isdir(path):
      try:
        shutil.rmtree(path)
      except OSError as e:
        return {"ok": False, "error": f"Unable to clear shader cache: {e}"}
    return {"ok": True, "path": path, "bytes": 0, "size": "0 B"}

  def _selected_game_package(self):
    """Resolve the last launched package or the package for the selected source folder."""
    if self.game_pkg and os.path.isdir(self.game_pkg):
      return os.path.abspath(self.game_pkg)
    if self.folder and not L.is_archive_source(self.folder):
      try:
        return os.path.abspath(L.package_dir(self.cfg["settings"]["output_dir"] or L.DEFAULT_OUTPUT_DIR,
                                             self.game_name(), self.cfg["relinker"]["target"]))
      except Exception:
        pass
    return ""

  def game_folders_info(self):
    pkg = self._selected_game_package()
    app0 = os.path.join(pkg, "app0") if pkg else ""
    return {"package": pkg,
            "sd": os.path.isdir(os.path.join(pkg, "_sd")) if pkg else False,
            "sd_mem": os.path.isdir(os.path.join(pkg, "_sd_mem")) if pkg else False,
            "trophy": os.path.isdir(os.path.join(pkg, "_trophy")) if pkg else False,
            "trophies": L.trophy_summary(pkg),
            "ampr_dump": os.path.isfile(os.path.join(app0, "ampr_assets.index")) if app0 else False}

  def env_catalog(self):
    return [{"name": n, "kind": k, "default": d, "desc": t} for n, k, d, t in L.ENV_CATALOG]

  def open_entitlements_file(self, rt_values=None):
    """Opens the entitlements ini (the field value, else anyps5-entitlements.ini beside the game executable)
    in the default editor, creating it with a short template when it does not exist yet."""
    rt = dict(self.cfg["runtime"])
    if isinstance(rt_values, dict):
      rt.update({k: str(v) for k, v in rt_values.items()})
    path = rt.get("entitlements_file", "").strip()
    if not path:
      pkg = self._selected_game_package()
      if not pkg:
        return {"ok": False, "error": "Select a converted game package first, or type a file path above."}
      path = os.path.join(pkg, "anyps5-entitlements.ini")
    path = os.path.abspath(path)
    if not os.path.isfile(path):
      try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
          fh.write("# anyps5-entitlements.ini - add-ons (DLC) the game sees as owned\n"
                   "# One entitlement label per line (at most 15 characters). # or ; starts a comment.\n")
      except OSError as e:
        return {"ok": False, "error": f"Unable to create {path}: {e}"}
    return {"ok": bool(L.open_path(path)), "path": path}

  def open_game_folder(self, name):
    if name not in ("_sd", "_sd_mem", "_trophy"):
      return {"ok": False, "error": "Unknown game folder."}
    pkg = self._selected_game_package()
    path = os.path.join(pkg, name) if pkg else ""
    if not path or not os.path.isdir(path):
      return {"ok": False, "error": f"Folder {name} was not found. Launch the game or select a converted package first."}
    return {"ok": bool(L.open_path(path)), "path": path}

  def backup_game_folder(self, name):
    if name not in ("_sd", "_sd_mem"):
      return {"ok": False, "error": "Only save-data folders can be backed up."}
    pkg = self._selected_game_package()
    src = os.path.join(pkg, name) if pkg else ""
    if not src or not os.path.isdir(src):
      return {"ok": False, "error": f"Folder {name} was not found."}
    res = webview.windows[0].create_file_dialog(getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or webview.FOLDER_DIALOG)
    if not res:
      return {"ok": False, "cancelled": True}
    dest = res[0] if isinstance(res, (list, tuple)) else res
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive = os.path.join(dest, f"{name}-backup-{stamp}")
    try:
      shutil.make_archive(archive, "zip", root_dir=pkg, base_dir=name)
      return {"ok": True, "path": archive + ".zip"}
    except OSError as e:
      return {"ok": False, "error": str(e)}

  def clear_game_trophies(self):
    pkg = self._selected_game_package()
    path = os.path.join(pkg, "_trophy") if pkg else ""
    if not path or not os.path.isdir(path):
      return {"ok": False, "error": "Folder _trophy was not found."}
    try:
      shutil.rmtree(path)
      os.makedirs(path, exist_ok=True)
      return {"ok": True, "path": path}
    except OSError as e:
      return {"ok": False, "error": str(e)}

  def game_trophies_info(self):
    """Number of unlocked trophies in the selected package (see trophy_summary)."""
    return L.trophy_summary(self._selected_game_package())

  def export_debug_package(self, log_text):
    """Support zip (reports, settings, .registry.json) with the personal names hidden; asks where to save it."""
    cfg = {sec: dict(v) for sec, v in self.cfg.items()}
    target = cfg["relinker"]["target"]
    pkg = self.game_pkg or self._selected_game_package()
    game = os.path.basename(pkg.rstrip("/\\")) if pkg else (self.game_name() if self.folder else "")
    kind = getattr(getattr(webview, "FileDialog", None), "SAVE", None) or webview.SAVE_DIALOG
    res = webview.windows[0].create_file_dialog(kind, save_filename="anyps5-debug-" + time.strftime("%Y%m%d-%H%M%S") + ".zip")
    if not res:
      return {"ok": False, "cancelled": True}
    path = res if isinstance(res, str) else res[0]
    try:
      added = L.build_debug_zip(path, cfg, target, game, pkg, log_text or "", self.game_exit)
    except Exception as e:
      return {"ok": False, "error": str(e)}
    return {"ok": True, "path": path, "files": added}

  def open_screenshots_folder(self):
    """Opens the folder of the F12 screenshots: ANYPS5_SCREENSHOTS, or anyps5-screenshots/ next to the
    executable of the last started game (or of the game package selected in the app)."""
    cfg = {s: dict(v) for s, v in self.cfg.items()}
    rt = cfg["runtime"]
    pkg = self.game_pkg
    if not pkg and self.folder and not rt.get("screenshots_dir", "").strip():
      try:
        pkg = L.package_dir(cfg["settings"]["output_dir"] or L.DEFAULT_OUTPUT_DIR, self.game_name(), cfg["relinker"]["target"])
      except Exception:
        pkg = ""
    path = L.screenshots_dir(pkg, rt)
    if not path:
      self.q.put(("warn", "No screenshots folder yet: launch a game first, or set ANYPS5_SCREENSHOTS in Runtime settings."))
      return False
    if not os.path.isabs(path):
      self.q.put(("err", "ANYPS5_SCREENSHOTS must be an absolute path."))
      return False
    if not os.path.isdir(path):
      if os.path.isdir(os.path.dirname(path)) or rt.get("screenshots_dir", "").strip():
        try:
          os.makedirs(path, exist_ok=True)  # the game creates it at the first F12 anyway
        except OSError as e:
          self.q.put(("err", f"Unable to create {path}: {e}"))
          return False
      else:
        self.q.put(("warn", "No screenshots yet: the folder is created at the first F12 in the game (" + path + ")."))
        return False
    if not L.open_path(path):
      self.q.put(("err", f"Unable to open {path}"))
      return False
    return True

  def _shots_folder(self):
    """Existing screenshots folder of the selected/last game, else \"\"."""
    rt = dict(self.cfg["runtime"])
    pkg = self.game_pkg or self._selected_game_package()
    path = L.screenshots_dir(pkg, rt)
    return path if path and os.path.isabs(path) and os.path.isdir(path) else ""

  def list_screenshots(self, limit=12):
    """Newest PNG/JPG screenshots: [{name, size}] (the gallery loads each thumbnail with screenshot_data)."""
    path = self._shots_folder()
    if not path:
      return {"ok": True, "items": [], "path": ""}
    items = []
    try:
      for n in os.listdir(path):
        if n.lower().endswith((".png", ".jpg", ".jpeg", ".bmp")):
          st = os.stat(os.path.join(path, n))
          items.append((st.st_mtime, n, st.st_size))
    except OSError as e:
      return {"ok": False, "error": str(e)}
    items.sort(reverse=True)
    return {"ok": True, "path": path, "total": len(items),
            "items": [{"name": n, "size": z} for _m, n, z in items[:max(1, min(int(limit), 40))]]}

  def screenshot_data(self, name):
    """One screenshot as a data: URI (files above 12 MB are refused)."""
    path = self._shots_folder()
    if not path or name != os.path.basename(str(name)):
      return {"ok": False, "error": "Invalid screenshot."}
    f = os.path.join(path, name)
    try:
      if not os.path.isfile(f) or os.path.getsize(f) > 12 * 1024 * 1024:
        return {"ok": False, "error": "Screenshot not available or too large."}
      mime = "image/png" if name.lower().endswith(".png") else "image/bmp" if name.lower().endswith(".bmp") else "image/jpeg"
      with open(f, "rb") as fh:
        return {"ok": True, "uri": "data:%s;base64,%s" % (mime, base64.b64encode(fh.read()).decode("ascii"))}
    except OSError as e:
      return {"ok": False, "error": str(e)}

  def open_screenshot(self, name):
    path = self._shots_folder()
    if not path or name != os.path.basename(str(name)):
      return False
    return bool(L.open_path(os.path.join(path, name)))

  def open_runtime_folder(self, key, rt_values=None):
    """Opens the folder of a Runtime setting (trace files or video-out telemetry)."""
    names = {"trace_dir": "APS5_TRACE_DIR", "videoout_telemetry_dir": "APS5_VIDEOOUT_TELEMETRY_DIR"}
    if key not in names:
      return False
    rt = dict(self.cfg["runtime"])
    if isinstance(rt_values, dict):
      rt.update({k: str(v) for k, v in rt_values.items()})
    path = rt.get(key, "").strip()
    if not path:
      self.q.put(("warn", f"Set {names[key]} first" + (" (empty = the working directory of the game)." if key == "trace_dir" else ".")))
      return False
    if not os.path.isabs(path):
      self.q.put(("err", f"{names[key]} must be an absolute path."))
      return False
    try:
      os.makedirs(path, exist_ok=True)
    except OSError as e:
      self.q.put(("err", f"Unable to create {path}: {e}"))
      return False
    if not L.open_path(path):
      self.q.put(("err", f"Unable to open {path}"))
      return False
    return True

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

  def open_feedback(self, kind=""):
    """Opens the GitHub issues of the GUI. Only the fixed repo URL is used; the only data added to it are
    the app version and the system, visible and editable in the browser before the user posts anything."""
    import platform
    import webbrowser
    from urllib.parse import urlencode
    base = "https://github.com/%s/%s/issues" % (GUI_REPO_OWNER, GUI_REPO_NAME)
    if kind == "bug":
      system = "%s; target %s" % (platform.platform(), self.cfg["relinker"].get("target", ""))
      url = base + "/new?" + urlencode({"template": "bug.yml", "version": APP_VERSION, "system": system})
    elif kind == "idea":
      url = base + "/new?" + urlencode({"template": "suggestion.yml"})
    elif kind == "all":
      url = base
    else:
      url = base + "/new/choose"
    webbrowser.open(url)
    return True

  def pick_folder(self):
    res = webview.windows[0].create_file_dialog(getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
                                                or webview.FOLDER_DIALOG)
    if res:
      self.folder = res[0]
    return self.folder

  def pick_any_folder(self):
    """Folder dialog that only returns the choice (pick_folder also sets the selected game folder)."""
    res = webview.windows[0].create_file_dialog(getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
                                                or webview.FOLDER_DIALOG)
    if not res:
      return None
    return res if isinstance(res, str) else res[0]

  def pick_patch_file(self):
    kind = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
    res = webview.windows[0].create_file_dialog(kind, file_types=("Patch files (*.toml)", "All files (*.*)"))
    if not res:
      return None
    return res if isinstance(res, str) else res[0]

  def verify_patches(self, rl_values=None):
    """Verify the patch file of the settings dialog against the selected game folder."""
    rl = dict(self.cfg["relinker"])
    if isinstance(rl_values, dict):
      rl.update({k: str(v) for k, v in rl_values.items()})
    level, text = L.verify_patches(rl, self.folder)
    return {"level": level, "text": text}

  def suggest_patches(self, rl_values=None, list_all=False):
    """Ask tools/suggest_patches.py which catalog patch fits the selected game (nothing is applied)."""
    rl = dict(self.cfg["relinker"])
    if isinstance(rl_values, dict):
      rl.update({k: str(v) for k, v in rl_values.items()})
    level, text, patch = L.suggest_patches(rl, self.folder, bool(list_all))
    return {"level": level, "text": text, "patch": patch}

  def run_nid_patcher(self, rl_values=None):
    """Pick one or more native executables/libraries and patch their imports in place with nid_patcher."""
    rl = dict(self.cfg["relinker"])
    if isinstance(rl_values, dict):
      rl.update({k: str(v) for k, v in rl_values.items()})
    kind = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
    res = webview.windows[0].create_file_dialog(kind, allow_multiple=True, file_types=("All files (*.*)",))
    if not res:
      return {"level": "muted", "text": "Cancelled."}
    files = [res] if isinstance(res, str) else list(res)
    level, text = L.run_nid_patcher(rl, files)
    return {"level": level, "text": text}

  def verify_output(self, rl_values=None):
    """Ask for a converted Linux executable and check it against its <stem>.patches.toml manifest."""
    rl = dict(self.cfg["relinker"])
    if isinstance(rl_values, dict):
      rl.update({k: str(v) for k, v in rl_values.items()})
    kind = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
    res = webview.windows[0].create_file_dialog(kind, file_types=("All files (*.*)",))
    if not res:
      return {"level": "muted", "text": "Cancelled."}
    path = res if isinstance(res, str) else res[0]
    level, text = L.verify_output(rl, path)
    return {"level": level, "text": text}

  def relinker_version(self, rl_values=None):
    rl = dict(self.cfg["relinker"])
    if isinstance(rl_values, dict):
      rl.update({k: str(v) for k, v in rl_values.items()})
    level, text = L.relinker_version(rl)
    return {"level": level, "text": text}

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

  def make_bug_report(self, log_text):
    """Short, redacted text to paste in an issue."""
    cfg = {sec: dict(v) for sec, v in self.cfg.items()}
    target = cfg["relinker"]["target"]
    pkg = self.game_pkg
    game = os.path.basename(pkg.rstrip("/\\")) if pkg else ""
    return L.build_bug_report(cfg, target, game, pkg, log_text or "", self.game_exit)

  def make_report(self, log_text):
    """Test report (GAME_TESTING.md points 1-8) as plain text; the UI saves it to a file."""
    cfg = {sec: dict(v) for sec, v in self.cfg.items()}
    target = cfg["relinker"]["target"]
    pkg = self.game_pkg
    game = ""
    if not pkg and self.folder:
      game = self.game_name()
      pkg = L.package_dir(cfg["settings"]["output_dir"] or L.DEFAULT_OUTPUT_DIR, game, target)
    elif pkg:
      game = os.path.basename(pkg.rstrip("/\\"))
    return L.build_report(cfg, target, game, pkg, log_text or "", self.game_exit)

  def save_log(self, text, filename="anyps5-log.txt"):
    kind = getattr(getattr(webview, "FileDialog", None), "SAVE", None) or webview.SAVE_DIALOG
    res = webview.windows[0].create_file_dialog(kind, save_filename=filename)
    if res:
      path = res if isinstance(res, str) else res[0]
      with open(path, "w", encoding="utf-8") as f:
        f.write(text)
      return True
    return False

  # ---------- status ----------
  def status(self):
    rl, target = self.cfg["relinker"], self.cfg["relinker"]["target"]
    L.set_active_relinker_cfg(rl)
    if L.repo_unwrapper_path(rl):
      unw = ("tools/" + L.UNWRAP_SCRIPT_NAME + " (GitHub repo)  ✔", "ok")
    elif L.unwrapper_status():
      unw = (os.path.basename(L.unwrapper_status()) + "  ✔", "ok")
    elif rl.get("relinker_source") == "github":
      unw = ("will be taken from the GitHub repo  ⚠", "warn")
    elif rl.get("relinker_source") == "release":
      unw = ("will be downloaded from the GitHub release  ⚠", "warn")
    else:
      unw = ("not found in the app data folder  ✖", "err")
    li = L.libs_info(target, rl)
    libs = ((L.libs_label(li) + "  ✔", "ok") if li else
            (("will be built from the GitHub repo  ⚠", "warn") if L.wants_built_libs(rl)
             else ("will be downloaded from the GitHub release  ⚠", "warn") if rl.get("relinker_source") == "release"
             else (f"no {L.LIBS_ZIP_PREFIX}*.zip in the app data folder  ✖", "err")))
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
    rows += [["unwrapper", "unwrap_self.py", *unw], ["relinker", "Relinker", *L.relinker_status(rl)],
             ["libs", "prx-*.zip", *libs]]
    rows.append(["cpu", "CPU", *L.cpu_status(target, L.yes(self.cfg["settings"]["intel"]), L.yes(rl["rosetta"]))])
    rows.append(["gpu", "GPU", *L.gpu_status(self.cfg.get("runtime", {}).get("gpu", ""))])
    mem = L.memory_status()
    if mem and target == "windows":
      rows.append(["memory", "Memory", *mem])
    nv = L.nvidia_status()
    if nv:
      rows.append(["nvidia", "NVIDIA driver", *nv])
    ud = L.udmabuf_status()
    if ud and target not in ("windows", "macos"):
      rows.append(["udmabuf", "/dev/udmabuf", *ud])
    return clean(rows)

  def gpu_names(self):
    """Detected hardware GPUs (lowercase), offered as suggestions in the ANYPS5_GPU field."""
    return [g["name"].lower() for g in L.gpu_info()["gpus"] if not g["software"]]

  def space(self):
    if L.is_archive_source(self.folder):
      z = L.scan_archive(self.folder)
      if not z:
        return None
      if z.get("error"):
        return {"text": z["error"], "level": "err"}
      free = shutil.disk_usage(L.existing_parent(L.DATA_DIR)).free
      need = z["size"] * 2 + 64 * 1024 * 1024  # extracted copy + game copy (rough estimate)
      return {"text": f"~{L.format_size(need)} needed (archive extraction + package) · {L.format_size(free)} free"
                      + ("  ✔" if free >= need else "  ✖  not enough space"),
              "level": "ok" if free >= need else "err"}
    if not self.folder or not os.path.isdir(self.folder):
      return None
    s, rl = self.cfg["settings"], self.cfg["relinker"]
    try:
      r = L.space_requirements(self.folder, rl["target"], s["mode"], s["output_dir"] or L.DEFAULT_OUTPUT_DIR,
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
      res["error"] = "Select the game folder or an archive of the game."
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
      base = cfg["settings"]["output_dir"] or L.DEFAULT_OUTPUT_DIR
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
      if k == "step" and not self.cancel_ev.is_set() and action not in ("play", "download", "build", "restore"):
        sub = L.discord_substate(str(m))  # Discord: show which phase of the conversion is running
        if sub:
          self._rpc_set("Unwrapping game" if mode == "unwrap" else "Converting game", sub)
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
    self.running, self.result, self.cancelled = True, None, False
    self.cancel_ev.clear()
    self.holder = {}
    log0("step", title)
    if action == "download":
      self._rpc_set("Downloading tools", "Preparing release")
    elif action == "build":
      self._rpc_set("Building converter (relinker)", "Compiling from source")
    elif action == "restore":
      self._rpc_set("Restoring backup", "Restoring")
    elif mode == "unwrap":
      self._rpc_set("Unwrapping game", "Preparing")
    else:
      self._rpc_set("Converting game", "Preparing")

    def work():
      t0, ok = time.time(), False
      try:
        ok = bool(job(log0))
      except L.Cancelled:
        log0("warn", "Operation cancelled.")
      except Exception as e:
        log0("err", f"Unexpected error: {e}")
      dt = time.time() - t0
      self.cancelled = self.cancel_ev.is_set()
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
      self._rpc_idle()

    threading.Thread(target=work, daemon=True).start()
    return True

  def _pick_exe_path(self):
    kind = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
    d = getattr(self, "last_exe_dir", "")
    if not (d and os.path.isdir(d)):
      d = self.cfg["settings"]["output_dir"] or L.DEFAULT_OUTPUT_DIR
    res = webview.windows[0].create_file_dialog(
      kind, directory=d, file_types=("Game executable (*.exe;*.elf)", "All files (*.*)"))
    if not res:
      return None
    path = res if isinstance(res, str) else res[0]
    self.last_exe_dir = os.path.dirname(path)
    return path

  def pick_play_folder(self):
    """Choose the converted PC game package folder used by Play."""
    try:
      kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or webview.FOLDER_DIALOG
      # Start from the app's output folder (where converted games are created) to speed up the search
      start = self.cfg["settings"].get("output_dir") or L.DEFAULT_OUTPUT_DIR
      if not os.path.isdir(start):
        os.makedirs(start, exist_ok=True) if not self.cfg["settings"].get("output_dir") else None
      if not os.path.isdir(start):
        start = L.SCRIPT_DIR
      try:
        res = webview.windows[0].create_file_dialog(kind, directory=os.path.abspath(start))
      except TypeError:  # very old pywebview without the "directory" parameter
        res = webview.windows[0].create_file_dialog(kind)
      if not res:
        return self.play_folder
      # pywebview backends differ: some return a string, others a list of paths.
      path = res if isinstance(res, str) else res[0]
      if path and os.path.isdir(path):
        self.play_folder = os.path.abspath(path)
      elif path:
        return ""
    except Exception as e:
      log("err", f"Unable to choose converted PC game folder: {e}")
      return ""
    return self.play_folder

  def set_play_folder(self, path):
    self.play_folder = os.path.abspath((path or "").strip().strip('"')) if (path or "").strip() else ""
    return self.play_folder

  def _selected_converted_game(self):
    """Resolve the converted PC package, independently of the PS5 source path."""
    cfg = self.cfg
    target = cfg["relinker"]["target"]
    if self.play_folder:
      pkg = os.path.abspath(self.play_folder)
      game = os.path.basename(os.path.normpath(pkg))
    else:
      if not self.folder:
        raise FileNotFoundError("Set the PS5 source path for Convert, and choose the converted PC game folder for Play.")
      game = L.archive_game_name(self.folder) if L.is_archive_source(self.folder) else os.path.basename(os.path.normpath(os.path.abspath(self.folder)))
      base = cfg["settings"]["output_dir"] or L.DEFAULT_OUTPUT_DIR
      pkg = L.package_dir(base, game, target)
    exe = os.path.join(pkg, L.TARGETS[target]["exe"])
    if not os.path.isfile(exe):
      raise FileNotFoundError(f"Converted executable not found: {exe}. Select the converted PC game folder in Play, or convert/relink the PS5 source first.")
    return pkg, target, exe, game

  def play_exe(self):
    """Launch the executable in the converted package derived from Game path."""
    if self.game_proc is not None and self.game_proc.poll() is None:
      self.q.put(("warn", "The game is already running."))
      return False
    cfg = {s: dict(v) for s, v in self.cfg.items()}
    try:
      pkg, target, exe, game = self._selected_converted_game()
      self.game_proc = L.launch_game(pkg, target, cfg, exe=exe, log=lambda k, m: self.q.put((k, m)))
      self.q.put(("ok", f"Game started (PID {self.game_proc.pid}) from {pkg}"))
      self.game_pkg, self.game_exit = pkg, None
    except (OSError, ValueError) as e:
      self.q.put(("err", f"Unable to launch the game: {e}"))
      return False
    return True

  def shortcut_exe(self):
    """Create a shortcut for the converted executable derived from Game path."""
    cfg = {s: dict(v) for s, v in self.cfg.items()}
    try:
      pkg, target, exe, game = self._selected_converted_game()
      L.create_shortcut(pkg, os.path.basename(exe), target, cfg["runtime"],
                        game, lambda k, m: self.q.put((k, m)), L.SCRIPT_DIR)
    except (OSError, ValueError) as e:
      self.q.put(("err", f"Unable to create shortcut: {e}"))
      return False
    return True

  def _play(self, cfg, log):
    if not self.folder:
      log("err", "Select the game folder first.")
      return False
    base = cfg["settings"]["output_dir"] or L.DEFAULT_OUTPUT_DIR
    target = cfg["relinker"]["target"]
    pkg = L.package_dir(base, self.game_name(), target)
    if self.game_proc is not None and self.game_proc.poll() is None:
      log("warn", "The game is already running.")
      return False
    try:
      self.game_proc = L.launch_game(pkg, target, cfg, log=log)
      log("ok", f"Game started (PID {self.game_proc.pid}) from {pkg}")
      self.game_pkg, self.game_exit = pkg, None
    except FileNotFoundError:
      log("err", f"Run the Relink for this game first. Expected: {pkg}")
    except OSError as e:
      log("err", f"Unable to launch the game: {e}")
    return True

  def poll(self):
    lines, pct = [], "keep"
    p = self.game_proc
    if p is not None and self.game_exit is None and p.poll() is not None:
      self.game_exit = L.normalize_exit_code(p.returncode)
      self.q.put(("ok" if self.game_exit == 0 else "warn",
                  "Game ended, exit code " + L.explain_exit_code(self.game_exit)))
      self._rpc_idle()
    while not self.q.empty():
      k, m = self.q.get()
      if k == "percent":
        pct = m
      else:
        lines.append([k, str(m)])
    return {"lines": lines, "pct": pct, "running": self.running, "ok": self.result,
            "cancelled": bool(getattr(self, "cancelled", False))}

  def cancel(self):
    self.cancel_ev.set()
    p = self.holder.get("proc")
    try:
      if p is not None and p.poll() is None:
        L.kill_tree(p)
    except Exception:
      pass
    self.q.put(("warn", "Cancellation requested…"))
    self._rpc_idle()  # Discord back to "In the launcher / Idle" right away

  # ---------- settings ----------
  def check_input(self, text):
    return [f"Line {n}: {m}" for n, m in L.validate_input_ini(text)[:6]]

  def reset_input(self):
    return L.INPUT_TEMPLATE

  # ---------- import tools into the app data folder (Local files option) ----------
  def _tool_dest(self, name):
    """(relative destination inside the data folder, None) for a file the program knows, else (None, reason)."""
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
    if low == L.INPUT_INI_NAME:
      return L.INPUT_INI_NAME, None
    if low.endswith((".ttf", ".otf", ".ttc")):
      return os.path.join(L.FONTS_DIR_NAME, name), None
    if low.startswith("nid_patcher"):
      if os.name == "nt" and not low.endswith(".exe"):
        return None, "not a Windows nid_patcher (.exe)"
      if os.name != "nt" and (low.endswith(".exe") or low.endswith(L.NOT_BINARY_EXT)):
        return None, "not a nid_patcher for this system"
      return name, None
    return None, "not a file the program uses"

  def pick_tool_files(self):
    """Choose files to copy into the data folder. Returns the plan: [{src, name, dest, exists, skip}]."""
    kind = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
    res = webview.windows[0].create_file_dialog(
      kind, allow_multiple=True,
      file_types=("Tool files (*.py;*.zip;*.exe;*.ini;*.ttf;*.otf;*.ttc)", "All files (*.*)"))
    if not res:
      return []
    paths = [res] if isinstance(res, str) else list(res)
    plan = []
    for src in paths:
      name = os.path.basename(src)
      rel, why = self._tool_dest(name)
      item = {"src": src, "name": name, "skip": why or "", "dest": rel or "", "exists": False}
      if rel:
        dest = os.path.join(L.DATA_DIR, rel)
        item["exists"] = os.path.isfile(dest)
        try:
          if item["exists"] and os.path.samefile(src, dest):
            item["skip"] = "already in the app data folder"
        except OSError:
          pass
      plan.append(item)
    return plan

  def import_tool_files(self, plan, replace):
    """Copy the files of the plan into the data folder. Existing ones are overwritten only if `replace`."""
    done, kept, errors = [], [], []
    for it in plan or []:
      if it.get("skip") or not it.get("dest"):
        continue
      rel, why = self._tool_dest(os.path.basename(it["src"]))  # never trust the destination coming from the page
      if not rel:
        continue
      dest = os.path.join(L.DATA_DIR, rel)
      if os.path.isfile(dest) and not replace:
        kept.append(rel)
        continue
      try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".part"
        shutil.copyfile(it["src"], tmp)
        if os.name != "nt" and rel.lower().startswith((L.RELINKER_PREFIX, "nid_patcher")):
          os.chmod(tmp, os.stat(tmp).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(tmp, dest)
        done.append(rel)
      except OSError as e:
        errors.append(f"{rel}: {e}")
    L._unwrapper = None  # unwrap_self.py may have been replaced: reload it on next use
    L._libs_cache.clear()
    for item in [("ok", "Imported into the app data folder: " + ", ".join(done)) if done else None,
                 ("info", "Kept existing: " + ", ".join(kept)) if kept else None,
                 ("err", "Import failed: " + "; ".join(errors)) if errors else None]:
      if item:
        self.q.put(item)
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
        os.makedirs(L.DATA_DIR, exist_ok=True)
        if has:
          with open(L.INPUT_INI_FILE, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        elif os.path.isfile(L.INPUT_INI_FILE):
          os.remove(L.INPUT_INI_FILE)
      except OSError as e:
        return {"error": f"Unable to save {L.INPUT_INI_NAME}: {e}"}
    self.cfg = new
    L.save_config(new)
    self._rpc_refresh()
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