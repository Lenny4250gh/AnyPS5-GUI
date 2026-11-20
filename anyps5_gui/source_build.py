"""Relinker built from a GitHub repository (clone + CMake), and the build tools (Git, CMake, MinGW) detection
and automatic installation (Windows: winget for Git/CMake, WinLibs download for GCC)."""

import configparser
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import zipfile

from .settings import (
    Cancelled, hidden_error, LIBS_BUILD_DIR_NAME, LIBS_INFO_NAME, LIBS_OUT_NAME, RELINKER_TARGET_GUESS,
    UNWRAP_SCRIPT_NAME, WINLIBS_DIR, WINLIBS_TAG, WINLIBS_URL, WINLIBS_ZIP
)
from .config import yes
from .util import _remove_quiet, _rmtree_force, check_cancel, format_duration
from .repo import (
    _REPO_REF_RE, normalize_repo_url, read_built_libs, read_release_current, set_active_relinker_cfg,
    source_paths, winlibs_bin
)
from .process import build_parallel_jobs, kill_stale_build_processes, stream_process
from .unwrapper import _DECLINED_REPOS, REPO_UNWRAP_REL, repo_unwrapper_path, unwrapper_status
from .files import find_relinker

from . import unwrapper as unwrapper_mod


_OFFICIAL_REPO_RE = re.compile(
  r"^(?:https?://(?:www\.)?github\.com/|git@github\.com:|ssh://git@github\.com/|git://github\.com/)"
  r"boykopovar/AnyPS5(?:\.git)?/?$", re.I)


def is_official_repo(url):
  """True if the URL is the official AnyPS5 repository (boykopovar/AnyPS5 on github.com)."""
  url = normalize_repo_url(url)
  return bool(url and _OFFICIAL_REPO_RE.match(url))


def log_tool_source(rl, log):
  """Writes the repository and the branch/tag (or release tag) used by the conversion to the log,
  with a warning when the repository is not the official one. Nothing for local files."""
  source = rl.get("relinker_source")
  if source not in ("release", "github"):
    return
  url = normalize_repo_url(rl.get("repo_url", ""))
  if not url:
    return
  if source == "github":
    ref = rl.get("repo_ref", "").strip()
    log("info", f"Repository: {url} · branch/tag: {ref or 'default branch'}")
  else:
    cur = read_release_current(rl)
    tag = cur["tag"] if cur else (rl.get("release_tag", "").strip() or "latest")
    log("info", f"Repository: {url} · release tag: {tag}")
  if not is_official_repo(url):
    log("warn", "Unofficial repository: run only code you trust.")


def read_source_build(paths):
  """{'commit', 'target', 'binary'} of the last successful build, or None."""
  cfg = configparser.ConfigParser(interpolation=None)
  try:
    cfg.read(paths["info"], encoding="utf-8")
  except Exception as _exc:
    hidden_error("read_source_build", _exc)
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


TOOL_NAMES = {"git": "Git", "cmake": "CMake", "mingw": "MinGW-w64 GCC 15.2.0 (WinLibs, with Ninja)"}


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
        if time.time() - last > 0.25 or (total and done >= total):
          last = time.time()
          if total:
            log("percent", done * 100.0 / total)
          log("progress", "Download: %d / %d MiB" % (done >> 20, total >> 20) if total else "Download: %d MiB" % (done >> 20))
    log("percent", None)
    log("progress", "")
    log("ok", "Download complete (%d MiB)" % (done >> 20))
  except (OSError, ValueError) as e:  # URLError/HTTPError/timeouts are OSError
    log("percent", None)
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
          log("percent", i * 100.0 / max(1, len(members)))
          log("progress", "Unpacking: %d / %d files" % (i, len(members)))
        zf.extract(name, tmp)
    log("percent", None)
    log("progress", "")
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
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
          check_cancel(cancel)
          chunk = r.read(1 << 20)
          if not chunk:
            break
          f.write(chunk)
          done += len(chunk)
          if total:
            log("percent", done * 100.0 / total)
            log("progress", "Download: %d / %d MiB" % (done >> 20, total >> 20))
      log("percent", None)
      log("progress", "")
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
  except Exception as _exc:
    hidden_error("ensure_source_libs", _exc)
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
    log("percent", None)
    # the [N/TOTAL] lines of ninja drive the progress bar (and the elapsed/remaining time line)
    code = stream_process(build_cmd, make_build_log(log), cancel, holder, "cmake --build", env=env,
                          errors=errors, logfile=logfile, idle_timeout=1200)
    log("percent", None)
    if code == 0:
      log("progress", "")
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
      if same_settings and unwrapper_mod.UPDATE_PROMPT and yes(rl.get("repo_update_ask", "yes")):
        log("step", "A new version of the repository is available: waiting for your choice")
        prompt_info = {"url": p["url"], "old": previous["commit"][:12], "new": remote[:12]}
        try:
          prompt_info.update(repo_changes(p["url"], repo, previous["commit"], remote, p["ref"], env))
        except Exception as _exc:
          hidden_error("ensure_source_relinker", _exc)
          pass  # the list of changes is only a courtesy
        check_cancel(cancel)
        answer = unwrapper_mod.UPDATE_PROMPT(prompt_info, cancel)
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
