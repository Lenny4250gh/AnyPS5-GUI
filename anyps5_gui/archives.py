"""Game given as an archive (.zip .rar ...), extracted into <data>/extracted/<name>.

  .zip / .tar*      Python standard library, nothing to install
  .rar .7z .iso...  7-Zip (7z / 7zz in the app data folder or in PATH); fallback: the optional
                    python modules `rarfile` (+ UnRAR/WinRAR) and `py7zr`"""

import os
import re
import shutil
import subprocess
import tarfile
import time
import zipfile

from .settings import Cancelled, DATA_DIR, EXECUTABLE_NAME, MODULE_DIRS
from .util import _rmtree_force, _safe_filename, check_cancel, existing_parent, format_size
from .process import stream_process


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
