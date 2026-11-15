"""Running external programs: output streaming, killing process trees, build parallelism."""

import os
import re
import subprocess
import sys
import threading
import time

from .settings import Cancelled, hidden_error


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
  except Exception as _exc:
    hidden_error("kill_tree", _exc)
    pass
  try:
    proc.kill()
  except Exception as _exc:
    hidden_error("kill_tree", _exc)
    pass


def kill_stale_build_processes():
  """Windows: remove leftover glslang/ninja processes of a previous hung build."""
  if os.name != "nt":
    return
  for name in ("glslangValidator.exe", "glslang.exe", "ninja.exe"):
    try:
      subprocess.run(["taskkill", "/F", "/T", "/IM", name], capture_output=True, timeout=20,
                     creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as _exc:
      hidden_error("kill_stale_build_processes", _exc)
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
  except Exception as _exc:
    hidden_error("_total_ram_gib", _exc)
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
