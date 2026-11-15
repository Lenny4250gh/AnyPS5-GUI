"""Small helpers shared by the other modules: cancellation, sizes and durations, paths of a game package."""

import os
import re
import shutil
import stat
import struct
import sys

from .settings import Cancelled, SELF_MAGICS, TARGETS


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


def format_duration(seconds):
  seconds = int(seconds)
  if seconds >= 3600:
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
  if seconds >= 60:
    return f"{seconds // 60}m {seconds % 60:02d}s"
  return f"{seconds}s"


def check_cancel(cancel):
  if cancel.is_set():
    raise Cancelled()


def _remove_quiet(path):
  if os.path.exists(path):
    try:
      os.remove(path)
    except OSError:
      pass


def split_list(text):
  return [p.strip().strip('"') for p in re.split(r"[;,\n]", text) if p.strip()]


def _rmtree_force(path):
  def onerror(func, p, _exc):
    try:
      os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
      func(p)
    except OSError:
      pass
  shutil.rmtree(path, onerror=onerror)


def _safe_filename(name):
  return re.sub(r'[<>:"/\\|?*]', "_", name).strip(" .") or "game"


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
