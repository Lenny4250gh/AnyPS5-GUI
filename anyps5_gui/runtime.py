"""Runtime: environment variables, validation of the Runtime options, input mapper, and game shortcut."""

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import zipfile

from .settings import DATA_DIR, hidden_error, INPUT_INI_FILE, INPUT_INI_NAME, OVERLAY_POSITIONS
from .config import yes
from .util import _rmtree_force, _safe_filename, check_cancel
from .repo import github_owner_repo, read_release_current, release_paths, source_paths
from .input_ini import validate_input_ini
from .release import _download_file
from .env_catalog import ENV_CATALOG


PAD_HELPER = os.path.join("tools", "pad_input.py")


def _whole(text):
  return re.fullmatch(r"[0-9]+", text.strip()) is not None


# Advanced runtime variables (docs/dev/ENVIRONMENT.md): key -> (variable, minimum, maximum, windows only)
ADVANCED_INTS = {
  "pipelined_draws": ("APS5_PIPELINED_DRAWS", 0, None, False),
  "pipeline_journal_entries": ("APS5_PIPELINE_JOURNAL_ENTRIES", 1, None, False),
  "pipeline_journal_max_mib": ("APS5_PIPELINE_JOURNAL_MAX_MIB", 1, None, False),
  "pipeline_journal_threads": ("APS5_PIPELINE_JOURNAL_THREADS", 0, None, False),
  "pipeline_store_entries": ("APS5_PIPELINE_STORE_ENTRIES", 1, None, False),
  "address_copy_max_mib": ("APS5_ADDRESS_COPY_MAX_MIB", 1, None, False),
  "pm4_ring": ("APS5_PM4_RING", 0, 4096, False),
  "gpu_wait_timeout_ms": ("APS5_GPU_WAIT_TIMEOUT_MS", 1, None, False),
  "stall_watchdog_secs": ("APS5_STALL_WATCHDOG_SECS", 0, 86400, False),
  "hang_dump_secs": ("APS5_HANG_DUMP_SECS", 1, None, True),
}
ADVANCED_FLAGS = (  # key -> variable (set to 1); the runtime reads their presence
  ("draw_pipeline", "APS5_DRAW_PIPELINE", False), ("pipeline_dispatch", "APS5_PIPELINE_DISPATCH", False),
  ("verify_draw_pipeline", "APS5_VERIFY_DRAW_PIPELINE", False), ("no_pipeline_lto", "APS5_NO_PIPELINE_LTO", False),
  ("no_pipeline_journal", "APS5_NO_PIPELINE_JOURNAL", False), ("skip_refused_draws", "APS5_SKIP_REFUSED_DRAWS", False),
  ("mimalloc", "APS5_MIMALLOC", True),
)


def _crash_stack_value(text):
  """Integer value of APS5_CRASH_STACK_BYTES (decimal or 0x hexadecimal), or None if malformed."""
  t = text.strip()
  if re.fullmatch(r"[0-9]+", t):
    return int(t)
  if re.fullmatch(r"0[xX][0-9a-fA-F]+", t):
    return int(t, 16)
  return None


def validate_advanced_runtime(rt):
  for key, (var, lo, hi, _win) in ADVANCED_INTS.items():
    v = rt.get(key, "").strip()
    if not v:
      continue
    if not _whole(v) or int(v) < lo or (hi is not None and int(v) > hi):
      return f"{var} must be a whole number" + (f" from {lo} to {hi}." if hi is not None else f" of {lo} or more.")
  cs = rt.get("crash_stack_bytes", "").strip()
  if cs:
    n = _crash_stack_value(cs)
    if n is None or n > 1048576:
      return "APS5_CRASH_STACK_BYTES must be a decimal or 0x hexadecimal number, at most 1048576."
  if yes(rt.get("verify_draw_pipeline", "no")) and not yes(rt.get("draw_pipeline", "no")):
    return "APS5_VERIFY_DRAW_PIPELINE needs APS5_DRAW_PIPELINE (it checks that pipeline)."
  return None


def validate_runtime_options(rt, rl=None):
  """Returns None if the Runtime settings are valid, otherwise a message.
  rl (relinker options) is optional: it enables the checks that depend on them."""
  pad = rt.get("pad_input_file", "").strip()
  if pad and not os.path.isabs(pad):
    return "APS5_PAD_INPUT_FILE must be an absolute path."
  lang = rt.get("language", "").strip()
  if lang and not (_whole(lang) and int(lang) <= 30):
    return "ANYPS5_LANGUAGE must be a console language number from 0 to 30."
  if rl is not None and rt.get("trace_calls", "").strip() and rl.get("target") == "windows" \
      and not yes(rl.get("trace_imports", "no")):
    return "APS5_TRACE_CALLS needs --trace-imports (Conversion options): without it nothing is recorded."
  prio = rt.get("thread_priority", "").strip()
  if prio and prio not in ("off", "high", "rt"):
    return "APS5_THREAD_PRIORITY must be off, high or rt."
  nice = rt.get("thread_nice", "").strip()
  if nice and not re.fullmatch(r"-?[0-9]+", nice):
    return "APS5_THREAD_NICE must be a whole number (default -10)."
  rtp = rt.get("thread_rt", "").strip()
  if rtp and not _whole(rtp):
    return "APS5_THREAD_RT must be a whole number (default 10)."
  frames = rt.get("trace_frames", "").strip()
  seconds = rt.get("trace_seconds", "").strip()
  if frames and seconds:
    return "APS5_TRACE_FRAMES and APS5_TRACE_SECONDS cannot be used together."
  if frames and not re.fullmatch(r"[0-9]+:[0-9]+", frames):
    return "APS5_TRACE_FRAMES must be <start>:<count>, whole numbers."
  if seconds:
    if not re.fullmatch(r"[0-9]+:[0-9]+", seconds) or int(seconds.split(":")[1]) < 1:
      return "APS5_TRACE_SECONDS must be <start>:<length>, whole numbers, length above 0."
  every = rt.get("trace_summary_every", "").strip()
  if every and not (_whole(every) and int(every) >= 1):
    return "APS5_TRACE_SUMMARY_EVERY must be a whole number above 0."
  events = rt.get("trace_events", "").strip()
  if events and not (_whole(events) and 1 <= int(events) <= 4194304):
    return "APS5_TRACE_EVENTS must be a whole number from 1 to 4194304."
  tdir = rt.get("trace_dir", "").strip()
  if tdir and not os.path.isabs(tdir):
    return "APS5_TRACE_DIR must be an absolute path."
  opos = rt.get("overlay_pos", "").strip()
  if opos and opos not in OVERLAY_POSITIONS:
    return "ANYPS5_OVERLAY_POS must be tl, tr, bl or br."
  oscale = str(rt.get("overlay_scale", "")).strip()
  if oscale and not (_whole(oscale) and 1 <= int(oscale) <= 8):
    return "ANYPS5_OVERLAY_SCALE must be a whole number from 1 to 8 (or empty for automatic)."
  sdir = rt.get("screenshots_dir", "").strip()
  if sdir and not os.path.isabs(sdir):
    return "ANYPS5_SCREENSHOTS must be an absolute path."
  spk = str(rt.get("pad_speaker_db", "0")).strip().lower()
  if spk not in ("", "mute", "off", "-inf"):
    try:
      if not -60 <= float(spk) <= 0:
        raise ValueError
    except ValueError:
      return "APS5_PAD_SPEAKER_FALLBACK_DB must be a number from -60 to 0, or mute."
  lf = rt.get("log_file", "").strip()
  if lf and (not os.path.isabs(lf) or os.path.isdir(lf)):
    return "ANYPS5_LOG_FILE must be an absolute path to a file, not a folder."
  adv_err = validate_advanced_runtime(rt)
  if adv_err:
    return adv_err
  if yes(rt.get("trace_bench", "no")) and not every:
    return "APS5_TRACE_BENCH=1 needs APS5_TRACE_SUMMARY_EVERY (e.g. 30): set the interval too."
  tcf = rt.get("trace_calls_file", "").strip()
  if tcf and not os.path.isabs(tcf):
    return "APS5_TRACE_CALLS_FILE must be an absolute path to a file."
  if tcf and not rt.get("trace_calls", "").strip():
    return "APS5_TRACE_CALLS_FILE needs APS5_TRACE_CALLS (and --trace-imports) to record anything."
  tel = rt.get("videoout_telemetry_dir", "").strip()
  if tel and not os.path.isabs(tel):
    return "APS5_VIDEOOUT_TELEMETRY_DIR must be an absolute path."
  disp = rt.get("display_index", "").strip()
  if disp and not _whole(disp):
    return "ANYPS5_DISPLAY_INDEX must be a whole number from 0 up (0 = first display)."
  upscale = rt.get("upscale", "").strip().lower()
  if upscale and upscale != "fsr":
    return "ANYPS5_UPSCALE must be empty or fsr."
  sharp = rt.get("fsr_sharpness", "").strip()
  if sharp:
    try:
      sv = float(sharp)
    except ValueError:
      return "ANYPS5_FSR_SHARPNESS must be a number from 0.0 to 2.0."
    if not (0.0 <= sv <= 2.0):
      return "ANYPS5_FSR_SHARPNESS must be a number from 0.0 to 2.0."
    if upscale != "fsr":
      return "ANYPS5_FSR_SHARPNESS has no effect without ANYPS5_UPSCALE=fsr."
  fps = rt.get("fps_limit", "").strip()
  if fps:
    try:
      fv = float(fps)
    except ValueError:
      return "ANYPS5_FPS_LIMIT must be 0 (no limit) or a number from 0.5 to 1000000."
    if fv != 0 and not (0.5 <= fv <= 1000000):
      return "ANYPS5_FPS_LIMIT must be 0 (no limit) or a number from 0.5 to 1000000."
  for key, label, hi in (("video_out_max_width", "ANYPS5_VIDEO_OUT_MAX_WIDTH", 3840),
                         ("video_out_max_height", "ANYPS5_VIDEO_OUT_MAX_HEIGHT", 2160)):
    v = rt.get(key, "").strip()
    if not v:
      continue
    if not _whole(v) or int(v) < 1:
      return f"{label} must be a positive whole number (below {hi} to hint the host resolution)."
  vol = str(rt.get("volume", "")).strip()
  if vol and not (_whole(vol) and 0 <= int(vol) <= 100):
    return "ANYPS5_VOLUME must be a whole number from 0 to 100."
  pad_id = str(rt.get("pad_identify", "")).strip().lower()
  if pad_id and pad_id not in ("off", "connect", "always"):
    return "ANYPS5_PAD_IDENTIFY must be off, connect or always."
  port0 = str(rt.get("pad_port0_state", "")).strip().lower()
  if port0 and port0 not in ("default", "controller"):
    return "ANYPS5_PAD_PORT0_STATE must be default or controller."
  date_fmt = str(rt.get("date_format", "")).strip().lower()
  if date_fmt and date_fmt not in ("0", "1", "2", "auto"):
    return "ANYPS5_DATE_FORMAT must be 0, 1, 2 or auto."
  time_fmt = str(rt.get("time_format", "")).strip().lower()
  if time_fmt and time_fmt not in ("0", "1", "auto"):
    return "ANYPS5_TIME_FORMAT must be 0, 1 or auto."
  free_sp = str(rt.get("free_space", "")).strip().lower()
  if free_sp and free_sp not in ("fixed", "host"):
    return "ANYPS5_FREE_SPACE must be fixed or host."
  open_urls = str(rt.get("open_urls", "")).strip().lower()
  if open_urls and open_urls not in ("never", "ask", "always"):
    return "ANYPS5_OPEN_URLS must be never, ask or always."
  psn = str(rt.get("psn_dialogs", "")).strip().lower()
  if psn and psn not in ("silent", "show"):
    return "ANYPS5_PSN_DIALOGS must be silent or show."
  aps5_rep = str(rt.get("aps5_report", "")).strip()
  if aps5_rep and aps5_rep not in ("0", "1"):
    return "APS5_REPORT must be 0 or 1."
  if yes(rt.get("stall_prompt", "no")) and not str(rt.get("stall_watchdog_secs", "")).strip():
    return "ANYPS5_STALL_PROMPT=1 needs APS5_STALL_WATCHDOG_SECS (set a stall watchdog interval)."
  ent = rt.get("entitlements_file", "").strip()
  if ent and not os.path.isfile(ent):
    return f"ANYPS5_ENTITLEMENTS: '{ent}' is not an existing file."
  return validate_catalog_env(rt)
ENV_CATALOG_KINDS = {n: k for n, k, _d, _t in ENV_CATALOG}


def catalog_env_values(rt):
  """Dict {VARIABLE: value} chosen in the 'All environment variables' panel (rt['env_catalog'] is JSON)."""
  raw = rt.get("env_catalog", "").strip()
  if not raw:
    return {}
  try:
    data = json.loads(raw)
  except ValueError:
    return None
  if not isinstance(data, dict):
    return None
  return {str(k): str(v) for k, v in data.items()}


def validate_catalog_env(rt):
  data = catalog_env_values(rt)
  if data is None:
    return "The list of extra environment variables is damaged: clear it in Runtime > All environment variables."
  for name, value in data.items():
    kind = ENV_CATALOG_KINDS.get(name)
    if kind is None:
      return f"{name} is not a documented environment variable."
    value = value.strip()
    if kind == "integer" and value and not re.fullmatch(r"-?[0-9]+|0[xX][0-9a-fA-F]+", value):
      return f"{name} must be a whole number (decimal or 0x hexadecimal)."
    if kind == "path" and value and not os.path.isabs(value):
      return f"{name} must be an absolute path."
  return None


def find_pad_helper(rl):
  """tools/pad_input.py in <data>/tools or in the cloned repository, else None."""
  candidates = [os.path.join(DATA_DIR, PAD_HELPER)]
  paths = source_paths(rl) if rl.get("relinker_source") == "github" else None
  if paths:
    candidates.append(os.path.join(paths["repo"], PAD_HELPER))
  for c in candidates:
    if os.path.isfile(c):
      return c
  return None


INPUT_MAPPER_REL = os.path.join("tools", "input-mapper")
INPUT_MAPPER_PORT = 4173


def find_input_mapper(rl):
  """tools/input-mapper in <data>/tools or in the cloned repository, else None. Returns (folder_to_run_from, mapper_dir)."""
  candidates = [DATA_DIR]
  paths = source_paths(rl) if rl.get("relinker_source") == "github" else None
  if paths:
    candidates.append(paths["repo"])
  for base in candidates:
    if os.path.isfile(os.path.join(base, INPUT_MAPPER_REL, "package.json")):
      return base, os.path.join(base, INPUT_MAPPER_REL)
  return None


def download_input_mapper(rl, log, cancel):
  """Release option: tools/input-mapper is not a release asset, so it is taken from the source
  archive of the release tag (else of the default branch) and unpacked into <data>/tools/input-mapper.
  Returns '' on success, otherwise the error text."""
  paths = release_paths(rl)
  gh = github_owner_repo(rl.get("repo_url", "")) if paths else None
  if not gh:
    return "The Release option needs a github.com repository URL to download the input mapper."
  owner, repo = gh
  cur = read_release_current(rl)
  refs = []
  if cur:
    refs.append((cur["tag"], "refs/tags/" + cur["tag"], cur["tag"]))
  refs.append(("HEAD", "HEAD", "the default branch"))
  prefix = "tools/input-mapper/"
  dest = os.path.join(DATA_DIR, INPUT_MAPPER_REL)
  tmp_dir = dest + ".part"
  os.makedirs(os.path.dirname(dest), exist_ok=True)
  for ref, path_ref, desc in refs:
    check_cancel(cancel)
    archive = os.path.join(DATA_DIR, "input-mapper-source.zip.part")
    url = "https://github.com/%s/%s/archive/%s.zip" % (owner, repo, path_ref)
    if not _download_file(url, archive, 0, "", log, cancel, "input-mapper (%s)" % desc, quiet=True):
      continue
    try:
      if os.path.isdir(tmp_dir):
        _rmtree_force(tmp_dir)
      found = False
      with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
          name = info.filename
          top, _, rest = name.partition("/")  # "<repo>-<tag>/tools/input-mapper/..."
          if not rest.startswith(prefix) or info.is_dir():
            continue
          rel = rest[len(prefix):]
          parts = rel.split("/")
          if not rel or any(x in ("", ".", "..") for x in parts) or ":" in rel or rel.startswith("/"):
            continue  # never write outside the mapper folder
          target = os.path.join(tmp_dir, *parts)
          os.makedirs(os.path.dirname(target), exist_ok=True)
          with z.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
          found = True
      if not found or not os.path.isfile(os.path.join(tmp_dir, "package.json")):
        continue  # this ref has no input mapper: try the default branch
      if os.path.isdir(dest):
        _rmtree_force(dest)
      os.replace(tmp_dir, dest)
      return ""
    except (OSError, zipfile.BadZipFile) as e:
      return "Unable to unpack the input mapper: %s" % e
    finally:
      try:
        os.remove(archive)
      except OSError:
        pass
      if os.path.isdir(tmp_dir):
        _rmtree_force(tmp_dir)
  return "tools/input-mapper was not found in the repository (release or default branch), or the download failed. Check the connection."


def install_input_mapper_deps(mapper_dir, npm):
  """npm install for the mapper if its package.json lists dependencies and node_modules is missing."""
  try:
    with open(os.path.join(mapper_dir, "package.json"), encoding="utf-8") as f:
      deps = (json.load(f) or {}).get("dependencies") or {}
  except (OSError, ValueError):
    return ""
  if not deps or os.path.isdir(os.path.join(mapper_dir, "node_modules")):
    return ""
  kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
  try:
    res = subprocess.run([npm, "install", "--omit=dev", "--no-audit", "--no-fund"], cwd=mapper_dir,
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, **kw)
  except (OSError, subprocess.SubprocessError) as e:
    return "npm install of the input mapper failed: %s" % e
  return "" if res.returncode == 0 else "npm install of the input mapper failed (exit code %d)." % res.returncode


def node_status():
  """(path, major, text): node executable, its major version and an error text ('' when Node 22+ is available)."""
  node = shutil.which("node")
  if not node:
    return None, 0, "Node.js is not installed. Install Node.js 22 or newer from nodejs.org, then try again."
  try:
    kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10, **kw).stdout.strip()
    major = int(re.match(r"v?(\d+)", out).group(1))
  except Exception as _exc:
    hidden_error("node_status", _exc)
    return node, 0, "Unable to read the Node.js version (node --version failed)."
  if major < 22:
    return node, major, f"Node.js {out} is too old: the input mapper needs Node.js 22 or newer."
  return node, major, ""


def sync_input_ini(pkg_dir, rt, log=None):
  """Copy <data>/anyps5-input.ini over the ini next to the game executable (pkg_dir).
  Called at every launch, so the game always uses the CURRENT Input tab settings."""
  if not yes(rt.get("copy_input_ini", "yes")) or not os.path.isfile(INPUT_INI_FILE):
    return False
  try:
    with open(INPUT_INI_FILE, encoding="utf-8-sig") as f:
      errs = validate_input_ini(f.read())
    if errs:
      if log:
        log("warn", f"{INPUT_INI_NAME} not copied: line {errs[0][0]}: {errs[0][1]}")
      return False
    shutil.copy2(INPUT_INI_FILE, os.path.join(pkg_dir, INPUT_INI_NAME))
    if log:
      log("ok", f"{INPUT_INI_NAME} updated in {pkg_dir}")
    return True
  except Exception as e:
    if log:
      log("warn", f"Copying {INPUT_INI_NAME} failed: {e}")
    return False


def runtime_env(rt, target, pkg_dir):
  """Dictionary of the environment variables to set at launch."""
  env = {}
  if rt["language"].strip() != "":
    env["ANYPS5_LANGUAGE"] = rt["language"].strip()
  if rt["gpu"].strip():
    env["ANYPS5_GPU"] = rt["gpu"].strip()
  if rt["present_mode"].strip():
    env["ANYPS5_PRESENT_MODE"] = rt["present_mode"].strip()
  if rt["fonts_dir"].strip():
    env["ANYPS5_SYSTEM_FONTS"] = rt["fonts_dir"].strip()
  if yes(rt["store_at_flip"]):
    env["APS5_STORE_AT_FLIP"] = "1"
  for key, var in (("sdl_videodriver", "SDL_VIDEODRIVER"), ("thread_priority", "APS5_THREAD_PRIORITY"),
                   ("thread_nice", "APS5_THREAD_NICE"), ("thread_rt", "APS5_THREAD_RT"),
                   ("trace_frames", "APS5_TRACE_FRAMES"), ("trace_seconds", "APS5_TRACE_SECONDS"),
                   ("trace_summary_every", "APS5_TRACE_SUMMARY_EVERY"), ("trace_events", "APS5_TRACE_EVENTS"),
                   ("trace_dir", "APS5_TRACE_DIR")):
    if rt.get(key, "").strip():
      if key == "sdl_videodriver" and target == "windows":
        continue  # BUILD.md: SDL_VIDEODRIVER is about the Linux X11/Wayland backends
      env[var] = rt[key].strip()
  if yes(rt.get("overlay", "no")):
    env["ANYPS5_OVERLAY"] = "1"  # shown from the first frame (F10 toggles it)
  if rt.get("overlay_pos", "").strip():
    env["ANYPS5_OVERLAY_POS"] = rt["overlay_pos"].strip()
  if rt.get("screenshots_dir", "").strip():
    env["ANYPS5_SCREENSHOTS"] = rt["screenshots_dir"].strip()
  for key, var, win_only in ADVANCED_FLAGS:
    if yes(rt.get(key, "no")) and not (win_only and target != "windows"):
      env[var] = "1"
  for key, (var, _lo, _hi, win_only) in ADVANCED_INTS.items():
    if rt.get(key, "").strip() and not (win_only and target != "windows"):
      env[var] = str(int(rt[key].strip()))
  if rt.get("crash_stack_bytes", "").strip() and target == "windows":
    env["APS5_CRASH_STACK_BYTES"] = rt["crash_stack_bytes"].strip()
  if yes(rt.get("trace_bench", "no")):
    env["APS5_TRACE_BENCH"] = "1"  # the runtime refuses any other value
  if yes(rt.get("trace_uds", "no")):
    env["APS5_TRACE_UDS"] = "1"
  if yes(rt.get("store_at_label", "no")):
    env["APS5_STORE_AT_LABEL"] = "1"
  if rt.get("videoout_telemetry_dir", "").strip():
    env["APS5_VIDEOOUT_TELEMETRY_DIR"] = rt["videoout_telemetry_dir"].strip()
  if rt.get("trace_calls_file", "").strip() and rt["trace_calls"].strip() and target == "windows":
    env["APS5_TRACE_CALLS_FILE"] = rt["trace_calls_file"].strip()
  if yes(rt.get("fullscreen", "no")):
    env["ANYPS5_FULLSCREEN"] = "1"  # any non-empty value other than 0; F11 still toggles in game
  if rt.get("display_index", "").strip():
    env["ANYPS5_DISPLAY_INDEX"] = str(int(rt["display_index"].strip()))
  # Video / presentation (USAGE.md / ENVIRONMENT.md): only set when the user chose a value
  upscale = rt.get("upscale", "").strip().lower()
  if upscale == "fsr":
    env["ANYPS5_UPSCALE"] = "fsr"
  sharp = rt.get("fsr_sharpness", "").strip()
  if sharp != "" and upscale == "fsr":
    env["ANYPS5_FSR_SHARPNESS"] = sharp  # 0.0–2.0; ignored without UPSCALE=fsr
  fps = rt.get("fps_limit", "").strip()
  if fps != "":
    env["ANYPS5_FPS_LIMIT"] = fps
  for key, var in (("video_out_max_width", "ANYPS5_VIDEO_OUT_MAX_WIDTH"),
                   ("video_out_max_height", "ANYPS5_VIDEO_OUT_MAX_HEIGHT")):
    if rt.get(key, "").strip():
      env[var] = rt[key].strip()
  if rt.get("entitlements_file", "").strip():
    env["ANYPS5_ENTITLEMENTS"] = os.path.abspath(rt["entitlements_file"].strip())
  if rt.get("shader_cache_dir", "").strip():
    env["ANYPS5_SHADER_CACHE_DIR"] = rt["shader_cache_dir"].strip()
  if yes(rt.get("no_shader_cache", "no")):
    env["ANYPS5_NO_SHADER_CACHE"] = "1"
  playgo_unlisted = str(rt.get("playgo_unlisted_chunks", "installed")).strip().lower()
  if playgo_unlisted in ("installed", "absent"):
    env["APS5_PLAYGO_UNLISTED_CHUNKS"] = playgo_unlisted
  if yes(rt.get("pipeline_stats", "no")):
    env["APS5_PIPELINE_STATS"] = "1"
  if yes(rt.get("gpu_checkpoints", "no")):
    env["APS5_GPU_CHECKPOINTS"] = "1"
  if yes(rt.get("diagnostics", "no")):
    # GAME_TESTING.md: [present] lines every 10 s, GPU timings, timestamped log
    env["APS5_PROFILE_DRAW"] = "1"
    env["APS5_PROFILE_GPU"] = "1"
    env["APS5_LOG_TIMESTAMPS"] = "1"
    if target == "windows":
      env["APS5_CRASH_INDIRECT_MEMORY"] = "1"  # crash report: bytes behind each register pointer
  rumble = str(rt.get("rumble", "100")).strip()
  if rumble.isdigit() and 0 <= int(rumble) < 100:
    env["ANYPS5_RUMBLE"] = rumble  # USAGE/ENVIRONMENT.md: 0-100 %, 100 = as the title asks
  spk = str(rt.get("pad_speaker_db", "0")).strip().lower()
  if spk in ("mute", "off", "-inf"):
    env["APS5_PAD_SPEAKER_FALLBACK_DB"] = "mute"
  elif spk not in ("", "0", "-0", "0.0"):  # 0 dB = default, variable left unset
    env["APS5_PAD_SPEAKER_FALLBACK_DB"] = spk
  # Host audio output (ANYPS5_VOLUME / MUTE): only set when the user chose a value
  vol = str(rt.get("volume", "")).strip()
  if vol != "":
    env["ANYPS5_VOLUME"] = vol  # 0–100; validated before launch
  if yes(rt.get("mute_on_focus_loss", "no")):
    env["ANYPS5_MUTE_ON_FOCUS_LOSS"] = "1"
  # Controller identify / port 0 presence / status panel
  pad_id = str(rt.get("pad_identify", "")).strip().lower()
  if pad_id in ("off", "connect", "always"):
    env["ANYPS5_PAD_IDENTIFY"] = pad_id
  port0 = str(rt.get("pad_port0_state", "")).strip().lower()
  if port0 in ("default", "controller"):
    env["ANYPS5_PAD_PORT0_STATE"] = port0
  if yes(rt.get("pad_status", "no")):
    env["ANYPS5_PAD_STATUS"] = "1"
  # Locale / system service / saves / debug startup (only when the user set a value)
  date_fmt = str(rt.get("date_format", "")).strip().lower()
  if date_fmt in ("0", "1", "2", "auto"):
    env["ANYPS5_DATE_FORMAT"] = date_fmt
  time_fmt = str(rt.get("time_format", "")).strip().lower()
  if time_fmt in ("0", "1", "auto"):
    env["ANYPS5_TIME_FORMAT"] = time_fmt
  free_sp = str(rt.get("free_space", "")).strip().lower()
  if free_sp in ("fixed", "host"):
    env["ANYPS5_FREE_SPACE"] = free_sp
  open_urls = str(rt.get("open_urls", "")).strip().lower()
  if open_urls in ("never", "ask", "always"):
    env["ANYPS5_OPEN_URLS"] = open_urls
  psn = str(rt.get("psn_dialogs", "")).strip().lower()
  if psn in ("silent", "show"):
    env["ANYPS5_PSN_DIALOGS"] = psn
  if yes(rt.get("stall_prompt", "no")):
    env["ANYPS5_STALL_PROMPT"] = "1"  # needs APS5_STALL_WATCHDOG_SECS (stall_watchdog_secs)
  if yes(rt.get("autorun_no_pause", "no")):
    env["ANYPS5_AUTORUN_NO_PAUSE"] = "1"
  if yes(rt.get("input_watch", "no")):
    env["ANYPS5_INPUT_WATCH"] = "1"
  oscale = str(rt.get("overlay_scale", "")).strip()
  if oscale:
    env["ANYPS5_OVERLAY_SCALE"] = oscale  # validated 1-8 by validate_runtime_options
  mic = str(rt.get("audio_in_device", "")).strip()
  if mic:
    env["ANYPS5_AUDIO_IN_DEVICE"] = mic
  if yes(rt.get("audio_in_mute_on_focus_loss", "no")):
    env["ANYPS5_AUDIO_IN_MUTE_ON_FOCUS_LOSS"] = "1"
  if yes(rt.get("rumble_keep_unfocused", "no")):
    env["ANYPS5_RUMBLE_ON_FOCUS_LOSS"] = "0"  # default is 1: no vibration without focus  # reload anyps5-input.ini by itself when it changes
  aps5_rep = str(rt.get("aps5_report", "")).strip()
  if aps5_rep in ("0", "1"):
    env["APS5_REPORT"] = aps5_rep
  if yes(rt.get("net_disconnected", "no")):
    env["APS5_NET_DISCONNECTED"] = "1"
  if rt.get("log_file", "").strip():
    env["ANYPS5_LOG_FILE"] = rt["log_file"].strip()
  if rt.get("game_mode", "off") in ("on", "auto"):
    env["APS5_GAME_MODE"] = rt["game_mode"]  # Feral GameMode on Linux, activity on macOS
  if yes(rt.get("host_report", "yes")):
    env["ANYPS5_HOST_REPORT"] = "1"  # USAGE.md: host report on stderr, once, for bug reports
  if rt.get("pad_input_file", "").strip():
    env["APS5_PAD_INPUT_FILE"] = rt["pad_input_file"].strip()
  if rt["trace_calls"].strip() and target == "windows":
    env["APS5_TRACE_CALLS"] = rt["trace_calls"].strip()
  if yes(rt["disable_dozen"]) and target == "windows":
    env["VK_LOADER_DRIVERS_DISABLE"] = "*dzn*"
  if yes(rt["copy_input_ini"]) and os.path.isfile(os.path.join(pkg_dir, INPUT_INI_NAME)):
    env["ANYPS5_INPUT_CONFIG"] = os.path.join(pkg_dir, INPUT_INI_NAME)
  for name, value in (catalog_env_values(rt) or {}).items():  # Runtime > All environment variables
    kind = ENV_CATALOG_KINDS.get(name)
    if kind == "presence":
      env[name] = "1"
    elif kind and value.strip():
      env[name] = value.strip()
  for item in re.split(r"[;\n]", rt.get("extra_env", "")):
    if "=" in item:
      k, v = item.split("=", 1)
      if k.strip():
        env[k.strip()] = v.strip()
  return env


def title_name(pkg_dir):
  """Real game title (e.g. "Dreaming Sarah") from <pkg>/app0/sce_sys/param.json; "" if unreadable."""
  try:
    with open(os.path.join(pkg_dir, "app0", "sce_sys", "param.json"), encoding="utf-8-sig") as f:
      j = json.load(f)
    loc = j.get("localizedParameters") or {}
    langs = [loc.get("defaultLanguage"), "en-US", "en-GB"] + [k for k in loc if isinstance(loc.get(k), dict)]
    for lang in langs:
      t = (loc.get(lang) or {}).get("titleName") if lang else None
      if isinstance(t, str) and t.strip():
        return t.strip()
    t = j.get("titleName")
    return t.strip() if isinstance(t, str) else ""
  except Exception as _exc:
    hidden_error("title_name", _exc)
    return ""


_DISCORD_SUBSTATES = (
  (r"^\[1b\]", "Building relinker"), (r"^\[1c\]", "Building libs"),
  (r"^\[2\]", "Preparing output folder"), (r"^\[3\] Moving", "Moving game files"),
  (r"^\[3\] Copying", "Copying game files"), (r"^\[3b\]", "Moving .prx files"),
  (r"^\[4\] Automatic unwrap \(", "Unwrapping game files"), (r"^\[5\]", "Copying libs"),
  (r"^\[6\]", "Relinking"), (r"^\[7\]", "Creating shortcut"), (r"^\[8\]", "Checking imports"),
  (r"^Summary", "Finishing"),
)


def discord_substate(step_msg):
  """Short Discord status for a conversion step line (\"[5] Copying 'libs'...\" -> \"Copying libs\"), or \"\"."""
  for pat, text in _DISCORD_SUBSTATES:
    if re.search(pat, step_msg):
      return text
  return ""


def windows_shortcut_spec(exe_path, env, sync=None):
  """(target, arguments) of the .lnk. A shortcut cannot carry environment variables, so when
  there are some it starts cmd.exe, which sets them and runs the exe (window stays open only if
  the game exits with an error, so the message can be read). Without variables: the exe itself."""
  if not env and not sync:
    return exe_path, ""
  parts = []
  if sync:  # refresh the input ini from the data folder every time the shortcut is used
    parts.append(f'(if exist "{sync[0]}" copy /y "{sync[0]}" "{sync[1]}" >nul)')
  parts += [f'set "{k}={v}"' for k, v in env.items()]
  parts.append(f'"{exe_path}"')
  target = os.environ.get("ComSpec") or r"C:\Windows\System32\cmd.exe"
  return target, '/c "' + " && ".join(parts) + ' || pause"'


def _desktop_quote(text):
  """Quoting of one argument in the Exec= key of a .desktop file."""
  return '"' + re.sub(r'([\\"`$])', r"\\\1", text) + '"'


def create_shortcut(pkg_dir, exe_name, target, rt, game_name, log, dest_dir=None):
  """Shortcut named after the game, next to the executable, with the Runtime settings baked in
  (Windows .lnk, Linux .desktop, macOS .command). Replaces the old launch.bat / launch.sh."""
  env = runtime_env(rt, target, pkg_dir)
  # the ini sits next to the executable: ANYPS5_INPUT_CONFIG is not needed
  env.pop("ANYPS5_INPUT_CONFIG", None)
  exe_path = os.path.abspath(os.path.join(pkg_dir, exe_name))
  sync = None
  if yes(rt.get("copy_input_ini", "yes")) and os.path.isfile(INPUT_INI_FILE):
    sync = (os.path.abspath(INPUT_INI_FILE), os.path.join(os.path.abspath(pkg_dir), INPUT_INI_NAME))
    sync_input_ini(pkg_dir, rt)  # also right now
  dest_dir = dest_dir or pkg_dir  # where the shortcut file is written (the exe stays in pkg_dir)
  game_name = title_name(pkg_dir) or game_name  # real title (e.g. "Dreaming Sarah") instead of the title ID
  base = _safe_filename(game_name)
  try:
    if target == "windows":
      if os.name != "nt":
        log("warn", "A Windows shortcut (.lnk) can only be created when the GUI runs on Windows.")
        return
      path = os.path.abspath(os.path.join(dest_dir, base + ".lnk"))
      tgt, args = windows_shortcut_spec(exe_path, env, sync)
      script = ("$s = New-Object -ComObject WScript.Shell; $l = $s.CreateShortcut($env:LNK_PATH); "
                "$l.TargetPath = $env:LNK_TARGET; $l.Arguments = $env:LNK_ARGS; "
                "$l.WorkingDirectory = $env:LNK_WD; $l.IconLocation = $env:LNK_ICON; "
                "$l.Description = $env:LNK_DESC; $l.Save()")
      penv = dict(os.environ, LNK_PATH=path, LNK_TARGET=tgt, LNK_ARGS=args,
                  LNK_WD=os.path.abspath(pkg_dir), LNK_ICON=exe_path + ",0", LNK_DESC=game_name)
      res = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        env=penv, capture_output=True, text=True, timeout=60,
        creationflags=subprocess.CREATE_NO_WINDOW)
      if res.returncode != 0 or not os.path.isfile(path):
        raise RuntimeError((res.stderr or res.stdout or "PowerShell failed").strip())
    else:
      if os.path.isfile(exe_path):
        os.chmod(exe_path, os.stat(exe_path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
      if target == "macos":
        path = os.path.join(dest_dir, base + ".command")
        lines = ["#!/bin/sh", f"cd {shlex.quote(os.path.abspath(pkg_dir))} || exit 1"]
        if sync:
          lines.append(f"[ -f {shlex.quote(sync[0])} ] && cp -f {shlex.quote(sync[0])} {shlex.quote(sync[1])}")
        lines += [f"export {k}='{v}'" for k, v in env.items()]
        lines += [f'exec {shlex.quote(exe_path)} "$@"']
        content = "\n".join(lines) + "\n"
      else:
        path = os.path.join(dest_dir, base + ".desktop")
        exec_line = "env " + " ".join(_desktop_quote(f"{k}={v}") for k, v in env.items())
        exec_line = (exec_line + " " if env else "") + _desktop_quote(exe_path)
        if sync:
          script = (f"[ -f {shlex.quote(sync[0])} ] && cp -f {shlex.quote(sync[0])} {shlex.quote(sync[1])}; "
                    f"exec {exec_line}")
          exec_line = "sh -c " + _desktop_quote(script.replace("%", "%%"))
        content = ("[Desktop Entry]\nType=Application\nName=%s\nExec=%s\nPath=%s\nTerminal=true\n"
                   % (game_name, exec_line, os.path.abspath(pkg_dir)))
      with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
      os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log("ok", f"Shortcut created: {path} ({len(env)} variables)")
  except Exception as e:
    log("warn", f"Shortcut not created: {e}")
