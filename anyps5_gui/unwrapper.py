"""The external unwrap_self.py (official AnyPS5 unwrapper): where it is and how it is loaded."""

# imported here because unwrap_self.py uses them: they are also needed when the
# GUI is bundled with PyInstaller
import argparse  # noqa: F401
import collections  # noqa: F401
import math  # noqa: F401
import struct  # noqa: F401
import pathlib  # noqa: F401

import importlib.util
import os

from .settings import DATA_DIR, UNWRAP_SCRIPT_NAME
from .repo import release_dir, source_paths

from . import repo as repo_mod


_unwrapper = None
_unwrapper_path = None
UPDATE_PROMPT = None   # set by the UI: function(info, cancel) -> {"update": bool} or None (asks about a new commit)
_DECLINED_REPOS = set()  # repository folders for which the user chose "keep this build" in this run
REPO_UNWRAP_REL = ("tools", UNWRAP_SCRIPT_NAME)  # position in the official repo: tools/unwrap_self.py


def repo_unwrapper_path(rl=None):
  """tools/unwrap_self.py of the cloned repository when "Build from GitHub source" is active
  and the file exists there, otherwise None."""
  rl = rl if rl is not None else repo_mod._active_rl
  if not rl or rl.get("relinker_source") != "github":
    return None
  paths = source_paths(rl)
  if not paths:
    return None
  path = os.path.join(paths["repo"], *REPO_UNWRAP_REL)
  return path if os.path.isfile(path) else None


def release_unwrapper_path(rl=None):
  """unwrap_self.py downloaded with the release (Release option active), else None."""
  d = release_dir(rl)
  path = os.path.join(d, UNWRAP_SCRIPT_NAME) if d else None
  return path if path and os.path.isfile(path) else None


def unwrapper_path():
  """unwrap_self.py to use: the one of the release (Release option) or of the cloned repo (GitHub
  option), else the data folder."""
  path = release_unwrapper_path() or repo_unwrapper_path()
  if path:
    return path
  path = os.path.join(DATA_DIR, UNWRAP_SCRIPT_NAME)
  return path if os.path.isfile(path) else None


def load_unwrapper():
  """Load unwrap_self.py (from the cloned repo if the GitHub option is active, else from the
  data folder). Returns the module or None."""
  global _unwrapper, _unwrapper_path
  path = unwrapper_path()
  if path is None:
    return None
  if _unwrapper is not None and _unwrapper_path == path:
    return _unwrapper
  spec = importlib.util.spec_from_file_location("unwrap_self_external", path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  if not hasattr(module, "unwrap") or not hasattr(module, "Refused"):
    raise RuntimeError("%s does not expose unwrap()/Refused: incompatible version" % UNWRAP_SCRIPT_NAME)
  _tolerate_empty_orphans(module)
  _unwrapper = module
  _unwrapper_path = path
  return module


def _tolerate_empty_orphans(module):
  """The official unwrap_self.py rejects EMPTY non-LOAD segments (filesz 0) whose vaddr lies
  outside every PT_LOAD, even when there is nothing to copy. Here they are let through without
  touching the official file: the vaddr is zeroed only during place_segments and then restored."""
  original = getattr(module, "place_segments", None)
  if original is None or getattr(original, "_tolerant", False):
    return
  PT_LOAD = getattr(module, "PT_LOAD", 1)

  def tolerant(data, headers, records):
    loads = [h for h in headers if h["type"] == PT_LOAD]
    saved = []
    for h in headers:
      if h["type"] == PT_LOAD or h["vaddr"] == 0 or h["size"] != 0:
        continue
      inside = any(l["memory"] and l["vaddr"] <= h["vaddr"] < l["vaddr"] + l["memory"] for l in loads)
      if not inside:
        saved.append((h, h["vaddr"]))
        h["vaddr"] = 0
        h["source"] = 0  # empty segment: no data to copy, avoids the offset check
    try:
      return original(data, headers, records)
    finally:
      for h, vaddr in saved:
        h["vaddr"] = vaddr

  tolerant._tolerant = True
  module.place_segments = tolerant


def unwrapper_status():
  return unwrapper_path()
