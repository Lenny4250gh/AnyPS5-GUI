"""Relink: the conversion itself (dry run, relink, import audit)."""

import json
import os
import re
import shutil
import subprocess
import time

from .settings import (
    AUDIT_SCRIPT, BACKUP_DIR_NAME, Cancelled, COPY_WORKERS, DATA_DIR, DEFAULT_OUTPUT_DIR, EXECUTABLE_NAME,
    FONTS_DIR_NAME, hidden_error, INPUT_INI_FILE, INPUT_INI_NAME, LIBS_DIR_NAME, LIBS_ZIP_PREFIX,
    OUTPUT_NEXT_TO_APP0, RELINKER_PREFIX, SCRIPT_DIR, TARGETS, UNWRAP_SCRIPT_NAME
)
from .config import yes
from .util import _remove_quiet, check_cancel, dir_size, file_state, is_inside, package_dir, split_list
from .repo import set_active_relinker_cfg
from .input_ini import validate_input_ini
from .process import stream_process
from .unwrapper import unwrapper_status
from .files import (
    cancellable_copy, cancellable_move, check_module_dirs, effective_module_dirs, existing_module_dirs,
    extract_libs, find_module_conflicts, find_python, find_relinker, get_backup_dir, libs_label,
    libs_zip_info, module_files, move_root_prx, Mover, wants_built_libs
)
from .disk import log_space, space_requirements
from .release import ensure_release_assets
from .source_build import (
    ensure_repo_unwrapper, ensure_source_libs, ensure_source_relinker, log_tool_source, resolve_relinker,
    source_preflight
)
from .runtime import create_shortcut, runtime_env, validate_runtime_options
from .relink_options import (
    anyps5_build_info, build_relink_command, relinker_version, unsupported_relinker_flags,
    validate_relink_options
)
from .basic_ops import _remove_empty_dirs, log_results, move_to_trash, open_trash, ST_ERROR, unwrap_game


def do_relink(folder, cfg, log, cancel, holder):
  """Wrapper: if the relink fails or is cancelled, the created folder goes to the trash.
  With keep_game = no the game is moved: on failure/cancel the files are put back first."""
  created = []
  ok = False
  move = not yes(cfg["settings"].get("keep_game", "yes"))
  mover = None
  game_name = os.path.basename(os.path.normpath(folder))
  if move:
    s = cfg["settings"]
    out_base = s["output_dir"] or DEFAULT_OUTPUT_DIR
    pkg = package_dir(out_base, game_name, cfg["relinker"]["target"])
    mover = Mover(folder, os.path.join(pkg, "app0"))
    log("info", "'Keep PS5 game' is OFF: the game will be MOVED (safe mode).")
  try:
    ok = _do_relink(folder, cfg, log, cancel, holder, created, mover)
    if ok and mover is not None:
      _remove_empty_dirs(folder)
      log("ok", "Source folder cleaned up." if not os.path.isdir(folder)
          else "Source folder kept (some files could not be removed).")
    return ok
  finally:
    if not ok:
      safe_to_trash = True
      if mover is not None and mover.moved:
        log("step", "Restoring the game to its original folder")
        if mover.rollback(get_backup_dir(game_name), log):
          log("ok", f"{len(mover.moved)} files put back in {folder}")
        else:
          safe_to_trash = False
          log("err", "Restore incomplete: the package folder was NOT deleted, "
                     "the missing files are still inside it.")
      trashed = False
      for d in created:
        if safe_to_trash and os.path.isdir(d):
          if move_to_trash(d):
            trashed = True
            log("info", f"Incomplete folder moved to the trash: {d}")
          else:
            log("warn", f"Unable to move it to the trash, delete it by hand: {d}")
        elif os.path.isdir(d):
          log("warn", f"Folder left in place: {d}")
      if trashed and yes(cfg["settings"].get("open_trash", "yes")):
        if open_trash():
          log("info", "Trash folder opened (you can restore or empty it from there).")


def do_dry_run(folder, cfg, log, cancel, holder):
  """Conversion preview: `relinker --dry-run` converts in memory and lists the files it would create.
  Nothing is written, no folder is created and the game is not copied or unwrapped (USAGE.md, "Dry run")."""
  s, rl = cfg["settings"], dict(cfg["relinker"])
  set_active_relinker_cfg(rl)
  target = rl["target"]
  game_name = os.path.basename(os.path.normpath(folder))
  eboot = os.path.join(folder, EXECUTABLE_NAME)
  state = file_state(eboot)
  if state == "self":
    log("err", f"{EXECUTABLE_NAME} is still a SELF: the preview needs the unwrapped game, because unwrapping "
               "writes files. Run Unwrap only first, then try the preview again.")
    return False
  if state != "elf":
    log("err", f"{EXECUTABLE_NAME} not found or not a valid ELF in the game folder.")
    return False
  relinker = resolve_relinker(rl)
  if not relinker or not os.path.isfile(relinker):
    log("err", "Relinker not available yet: run a conversion once (or download / build it) so the app has it.")
    return False
  if yes(rl.get("autorun", "no")):
    log("info", "--autorun is ignored in the preview (the relinker does not allow it with --dry-run).")
    rl["autorun"] = "no"
  smp = rl.get("sce_module_path", "").strip()
  modbase = os.path.abspath(smp) if smp else folder
  extra_dirs = effective_module_dirs(modbase, rl, log, explicit_base=bool(smp))
  rl["module_dirs"] = ";".join(extra_dirs)
  if not yes(rl.get("skip_sce_module", "no")):
    conflicts = find_module_conflicts(modbase, extra_dirs)
    if conflicts:
      log("err", "Modules with the same name in different folders (the relinker would stop):")
      for name, ds in conflicts:
        log("err", f"  {name}: " + ", ".join(ds))
      return False
  output_base = s["output_dir"] or DEFAULT_OUTPUT_DIR
  pkg_dir = package_dir(output_base, game_name, target)
  out_exe = os.path.join(pkg_dir, TARGETS[target]["exe"])
  cmd = build_relink_command(relinker, yes(s["intel"]), rl, eboot, out_exe)
  bad = [a for a in cmd if a in ("--autorun", "--verify-patches")]
  if bad:
    log("err", f"{bad[0]} (from the extra arguments) cannot be used with the preview: remove it and try again.")
    return False
  cmd.insert(1, "--dry-run")
  log("step", "Running the relinker in preview mode (nothing is written)")
  log("detail", "Command: " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
  planned = []

  def sink(kind, text):
    text = str(text)
    if text.startswith("Would write: "):
      planned.append(text[len("Would write: "):].strip())
    elif not text.startswith("No output files were written"):
      log(kind, text)

  try:
    code = stream_process(cmd, sink, cancel, holder, "Relinker", stdin=subprocess.DEVNULL)
  except Cancelled:
    raise
  except Exception as e:
    log("err", f"Unable to start the relinker: {e}")
    return False
  if code != 0:
    meaning = {1: "invalid arguments", 2: "conversion failed (see below)"}.get(code, "")
    log("err", f"The relinker exited with code {code}" + (f": {meaning}." if meaning else "."))
    for line in holder.get("tail", [])[-25:]:
      log("err" if re.search(r"error|fail|cannot|unable|missing|unsupported|invalid", line, re.I) else "info", line)
    return False
  log("step", f"Files that would be created ({len(planned)})")
  root = os.path.normpath(pkg_dir)
  for p in planned:
    try:
      rel = os.path.relpath(os.path.normpath(p), root)
    except ValueError:
      rel = p
    log("detail", p if rel.startswith("..") else rel)
  log("detail", f"Relative to: {pkg_dir}")
  log("info", "The game files, the libs folder and the other files copied by a real conversion are not in this "
              "list: only what the relinker itself creates.")
  try:
    if any(n.lower().endswith(".prx") for n in os.listdir(folder)) and yes(rl.get("move_root_prx", "yes")):
      log("info", "The .prx files next to eboot.bin are moved into the module folder only in a real conversion, "
                  "so the preview may list fewer modules.")
  except OSError:
    pass
  log("ok", "Preview complete: nothing was written, no folder was created.")
  return True


def _do_relink(folder, cfg, log, cancel, holder, created, mover=None):
  s, rl, rt = cfg["settings"], cfg["relinker"], cfg["runtime"]
  set_active_relinker_cfg(rl)
  game_name = os.path.basename(os.path.normpath(folder))
  auto = yes(s["auto_unwrap"])
  intel = yes(s["intel"])
  target = rl["target"]
  info = TARGETS[target]
  skip_modules = yes(rl.get("skip_sce_module", "no"))
  smp = rl.get("sce_module_path", "").strip()
  modbase = os.path.abspath(smp) if smp else folder  # parent of the module folders
  extra_dirs = effective_module_dirs(modbase, rl, log, explicit_base=bool(smp))
  rl = dict(rl, module_dirs=";".join(extra_dirs))  # the relinker command also uses these
  if extra_dirs:
    log("info", "Module folders passed to --module-dir: " + ", ".join(extra_dirs))
  conflicts = [] if skip_modules else find_module_conflicts(modbase, extra_dirs)
  if conflicts:
    log("err", "Modules with the same name in different folders (the relinker would stop with "
               "'Conflicting guest output filename'):")
    for name, ds in conflicts:
      log("err", f"  {name}: " + ", ".join(ds))
    log("info", "Remove the unwanted folder from '--module-dir' (Conversion options) or delete the duplicate "
                "from the game folder, then retry.")
    return False
  executables = split_list(rl["executables"])
  if yes(rl.get("lazy_binding", "no")) and not skip_modules and (existing_module_dirs(modbase) or extra_dirs):
    log("err", "--lazy-binding is incompatible with bundled ELF modules (USAGE.md), and this game has some. "
               "Turn off lazy binding (Conversion options).")
    return False

  # 1. Requirements
  log("step", "[1] Checking requirements")
  problem = validate_relink_options(intel, rl) or validate_runtime_options(rt, rl)
  if problem:
    log("err", f"Invalid options: {problem}")
    return False
  log("ok", f"Target system: {info['label']}")

  use_source = rl.get("relinker_source") == "github"
  if use_source:
    problem = source_preflight(rl)
    if problem:
      log("err", problem)
      return False
    log_tool_source(rl, log)
    log("ok", "Relinker: will be built from the repository above")
    relinker = None  # built after the quick checks below
  else:
    if rl.get("relinker_source") == "release":
      log("step", "[1a] Downloading the files of the GitHub release")
      if not ensure_release_assets(rl, log, cancel, target, need=("relinker", "libs", "unwrap", "docs") if auto
                                   else ("relinker", "libs", "docs")):
        return False
      check_cancel(cancel)
      log_tool_source(rl, log)
    relinker = find_relinker()
    if not relinker:
      log("err", f"No '{RELINKER_PREFIX}*' found in the app data folder.")
      log("detail", f"Searched in: {DATA_DIR}")
      return False
    log("ok", f"Relinker: {os.path.basename(relinker)}")

  build_libs = wants_built_libs(rl)
  libs = None
  if build_libs:
    log("ok", "Libraries: will be built from the GitHub repository (target 'libs')")
  else:
    libs = libs_zip_info(target)
    if not libs:
      log("err", f"No '{LIBS_ZIP_PREFIX}*.zip' containing the '{LIBS_DIR_NAME}' folder found in the app data folder.")
      log("detail", f"Put the AnyPS5 libs zip downloaded from GitHub (its name starts with '{LIBS_ZIP_PREFIX}', no need to extract it) in: {DATA_DIR}")
      return False
    log("ok", f"Libraries: {os.path.basename(libs['zip'])} → {libs['name']}/ ({len(libs['entries'])} files)")

  state = file_state(os.path.join(folder, EXECUTABLE_NAME))
  if state == "missing":
    log("err", f"{EXECUTABLE_NAME} not found in the game folder.")
    return False
  if state == "self" and auto:
    log("info", f"{EXECUTABLE_NAME} is a SELF: it will be unwrapped on the copy.")
  elif state == "self":
    log("err", f"{EXECUTABLE_NAME} is a SELF, not an ELF.")
    log("info", "Enable 'Automatic unwrap' or run Unwrap mode first.")
    return False
  elif state != "elf":
    log("err", f"{EXECUTABLE_NAME} is not a valid ELF (still encrypted / protected?).")
    return False
  else:
    log("ok", f"{EXECUTABLE_NAME} is a valid ELF")

  if not os.path.isdir(os.path.join(folder, "sce_sys")):
    log("err", "Folder 'sce_sys' not found in the game folder.")
    return False
  log("ok", "Folder 'sce_sys' found")
  if skip_modules:
    log("warn", "--skip-sce-module: bundled modules and the module folder checks are disabled "
                "(debug only: the game will be extremely unstable).")
  else:
    problem = check_module_dirs(modbase)
    if problem:
      log("err", f"Modules: {problem}.")
      if smp:
        log("detail", f"Searched in: {modbase}")
      return False
    log("ok", "Module folders: " + ", ".join(existing_module_dirs(modbase)))
    if smp:
      log("info", f"--sce-module-path: the modules are read from {modbase}")
    if yes(rl.get("lazy_binding", "no")) and existing_module_dirs(modbase):
      log("err", "--lazy-binding is incompatible with bundled ELF modules: disable it.")
      return False
    excl = split_list(rl.get("exclude_sce_modules", ""))
    if excl:
      known = {os.path.basename(rel) for rel, _p in module_files(modbase, extra_dirs)}
      missing = [n for n in excl if n not in known]
      if missing:
        log("err", "--exclude-sce-module: file not found in the module folders: " + ", ".join(missing))
        return False
  for e in executables:
    if not os.path.isfile(os.path.join(modbase, e)):
      log("err", f"--executable: '{e}' does not exist in " + ("the module parent folder." if smp else "the game folder."))
      return False
  if auto and not use_source and unwrapper_status() is None:
    log("err", f"'{UNWRAP_SCRIPT_NAME}' not found in the app data folder: it is required for automatic unwrap.")
    return False
  check_cancel(cancel)

  if use_source:
    log("step", "[1b] Building the relinker from the GitHub source")
    relinker = ensure_source_relinker(rl, log, cancel, holder)
    if not relinker:
      return False
    log("ok", f"Relinker: {os.path.basename(relinker)} (from source)")
    check_cancel(cancel)
    if auto and not ensure_repo_unwrapper(rl, log, cancel, holder):
      return False
    if build_libs:
      log("step", "[1c] Building the libs from the GitHub source")
      libs = ensure_source_libs(rl, log, cancel, holder, target)
      if not libs:
        return False
      log("ok", f"Libraries: {len(libs['entries'])} files built")
      if target == "windows":
        if any(e[1].lower().endswith(".dll") for e in libs["entries"]):
          log("ok", "Windows target: MinGW runtime DLLs included in libs")
        else:
          log("warn", "Windows target: MinGW runtime DLLs (WinLibs) not available on this system: "
                      "they will be missing from the libs folder.")
      check_cancel(cancel)

  # 2. Output
  output_base = s["output_dir"] or DEFAULT_OUTPUT_DIR
  if not s["output_dir"]:
    try:
      os.makedirs(output_base, exist_ok=True)  # default "Converted games" folder
    except OSError:
      pass
  if not os.path.isdir(output_base):
    log("err", f"Invalid output folder: {output_base}")
    return False
  pkg_dir = package_dir(output_base, game_name, target)
  app0_dir = os.path.join(pkg_dir, "app0")
  log("step", "[2] Creating the output folder (the game goes into its 'app0' subfolder)")
  log("detail", pkg_dir)
  if is_inside(pkg_dir, folder):
    log("err", "The output folder is INSIDE the game folder, so the tool would keep copying "
               "its own output forever (the size would grow past 100% and never finish).")
    log("info", "Fix: choose an output folder OUTSIDE the game folder, then start again.")
    log("detail", f"Game folder:   {folder}")
    log("detail", f"Output folder: {output_base}")
    return False
  try:
    if not log_space(space_requirements(folder, target, "relink", output_base, auto,
                                        move=mover is not None), log):
      log("err", "Not enough disk space: free some space or choose another output folder.")
      return False
  except OSError as e:
    log("warn", f"Unable to check the disk space: {e}")
  if os.path.exists(pkg_dir):
    log("info", "Removing the existing folder (confirmed)...")
    try:
      shutil.rmtree(pkg_dir)
    except Exception as e:
      log("err", f"Unable to remove the folder: {e}")
      return False
  os.makedirs(pkg_dir)
  created.append(pkg_dir)
  log("ok", "Output folder created.")
  check_cancel(cancel)

  # 3. Copy the game
  if mover is not None:
    log("step", "[3] Moving the game files into 'app0' (each original is deleted only after its new copy is checked)")
    copier = cancellable_move(mover, cancel, log, dir_size(folder))
  else:
    log("step", "[3] Copying the game files into 'app0' (your original game folder is not touched)")
    copier = cancellable_copy(cancel, log, dir_size(folder), sync=False, workers=COPY_WORKERS)
  try:
    shutil.copytree(folder, app0_dir, copy_function=copier)
    copier.finish()
  except Cancelled:
    copier.close() if hasattr(copier, "close") else None
    raise
  except Exception as e:
    copier.close() if hasattr(copier, "close") else None
    log("err", f"Copy failed: {e}")
    return False
  if mover is not None:
    log("ok", f"Game moved into 'app0' ({len(mover.moved)} files)."
              + (f" {mover.leftovers} files could not be removed from the source." if mover.leftovers else ""))
  else:
    log("ok", "Game copied and renamed 'app0'.")
  check_cancel(cancel)

  # 3b. .prx files next to eboot.bin -> module folder (on the copy only)
  if yes(rl.get("move_root_prx", "yes")) and not skip_modules and not smp:
    if mover is not None:
      log("info", "Move mode: the .prx files in the game root are left where they are "
                  "(they are moved only when the game is copied, so the original is never changed).")
    else:
      log("step", "[3b] Moving the .prx files next to eboot.bin into the module folder (on the copy)")
      move_root_prx(app0_dir, log)
      check_cancel(cancel)

  # 4. Unwrap on the copy (optional)
  if auto:
    log("step", "[4] Automatic unwrap (on the copy in app0)")
    if mover is not None:
      log("info", f"Move mode: the original SELF files are saved in {BACKUP_DIR_NAME}/{game_name}.")
    if smp:
      log("warn", "--sce-module-path: the modules in that folder are not unwrapped automatically "
                  "(unwrap them first with Unwrap mode).")
    results = unwrap_game(app0_dir, get_backup_dir(game_name) if mover is not None else None, log, cancel,
                          with_modules=yes(rl["unwrap_modules"]) and not skip_modules and not smp,
                          extra_dirs=[] if smp else extra_dirs,
                          executables=[] if smp else executables)
    log_results(results, log)
    if any(r[1][0] == ST_ERROR for r in results):
      log("err", "Unwrap failed: relink cancelled.")
      return False
    if file_state(os.path.join(app0_dir, EXECUTABLE_NAME)) != "elf":
      log("err", f"After the unwrap {EXECUTABLE_NAME} is not an ELF: relink cancelled.")
      return False
  else:
    log("step", "[4] Automatic unwrap disabled: skipping")
  check_cancel(cancel)

  # 5. Libs, fonts, input
  log("step", f"[5] Copying '{LIBS_DIR_NAME}' and the optional files")
  try:
    n = extract_libs(libs, os.path.join(pkg_dir, LIBS_DIR_NAME), cancel)
  except Cancelled:
    raise
  except Exception as e:
    log("err", f"Extracting '{LIBS_DIR_NAME}' failed: {e}")
    return False
  log("ok", f"'{LIBS_DIR_NAME}' extracted from {libs_label(libs)} ({n} files).")
  fonts_src = os.path.join(DATA_DIR, FONTS_DIR_NAME)
  if os.path.isdir(fonts_src):
    try:
      shutil.copytree(fonts_src, os.path.join(pkg_dir, FONTS_DIR_NAME))
      log("ok", f"'{FONTS_DIR_NAME}' copied.")
    except Exception as e:
      log("warn", f"Copying '{FONTS_DIR_NAME}' failed: {e}")
  else:
    log("detail", f"No '{FONTS_DIR_NAME}' folder: if the game uses system fonts, add it to the app data folder.")
  if yes(rt["copy_input_ini"]) and os.path.isfile(INPUT_INI_FILE):
    try:
      with open(INPUT_INI_FILE, encoding="utf-8-sig") as f:
        text = f.read()
      errs = validate_input_ini(text)
      if errs:
        log("warn", f"{INPUT_INI_NAME} not copied: line {errs[0][0]}: {errs[0][1]}")
      else:
        shutil.copy2(INPUT_INI_FILE, os.path.join(pkg_dir, INPUT_INI_NAME))
        log("ok", f"{INPUT_INI_NAME} copied into the package.")
    except Exception as e:
      log("warn", f"Copying {INPUT_INI_NAME} failed: {e}")
  check_cancel(cancel)

  # 6. Relinker
  out_dir = pkg_dir if OUTPUT_NEXT_TO_APP0 else app0_dir
  out_exe = os.path.join(out_dir, info["exe"])
  input_file = os.path.join(app0_dir, EXECUTABLE_NAME)
  unsupported = unsupported_relinker_flags(relinker, intel, rl)
  if unsupported:
    log("err", "This relinker does not know: " + ", ".join(unsupported) + " (not listed by 'relinker --help').")
    log("info", "Turn these options off (Conversion options), or use a relinker build that has them.")
    return False
  cmd = build_relink_command(relinker, intel, rl, input_file, out_exe)
  log("step", "[6] Running the relinker")
  log("detail", "Command: " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
  autorun = yes(rl.get("autorun", "no"))
  run_env = None
  if autorun:
    run_env = dict(os.environ)
    run_env.update(runtime_env(rt, target, pkg_dir))
    log("info", "--autorun: after the conversion the relinker starts the game (with the Runtime settings) "
                "and this step ends when the game exits.")
  try:
    code = stream_process(cmd, log, cancel, holder, "Relinker", env=run_env,
                          stdin=subprocess.DEVNULL if autorun else None)
  except Cancelled:
    raise
  except Exception as e:
    log("err", f"Unable to start the relinker: {e}")
    return False

  if code != 0 and autorun and os.path.isfile(out_exe):
    # with --autorun the exit code is the one of the game: the conversion itself worked
    log("warn", f"The game exited with code {code}.")
    code = 0
  if code != 0:
    meaning = {1: "invalid arguments", 2: "conversion failed (see below)"}.get(code, "")
    log("err", f"The relinker exited with code {code}" + (f": {meaning}." if meaning else "."))
    log("step", "Last relinker output")
    for line in holder.get("tail", [])[-25:]:
      log("err" if re.search(r"error|fail|cannot|unable|missing|unsupported|invalid", line, re.I) else "info", line)
    return False
  if not os.path.isfile(out_exe):
    log("warn", f"Relinker finished, but {info['exe']} was not found in the output path.")
    return False
  log("ok", f"{info['exe']} created.")
  check_cancel(cancel)

  # Relinker version used for this conversion: logged and saved in the package (useful for support)
  try:
    _lv, _vt = relinker_version(cfg["relinker"])
    _vt = (_vt or "").strip()
    log("info" if _lv == "ok" else "warn", "Relinker version: " + (_vt.splitlines()[0] if _vt else "unknown"))
    with open(os.path.join(pkg_dir, "conversion_info.json"), "w", encoding="utf-8") as _f:
      json.dump({"relinker_version": _vt, "relinker_version_ok": _lv == "ok", "target": target,
                 **anyps5_build_info(cfg["relinker"]),
                 "game": game_name, "converted_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                _f, indent=2, ensure_ascii=False)
  except Exception as _e:
    log("warn", f"Could not save the relinker version: {_e}")

  # 7. Shortcut named after the game
  if yes(rl["make_launcher"]):
    log("step", "[7] Game shortcut")
    create_shortcut(out_dir, info["exe"], target, rt, game_name, log, SCRIPT_DIR)

  # system warnings
  if (target not in ("windows", "macos") and os.name != "nt"
      and os.path.exists("/dev/udmabuf")
      and not os.access("/dev/udmabuf", os.R_OK | os.W_OK)):
    log("warn", "/dev/udmabuf is not accessible: add your user to the kvm group and log in again "
                "(or: setfacl -m u:$USER:rw /dev/udmabuf). Not needed with the proprietary NVIDIA driver.")
  if target == "windows":
    log("detail", "Windows: direct memory (up to 13824 MiB) counts toward the commit limit. "
                  "If 'replace guest placeholder with resident direct memory' appears, enlarge the page file.")

  # 8. Import audit
  if yes(rl["import_audit"]):
    log("step", "[8] Import audit")
    run_import_audit(out_exe, app0_dir, pkg_dir, rl, log, cancel, holder)

  log("step", "Summary")
  log("detail", f"Game:       {game_name}")
  log("detail", f"Package:    {pkg_dir}")
  log("detail", f"Structure:  app0/  {LIBS_DIR_NAME}/")
  log("detail", f"Executable: {out_exe}")
  return True


def run_import_audit(out_exe, app0_dir, pkg_dir, rl, log, cancel, holder):
  script = os.path.join(DATA_DIR, AUDIT_SCRIPT)
  if not os.path.isfile(script):
    log("warn", f"{AUDIT_SCRIPT.replace(os.sep, '/')} not found in the app data folder: audit skipped.")
    return
  if not yes(rl["registry"]):
    log("warn", "The audit requires --registry: enable it in the options. Audit skipped.")
    return
  python = find_python()
  if not python:
    log("warn", "Python not found in PATH: audit skipped.")
    return
  stem = os.path.splitext(os.path.basename(out_exe))[0]
  out_dir = os.path.dirname(out_exe)
  registries = sorted(f for f in os.listdir(out_dir)
                      if f.startswith(stem) and f.endswith(".registry.json"))
  if not registries:
    log("warn", "No .registry.json file found: audit skipped.")
    return
  cmd = [python, script] + [os.path.join(out_dir, r) for r in registries]
  cmd += ["--libs", os.path.join(pkg_dir, LIBS_DIR_NAME)]
  smp = rl.get("sce_module_path", "").strip()
  mod_base = os.path.abspath(smp) if smp else app0_dir
  if not yes(rl.get("skip_sce_module", "no")):
    for d in existing_module_dirs(mod_base):
      cmd += ["--modules", os.path.join(mod_base, d)]
  names = rl.get("names_file", "").strip()
  if names:
    if os.path.isfile(names):
      cmd += ["--names", names]
    else:
      log("warn", f"Names file not found ({names}): audit without --names.")
  if yes(rl.get("audit_strict_stubs", "no")):
    cmd.append("--strict-stubs")
  if yes(rl.get("audit_strict_providers", "no")):
    cmd.append("--strict-providers")
  src = rl.get("audit_source", "").strip()
  if src:
    if os.path.isdir(src):
      cmd += ["--source", src]
    else:
      log("warn", f"Audit source folder not found ({src}): audit without --source.")
  json_path = os.path.join(out_dir, stem + ".audit.json")
  _remove_quiet(json_path)
  cmd += ["--json", json_path]
  try:
    code = stream_process(cmd, log, cancel, holder, "Audit", cwd=DATA_DIR)
  except Cancelled:
    raise
  except Exception as e:
    log("warn", f"Audit could not be run: {e}")
    return
  meaning = {0: "nothing blocks loading", 1: "there are 'absent' imports or missing libraries",
             2: "an input is not readable"}.get(code, "")
  if code == 1 and (yes(rl.get("audit_strict_stubs", "no")) or yes(rl.get("audit_strict_providers", "no"))):
    meaning += " (or stub / other-library imports, because of the strict options)"
  log("ok" if code == 0 else "warn", f"Audit finished with code {code}: {meaning}")
  summary = summarize_audit(json_path)
  if summary:
    log("ok" if code == 0 else "warn", summary)


def _audit_find(node, key):
  """First value stored under `key` anywhere in the audit JSON (case-insensitive)."""
  if isinstance(node, dict):
    for k, v in node.items():
      if str(k).lower() == key:
        return v
    for v in node.values():
      r = _audit_find(v, key)
      if r is not None:
        return r
  elif isinstance(node, list):
    for v in node:
      r = _audit_find(v, key)
      if r is not None:
        return r
  return None


def summarize_audit(json_path):
  """One readable line from import_audit's --json: implemented / stub / absent / module. '' if unreadable."""
  try:
    with open(json_path, encoding="utf-8") as f:
      data = json.load(f)
  except Exception as _exc:
    hidden_error("summarize_audit", _exc)
    return ""
  counts = {}
  for key in ("implemented", "stub", "absent", "module"):
    v = _audit_find(data, key)
    if isinstance(v, (list, dict)):
      v = len(v)
    if isinstance(v, int) and not isinstance(v, bool):
      counts[key] = v
  if not counts:
    return ""
  text = (f"Audit: {counts.get('implemented', 0)} implemented, {counts.get('stub', 0)} stub "
          f"(load, but throw when called), {counts.get('absent', 0)} absent "
          f"(the game will not load), {counts.get('module', 0)} from the title's own modules.")
  if counts.get("absent"):
    names = _audit_find(data, "absent")
    if isinstance(names, list):
      shown = [str(n.get("name") or n.get("nid") or n) if isinstance(n, dict) else str(n) for n in names[:8]]
      text += " Absent: " + ", ".join(shown) + (" …" if len(names) > 8 else "")
  return text
