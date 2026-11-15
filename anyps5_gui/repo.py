"""Where the tools come from: paths of the GitHub release and of the source build, and the relinker options in use."""

import configparser
import hashlib
import os
import re

from .settings import (
    DATA_DIR, DEFAULT_REPO_URL, hidden_error, LIBS_DIR_NAME, LIBS_INFO_NAME, LIBS_OUT_NAME,
    MINGW_RUNTIME_DLLS, RELEASE_CURRENT_NAME, RELEASE_DIR_NAME, SOURCE_CLONED_MARK, SOURCE_DIR_NAME,
    SOURCE_INFO_NAME, WINLIBS_DIR
)


_active_rl = None  # relinker options in use (set by the logic/UI): decides where unwrap_self.py comes from


def set_active_relinker_cfg(rl):
  global _active_rl
  _active_rl = rl


def github_mode():
  return bool(_active_rl) and _active_rl.get("relinker_source") == "github"


_GITHUB_REPO_RE = re.compile(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", re.I)
_RELEASE_TAG_RE = re.compile(r"^[\w][\w./+-]*$")


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
    except Exception as _exc:
      hidden_error("read_release_current", _exc)
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


def winlibs_bin():
  """mingw64/bin of the WinLibs GCC 15.2.0 installed by the GUI, or None."""
  d = os.path.join(WINLIBS_DIR, "mingw64", "bin")
  return d if os.path.isfile(os.path.join(d, "g++.exe")) else None


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
