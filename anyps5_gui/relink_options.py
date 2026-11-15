"""Relinker options: validation, command line, patches, version and the flags the relinker knows."""

import os
import re
import shlex
import shutil
import subprocess

from .settings import DATA_DIR, EXECUTABLE_NAME, hidden_error, TARGETS
from .config import host_target, yes
from .util import file_state, split_list
from .repo import normalize_repo_url, read_release_current, set_active_relinker_cfg, source_paths
from .files import find_python
from .source_build import find_built_relinker, read_source_build, resolve_relinker
from .runtime import _whole


def parse_address(text):
  text = text.strip()
  return int(text, 16) if text.lower().startswith("0x") else int(text)


def validate_relink_options(intel, rl):
  """Check the combinations forbidden by USAGE.md. Returns None or a message."""
  target = rl["target"]
  rosetta = yes(rl["rosetta"])
  if rosetta and target == "windows":
    return "--to-rosetta conflicts with --windows."
  if yes(rl.get("to_avx", "no")):
    if target != "macos":
      return "--to-avx requires the macOS target."
    if rosetta:
      return "--to-avx and --to-rosetta cannot be used together: --to-avx is for Intel Macs with AVX but without AVX2, --to-rosetta for Apple silicon."
  for key, flag in (("gui", "--windows-gui"), ("diagnostics", "--windows-diagnostics"),
                    ("trace_imports", "--trace-imports")):
    if yes(rl[key]) and target != "windows":
      return f"{flag} requires the Windows target."
  if rl["fixed_base"].strip():
    if target != "windows":
      return "--windows-fixed-base requires the Windows target."
    try:
      addr = parse_address(rl["fixed_base"])
    except ValueError:
      return "--windows-fixed-base: invalid address (decimal or 0x...)."
    if addr % 0x10000 or not (0x20000 <= addr <= 0x7FFFFFFF0000):
      return "--windows-fixed-base: needs a 64 KiB-aligned address between 0x20000 and 0x7fffffff0000."
  if rl.get("linux_fixed_base", "").strip():
    if target != "linux":
      return "--linux-fixed-base requires the Linux target."
    try:
      addr = parse_address(rl["linux_fixed_base"])
    except ValueError:
      return "--linux-fixed-base: invalid address (decimal or 0x...)."
    if addr % 0x10000 or not (0x10000 <= addr <= 0x7FFFFFFF0000):
      return "--linux-fixed-base: needs a 64 KiB-aligned address between 0x10000 and 0x7fffffff0000."
    if yes(rl.get("lazy_binding", "no")):
      return "--linux-fixed-base conflicts with --lazy-binding."
  if rl.get("macos_fixed_base", "").strip():
    if target != "macos":
      return "--macos-fixed-base requires the macOS target."
    try:
      addr = parse_address(rl["macos_fixed_base"])
    except ValueError:
      return "--macos-fixed-base: invalid address (decimal or 0x...)."
    if addr % 0x10000 or not (0x20000 <= addr <= 0x7FFFFFFF0000):
      return "--macos-fixed-base: needs a 64 KiB-aligned address between 0x20000 and 0x7fffffff0000."
  if rl["unused_filter"] not in ("0", "1", "2"):
    return "unused-filter must be 0, 1 or 2."
  rpath = rl["rpath"].strip()
  if rpath and target == "windows" and not rpath.isascii():
    return "--rpath on Windows requires an ASCII path."
  if rpath and target == "linux" and not (rpath.startswith("/") or rpath.startswith("$ORIGIN")):
    return "--rpath on Linux requires an absolute path or one starting with $ORIGIN."
  skip = yes(rl.get("skip_sce_module", "no"))
  smp = rl.get("sce_module_path", "").strip()
  excl = split_list(rl.get("exclude_sce_modules", ""))
  if skip:
    if smp:
      return "--skip-sce-module conflicts with --sce-module-path."
    if rl.get("module_dirs", "").strip():
      return "--skip-sce-module conflicts with --module-dir."
    if rl.get("executables", "").strip():
      return "--skip-sce-module conflicts with --executable."
    if excl:
      return "--skip-sce-module conflicts with --exclude-sce-module."
  if smp and not os.path.isdir(os.path.abspath(smp)):
    return f"--sce-module-path: '{smp}' is not an existing folder."
  for name in excl:
    if "/" in name or "\\" in name:
      return f"--exclude-sce-module takes a file name, not a path: '{name}'."
  if yes(rl.get("lazy_binding", "no")) and yes(rl["trace_imports"]):
    return "--trace-imports conflicts with --lazy-binding."
  pf = rl.get("patches_file", "").strip()
  if pf and yes(rl.get("no_patches", "no")):
    return "--patches conflicts with --no-patches."
  if pf and not os.path.isfile(os.path.abspath(pf)):
    return f"--patches: '{pf}' is not an existing file."
  extra, err = parse_extra_args(rl)
  if err:
    return err
  if extra:
    managed = {a for a in build_relink_command("relinker", intel, dict(rl, extra_args=""), "in", "out")
               if a.startswith("--")}
    for a in extra:
      flag = a.split("=", 1)[0]
      if flag.startswith("--") and flag in managed:
        return f"Extra arguments: {flag} is already set by the options above; remove one of them."
  if yes(rl.get("autorun", "no")) and target != host_target():
    return ("--autorun needs the target system to be the one of this computer ("
            + TARGETS[host_target()]["label"].split(" (")[0] + ").")
  return None


def build_relink_command(relinker, intel, rl, input_file, out_exe):
  target = rl["target"]
  cmd = [relinker]
  if target == "windows":
    cmd.append("--windows")
    if yes(rl["gui"]):
      cmd.append("--windows-gui")
    if yes(rl["diagnostics"]):
      cmd.append("--windows-diagnostics")
    if yes(rl.get("frame_red_zone", "no")):
      cmd.append("--frame-red-zone")  # Windows only: frame for leaf functions that use the red zone
    if rl["fixed_base"].strip():
      cmd += ["--windows-fixed-base", rl["fixed_base"].strip()]
    if yes(rl["trace_imports"]):
      cmd.append("--trace-imports")
  elif target == "macos":
    cmd.append("--macos")
    if rl.get("macos_fixed_base", "").strip():
      cmd += ["--macos-fixed-base", rl["macos_fixed_base"].strip()]
  elif target == "linux":
    if rl.get("linux_fixed_base", "").strip():
      cmd += ["--linux-fixed-base", rl["linux_fixed_base"].strip()]
  if yes(rl["rosetta"]):
    cmd.append("--to-rosetta")  # implies --to-intel
  elif yes(rl.get("to_avx", "no")) and target == "macos":
    cmd.append("--to-avx")  # implies --to-intel, requires --macos
  elif intel:
    cmd.append("--to-intel")
  if rl["unused_filter"] != "0":
    cmd.append("unused-filter=" + rl["unused_filter"])
  if yes(rl["registry"]):
    cmd.append("--registry")
  if rl["rpath"].strip():
    cmd += ["--rpath", rl["rpath"].strip()]
  for d in split_list(rl["module_dirs"]):
    cmd += ["--module-dir", d]
  for e in split_list(rl["executables"]):
    cmd += ["--executable", e]
  if rl.get("sce_module_path", "").strip():
    cmd += ["--sce-module-path", os.path.abspath(rl["sce_module_path"].strip())]
  if yes(rl.get("skip_sce_module", "no")):
    cmd.append("--skip-sce-module")
  for name in split_list(rl.get("exclude_sce_modules", "")):
    cmd += ["--exclude-sce-module", name]
  for key, flag in (("skip_syscall_check", "--skip-syscall-check"),
                    ("trap_syscall_stubs", "--trap-sdk-syscall-stubs"),
                    ("lazy_binding", "--lazy-binding"),
                    ("autorun", "--autorun"),
                    ("function_symbol_roots", "--function-symbol-roots")):
    if yes(rl.get(key, "no")):
      cmd.append(flag)
  # Linux only (USAGE.md): omit deps that no converted module provides and that are not on --rpath
  if target == "linux" and yes(rl.get("omit_unavailable_dependencies", "no")):
    cmd.append("--omit-unavailable-dependencies")
  if yes(rl.get("no_patches", "no")):
    cmd.append("--no-patches")
  elif rl.get("patches_file", "").strip():
    cmd += ["--patches", os.path.abspath(rl["patches_file"].strip())]
  extra, _err = parse_extra_args(rl)
  cmd += extra
  cmd += [input_file, out_exe]
  return cmd


def parse_extra_args(rl):
  """Free-form relinker arguments (field "extra_args"). Returns (list, error or None)."""
  text = (rl.get("extra_args", "") or "").strip()
  if not text:
    return [], None
  try:
    return shlex.split(text, posix=(os.name != "nt")), None
  except ValueError as e:
    return [], f"Extra arguments: {e}"


def verify_patches(rl, folder):
  """Run `relinker --verify-patches <file> <eboot>` (writes nothing). Returns (level, text)."""
  pf = rl.get("patches_file", "").strip()
  if not pf:
    auto = os.path.join(folder, "relinker-patches.toml") if folder else ""
    if auto and os.path.isfile(auto):
      pf = auto
    else:
      return "warn", "Choose a patch file first (or put relinker-patches.toml next to eboot.bin)."
  pf = os.path.abspath(pf)
  if not os.path.isfile(pf):
    return "err", f"Patch file not found: {pf}"
  if not folder or not os.path.isdir(folder):
    return "warn", "Choose the game folder first."
  eboot = os.path.join(folder, EXECUTABLE_NAME)
  state = file_state(eboot)
  if state == "self":
    return "warn", f"{EXECUTABLE_NAME} is still a SELF: unwrap the game first (Unwrap mode), then verify."
  if state != "elf":
    return "err", f"{EXECUTABLE_NAME} not found or not a valid ELF in the game folder."
  set_active_relinker_cfg(rl)
  relinker = resolve_relinker(rl)
  if not relinker or not os.path.isfile(relinker):
    return "err", "Relinker not available yet: run a conversion once (or build it) so the app has it."
  cmd = [relinker, "--verify-patches", pf]
  smp = rl.get("sce_module_path", "").strip()
  if smp:
    cmd += ["--sce-module-path", os.path.abspath(smp)]
  for d in split_list(rl.get("module_dirs", "")):
    cmd += ["--module-dir", d]
  for e in split_list(rl.get("executables", "")):
    cmd += ["--executable", e]
  cmd.append(eboot)
  kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
  try:
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                         timeout=120, **kwargs)
  except (OSError, subprocess.SubprocessError) as e:
    return "err", f"Could not run the relinker: {e}"
  out = "\n".join(x for x in ((res.stdout or "").strip(), (res.stderr or "").strip()) if x)
  if res.returncode == 0:
    return "ok", "Patches OK: no mismatch.\n" + out if out else "Patches OK: no mismatch."
  if res.returncode == 1:
    return "err", "Patch mismatch (these patches do not fit this game version):\n" + out
  return "err", "Verification failed:\n" + out


def find_repo_tool(rl, rel):
  """tools/<script> in <data>/tools or in the cloned repository, else None."""
  candidates = [os.path.join(DATA_DIR, rel)]
  paths = source_paths(rl) if rl.get("relinker_source") == "github" else None
  if paths:
    candidates.append(os.path.join(paths["repo"], rel))
  for c in candidates:
    if os.path.isfile(c):
      return c
  return None


def suggest_patches(rl, folder, list_all=False):
  """Run tools/suggest_patches.py on the selected unwrapped game. Returns (level, text, patch_file)."""
  catalog = rl.get("patch_catalog", "").strip()
  if not catalog:
    return "warn", "Choose the patch catalog folder first (the one with index.json).", ""
  catalog = os.path.abspath(catalog)
  if not os.path.isfile(os.path.join(catalog, "index.json")):
    return "err", f"No index.json in {catalog}.", ""
  if not folder or not os.path.isdir(folder):
    return "warn", "Choose the game folder first.", ""
  eboot = os.path.join(folder, EXECUTABLE_NAME)
  state = file_state(eboot)
  if state == "self":
    return "warn", f"{EXECUTABLE_NAME} is still a SELF: unwrap the game first (Unwrap mode).", ""
  if state != "elf":
    return "err", f"{EXECUTABLE_NAME} not found or not a valid ELF in the game folder.", ""
  param = os.path.join(folder, "sce_sys", "param.json")
  if not os.path.isfile(param):
    return "err", "sce_sys/param.json not found in the game folder (it holds the title id and version).", ""
  timeout = rl.get("patch_timeout", "").strip()
  if timeout and not (_whole(timeout) and int(timeout) >= 1):
    return "err", "Advisor timeout must be a whole number of seconds, 1 or more.", ""
  script = find_repo_tool(rl, os.path.join("tools", "suggest_patches.py"))
  if not script:
    return "err", "tools/suggest_patches.py not found: clone the repository (Source build) or copy it into the app data folder under tools/.", ""
  python = find_python()
  if not python:
    return "err", "Python was not found in PATH (the advisor needs Python 3.11 or newer).", ""
  kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
  try:
    ok = subprocess.run([python, "-c", "import sys;print(int(sys.version_info>=(3,11)))"], capture_output=True,
                        text=True, timeout=15, **kw).stdout.strip()
  except (OSError, subprocess.SubprocessError):
    ok = ""
  if ok != "1":
    return "err", "The patch advisor needs Python 3.11 or newer.", ""
  set_active_relinker_cfg(rl)
  relinker = resolve_relinker(rl)
  if not relinker or not os.path.isfile(relinker):
    return "err", "Relinker not available yet: run a conversion once (or build it) so the app has it.", ""
  cmd = [python, script, "--catalog", catalog, "--param-json", param, "--input", eboot, "--relinker", relinker]
  smp = rl.get("sce_module_path", "").strip()
  if smp:
    cmd += ["--sce-module-path", os.path.abspath(smp)]
  elif yes(rl.get("skip_sce_module", "no")):
    cmd.append("--skip-sce-module")
  for d in split_list(rl.get("module_dirs", "")):
    cmd += ["--module-dir", d]
  for e in split_list(rl.get("executables", "")):
    cmd += ["--executable", e]
  if timeout:
    cmd += ["--timeout-seconds", timeout]
  if list_all:
    cmd.append("--list-verified")
  try:
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                         timeout=900, **kw)
  except (OSError, subprocess.SubprocessError) as e:
    return "err", f"Could not run the advisor: {e}", ""
  out = "\n".join(x for x in ((res.stdout or "").strip(), (res.stderr or "").strip()) if x)
  patch = ""
  if res.returncode == 0 and not list_all:
    for line in out.splitlines():  # "  --patches <file> [--module-dir ...]" printed under "Use these options"
      if line.strip().startswith("--patches"):
        try:
          parts = shlex.split(line.strip(), posix=(os.name != "nt"))
        except ValueError:
          break
        if len(parts) >= 2:
          patch = parts[1]
          if not os.path.isabs(patch):
            patch = os.path.join(catalog, patch)
        break
  if res.returncode == 0:
    return "ok", out or "Verified.", patch
  if res.returncode == 1:
    return "warn", "No single verified patch for this game:\n" + out, ""
  return "err", "The advisor could not finish:\n" + out, ""


def find_nid_patcher(rl):
  p = rl.get("nid_patcher_path", "").strip()
  if p:
    return os.path.abspath(p) if os.path.isfile(p) else None
  paths = source_paths(rl) if rl.get("relinker_source") == "github" else None
  if paths and os.path.isdir(paths["build"]):
    found = find_built_relinker([(paths["build"], False)], "nid_patcher")
    if found:
      return found
  local = os.path.join(DATA_DIR, "nid_patcher.exe" if os.name == "nt" else "nid_patcher")
  if os.path.isfile(local):  # imported with Local files
    return local
  return shutil.which("nid_patcher")


def run_nid_patcher(rl, files):
  """`nid_patcher --exec [--only-sce] [--preserve-*] <files>`: patches the imports of the files IN PLACE."""
  exe = find_nid_patcher(rl)
  if not exe:
    return "err", "nid_patcher not found: set its path above, or build the repository (Source build)."
  files = [f for f in files if f]
  if not files:
    return "warn", "No file chosen."
  missing = [f for f in files if not os.path.isfile(f)]
  if missing:
    return "err", f"File not found: {missing[0]}"
  pe, pl = rl.get("nid_preserve_exports", "").strip(), rl.get("nid_preserve_list", "").strip()
  if pe and pl:
    return "err", "--preserve-exports and --preserve-export-list cannot be used together."
  if pl and not os.path.isfile(pl):
    return "err", f"Export list not found: {pl}"
  cmd = [exe, "--exec"]
  if yes(rl.get("nid_only_sce", "no")):
    cmd.append("--only-sce")
  if pe:
    cmd += ["--preserve-exports", os.path.abspath(pe) if os.path.exists(pe) else pe]
  elif pl:
    cmd += ["--preserve-export-list", os.path.abspath(pl)]
  cmd += files
  kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
  try:
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, **kw)
  except (OSError, subprocess.SubprocessError) as e:
    return "err", f"Could not run nid_patcher: {e}"
  out = "\n".join(x for x in ((res.stdout or "").strip(), (res.stderr or "").strip()) if x)
  if res.returncode == 0:
    return "ok", f"Imports patched in {len(files)} file(s).\n" + out if out else f"Imports patched in {len(files)} file(s)."
  return "err", f"nid_patcher failed (code {res.returncode}). Unsupported layouts fail before the file is written.\n" + out


def verify_output(rl, output_path):
  """Run `relinker --verify-patches <manifest> --verify-output [--executable ..] <output>` (writes nothing).
  output_path = a converted Linux ELF; the manifest is the <output-stem>.patches.toml written beside it.
  Returns (level, text)."""
  if not output_path or not os.path.isfile(output_path):
    return "warn", "Choose the converted Linux executable first."
  output_path = os.path.abspath(output_path)
  if file_state(output_path) != "elf":
    return "err", "This is not a converted Linux ELF (PE and Mach-O outputs cannot be verified)."
  stem = os.path.splitext(output_path)[0]
  manifest = next((m for m in (stem + ".patches.toml", output_path + ".patches.toml") if os.path.isfile(m)), "")
  if not manifest:
    return "err", (f"No manifest found: expected {os.path.basename(stem)}.patches.toml next to the output. "
                   "It is written only when at least one patch was applied during the conversion.")
  set_active_relinker_cfg(rl)
  relinker = resolve_relinker(rl)
  if not relinker or not os.path.isfile(relinker):
    return "err", "Relinker not available yet: run a conversion once (or build it) so the app has it."
  cmd = [relinker, "--verify-patches", manifest, "--verify-output"]
  for e in split_list(rl.get("executables", "")):
    cmd += ["--executable", e]
  cmd.append(output_path)
  kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
  try:
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                         timeout=120, **kwargs)
  except (OSError, subprocess.SubprocessError) as e:
    return "err", f"Could not run the relinker: {e}"
  out = "\n".join(x for x in ((res.stdout or "").strip(), (res.stderr or "").strip()) if x)
  if res.returncode == 0:
    return "ok", "Output OK: no mismatch.\n" + out if out else "Output OK: no mismatch."
  if res.returncode == 1:
    return "err", "Output mismatch (the converted file does not hold the patches):\n" + out
  return "err", "Verification failed:\n" + out


def anyps5_build_info(rl):
  """Which AnyPS5 build the relinker comes from: release tag or source commit (for conversion_info.json)."""
  info = {"anyps5_source": rl.get("relinker_source", "") or "custom"}
  try:
    if rl.get("relinker_source") == "release":
      cur = read_release_current(rl)
      if cur:
        info["anyps5_release_tag"] = cur["tag"]
      info["anyps5_repo"] = normalize_repo_url(rl.get("repo_url", ""))
    elif rl.get("relinker_source") == "github":
      paths = source_paths(rl)
      built = read_source_build(paths) if paths else None
      if built:
        info["anyps5_commit"] = built["commit"]
      if paths:
        info["anyps5_repo"] = paths["url"]
        info["anyps5_ref"] = paths["ref"]
  except Exception as _exc:
    hidden_error("anyps5_build_info", _exc)
    pass
  return info


def relinker_version(rl):
  """Run `relinker --version`. Returns (level, text)."""
  set_active_relinker_cfg(rl)
  relinker = resolve_relinker(rl)
  if not relinker or not os.path.isfile(relinker):
    return "warn", "Relinker not available yet: run a conversion once (or build it) so the app has it."
  kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
  try:
    res = subprocess.run([relinker, "--version"], capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=30, **kwargs)
  except (OSError, subprocess.SubprocessError) as e:
    return "err", f"Could not run the relinker: {e}"
  out = "\n".join(x for x in ((res.stdout or "").strip(), (res.stderr or "").strip()) if x)
  if res.returncode != 0:
    return "err", "This relinker does not answer --version (it may be an older build):\n" + out
  return "ok", out or "(no version printed)"


_RELINKER_HELP_CACHE = {}


def parse_relinker_help(text):
  """The options (--name) mentioned in the output of `relinker --help`."""
  return set(re.findall(r"(?<![\w-])--[A-Za-z][\w-]*", text or ""))


def relinker_help_flags(relinker):
  """Options listed by `relinker --help`, as a set. None when this build does not answer in a way we can read
  (older build, not a relinker...): in that case nothing is disabled. Cached until the file changes."""
  try:
    st = os.stat(relinker)
  except OSError:
    return None
  key = (relinker, st.st_mtime_ns, st.st_size)
  if key in _RELINKER_HELP_CACHE:
    return _RELINKER_HELP_CACHE[key]
  flags = None
  kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
  try:
    res = subprocess.run([relinker, "--help"], capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=15, **kwargs)
    found = parse_relinker_help((res.stdout or "") + "\n" + (res.stderr or ""))
    if res.returncode == 0 and len(found) >= 3:  # a real help text lists many options
      flags = found
  except (OSError, subprocess.SubprocessError) as e:
    hidden_error("relinker --help", e)
  if len(_RELINKER_HELP_CACHE) > 20:
    _RELINKER_HELP_CACHE.clear()
  _RELINKER_HELP_CACHE[key] = flags
  return flags


def relinker_capabilities(rl):
  """Options the relinker that would be used right now understands: {"known": bool, "flags": [...]}."""
  set_active_relinker_cfg(rl)
  relinker = resolve_relinker(rl)
  flags = relinker_help_flags(relinker) if relinker and os.path.isfile(relinker) else None
  return {"known": flags is not None, "flags": sorted(flags or [])}


def unsupported_relinker_flags(relinker, intel, rl):
  """Options chosen in the interface that this relinker build does not list in --help (empty if unknown)."""
  flags = relinker_help_flags(relinker) if relinker and os.path.isfile(relinker) else None
  if flags is None:
    return []
  cmd = build_relink_command("relinker", intel, dict(rl, extra_args=""), "in", "out")
  return sorted({a for a in cmd if a.startswith("--") and a not in flags})
