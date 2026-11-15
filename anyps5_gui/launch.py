"""Starting a converted game: checks before launch, executable permissions, launch."""

import json
import os
import subprocess

from .settings import hidden_error, LIBS_DIR_NAME, MINGW_RUNTIME_DLLS, TARGETS
from .runtime import runtime_env, sync_input_ini


def missing_runtime_dlls(pkg_dir, target):
  """MinGW runtime DLLs absent from <pkg>/libs on Windows (error 126 at start). [] if fine or not applicable."""
  libs = os.path.join(pkg_dir, LIBS_DIR_NAME)
  if target != "windows" or not os.path.isdir(libs):
    return []
  names = {n.lower() for n in os.listdir(libs)}
  if not any(n.endswith(".prx") for n in names):
    return []
  return [d for d in MINGW_RUNTIME_DLLS if d.lower() not in names]


def preflight_checks(pkg_dir, target, exe):
  """Checks before starting. Returns (errors, warnings): plain texts instead of cryptic start-up errors."""
  errors, warnings = [], []
  app0 = os.path.join(pkg_dir, "app0")
  if not os.path.isfile(exe):
    errors.append(f"{os.path.basename(exe)} was not found in the package: convert the game again.")
  pj = os.path.join(app0, "sce_sys", "param.json")
  if not os.path.isfile(pj):
    warnings.append("app0/sce_sys/param.json is missing: the title name and id cannot be read.")
  else:
    try:
      with open(pj, encoding="utf-8-sig") as f:
        j = json.load(f)
      loc = j.get("localizedParameters") or {}
      if not j.get("titleId") and not j.get("contentId"):
        warnings.append("param.json has no titleId.")
      if not any(isinstance(v, dict) and v.get("titleName") for v in loc.values()):
        warnings.append("param.json has no titleName.")
    except (OSError, ValueError):
      warnings.append("app0/sce_sys/param.json cannot be read (damaged file?).")
  if not os.path.isfile(os.path.join(app0, "eboot.bin")) and not os.path.isdir(os.path.join(pkg_dir, "app0")):
    errors.append("the app0 folder is missing from the package.")
  elif not os.path.isfile(os.path.join(app0, "eboot.bin")):
    warnings.append("app0/eboot.bin is missing: the title may not start.")
  libs = os.path.join(pkg_dir, LIBS_DIR_NAME)
  if target == "windows" and os.path.isdir(libs):
    # AnyPS5's own Windows libraries are PE files (DLLs) that are named *.prx, so the extension says nothing:
    # only a file that is not a PE ("MZ") is an original PS5 module (ELF / SELF) or a Linux build, which gives error 193.
    prx = []
    for n in sorted(os.listdir(libs)):
      if not n.lower().endswith(".prx"):
        continue
      try:
        with open(os.path.join(libs, n), "rb") as fh:
          if fh.read(2) != b"MZ":
            prx.append(n)
      except OSError as _exc:
        hidden_error("preflight_checks libs", _exc)
    if prx:
      errors.append(f"{LIBS_DIR_NAME}/ contains .prx files that are not Windows libraries ({', '.join(prx[:3])}"
                    f"{'...' if len(prx) > 3 else ''}): original PS5 modules or the libs of another system cause error 193 on Windows. "
                    "Remove them or convert again with the Windows libs (prx-windows-*.zip).")
  return errors, warnings


def fix_exec_permissions(pkg_dir, target, exe, log=None):
  """Linux/macOS: give back the execute bit lost after a zip or a download. Returns the fixed files."""
  fixed = []
  if os.name == "nt" or target == "windows":
    return fixed
  cands = [exe]
  for d in (pkg_dir, os.path.join(pkg_dir, "app0")):
    try:
      cands += [os.path.join(d, n) for n in os.listdir(d) if n.endswith((".sh", ".elf", ".so"))]
    except OSError:
      pass
  for f in cands:
    if os.path.isfile(f) and not os.access(f, os.X_OK) and (f == exe or not f.endswith(".so")):
      try:
        os.chmod(f, os.stat(f).st_mode | 0o111)
        fixed.append(f)
        if log:
          log("warn", f"{os.path.basename(f)} had no execute permission: fixed (chmod +x).")
      except OSError:
        pass
  return fixed


def launch_game(pkg_dir, target, cfg, exe=None, log=None):
  exe = exe or os.path.join(pkg_dir, TARGETS[target]["exe"])
  fix_exec_permissions(pkg_dir, target, exe, log)
  errors, warnings = preflight_checks(pkg_dir, target, exe)
  if log:
    for w in warnings:
      log("warn", "Check before start: " + w)
  if errors:
    raise OSError("Check before start: " + " ".join(errors))
  if not os.path.isfile(exe):
    raise FileNotFoundError(exe)
  missing = missing_runtime_dlls(pkg_dir, target)
  if missing:
    raise OSError(f"{LIBS_DIR_NAME}/ lacks {', '.join(missing)} (every library would fail with error 126): "
                  "copy them from the mingw64/bin of the toolchain that built the libs, or use the release libs.")
  sync_input_ini(pkg_dir, cfg["runtime"], log)  # always start with the current input settings
  env = dict(os.environ)
  env.update(runtime_env(cfg["runtime"], target, pkg_dir))
  kwargs = {}
  if os.name == "nt":
    kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
  return subprocess.Popen([exe], cwd=pkg_dir, env=env, **kwargs)
