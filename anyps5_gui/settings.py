"""Paths, constants and default settings (data folder, defaults, target systems, languages)."""

import os
import sys
import threading
import time


if getattr(sys, "frozen", False):  # PyInstaller executable
  SCRIPT_DIR = os.path.dirname(sys.executable)
else:
  # this file is in the anyps5_gui folder: the program folder is its parent
  SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
HIDDEN_ERRORS_FILE = os.path.join(DATA_DIR, "hidden-errors.log")
_HIDDEN_ERRORS = []  # this session: (time, where, text), newest last, at most 200
_HIDDEN_LOCK = threading.Lock()


def hidden_error(where, exc=None):
  """Records an error the program deliberately does not show (best-effort steps, optional features).
  Kept in memory for the Debug package and appended to hidden-errors.log in the data folder. Never raises."""
  try:
    text = f"{type(exc).__name__}: {exc}" if exc is not None else "unknown error"
    with _HIDDEN_LOCK:
      if _HIDDEN_ERRORS and _HIDDEN_ERRORS[-1][1:] == (where, text):
        return  # the same error again and again: keep one line
      stamp = time.strftime("%Y-%m-%d %H:%M:%S")
      _HIDDEN_ERRORS.append((stamp, where, text))
      del _HIDDEN_ERRORS[:-200]
      try:
        os.makedirs(DATA_DIR, exist_ok=True)
        if os.path.isfile(HIDDEN_ERRORS_FILE) and os.path.getsize(HIDDEN_ERRORS_FILE) > 256 * 1024:
          with open(HIDDEN_ERRORS_FILE, "rb") as f:
            f.seek(-128 * 1024, os.SEEK_END)
            tail = f.read().split(b"\n", 1)[-1]
          with open(HIDDEN_ERRORS_FILE, "wb") as f:
            f.write(tail)
        with open(HIDDEN_ERRORS_FILE, "a", encoding="utf-8", errors="replace") as f:
          f.write(f"{stamp}  {where}: {text}\n")
      except OSError:
        pass
  except Exception:
    pass


def hidden_errors_text():
  with _HIDDEN_LOCK:
    return "".join(f"{t}  {w}: {x}\n" for t, w, x in _HIDDEN_ERRORS)


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
    "target": "windows", "rosetta": "no", "to_avx": "no", "gui": "no", "diagnostics": "no",
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
    "function_symbol_roots": "no", "omit_unavailable_dependencies": "no",
  },
  "runtime": {
    "language": "", "gpu": "", "present_mode": "", "fonts_dir": "",
    "store_at_flip": "no", "trace_calls": "", "disable_dozen": "no",
    "copy_input_ini": "yes", "input_watch": "no",
    "overlay_scale": "", "audio_in_device": "", "audio_in_mute_on_focus_loss": "no",
    "rumble_keep_unfocused": "no",
    "extra_env": "", "pad_input_file": "",
    "sdl_videodriver": "", "thread_priority": "", "thread_nice": "", "thread_rt": "",
    "trace_frames": "", "trace_seconds": "", "trace_summary_every": "", "trace_events": "",
    "trace_dir": "", "pipeline_stats": "no", "gpu_checkpoints": "no",
    "diagnostics": "no",
    "overlay": "no", "overlay_pos": "", "screenshots_dir": "", "shader_cache_dir": "", "no_shader_cache": "no",
    "fullscreen": "no", "display_index": "", "entitlements_file": "",
    "upscale": "", "fsr_sharpness": "", "fps_limit": "",
    "video_out_max_width": "", "video_out_max_height": "",
    "trace_bench": "no", "trace_calls_file": "", "trace_uds": "no", "videoout_telemetry_dir": "",
    "store_at_label": "no",
    "draw_pipeline": "no", "pipelined_draws": "", "pipeline_dispatch": "no", "verify_draw_pipeline": "no",
    "no_pipeline_lto": "no", "no_pipeline_journal": "no", "pipeline_journal_entries": "",
    "pipeline_journal_max_mib": "", "pipeline_journal_threads": "", "pipeline_store_entries": "",
    "address_copy_max_mib": "", "pm4_ring": "", "skip_refused_draws": "no", "mimalloc": "no",
    "gpu_wait_timeout_ms": "", "stall_watchdog_secs": "", "hang_dump_secs": "", "crash_stack_bytes": "",
    "no_discord_rpc": "no", "env_catalog": "", "host_report": "yes", "rumble": "100", "game_mode": "off", "pad_speaker_db": "0", "log_file": "",
    "sound_fx": "yes", "sound_volume": "60",
    "volume": "", "mute_on_focus_loss": "no",
    "pad_identify": "", "pad_port0_state": "", "pad_status": "no",
    "date_format": "", "time_format": "",
    "free_space": "", "open_urls": "", "psn_dialogs": "",
    "stall_prompt": "no", "autorun_no_pause": "no",
    "aps5_report": "", "net_disconnected": "no",
    "playgo_unlisted_chunks": "installed",
  },
}


class Cancelled(Exception):
  pass
# docs/dev/BUILD.md: on Windows ONLY this toolchain is supported (newer GCC such as MSYS2's 16.x selects
# SjLj exceptions and the project's DWARF check fails).
WINLIBS_TAG = "15.2.0posix-14.0.0-ucrt-r7"
WINLIBS_ZIP = "winlibs-x86_64-posix-seh-gcc-15.2.0-mingw-w64ucrt-14.0.0-r7.zip"
WINLIBS_URL = "https://github.com/brechtsanders/winlibs_mingw/releases/download/%s/%s" % (WINLIBS_TAG, WINLIBS_ZIP)
WINLIBS_DIR = os.path.join(DATA_DIR, "toolchain", "winlibs-" + WINLIBS_TAG)


LIBS_BUILD_DIR_NAME = "build-libs"
LIBS_OUT_NAME = "libs-out"
LIBS_INFO_NAME = "build_libs.ini"
MINGW_RUNTIME_DLLS = ("libgcc_s_seh-1.dll", "libstdc++-6.dll", "libwinpthread-1.dll")

# ---------- GUI update check ----------
APP_VERSION = "1.3.0"
