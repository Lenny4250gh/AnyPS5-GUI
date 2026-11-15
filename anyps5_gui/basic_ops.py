"""Unwrap and restore of a game, trash, data folder relocation and migration."""

import os
import shutil
import subprocess
import sys
import time

from .settings import (
    BACKUP_DIR_NAME, DATA_DIR, DEFAULT_DATA_DIR, hidden_error, LEGACY_DATA_DIR, LOCATION_FILE,
    SCREENSHOTS_DIR_NAME, STORAGE_EDITABLE, UNWRAP_SCRIPT_NAME
)
from .config import yes
from .util import _remove_quiet, check_cancel, dir_size, existing_parent, file_state, format_size, split_list
from .repo import github_mode, release_mode, set_active_relinker_cfg
from .unwrapper import load_unwrapper, unwrapper_status
from .files import effective_module_dirs, game_targets, get_backup_dir
from .disk import log_space, space_requirements
from .release import ensure_release_assets
from .source_build import ensure_repo_unwrapper


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
  except Exception as _exc:
    hidden_error("move_to_trash", _exc)
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
  except Exception as _exc:
    hidden_error("move_to_trash", _exc)
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
  except Exception as _exc:
    hidden_error("open_trash", _exc)
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
  except Exception as _exc:
    hidden_error("open_path", _exc)
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
      except Exception as _exc:
        hidden_error("relocate_data_dir", _exc)
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
      except Exception as _exc:
        hidden_error("relocate_data_dir", _exc)
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
  except Exception as _exc:
    hidden_error("migrate_legacy_data", _exc)
    pass
  return moved, left


def _remove_empty_dirs(root):
  """Removes the empty folders left in the source after a move (root included)."""
  for cur, _dirs, _files in os.walk(root, topdown=False):
    try:
      os.rmdir(cur)
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
