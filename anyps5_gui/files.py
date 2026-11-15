"""Files of a game and of the AnyPS5 libs: finding the relinker, module folders, libs zip, copy and move."""

import os
import shutil
import stat
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .settings import (
    BACKUP_DIR_NAME, DATA_DIR, EXECUTABLE_NAME, LIBS_DIR_NAME, LIBS_ZIP_PREFIX, MODULE_DIRS,
    NOT_BINARY_EXT, RELEASE_LIBS_FOR, RELINKER_PREFIX, TARGETS
)
from .config import yes
from .util import _rmtree_force, check_cancel, file_state, format_duration, format_size, split_list
from .repo import read_built_libs, release_dir, source_paths
from .unwrapper import unwrapper_status
from .archives import archive_listing, ArchiveError, EXTRACT_DIR_NAME, open_archive

from . import repo as repo_mod


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
  rl = rl if rl is not None else repo_mod._active_rl
  return bool(rl) and rl.get("relinker_source") == "github" and yes(rl.get("repo_libs_from_repo", "yes"))


def libs_info(target, rl=None):
  """Libs to put in the package: the ones built from the repo (if that option is active and
  they were built), otherwise the prx-*.zip. None if not available (yet)."""
  rl = rl if rl is not None else repo_mod._active_rl
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
