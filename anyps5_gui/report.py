"""Exit codes, log reading, run report and the test report (GAME_TESTING.md, "Reporting a result")."""

import json
import os
import re
import sys
import zipfile

from .settings import APP_VERSION, CONFIG_FILE, hidden_error, hidden_errors_text
from .system_checks import _run_text, _SOFT_GPU, cpu_info, gpu_info, memory_status
from .config import yes, yn
from .runtime import runtime_env


EXIT_CODES = {
  0: "the game closed normally",
  124: "the time cap ended the run (timeout)",
  126: "a library could not be loaded (a MinGW DLL or a .prx is missing)",
  127: "a required file was not found",
  134: "uncaught exception or abort() in the title",
  137: "the process was killed (SIGKILL, often out of memory)",
  139: "segmentation fault: the title touched memory it cannot access",
  216: "Windows refused to start the file (an ELF output was built without --windows)",
  193: "Windows tried to load a PS5 module as a DLL (a game .prx is in libs/)",
  0xC0000005: "access violation: the title or AnyPS5 touched invalid memory",
  0xC0000409: "uncaught exception or abort() (the log ends with FATAL: abort())",
  0xC000013A: "the process was closed with Ctrl+C or its console was closed",
  0xC0000142: "a DLL failed to initialize (error 126/193 territory: check libs/)",
  0xC0000135: "a required DLL was not found",
}


def normalize_exit_code(code):
  """Popen reports a signal as -N on POSIX (shells show 128+N) and a signed value on Windows."""
  if code is None:
    return None
  if code < 0:
    return (code & 0xFFFFFFFF) if os.name == "nt" else 128 - code
  return code


def explain_exit_code(code):
  """Plain-language text for an exit code of the title."""
  code = normalize_exit_code(code)
  if code is None:
    return "still running"
  text = EXIT_CODES.get(code)
  shown = f"0x{code:08x}" if code > 0xFFFF else str(code)
  if text is None:
    text = "the title stopped with an error: check the last log lines" if code else "ok"
  return f"{shown}: {text}"


def battery_status():
  """'AC', 'battery' or '' when unknown (GAME_TESTING.md: a run on battery can look like a regression)."""
  try:
    if os.name == "nt":
      import ctypes

      class SPS(ctypes.Structure):
        _fields_ = [("AC", ctypes.c_ubyte), ("Flag", ctypes.c_ubyte), ("Pct", ctypes.c_ubyte),
                    ("Sav", ctypes.c_ubyte), ("Life", ctypes.c_ulong), ("Full", ctypes.c_ulong)]
      s = SPS()
      if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
        return {0: "battery", 1: "AC"}.get(s.AC, "")
    elif sys.platform == "darwin":
      out = _run_text(["pmset", "-g", "batt"])
      return "AC" if "AC Power" in out else "battery" if "Battery Power" in out else ""
    else:
      base = "/sys/class/power_supply"
      online, has_bat = None, False
      for n in os.listdir(base):
        p = os.path.join(base, n)
        kind = open(os.path.join(p, "type")).read().strip()
        if kind == "Mains":
          online = online or open(os.path.join(p, "online")).read().strip() == "1"
        elif kind == "Battery":
          has_bat = True
      if online:
        return "AC"
      return "battery" if has_bat else "AC"  # no battery at all: a desktop on mains
  except Exception as _exc:
    hidden_error("battery_status", _exc)
    pass
  return ""


# Known errors: (pattern in the log, what to do). Shown in the reading of the report.
KNOWN_ERRORS = [
  (r"nvgpucomp64\.dll", "crash in nvgpucomp64.dll: known NVIDIA driver problem (e.g. 610.47); install another driver version"),
  (r"/dev/udmabuf", "/dev/udmabuf is not usable: on Linux add your user to the kvm group (sudo usermod -aG kvm $USER) and log in again"),
  (r"Ran out of memory|std::bad_alloc|out of memory", "out of memory: close other programs and raise the Windows paging file (direct memory needs up to 13.8 GiB)"),
  (r"Conflicting guest output filename", "two files would be written with the same name: check the output folder and the unwrapped game for duplicate names"),
  (r"(?:error|code)\s*:?\s*126\b|LoadLibrary.*126", "error 126: DLLs missing in libs/ (copy them from mingw64/bin or use the release libs)"),
  (r"(?:error|code)\s*:?\s*193\b|LoadLibrary.*193", "error 193: a wrong-architecture or original .prx file is in libs/; remove it and convert again"),
  (r"execv failed: Permission denied", "execute permission lost: run chmod +x on the executable (the GUI does it before start)"),
]


def known_error_notes(text):
  return [msg for pat, msg in KNOWN_ERRORS if re.search(pat, text, re.IGNORECASE)]


def analyze_log(text):
  """Facts read from a run log: GPU, presents per 10 s, milestones and what stopped the title."""
  gpu = re.findall(r"Selected GPU name=(.+)", text)
  presents = [int(n) for n in re.findall(r"\[present\]\s+(\d+)\s+presents over 10 s", text)]
  stop = [l.strip() for l in text.splitlines()
          if re.search(r"FATAL:|terminate called|what\(\)|presentation failed|ASSERT|VK_ERROR_DEVICE_LOST|"
                       r"vkAllocateMemory.*-2|sceSystemServiceLoadExec: execv failed", l)]
  levels = re.findall(r"Level has started:\s*(.+)", text)
  notes = []
  if gpu and _SOFT_GPU.search(gpu[-1]):
    notes.append("a software renderer was selected: very slow, set ANYPS5_GPU to the real GPU")
  if presents and presents[-1] == 0:
    notes.append("no frames in the last 10 s: the title stalled (try one input, then check again)")
  if any("DEVICE_LOST" in l for l in stop):
    notes.append("device lost: on AMD the first run can fail while shaders compile, run again with warm caches")
  if any("vkAllocateMemory" in l for l in stop):
    notes.append("the GPU ran out of memory")
  if any("execv failed" in l for l in stop):
    notes.append("an executable lost its permission bits: run chmod +x on it")
  if re.search(r"^\[gpu\] host import budget\b", text, re.MULTILINE):
    notes.append("[gpu] host import budget detected: APS5_HOST_IMPORT_MIB affects pinned host-import memory budget; change it only for controlled comparisons, not as a general tuning setting")
  if re.search(r"^\[texture-cache\]", text, re.MULTILINE):
    notes.append("[texture-cache] diagnostics detected: APS5_TEXTURE_CACHE_MIB controls the texture-cache budget; change it only for controlled comparisons, not as a general tuning setting")
  notes += [n for n in known_error_notes(text) if n not in notes and not (
    "chmod" in n and any("chmod" in x for x in notes))]
  return {"gpu": gpu[-1].strip() if gpu else "", "presents": presents, "stop": stop[-3:],
          "level": levels[-1].strip() if levels else "", "notes": notes}


def title_info(pkg_dir):
  """(title ID, content version) from <pkg>/app0/sce_sys/param.json; empty strings if unreadable."""
  try:
    with open(os.path.join(pkg_dir, "app0", "sce_sys", "param.json"), encoding="utf-8-sig") as f:
      j = json.load(f)
    return str(j.get("titleId", "")), str(j.get("contentVersion", ""))
  except Exception as _exc:
    hidden_error("title_info", _exc)
    return "", ""


def runtime_vars_text(cfg, target, pkg_dir):
  env = runtime_env(cfg["runtime"], target, pkg_dir)
  return "\n".join(f"{k}={v}" for k, v in sorted(env.items())) or "(none)"


def build_report(cfg, target, game, pkg_dir, log_text, exit_code=None, tail=None):
  """Plain text with the points 1-8 of GAME_TESTING.md, filled in as far as the GUI knows them.
  Unknown fields stay empty. Measured data and the automatic reading are kept apart."""
  import platform
  import datetime
  c = cpu_info()
  g = gpu_info()
  tid, ver = title_info(pkg_dir) if pkg_dir else ("", "")
  guest_log = ""
  for name in ("anyps5.log", "run.log"):
    p = os.path.join(pkg_dir or "", name)
    if os.path.isfile(p):
      try:
        with open(p, encoding="utf-8", errors="replace") as f:
          guest_log = f.read()
        break
      except OSError:
        pass
  source = guest_log or log_text
  facts = analyze_log(source)
  gpu_line = facts["gpu"] or "; ".join(x["name"] for x in g["gpus"] if not x["software"]) or ""
  lines = source.splitlines()[-tail:] if tail else source.splitlines()
  mem = memory_status()
  rt = cfg["runtime"]
  bar = "=" * 60
  def head(t):
    return [bar, t.upper(), bar]
  out = [
    "AnyPS5 TEST REPORT",
    "",
    *head("Measured data"),
    f"1. Build: GUI {APP_VERSION}; relinker commit/PR: (fill in)",
    f"2. Title: {tid or '(fill in)'}  content version {ver or '(fill in)'}  ({game or 'unknown'})",
    f"3. Host: CPU {c['name'] or c['arch']} ({c['vendor']}); OS {platform.platform()}; "
    f"GPU {gpu_line or '(fill in)'}; driver (fill in); power {battery_status() or '(fill in)'}; "
    f"display refresh rate (fill in)",
    f"   Target: {target}" + (f"; {mem[0]}" if mem and os.name == 'nt' else ""),
    "4. Command and variables:",
    runtime_vars_text(cfg, target, pkg_dir or ""),
    f"   --to-intel: {yn(yes(cfg['settings']['intel']))}; caches: (empty / warm)",
    f"5. How far it got: {facts['level'] or '(fill in)'}",
    f"   Exit code: {explain_exit_code(exit_code) if exit_code is not None else '(fill in)'}",
    f"6. Presents per 10 s: {', '.join(map(str, facts['presents'][-6:])) or '(none in the log)'}",
    "7. Runs / duration: (fill in)",
    f"8. Date: {datetime.date.today().isoformat()}",
    "",
    *head(f"Full log ({len(lines)} lines)" + ("" if guest_log else " - GUI log, the title's own output is not captured")),
    *lines,
    "",
  ]
  if guest_log and log_text.strip():
    out += [*head(f"GUI log ({len(log_text.splitlines())} lines)"), *log_text.splitlines(), ""]
  out += head("Automatic reading (generated, not measured)")
  out += [f"- {n}" for n in facts["notes"]] or ["- nothing unusual found in the log"]
  out += [f"- stop line: {s}" for s in facts["stop"]]
  return "\n".join(out) + "\n"


REPORT_NAME = "anyps5-report.json"
RECIPE_NAME = "anyps5-recipe.json"
_STAGE_LABEL = {"unknown": "unknown (nothing observed)", "loads": "loads (reached the entry point)",
                "starts": "starts (game code running)", "first-draw": "first-draw (first draw sent to the GPU)",
                "presents": "presents (frames shown)"}
_EXIT_LABEL = {"clean": "clean exit", "crash": "crash", "abort": "abort (uncaught error)", "killed": "killed"}


def read_run_report(exe_dir, started=None):
  """Reads anyps5-report.json and anyps5-recipe.json next to the game's executable.
  Returns {"report", "recipe", "path", "error"}: a missing file is normal (crash, kill, report off) and gives
  report None with no error; a file older than the launch (`started`, seconds) is an old run and is ignored."""
  res = {"report": None, "recipe": None, "path": "", "error": ""}
  if not exe_dir:
    return res
  for key, name in (("report", REPORT_NAME), ("recipe", RECIPE_NAME)):
    path = os.path.join(exe_dir, name)
    try:
      if not os.path.isfile(path):
        continue
      if started is not None and os.path.getmtime(path) < started - 2:
        continue  # left by an earlier run
      if os.path.getsize(path) > 1024 * 1024:
        raise ValueError("file too big")
      with open(path, encoding="utf-8") as f:
        data = json.load(f)
      if not isinstance(data, dict):
        raise ValueError("not a JSON object")
      res[key] = data
      if key == "report":
        res["path"] = path
    except (OSError, ValueError) as e:  # ValueError includes JSON errors
      res["error"] = f"{name} could not be read: {e}"
  return res


def _num(v, nd=1, unit=""):
  """Number as text; null (unknown) is shown as 'unknown', never as 0."""
  if isinstance(v, bool) or not isinstance(v, (int, float)):
    return "unknown"
  return (f"{v:.{nd}f}" if nd else f"{v:.0f}") + unit


def _dig(d, keys):
  for k in keys:
    if not isinstance(d, dict):
      return None
    d = d.get(k)
  return d


def summarize_run_report(rep, recipe=None, max_items=8):
  """Readable lines for a run report (docs/user/run-report.md). null means 'unknown'."""
  run = rep.get("run") if isinstance(rep.get("run"), dict) else {}
  out = []
  stage = run.get("stage")
  out.append("Stage reached: " + _STAGE_LABEL.get(stage, "unknown"))
  ms = run.get("milestones") if isinstance(run.get("milestones"), dict) else {}
  names = (("loaded", "loaded"), ("started", "started"), ("firstDraw", "first draw"), ("firstPresent", "first present"))
  seen = [f"{lab} {_num(ms[k], 1, ' s')}" for k, lab in names if k in ms]
  cov = rep.get("coverage") if isinstance(rep.get("coverage"), dict) else {}
  if seen:
    out.append("Milestones: " + ", ".join(seen))
  sysname = _dig(rep, ("system", "os"))
  if stage in ("first-draw", "presents") and "loaded" not in ms and sysname in ("linux", "windows"):
    out.append("(Linux and Windows do not record 'loaded' and 'started': a missing one is not a failure.)")
  ex = _EXIT_LABEL.get(run.get("exit"), "unknown")
  code = run.get("exitCode")
  out.append(f"Exit: {ex}" + (f", code {code}" if isinstance(code, int) else ", code unknown")
             + (f" · ran {_num(run.get('seconds'), 1, ' s')}" if run.get("seconds") is not None else ""))
  stop = rep.get("stop") if isinstance(rep.get("stop"), dict) else {}
  if stop.get("kind") not in (None, "none"):
    msg = str(stop.get("message") or "").strip()
    src = stop.get("source")
    out.append(f"Stopped by: {stop.get('kind')} ({stop.get('reason')})" + (f": {msg}" if msg else "")
               + (f" [{src}]" if src else ""))
  elif stop.get("reason") == "quit-requested":
    out.append("Stopped by: the game was asked to quit")
  frames = run.get("framesPresented")
  fshow = frames if isinstance(frames, int) else "unknown"
  if run.get("averageFps") is None:
    out.append(f"Frames shown: {fshow} · average FPS: unknown (fewer than 2 frames were shown)")
  else:
    out.append(f"Frames shown: {fshow} · average FPS: {_num(run.get('averageFps'), 1)} "
               f"over {_num(run.get('fpsWindowSeconds'), 1, ' s')}")
  fm = run.get("frameMs") if isinstance(run.get("frameMs"), dict) else None
  if fm:
    out.append(f"Frame time: p50 {_num(fm.get('p50'), 1, ' ms')} · p90 {_num(fm.get('p90'), 1, ' ms')} · "
               f"p99 {_num(fm.get('p99'), 1, ' ms')} (last 2048 frames)")
  else:
    out.append("Frame time: unknown")
  miss = _dig(rep, ("missing", "functions"))
  if cov.get("missing") is False:
    out.append("Missing functions: not collected by this build")
  elif isinstance(miss, list) and miss:
    items = [f"{(m.get('library') or '?')}::{m.get('name')} (x{m.get('calls')})"
             for m in miss[:max_items] if isinstance(m, dict)]
    more = len(miss) - max_items
    out.append(f"Missing functions ({len(miss)}): " + ", ".join(items) + (f" … +{more} more" if more > 0 else ""))
  else:
    out.append("Missing functions: none")
  sh = rep.get("shaders") if isinstance(rep.get("shaders"), dict) else {}
  if cov.get("shaders") is False:
    out.append("Shaders: not collected by this build")
  else:
    line = f"Shaders: {_num(sh.get('compiled'), 0)} compiled, {_num(sh.get('failed'), 0)} failed"
    if sh.get("cacheHits") is not None:
      line += f", {_num(sh.get('cacheHits'), 0)} from cache"
    out.append(line)
    uns = sh.get("unsupported")
    if isinstance(uns, list) and uns:
      items = []
      for u in uns[:max_items]:
        if isinstance(u, dict):
          op = u.get("opcode") or (f"opcode {u.get('opcodeValue')}" if u.get("opcodeValue") is not None
                                   else "unknown instruction")
          items.append(f"{u.get('encoding') or '?'} {op} (x{u.get('count')})")
      more = len(uns) - max_items
      out.append(f"Unsupported shader instructions ({len(uns)}): " + ", ".join(items)
                 + (f" … +{more} more" if more > 0 else ""))
  trunc = rep.get("truncated") if isinstance(rep.get("truncated"), dict) else {}
  cut = [f"{k} {v}" for k, v in trunc.items() if isinstance(v, int) and v > 0]
  if cut:
    out.append("Some lists were cut: " + ", ".join(cut))
  if run.get("recovered"):
    out.append("This report was recovered after the run ended without writing one.")
  if run.get("incomplete"):
    out.append("The report is marked incomplete: some observations were dropped.")
  if recipe:
    env = recipe.get("environment") if isinstance(recipe.get("environment"), dict) else {}
    names_env = sorted(env)
    out.append(f"Run recipe: {len(env)} ANYPS5_* variable(s) set"
               + (" (" + ", ".join(names_env[:6]) + (", …" if len(names_env) > 6 else "") + ")" if env else ""))
  return out


def run_report_text(exe_dir, started=None):
  """Text block for the bug report and the debug package ('' when no game was started)."""
  if not exe_dir:
    return ""
  r = read_run_report(exe_dir, started)
  if r["report"] is None:
    return r["error"] or ("No run report found (the game was killed or crashed, the report is off with "
                          "APS5_REPORT=0, or the game has not finished yet).")
  return "\n".join(summarize_run_report(r["report"], r["recipe"], max_items=20))


def redact_personal(text):
  """Hide user name, computer name and home folder in text meant for a public issue."""
  names = {os.environ.get(k, "") for k in ("USERNAME", "USER", "COMPUTERNAME", "HOSTNAME")}
  home = os.path.expanduser("~")
  if home and home != "~":
    text = text.replace(home, "~").replace(home.replace("\\", "/"), "~")
  for n in names:
    if len(n) >= 3:
      text = re.sub(re.escape(n), "<redacted>", text, flags=re.I)
  return text


def build_bug_report(cfg, target, game, pkg_dir, log_text, exit_code=None, tail=60, run_report=""):
  """Short report to paste in a GitHub issue: header, host report block (if any), last log lines."""
  full = build_report(cfg, target, game, pkg_dir, log_text, exit_code, tail=tail)
  lines = (log_text or "").splitlines()
  start = next((i for i, l in enumerate(lines) if "host report" in l.lower()), None)
  host = []
  if start is not None:
    for l in lines[start:start + 40]:
      if host and not l.strip():
        break
      host.append(l)
  head = full.split("=" * 60)[0] + "".join(full.split("=" * 60)[1:3])
  parts = [head.rstrip()]
  if run_report:
    parts += ["", "Run report (anyps5-report.json):", run_report]
  parts += ["", "Host report:" if host else "Host report: (not in the log)"] + host
  parts += ["", f"Last {min(tail, len(lines))} log lines:", *lines[-tail:]]
  return redact_personal("\n".join(parts) + "\n")


def trophy_summary(pkg):
  """Counts the unlocked trophies stored in <pkg>/_trophy (formats from AnyPS5 USAGE.md and libSceNpTrophy):
  PS5 `<user>_<label>.txt` has one `<trophy id> <unlock time>` line per trophy, `.stats.txt` holds statistics
  (not counted), PS4 `trophyNN.<user>` has one trophy id per line. Damaged files are listed, not guessed."""
  res = {"exists": False, "unlocked": 0, "files": [], "stats_files": 0, "damaged": []}
  d = os.path.join(pkg, "_trophy") if pkg else ""
  if not d or not os.path.isdir(d):
    return res
  res["exists"] = True
  try:
    names = sorted(os.listdir(d))
  except OSError:
    return res
  for name in names:
    path = os.path.join(d, name)
    if not os.path.isfile(path) or name.endswith(".tmp"):
      continue
    if name.endswith(".stats.txt"):
      res["stats_files"] += 1
      continue
    ps5 = name.endswith(".txt")
    ps4 = re.fullmatch(r"trophy\d+\.-?\d+", name) is not None
    if not (ps5 or ps4):
      continue
    ids, bad = set(), False
    try:
      with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
          parts = line.split()
          if not parts and ps4:
            continue  # the PS4 reader skips whitespace; for PS5 a blank line is damage (USAGE.md)
          if ps5 and len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            ids.add(int(parts[0]))
          elif ps4 and len(parts) == 1 and parts[0].isdigit():
            ids.add(int(parts[0]))
          else:
            bad = True
    except OSError:
      bad = True
    if bad:
      res["damaged"].append(name)
    res["files"].append({"name": name, "unlocked": len(ids)})
    res["unlocked"] += len(ids)
  return res


def build_debug_zip(path, cfg, target, game, pkg_dir, log_text, exit_code=None, exe_dir="", started=None):
  """Writes a zip for support: bug report, full report, settings and .registry.json files.
  User name, computer name and home folder are replaced in every text file. Returns the list of names added."""
  added = []
  with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
    def put(name, text):
      z.writestr(name, redact_personal(text))
      added.append(name)
    put("bug-report.txt", build_bug_report(cfg, target, game, pkg_dir, log_text or "", exit_code,
                                           run_report=run_report_text(exe_dir, started)))
    if exe_dir:
      rr = read_run_report(exe_dir, started)
      for key, name in (("report", REPORT_NAME), ("recipe", RECIPE_NAME)):
        if rr[key] is not None:  # the runtime already filters names and paths; put() redacts once more
          put(name, json.dumps(rr[key], indent=2, ensure_ascii=False))
    put("report.txt", build_report(cfg, target, game, pkg_dir, log_text or "", exit_code, tail=0))
    if hidden_errors_text():
      put("hidden-errors.log", hidden_errors_text())  # errors the app handled without showing them (this session)
    try:
      with open(CONFIG_FILE, encoding="utf-8", errors="replace") as f:
        put("config.ini", f.read())
    except OSError:
      pass
    total = 0
    if pkg_dir and os.path.isdir(pkg_dir):
      for n in sorted(os.listdir(pkg_dir)):
        p = os.path.join(pkg_dir, n)
        if n.endswith(".registry.json") and os.path.isfile(p):
          size = os.path.getsize(p)
          if total + size > 20 * 1024 * 1024:
            put("registry-skipped.txt", f"{n} skipped: the registry files exceed 20 MB in total.\n")
            continue
          total += size
          with open(p, encoding="utf-8", errors="replace") as f:
            put("registry/" + n, f.read())
  return added
