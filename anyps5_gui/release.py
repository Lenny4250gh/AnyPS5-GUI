"""Files of a GitHub release (relinker, prx zips, docs, unwrap_self.py): download and reuse."""

import configparser
import hashlib
import os
import stat
import time

from .settings import (
    Cancelled, LIBS_ZIP_PREFIX, NOT_BINARY_EXT, RELEASE_DOCS, RELEASE_INFO_NAME, RELEASE_LIBS_FOR,
    RELINKER_PREFIX, UNWRAP_SCRIPT_NAME
)
from .config import yes
from .util import check_cancel, format_size
from .repo import _RELEASE_TAG_RE, github_owner_repo, read_release_current, release_paths
from .unwrapper import REPO_UNWRAP_REL, unwrapper_status


RAW_URL = "https://raw.githubusercontent.com/%s/%s/%s/%s"
API_URL = "https://api.github.com/repos/%s/%s/releases/%s"


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
