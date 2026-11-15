"""Library: the PS5 dumps and the converted games found in the folders the user chose (and in the output folder).

Only the folders themselves and their direct sub-folders are looked at (never the whole disk).
The user's data (extra folders, comments, per-game runtime settings, size cache, view: table/grid and column widths) is in library.json
in the data folder."""

import base64
import json
import os
import threading
import time

from .settings import DATA_DIR, DEFAULT_OUTPUT_DIR, EXECUTABLE_NAME, TARGETS, hidden_error
from .util import file_state
from .files import folder_game_problems
from .runtime import title_name
from .report import read_run_report

LIBRARY_FILE = os.path.join(DATA_DIR, "library.json")
# runtime settings that can be changed for a single game (the others come from the global settings)
GAME_RT_KEYS = ("language", "gpu", "present_mode", "upscale", "fps_limit", "fullscreen", "overlay", "extra_env")
_lock = threading.RLock()
_sizing = set()  # packages whose size is being calculated


def load_library():
  data = {"dirs": [], "dump_dirs": [], "comments": {}, "game_rt": {}, "sizes": {}, "ui": {}}
  try:
    with open(LIBRARY_FILE, encoding="utf-8") as f:
      j = json.load(f)
    if isinstance(j, dict):
      for k in data:
        if isinstance(j.get(k), type(data[k])):
          data[k] = j[k]
  except FileNotFoundError:
    pass
  except Exception as _exc:
    hidden_error("load_library", _exc)
  return data


def save_library(data):
  with _lock:
    try:
      os.makedirs(DATA_DIR, exist_ok=True)
      tmp = LIBRARY_FILE + ".tmp"
      with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
      os.replace(tmp, LIBRARY_FILE)
    except Exception as _exc:
      hidden_error("save_library", _exc)


def norm(p):
  return os.path.normpath(os.path.abspath(p))


def is_fs_root(p):
  p = norm(p)
  return os.path.dirname(p) == p


def package_exe(pkg):
  """(target, executable path) of a converted package, or ("", "")."""
  info = read_json(os.path.join(pkg, "conversion_info.json"))
  t = info.get("target") if isinstance(info.get("target"), str) else ""
  order = ([t] if t in TARGETS else []) + [k for k in TARGETS if k != t]
  for k in order:
    exe = os.path.join(pkg, TARGETS[k]["exe"])
    if os.path.isfile(exe):
      return k, exe
  return "", ""


def is_package(p):
  return os.path.isdir(os.path.join(p, "app0")) and bool(package_exe(p)[0])


def library_roots(cfg, data):
  out = [cfg["settings"].get("output_dir") or DEFAULT_OUTPUT_DIR]
  out += [d for d in data["dirs"] if isinstance(d, str)]
  seen, res = set(), []
  for d in out:
    n = os.path.normcase(norm(d))
    if n not in seen:
      seen.add(n)
      res.append(norm(d))
  return res


def find_packages(roots):
  """Converted packages: each root itself (if it is one) or its direct sub-folders."""
  found, seen = [], set()

  def add(p):
    n = os.path.normcase(norm(p))
    if n not in seen:
      seen.add(n)
      found.append(norm(p))
  for r in roots:
    try:
      if is_fs_root(r) or not os.path.isdir(r):
        continue
      if is_package(r):
        add(r)
        continue
      for n in sorted(os.listdir(r), key=str.lower):
        p = os.path.join(r, n)
        if os.path.isdir(p) and is_package(p):
          add(p)
    except OSError as _exc:
      hidden_error("find_packages", _exc)
  return found


def is_dump(p):
  return os.path.isdir(p) and not folder_game_problems(p)


def find_dumps(roots):
  """PS5 game dumps: each root itself (if it is one) or its direct sub-folders."""
  found, seen = [], set()

  def add(p):
    n = os.path.normcase(norm(p))
    if n not in seen:
      seen.add(n)
      found.append(norm(p))
  for r in roots:
    try:
      if is_fs_root(r) or not os.path.isdir(r):
        continue
      if is_dump(r):
        add(r)
        continue
      for n in sorted(os.listdir(r), key=str.lower):
        p = os.path.join(r, n)
        if is_dump(p):
          add(p)
    except OSError as _exc:
      hidden_error("find_dumps", _exc)
  return found


def read_json(path):
  try:
    with open(path, encoding="utf-8-sig") as f:
      j = json.load(f)
    return j if isinstance(j, dict) else {}
  except Exception:
    return {}


def _param(pkg):
  return read_json(os.path.join(pkg, "app0", "sce_sys", "param.json"))


def _dump_title(pj):
  loc = pj.get("localizedParameters") or {}
  for lang in [loc.get("defaultLanguage"), "en-US", "en-GB"] + [k for k in loc if isinstance(loc.get(k), dict)]:
    t = (loc.get(lang) or {}).get("titleName") if lang else None
    if isinstance(t, str) and t.strip():
      return t.strip()
  return str(pj.get("titleName") or "").strip()


def dump_row(path):
  pj = read_json(os.path.join(path, "sce_sys", "param.json"))
  return {"path": path, "name": _dump_title(pj) or os.path.basename(path), "serial": str(pj.get("titleId") or ""),
          "version": str(pj.get("contentVersion") or ""), "eboot": file_state(os.path.join(path, EXECUTABLE_NAME)),
          "icon": os.path.isfile(os.path.join(path, "sce_sys", "icon0.png"))}


def _strip_suffix(name):
  low = name.lower()
  for t in TARGETS.values():
    if low.endswith("-" + t["suffix"]):
      return low[:-len(t["suffix"]) - 1]
  return low


def game_key(serial, folder_name):
  """Title ID when known, else the folder name (without the -windows / -linux / -macos suffix)."""
  return serial.strip().upper() if serial.strip() else "name:" + _strip_suffix(folder_name)


def build_games(pkg_rows, dump_rows, comments):
  """One entry per game: the dumps and the converted packages that share a Title ID (or, without one, a name)."""
  games = {}
  for d in dump_rows:
    g = games.setdefault(game_key(d["serial"], os.path.basename(d["path"])), {"dumps": [], "pkgs": []})
    g["dumps"].append(d)
  for r in pkg_rows:
    g = games.setdefault(game_key(r["serial"], os.path.basename(r["path"])), {"dumps": [], "pkgs": []})
    g["pkgs"].append(r)
  out = []
  for key, g in games.items():
    g["pkgs"].sort(key=lambda r: r.get("when", 0), reverse=True)  # the most recent conversion first
    first = (g["pkgs"] or g["dumps"])[0]
    targets = [r["target"] for r in g["pkgs"]]
    g.update({"key": key, "name": first["name"], "serial": first["serial"], "version": first["version"],
              "comment": str(comments.get(key, "") or (comments.get(first["path"], "") if g["pkgs"] else "")),
              "dup": len(g["dumps"]) > 1 or len(set(targets)) < len(targets)})
    out.append(g)
  return sorted(out, key=lambda g: g["name"].lower())


def converted_with(info):
  """Short text: which AnyPS5 build converted the package (from conversion_info.json)."""
  if not info:
    return ""
  parts = []
  tag = info.get("anyps5_release_tag") or ""
  commit = str(info.get("anyps5_commit") or "")[:7]
  ref = info.get("anyps5_ref") or ""
  if tag:
    parts.append(str(tag))
  elif commit:
    parts.append("commit " + commit + (" (" + str(ref) + ")" if ref else ""))
  elif info.get("anyps5_source"):
    parts.append(str(info["anyps5_source"]))
  repo = str(info.get("anyps5_repo") or "")
  if repo and "boykopovar/anyps5" not in repo.lower().replace("https://github.com/", ""):
    parts.append(repo.replace("https://github.com/", ""))
  if info.get("target") in TARGETS:
    parts.append(TARGETS[info["target"]]["label"].split(" (")[0])
  return " · ".join(parts)


_STATUS = {"presents": ("In game", "ok"), "first-draw": ("Draws", "ok"), "starts": ("Starts", "warn"),
           "loads": ("Loads", "warn"), "unknown": ("Nothing observed", "warn")}


def game_status(exe):
  """Status from the last run report (anyps5-report.json next to the executable)."""
  try:
    r = read_run_report(os.path.dirname(exe))
  except Exception as _exc:
    hidden_error("game_status", _exc)
    return {"text": "Unknown", "level": "muted"}
  rep = r.get("report")
  if not rep:
    return {"text": "Not tested", "level": "muted"}
  run = rep.get("run") if isinstance(rep.get("run"), dict) else {}
  text, level = _STATUS.get(run.get("stage"), ("Unknown", "muted"))
  if run.get("exit") in ("crash", "abort"):
    text, level = text + " · crashed", "err"
  return {"text": text, "level": level}


def _dir_size(path):
  total = 0
  for r, _d, files in os.walk(path):
    for n in files:
      try:
        total += os.path.getsize(os.path.join(r, n))
      except OSError:
        pass
  return total


def _stamp(pkg):
  try:
    return os.path.getmtime(os.path.join(pkg, "conversion_info.json"))
  except OSError:
    return 0


def _size_worker(todo, data):
  for pkg in todo:
    try:
      size = _dir_size(pkg)
      with _lock:
        data["sizes"][pkg] = {"size": size, "m": _stamp(pkg)}
    except Exception as _exc:
      hidden_error("size_worker", _exc)
    finally:
      _sizing.discard(pkg)
  save_library(data)


def start_sizes(pkgs, data, force=False):
  """Calculates the missing sizes in the background. Returns True while some are still being calculated."""
  todo = []
  with _lock:
    for p in pkgs:
      c = data["sizes"].get(p)
      if force and p not in _sizing:
        data["sizes"].pop(p, None)
        c = None
      if (not c or c.get("m") != _stamp(p)) and p not in _sizing:
        _sizing.add(p)
        todo.append(p)
  if todo:
    threading.Thread(target=_size_worker, args=(todo, data), daemon=True).start()
  return bool(_sizing)


def package_row(pkg, data):
  target, exe = package_exe(pkg)
  info = read_json(os.path.join(pkg, "conversion_info.json"))
  pj = _param(pkg)
  name = title_name(pkg) or os.path.basename(pkg)
  c = data["sizes"].get(pkg)
  size = c["size"] if c and c.get("m") == _stamp(pkg) else None
  return {"path": pkg, "name": name, "serial": str(pj.get("titleId") or ""),
          "version": str(pj.get("contentVersion") or ""), "size": size,
          "with": converted_with(info), "target": target, "status": game_status(exe),
          "when": _stamp(pkg), "custom": bool(data["game_rt"].get(pkg)),
          "icon": os.path.isfile(os.path.join(pkg, "app0", "sce_sys", "icon0.png"))}


def icon_uri(pkg, dump=False):
  f = os.path.join(pkg, "sce_sys" if dump else os.path.join("app0", "sce_sys"), "icon0.png")
  try:
    if os.path.isfile(f) and os.path.getsize(f) <= 4 * 1024 * 1024:
      with open(f, "rb") as fh:
        return "data:image/png;base64," + base64.b64encode(fh.read()).decode("ascii")
  except OSError as _exc:
    hidden_error("icon_uri", _exc)
  return ""


def merged_runtime(cfg, data, pkg):
  """Copy of the runtime settings with the settings of this single game on top."""
  rt = dict(cfg["runtime"])
  for k, v in (data["game_rt"].get(pkg) or {}).items():
    if k in GAME_RT_KEYS and isinstance(v, str) and v.strip():
      rt[k] = v.strip()
  return rt
