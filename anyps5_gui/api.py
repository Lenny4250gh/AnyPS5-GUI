"""The bridge between the interface (index.html) and the logic: the methods the page calls."""

import base64
import json
import os
import queue
import re
import shutil
import socket
import stat
import subprocess
import threading
import time
import webview

from .settings import (
    _HIDDEN_ERRORS, APP_VERSION, Cancelled, DATA_DIR, DEFAULT_DATA_DIR, DEFAULT_OUTPUT_DIR,
    DEFAULT_REPO_URL, DEFAULTS, FONTS_DIR_NAME, hidden_error, INPUT_INI_FILE, INPUT_INI_NAME, LANGUAGES,
    LIBS_ZIP_PREFIX, NOT_BINARY_EXT, OVERLAY_POSITIONS, PRESENT_MODES, RELINKER_PREFIX, SCRIPT_DIR,
    STORAGE_EDITABLE, STORAGE_PROBLEM, TARGETS, UNWRAP_SCRIPT_NAME
)
from .system_checks import cpu_status, gpu_info, gpu_status, memory_status, nvidia_status, udmabuf_status
from .config import host_target, load_config, save_config, yes
from .util import _rmtree_force, existing_parent, format_size, package_dir
from .repo import set_active_relinker_cfg
from .input_ini import INPUT_TEMPLATE, validate_input_ini
from .process import kill_tree
from .unwrapper import repo_unwrapper_path, unwrapper_status
from .archives import (
    ARCHIVE_DIALOG_FILTER, archive_game_name, cleanup_extracted, extract_game_archive, is_archive_source,
    scan_archive
)
from .files import (
    _libs_cache, archive_game_problems, folder_game_problems, get_backup_dir, libs_info, libs_label,
    scan_game, wants_built_libs
)
from .disk import build_notification, notify_desktop, space_requirements
from .release import ensure_release_assets, fetch_release_info
from .source_build import (
    ensure_source_relinker, install_build_tools, missing_build_tools, relinker_status, source_preflight,
    TOOL_NAMES
)
from .env_catalog import ENV_CATALOG
from .runtime import (
    create_shortcut, discord_substate, download_input_mapper, find_input_mapper, INPUT_MAPPER_PORT,
    INPUT_MAPPER_REL, install_input_mapper_deps, node_status, title_name, validate_runtime_options
)
from .launch import launch_game
from .relink_options import relinker_capabilities, validate_relink_options
from .basic_ops import (
    do_restore, do_unwrap, open_path, relocate_data_dir, screenshots_dir, storage_choice_to_dir,
    validate_storage_target
)
from .relink import do_dry_run, do_relink
from .report import (
    build_bug_report, build_debug_zip, build_report, explain_exit_code, normalize_exit_code,
    read_run_report, REPORT_NAME, run_report_text, summarize_run_report, title_info, trophy_summary
)
from .updater import (
    _PausableCancel, check_gui_update, GUI_RELEASES_URL, GUI_REPO_NAME, GUI_REPO_OWNER, pick_gui_asset,
    prepare_and_launch_update, UpdateUnavailable
)
from .library import (
    build_games, dump_row, find_dumps, find_packages, GAME_RT_KEYS, icon_uri, is_fs_root, library_roots,
    load_library, merged_runtime, norm, package_exe, package_row, save_library, start_sizes
)
from .discord_rpc import DiscordRPC
from .schema import SCHEMA

from . import unwrapper as unwrapper_mod
from . import relink_options as relink_options_mod


def clean(x):
  return json.loads(json.dumps(x, default=str))


LIB_COLUMNS = ("name", "serial", "version", "size", "state", "path", "with", "status", "comment", "act")


class Api:
  def __init__(self):
    self.cfg = load_config()
    self.rpc = DiscordRPC()
    self.rpc.enabled = lambda: not yes(self.cfg["runtime"].get("no_discord_rpc", "no"))
    threading.Thread(target=self.rpc.update, args=("In the launcher", "Idle"), daemon=True).start()
    self.schema = SCHEMA
    self.folder = ""
    self.play_folder = ""  # converted PC game package selected for Play
    self.q = queue.Queue()
    self.running = False
    self.result = None
    self.cancel_ev = threading.Event()
    self.holder = {}
    self.game_proc = None
    self.game_exit = None      # exit code of the last game that ended
    self.game_pkg = ""         # folder of the last started game
    self.game_exe_dir = ""     # folder of its executable: anyps5-report.json is written there
    self.game_started = None   # time.time() of the launch (reports of earlier runs are ignored)
    self.quitting = False      # True once the user confirmed closing during an operation
    self.mapper_proc = None    # input mapper web server (node), started from the Input tab
    self._quit_asked = False   # a confirmation popup is already on screen
    self._upd = {"state": "idle"}  # progress of the GUI self-update
    self._mov = {"state": "idle"}  # progress of moving the data folder
    self.relocated = False         # True once the data folder was moved: the app must be reopened
    self._ru_ev = None             # waiting for the answer of the "new repository version" popup
    self._ru_answer = None
    unwrapper_mod.UPDATE_PROMPT = self._ask_repo_update

  # ---------- closing the window ----------
  def on_closing(self):
    """Window close event. Idle (or already confirmed) -> close. Operation running -> the close is
    vetoed and the in-app popup asks for confirmation."""
    if self.quitting or not self.running:
      self.rpc.clear()
      self._stop_mapper()
      return True
    if not self._quit_asked:
      self._quit_asked = True

      def ask():  # evaluate_js from a separate thread: calling it inside the handler could deadlock
        try:
          webview.windows[0].evaluate_js("window.confirmQuit && window.confirmQuit()")
        except Exception:
          self._quit_asked = False
      threading.Thread(target=ask, daemon=True).start()
    return False

  # ---------- "new version of the repository" popup ----------
  def _ask_repo_update(self, info, cancel):
    """Called by the build thread: shows the popup in the page and waits for the answer.
    Returns {"update": bool} or None if the popup cannot be shown (then the update goes ahead)."""
    ev = threading.Event()
    self._ru_ev, self._ru_answer = ev, None
    try:
      shown = webview.windows[0].evaluate_js(
          "(window.askRepoUpdate ? (window.askRepoUpdate(%s), true) : false)" % json.dumps(info))
    except Exception as _exc:
      hidden_error("_ask_repo_update", _exc)
      shown = False
    if shown is not True:
      return None
    while not ev.wait(0.2):
      if cancel.is_set():
        return None
    return self._ru_answer

  def repo_update_answer(self, update, remember):
    """Answer of the popup. With 'remember' (do not show again) the choice becomes the setting:
    update -> 'Check for updates' ON, keep -> OFF; the question is not asked again."""
    update, remember = bool(update), bool(remember)
    if remember and not self.relocated:
      self.cfg["relinker"]["repo_update"] = "yes" if update else "no"
      self.cfg["relinker"]["repo_update_ask"] = "no"
      save_config(self.cfg)
    self._ru_answer = {"update": update, "remember": remember}
    if self._ru_ev is not None:
      self._ru_ev.set()
    return True

  def quit_answered(self):
    self._quit_asked = False
    return True

  def force_quit(self):
    """The user confirmed: stop the operation, wait for its cleanup/rollback, then close the app."""
    self.quitting = True
    self._quit_asked = False
    self.cancel()

    def finish():
      deadline = time.time() + 300  # rollback of a big move can take a while
      while self.running and time.time() < deadline:
        time.sleep(0.2)
      self._stop_mapper()
      try:
        webview.windows[0].destroy()
      except Exception:
        os._exit(0)
    threading.Thread(target=finish, daemon=True).start()
    return True

  # ---------- input mapper web (tools/input-mapper) ----------
  def _mapper_running(self):
    return self.mapper_proc is not None and self.mapper_proc.poll() is None

  def _stop_mapper(self):
    p, self.mapper_proc = self.mapper_proc, None
    if p is not None and p.poll() is None:
      try:
        kill_tree(p)
      except Exception as _exc:
        hidden_error("_stop_mapper", _exc)
        try:
          p.terminate()
        except Exception as _exc:
          hidden_error("_stop_mapper", _exc)
          pass

  def input_mapper_status(self):
    return {"running": self._mapper_running(), "url": f"http://127.0.0.1:{INPUT_MAPPER_PORT}"}

  def start_input_mapper(self):
    """Starts `npm --prefix tools/input-mapper start` in the background and opens it in the browser."""
    import webbrowser
    url = f"http://127.0.0.1:{INPUT_MAPPER_PORT}"
    if self._mapper_running():
      webbrowser.open(url)
      return {"ok": True, "running": True, "url": url, "text": "The input mapper is already running."}
    node, _major, err = node_status()
    if err:
      return {"ok": False, "running": False, "text": err}
    npm = shutil.which("npm")
    if not npm:
      return {"ok": False, "running": False, "text": "npm was not found (it comes with Node.js). Reinstall Node.js 22 or newer."}
    found = find_input_mapper(self.cfg["relinker"])
    if not found and self.cfg["relinker"].get("relinker_source") == "release":
      # Release option: tools/input-mapper is not in the release files, download it like unwrap_self.py
      err = download_input_mapper(self.cfg["relinker"], lambda *a, **k: None, threading.Event())
      if err:
        return {"ok": False, "running": False, "text": err}
      found = find_input_mapper(self.cfg["relinker"])
    if not found:
      return {"ok": False, "running": False,
              "text": "tools/input-mapper was not found. Clone the repository (Source build), choose Release "
                      "to download it, or copy the tools/input-mapper folder into the app data folder under tools/."}
    base, mapper_dir = found
    err = install_input_mapper_deps(mapper_dir, npm)
    if err:
      return {"ok": False, "running": False, "text": err}
    kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    env = dict(os.environ)
    env["PORT"] = str(INPUT_MAPPER_PORT)
    try:
      self.mapper_proc = subprocess.Popen([npm, "--prefix", INPUT_MAPPER_REL, "start"], cwd=base, env=env,
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, **kw)
    except OSError as e:
      return {"ok": False, "running": False, "text": f"Unable to start the input mapper: {e}"}
    for _ in range(40):  # wait up to ~8 s for the server to answer
      if self.mapper_proc.poll() is not None:
        self.mapper_proc = None
        return {"ok": False, "running": False,
                "text": f"The input mapper stopped right away (is port {INPUT_MAPPER_PORT} already in use?)."}
      try:
        with socket.create_connection(("127.0.0.1", INPUT_MAPPER_PORT), timeout=0.4):
          break
      except OSError:
        time.sleep(0.2)
    webbrowser.open(url)
    return {"ok": True, "running": True, "url": url,
            "text": "Input mapper opened in your browser. Export the .ini there, then use Import settings here."}

  def stop_input_mapper(self):
    self._stop_mapper()
    return {"ok": True, "running": False, "text": "Input mapper stopped."}

  # ---------- state ----------
  def get_state(self):
    inp = ""
    try:
      inp = open(INPUT_INI_FILE, encoding="utf-8-sig").read() if os.path.isfile(INPUT_INI_FILE) else INPUT_TEMPLATE
    except OSError:
      inp = INPUT_TEMPLATE
    return clean({"cfg": self.cfg, "schema": self.schema, "host": host_target(), "folder": self.folder,
                  "targets": {k: v["label"] for k, v in TARGETS.items()}, "languages": LANGUAGES,
                  "present_modes": PRESENT_MODES, "overlay_positions": OVERLAY_POSITIONS, "default_repo": DEFAULT_REPO_URL,
                  "defaults": DEFAULTS, "input": inp, "input_name": INPUT_INI_NAME,
                  "storage": {"path": DATA_DIR, "default": DEFAULT_DATA_DIR, "editable": STORAGE_EDITABLE,
                              "problem": STORAGE_PROBLEM},
                  "version": APP_VERSION})

  def _rpc_set(self, details, state=None, timer=False):
    """Update Discord Rich Presence without blocking the GUI."""
    # only the most recent request is sent: a slow older thread can no longer overwrite a newer status
    self._rpc_seq = getattr(self, "_rpc_seq", 0) + 1
    seq = self._rpc_seq

    def run():
      if seq == self._rpc_seq:
        self.rpc.update(details, state, timer)
    threading.Thread(target=run, daemon=True).start()

  def _rpc_idle(self):
    if not self.quitting:
      self._rpc_set("In the launcher", "Idle")

  def _rpc_refresh(self):
    """Apply the Discord Rich Presence setting right away (no restart needed)."""
    if yes(self.cfg["runtime"].get("no_discord_rpc", "no")):
      threading.Thread(target=self.rpc.clear, daemon=True).start()
    else:
      self._rpc_idle()

  def set_cfg(self, values):
    if self.relocated:
      return False
    for s, items in values.items():
      self.cfg[s].update({k: str(v) for k, v in items.items()})
    save_config(self.cfg)
    self._rpc_refresh()
    return True

  def check_update(self):
    return clean(check_gui_update())

  def start_update(self):
    """Download the release asset for this system, replace the app and restart it."""
    if self.running:
      return {"ok": False, "error": "Finish or cancel the running operation first."}
    if self._upd.get("state") in ("downloading", "installing", "paused"):
      return {"ok": True}
    self._upd = {"state": "downloading", "pct": 0, "text": "Looking for the release…"}
    threading.Thread(target=self._update_worker, daemon=True).start()
    return {"ok": True}

  def update_status(self):
    return clean(self._upd)

  def pause_update(self):
    c = getattr(self, "_upd_cancel", None)
    if c is not None and self._upd.get("state") == "downloading":
      c.paused.set()
      self._upd["state"] = "paused"
    return {"ok": True}

  def resume_update(self):
    c = getattr(self, "_upd_cancel", None)
    if c is not None and self._upd.get("state") == "paused":
      self._upd.update(state="downloading", text="Resuming…")
      c.paused.clear()
    return {"ok": True}

  def _update_worker(self):
    def status(**k):
      self._upd.update(k)
    try:
      info = fetch_release_info(GUI_REPO_OWNER, GUI_REPO_NAME, "")
      asset = pick_gui_asset(info["assets"])
      if not asset:
        raise UpdateUnavailable("the release has no file for this system")
      self._upd_cancel = _PausableCancel()
      prepare_and_launch_update(asset, status, self._upd_cancel)
    except UpdateUnavailable as e:
      status(state="failed", fallback=True, text="Automatic update not available: %s." % e)
      return
    except Exception as e:
      status(state="failed", fallback=True, text="Update failed: %s" % e)
      return
    status(state="restarting", pct=100, text="Restarting…")
    self.quitting = True
    time.sleep(0.8)
    try:
      webview.windows[0].destroy()
    except Exception:
      pass
    time.sleep(1.5)
    os._exit(0)

  # ---------- storage folder (app data) ----------
  def pick_storage(self):
    """Folder dialog; returns the data folder that would be used inside the picked folder."""
    res = webview.windows[0].create_file_dialog(getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
                                                or webview.FOLDER_DIALOG)
    return storage_choice_to_dir(res[0]) if res else None

  def default_storage(self):
    return DEFAULT_DATA_DIR

  def move_storage(self, path):
    """Moves the data folder to `path` in a thread (progress: storage_status)."""
    if self.running:
      return {"ok": False, "error": "Finish or cancel the running operation first."}
    if self._mov.get("state") == "moving":
      return {"ok": True}
    path = os.path.abspath(str(path or "").strip()) if path else ""
    problem = validate_storage_target(path)
    if problem:
      return {"ok": False, "error": problem}
    self._mov = {"state": "moving", "pct": None, "text": "Starting…"}
    threading.Thread(target=self._move_worker, args=(path,), daemon=True).start()
    return {"ok": True}

  def storage_status(self):
    return clean(self._mov)

  def _move_worker(self, path):
    def status(text, pct):
      self._mov.update(text=text, pct=pct)
    try:
      relocate_data_dir(path, status)
    except Exception as e:
      self._mov = {"state": "failed", "text": str(e)}
      return
    self.relocated = True
    self._mov = {"state": "done", "pct": 100, "text": "Done. Close and reopen the app to use the new folder.", "path": path}

  def shader_cache_info(self):
    """Return the effective AnyPS5 shader-cache path and its current size."""
    rt = dict(self.cfg.get("runtime", {}))
    pkg = self.game_pkg
    if not pkg and self.folder:
      try:
        pkg = package_dir(self.cfg["settings"]["output_dir"] or DEFAULT_OUTPUT_DIR,
                            self.game_name(), self.cfg["relinker"]["target"])
      except Exception as _exc:
        hidden_error("shader_cache_info", _exc)
        pkg = ""
    path = rt.get("shader_cache_dir", "").strip() or (os.path.join(pkg, "shader_cache") if pkg else "")
    total = 0
    if path and os.path.isdir(path):
      for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for name in files:
          fp = os.path.join(root, name)
          try:
            if not os.path.islink(fp):
              total += os.path.getsize(fp)
          except OSError:
            pass
    return {"path": path, "bytes": total, "size": ("%s B" % total if total < 1024 else "%.1f KiB" % (total / 1024) if total < 1024**2 else "%.1f MiB" % (total / 1024**2) if total < 1024**3 else "%.2f GiB" % (total / 1024**3))}

  def clear_shader_cache(self):
    """Remove only AnyPS5's shader_cache directory, never the driver's cache."""
    info = self.shader_cache_info()
    path = info["path"]
    if not path:
      return {"ok": False, "error": "Select a game package or set ANYPS5_SHADER_CACHE_DIR first."}
    if not os.path.isabs(path):
      return {"ok": False, "error": "ANYPS5_SHADER_CACHE_DIR must be an absolute path."}
    if os.path.isdir(path):
      try:
        shutil.rmtree(path)
      except OSError as e:
        return {"ok": False, "error": f"Unable to clear shader cache: {e}"}
    return {"ok": True, "path": path, "bytes": 0, "size": "0 B"}

  def _selected_game_package(self):
    """Resolve the last launched package or the package for the selected source folder."""
    if self.game_pkg and os.path.isdir(self.game_pkg):
      return os.path.abspath(self.game_pkg)
    if self.folder and not is_archive_source(self.folder):
      try:
        return os.path.abspath(package_dir(self.cfg["settings"]["output_dir"] or DEFAULT_OUTPUT_DIR,
                                             self.game_name(), self.cfg["relinker"]["target"]))
      except Exception as _exc:
        hidden_error("_selected_game_package", _exc)
        pass
    return ""

  def game_folders_info(self):
    pkg = self._selected_game_package()
    app0 = os.path.join(pkg, "app0") if pkg else ""
    return {"package": pkg,
            "sd": os.path.isdir(os.path.join(pkg, "_sd")) if pkg else False,
            "sd_mem": os.path.isdir(os.path.join(pkg, "_sd_mem")) if pkg else False,
            "trophy": os.path.isdir(os.path.join(pkg, "_trophy")) if pkg else False,
            "trophies": trophy_summary(pkg),
            "ampr_dump": os.path.isfile(os.path.join(app0, "ampr_assets.index")) if app0 else False}

  def env_catalog(self):
    return [{"name": n, "kind": k, "default": d, "desc": t} for n, k, d, t in ENV_CATALOG]

  def open_entitlements_file(self, rt_values=None):
    """Opens the entitlements ini (the field value, else anyps5-entitlements.ini beside the game executable)
    in the default editor, creating it with a short template when it does not exist yet."""
    rt = dict(self.cfg["runtime"])
    if isinstance(rt_values, dict):
      rt.update({k: str(v) for k, v in rt_values.items()})
    path = rt.get("entitlements_file", "").strip()
    if not path:
      pkg = self._selected_game_package()
      if not pkg:
        return {"ok": False, "error": "Select a converted game package first, or type a file path above."}
      path = os.path.join(pkg, "anyps5-entitlements.ini")
    path = os.path.abspath(path)
    if not os.path.isfile(path):
      try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
          fh.write("# anyps5-entitlements.ini - add-ons (DLC) the game sees as owned\n"
                   "# One entitlement label per line (at most 15 characters). # or ; starts a comment.\n")
      except OSError as e:
        return {"ok": False, "error": f"Unable to create {path}: {e}"}
    return {"ok": bool(open_path(path)), "path": path}

  def open_game_folder(self, name):
    if name not in ("_sd", "_sd_mem", "_trophy"):
      return {"ok": False, "error": "Unknown game folder."}
    pkg = self._selected_game_package()
    path = os.path.join(pkg, name) if pkg else ""
    if not path or not os.path.isdir(path):
      return {"ok": False, "error": f"Folder {name} was not found. Launch the game or select a converted package first."}
    return {"ok": bool(open_path(path)), "path": path}

  def backup_game_folder(self, name):
    if name not in ("_sd", "_sd_mem"):
      return {"ok": False, "error": "Only save-data folders can be backed up."}
    pkg = self._selected_game_package()
    src = os.path.join(pkg, name) if pkg else ""
    if not src or not os.path.isdir(src):
      return {"ok": False, "error": f"Folder {name} was not found."}
    res = webview.windows[0].create_file_dialog(getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or webview.FOLDER_DIALOG)
    if not res:
      return {"ok": False, "cancelled": True}
    dest = res[0] if isinstance(res, (list, tuple)) else res
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive = os.path.join(dest, f"{name}-backup-{stamp}")
    try:
      shutil.make_archive(archive, "zip", root_dir=pkg, base_dir=name)
      return {"ok": True, "path": archive + ".zip"}
    except OSError as e:
      return {"ok": False, "error": str(e)}

  def clear_game_trophies(self):
    pkg = self._selected_game_package()
    path = os.path.join(pkg, "_trophy") if pkg else ""
    if not path or not os.path.isdir(path):
      return {"ok": False, "error": "Folder _trophy was not found."}
    try:
      shutil.rmtree(path)
      os.makedirs(path, exist_ok=True)
      return {"ok": True, "path": path}
    except OSError as e:
      return {"ok": False, "error": str(e)}

  def game_trophies_info(self):
    """Number of unlocked trophies in the selected package (see trophy_summary)."""
    return trophy_summary(self._selected_game_package())

  def export_debug_package(self, log_text):
    """Support zip (reports, settings, .registry.json) with the personal names hidden; asks where to save it."""
    cfg = {sec: dict(v) for sec, v in self.cfg.items()}
    target = cfg["relinker"]["target"]
    pkg = self.game_pkg or self._selected_game_package()
    game = os.path.basename(pkg.rstrip("/\\")) if pkg else (self.game_name() if self.folder else "")
    kind = getattr(getattr(webview, "FileDialog", None), "SAVE", None) or webview.SAVE_DIALOG
    res = webview.windows[0].create_file_dialog(kind, save_filename="anyps5-debug-" + time.strftime("%Y%m%d-%H%M%S") + ".zip")
    if not res:
      return {"ok": False, "cancelled": True}
    path = res if isinstance(res, str) else res[0]
    try:
      added = build_debug_zip(path, cfg, target, game, pkg, log_text or "", self.game_exit,
                                exe_dir=self.game_exe_dir, started=self.game_started)
    except Exception as e:
      return {"ok": False, "error": str(e)}
    return {"ok": True, "path": path, "files": added}

  def open_screenshots_folder(self):
    """Opens the folder of the F12 screenshots: ANYPS5_SCREENSHOTS, or anyps5-screenshots/ next to the
    executable of the last started game (or of the game package selected in the app)."""
    cfg = {s: dict(v) for s, v in self.cfg.items()}
    rt = cfg["runtime"]
    pkg = self.game_pkg
    if not pkg and self.folder and not rt.get("screenshots_dir", "").strip():
      try:
        pkg = package_dir(cfg["settings"]["output_dir"] or DEFAULT_OUTPUT_DIR, self.game_name(), cfg["relinker"]["target"])
      except Exception as _exc:
        hidden_error("open_screenshots_folder", _exc)
        pkg = ""
    path = screenshots_dir(pkg, rt)
    if not path:
      self.q.put(("warn", "No screenshots folder yet: launch a game first, or set ANYPS5_SCREENSHOTS in Runtime settings."))
      return False
    if not os.path.isabs(path):
      self.q.put(("err", "ANYPS5_SCREENSHOTS must be an absolute path."))
      return False
    if not os.path.isdir(path):
      if os.path.isdir(os.path.dirname(path)) or rt.get("screenshots_dir", "").strip():
        try:
          os.makedirs(path, exist_ok=True)  # the game creates it at the first F12 anyway
        except OSError as e:
          self.q.put(("err", f"Unable to create {path}: {e}"))
          return False
      else:
        self.q.put(("warn", "No screenshots yet: the folder is created at the first F12 in the game (" + path + ")."))
        return False
    if not open_path(path):
      self.q.put(("err", f"Unable to open {path}"))
      return False
    return True

  def _shots_folder(self):
    """Existing screenshots folder of the selected/last game, else \"\"."""
    rt = dict(self.cfg["runtime"])
    pkg = self.game_pkg or self._selected_game_package()
    path = screenshots_dir(pkg, rt)
    return path if path and os.path.isabs(path) and os.path.isdir(path) else ""

  def list_screenshots(self, limit=12):
    """Newest PNG/JPG screenshots: [{name, size}] (the gallery loads each thumbnail with screenshot_data)."""
    path = self._shots_folder()
    if not path:
      return {"ok": True, "items": [], "path": ""}
    items = []
    try:
      for n in os.listdir(path):
        if n.lower().endswith((".png", ".jpg", ".jpeg", ".bmp")):
          st = os.stat(os.path.join(path, n))
          items.append((st.st_mtime, n, st.st_size))
    except OSError as e:
      return {"ok": False, "error": str(e)}
    items.sort(reverse=True)
    return {"ok": True, "path": path, "total": len(items),
            "items": [{"name": n, "size": z} for _m, n, z in items[:max(1, min(int(limit), 40))]]}

  def screenshot_data(self, name):
    """One screenshot as a data: URI (files above 12 MB are refused)."""
    path = self._shots_folder()
    if not path or name != os.path.basename(str(name)):
      return {"ok": False, "error": "Invalid screenshot."}
    f = os.path.join(path, name)
    try:
      if not os.path.isfile(f) or os.path.getsize(f) > 12 * 1024 * 1024:
        return {"ok": False, "error": "Screenshot not available or too large."}
      mime = "image/png" if name.lower().endswith(".png") else "image/bmp" if name.lower().endswith(".bmp") else "image/jpeg"
      with open(f, "rb") as fh:
        return {"ok": True, "uri": "data:%s;base64,%s" % (mime, base64.b64encode(fh.read()).decode("ascii"))}
    except OSError as e:
      return {"ok": False, "error": str(e)}

  def open_screenshot(self, name):
    path = self._shots_folder()
    if not path or name != os.path.basename(str(name)):
      return False
    return bool(open_path(os.path.join(path, name)))

  def open_runtime_folder(self, key, rt_values=None):
    """Opens the folder of a Runtime setting (trace files or video-out telemetry)."""
    names = {"trace_dir": "APS5_TRACE_DIR", "videoout_telemetry_dir": "APS5_VIDEOOUT_TELEMETRY_DIR"}
    if key not in names:
      return False
    rt = dict(self.cfg["runtime"])
    if isinstance(rt_values, dict):
      rt.update({k: str(v) for k, v in rt_values.items()})
    path = rt.get(key, "").strip()
    if not path:
      self.q.put(("warn", f"Set {names[key]} first" + (" (empty = the working directory of the game)." if key == "trace_dir" else ".")))
      return False
    if not os.path.isabs(path):
      self.q.put(("err", f"{names[key]} must be an absolute path."))
      return False
    try:
      os.makedirs(path, exist_ok=True)
    except OSError as e:
      self.q.put(("err", f"Unable to create {path}: {e}"))
      return False
    if not open_path(path):
      self.q.put(("err", f"Unable to open {path}"))
      return False
    return True

  def open_release_page(self):
    import webbrowser
    webbrowser.open(GUI_RELEASES_URL)  # fixed URL: nothing coming from the UI is opened
    return True

  def open_link(self, url):
    """Opens a link of the release notes in the browser (https only)."""
    import webbrowser
    from urllib.parse import urlparse
    u = urlparse(str(url or ""))
    if u.scheme == "https" and u.netloc:
      webbrowser.open(u.geturl())
      return True
    return False

  def open_origin_page(self):
    import webbrowser
    webbrowser.open("https://github.com/Lenny4250gh/AnyPS5-GUI")  # fixed URL
    return True

  def open_fork_guide(self):
    """Opens the 'forks and custom builds' section of the README in the system browser (fixed URL)."""
    import webbrowser
    webbrowser.open("https://github.com/Lenny4250gh/AnyPS5-GUI#advanced-forks-and-custom-builds")  # fixed URL
    return True

  def open_compatibility(self):
    """Opens the 'Game bring-up' discussions of AnyPS5 searching the title ID of the selected converted game.
    The address is fixed: the only thing added to it is the title ID read from param.json (letters, digits,
    '_' and '-' only)."""
    import urllib.parse
    import webbrowser
    pkg = self._selected_game_package()
    if not pkg:
      return {"ok": False, "error": "Select or launch a converted game first."}
    tid = title_info(pkg)[0].strip()
    if not tid:
      return {"ok": False, "error": "The title ID could not be read from app0/sce_sys/param.json."}
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,32}", tid):
      return {"ok": False, "error": "The title ID in param.json looks invalid: " + tid[:40]}
    q = urllib.parse.quote_plus('category:"Game bring-up" ' + tid)
    webbrowser.open("https://github.com/boykopovar/AnyPS5/discussions?discussions_q=" + q)
    return {"ok": True, "title_id": tid}

  def open_feedback(self, kind=""):
    """Opens the GitHub issues of the GUI. Only the fixed repo URL is used; the only data added to it are
    the app version and the system, visible and editable in the browser before the user posts anything."""
    import platform
    import webbrowser
    from urllib.parse import urlencode
    base = "https://github.com/%s/%s/issues" % (GUI_REPO_OWNER, GUI_REPO_NAME)
    if kind == "bug":
      system = "%s; target %s" % (platform.platform(), self.cfg["relinker"].get("target", ""))
      url = base + "/new?" + urlencode({"template": "bug.yml", "version": APP_VERSION, "system": system})
    elif kind == "idea":
      url = base + "/new?" + urlencode({"template": "suggestion.yml"})
    elif kind == "all":
      url = base
    else:
      url = base + "/new/choose"
    webbrowser.open(url)
    return True

  def pick_folder(self):
    res = webview.windows[0].create_file_dialog(getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
                                                or webview.FOLDER_DIALOG)
    if res:
      self.folder = res[0]
    return self.folder

  def pick_any_folder(self):
    """Folder dialog that only returns the choice (pick_folder also sets the selected game folder)."""
    res = webview.windows[0].create_file_dialog(getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
                                                or webview.FOLDER_DIALOG)
    if not res:
      return None
    return res if isinstance(res, str) else res[0]

  def pick_queue_games(self):
    """Queue: choose a game folder, or a folder that contains several game folders. -> {root, games[]}"""
    root = self.pick_any_folder()
    if not root:
      return None
    root = os.path.normpath(root)
    if not folder_game_problems(root):
      return {"root": root, "games": [root]}
    games = []
    try:
      for n in sorted(os.listdir(root), key=str.lower):
        p = os.path.join(root, n)
        if os.path.isdir(p) and not folder_game_problems(p):
          games.append(p)
    except OSError as _exc:
      hidden_error("pick_queue_games", _exc)
    return {"root": root, "games": games}

  def pick_queue_archives(self):
    """Queue: choose one or more game archives (.zip / .rar)."""
    kind = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
    res = webview.windows[0].create_file_dialog(kind, allow_multiple=True,
                                                file_types=(ARCHIVE_DIALOG_FILTER, "All files (*.*)"))
    if not res:
      return []
    return [r for r in ([res] if isinstance(res, str) else list(res)) if is_archive_source(r)]

  def queue_name(self, path):
    """Display name of a queued game (folder name, or the game name inside an archive)."""
    try:
      if is_archive_source(path):
        return archive_game_name(path)
    except Exception as _exc:
      hidden_error("queue_name", _exc)
    return os.path.basename(os.path.normpath(path)) or path

  # ---------- Library ----------
  def _lib_data(self):
    if not hasattr(self, "_lib"):
      self._lib = load_library()
    return self._lib

  def _lib_pkg(self, path):
    """The package `path`, only if it is one of the packages the Library lists (nothing else is touched)."""
    data = self._lib_data()
    want = os.path.normcase(norm(str(path or "")))
    for p in find_packages(library_roots(self.cfg, data)):
      if os.path.normcase(p) == want:
        return p
    return ""

  def _lib_dump(self, path):
    """The dump `path`, only if it is one of the dumps the Library lists."""
    want = os.path.normcase(norm(str(path or "")))
    for p in find_dumps(self._lib_data()["dump_dirs"]):
      if os.path.normcase(p) == want:
        return p
    return ""

  def library_list(self, refresh_sizes=False):
    """The games of the Library: one entry per Title ID with its dumps and converted packages. Sizes are
    calculated in the background: `sizing` is True while some are missing."""
    data = self._lib_data()
    roots = library_roots(self.cfg, data)
    pkgs, dumps = find_packages(roots), find_dumps(data["dump_dirs"])
    sizing = start_sizes(pkgs + dumps, data, bool(refresh_sizes))
    pkg_rows = [package_row(p, data) for p in pkgs]
    dump_rows = [dump_row(p) for p in dumps]
    for r in dump_rows:
      c = data["sizes"].get(r["path"])
      r["size"] = c["size"] if c else None
    games = build_games(pkg_rows, dump_rows, data["comments"])
    sizing = sizing or any(r["size"] is None for r in pkg_rows + dump_rows)
    return clean({"games": games, "roots": roots, "extra": list(data["dirs"]), "dump_dirs": list(data["dump_dirs"]),
                  "sizing": sizing, "running": self.game_proc is not None and self.game_proc.poll() is None,
                  "ui": data["ui"]})

  def library_icon(self, path):
    pkg = self._lib_pkg(path)
    if pkg:
      return icon_uri(pkg)
    dmp = self._lib_dump(path)
    return icon_uri(dmp, dump=True) if dmp else ""

  def library_add_dir(self, kind="conv"):
    """Adds a folder of PS5 dumps (kind "dump") or of converted games (kind "conv"). Only its direct
    sub-folders are looked at."""
    d = self.pick_any_folder()
    if not d:
      return {"ok": False, "cancelled": True}
    d = norm(d)
    if is_fs_root(d):
      return {"ok": False, "error": "Choose a folder with your games, not a whole drive."}
    data = self._lib_data()
    dump = kind == "dump"
    lst = data["dump_dirs"] if dump else data["dirs"]
    if d in lst or (not dump and d in library_roots(self.cfg, data)):
      return {"ok": False, "error": "This folder is already in the Library."}
    n = len(find_dumps([d]) if dump else find_packages([d]))
    lst.append(d)
    save_library(data)
    return {"ok": True, "path": d, "found": n}

  def library_remove_dir(self, path):
    data = self._lib_data()
    for k in ("dirs", "dump_dirs"):
      if path in data[k]:
        data[k].remove(path)
        save_library(data)
    return True

  def library_delete(self, kind, path):
    """Deletes (for good) a PS5 dump (kind "dump") or a converted package (kind "pkg") that the Library lists.
    Folders added to the Library (and the output folder) themselves are never deleted, and neither is a package
    whose game is running."""
    data = self._lib_data()
    if kind == "dump":
      target = self._lib_dump(path)
    elif kind == "pkg":
      target = self._lib_pkg(path)
    else:
      return {"ok": False, "error": "Unknown item."}
    if not target:
      return {"ok": False, "error": "This game is no longer in the Library."}
    n = os.path.normcase(norm(target))
    if is_fs_root(target) or n in {os.path.normcase(norm(d)) for d in library_roots(self.cfg, data) + list(data["dump_dirs"])}:
      return {"ok": False, "error": "This is a Library folder, not a game: remove it from \"Folders…\" instead."}
    if kind == "pkg" and self.game_proc is not None and self.game_proc.poll() is None and self.game_pkg \
            and os.path.normcase(norm(self.game_pkg)) == n:
      return {"ok": False, "error": "This game is running: close it first."}
    try:
      if os.path.islink(target):
        os.unlink(target)  # a link: remove the link only, never what it points to
      else:
        _rmtree_force(target)
    except OSError as e:
      return {"ok": False, "error": f"Could not delete the folder: {e}"}
    left = os.path.lexists(target)
    data["sizes"].pop(target, None)
    if kind == "pkg":
      data["game_rt"].pop(target, None)
    save_library(data)
    self.q.put(("warn" if left else "ok", ("Deleted only in part (some files are in use): " if left else "Deleted: ") + target))
    return {"ok": not left, "error": "Some files could not be deleted (in use?). Close the programs using them and try again." if left else ""}

  def library_set_ui(self, values):
    """How the Library looks: table or grid view and the width of each table column (kept in library.json:
    the web view does not keep localStorage between runs)."""
    if not isinstance(values, dict):
      return False
    ui = self._lib_data()["ui"]
    if "grid" in values:
      ui["grid"] = bool(values["grid"])
    if isinstance(values.get("widths"), dict):
      w = {k: int(v) for k, v in values["widths"].items()
           if k in LIB_COLUMNS and isinstance(v, (int, float)) and not isinstance(v, bool) and 40 <= v <= 3000}
      if w:
        ui["widths"] = w
      else:
        ui.pop("widths", None)
    save_library(self._lib_data())
    return True

  def library_set_comment(self, key, text):
    """Comment of a game (the key is its Title ID, or its name when it has none)."""
    key = str(key or "")
    if not key or len(key) > 300:
      return False
    data = self._lib_data()
    text = str(text or "").strip()[:500]
    if text:
      data["comments"][key] = text
    else:
      data["comments"].pop(key, None)
    save_library(data)
    return True

  def library_get_rt(self, path):
    pkg = self._lib_pkg(path)
    return {k: v for k, v in (self._lib_data()["game_rt"].get(pkg) or {}).items() if k in GAME_RT_KEYS} if pkg else {}

  def library_set_rt(self, path, values):
    pkg = self._lib_pkg(path)
    if not pkg or not isinstance(values, dict):
      return False
    data = self._lib_data()
    clean_v = {k: str(values[k]).strip() for k in GAME_RT_KEYS if str(values.get(k, "")).strip()}
    if clean_v:
      data["game_rt"][pkg] = clean_v
    else:
      data["game_rt"].pop(pkg, None)
    save_library(data)
    return True

  def library_launch(self, path):
    if self.game_proc is not None and self.game_proc.poll() is None:
      return {"ok": False, "error": "A game is already running."}
    pkg = self._lib_pkg(path)
    if not pkg:
      return {"ok": False, "error": "This game is no longer in the Library."}
    target, exe = package_exe(pkg)
    cfg = {s: dict(v) for s, v in self.cfg.items()}
    cfg["runtime"] = merged_runtime(cfg, self._lib_data(), pkg)
    try:
      self.game_proc = launch_game(pkg, target, cfg, exe=exe, log=lambda k, m: self.q.put((k, m)))
    except (OSError, ValueError) as e:
      self.q.put(("err", f"Unable to launch the game: {e}"))
      return {"ok": False, "error": str(e)}
    self.q.put(("ok", f"Game started (PID {self.game_proc.pid}) from {pkg}"))
    self.game_pkg, self.game_exit = pkg, None
    self.game_exe_dir, self.game_started = os.path.dirname(exe), time.time()
    self._rpc_set("Playing a converted game", "Running with AnyPS5", timer=True)
    return {"ok": True}

  def library_shortcut(self, path):
    pkg = self._lib_pkg(path)
    if not pkg:
      return {"ok": False, "error": "This game is no longer in the Library."}
    target, exe = package_exe(pkg)
    cfg = {s: dict(v) for s, v in self.cfg.items()}
    rt = merged_runtime(cfg, self._lib_data(), pkg)
    try:
      create_shortcut(pkg, os.path.basename(exe), target, rt, title_name(pkg) or os.path.basename(pkg),
                      lambda k, m: self.q.put((k, m)), SCRIPT_DIR)
    except (OSError, ValueError) as e:
      self.q.put(("err", f"Unable to create shortcut: {e}"))
      return {"ok": False, "error": str(e)}
    return {"ok": True}

  def library_open(self, path, name):
    """Opens the package folder, _sd, _sd_mem, _trophy or the screenshots folder of a Library game."""
    pkg = self._lib_pkg(path)
    if not pkg:
      return {"ok": False, "error": "This game is no longer in the Library."}
    if name == "screenshots":
      d = screenshots_dir(pkg, merged_runtime(self.cfg, self._lib_data(), pkg))
      d = d if os.path.isabs(d) else os.path.join(pkg, d)
    elif name == "package":
      d = pkg
    elif name in ("_sd", "_sd_mem", "_trophy"):
      d = os.path.join(pkg, name)
    else:
      return {"ok": False, "error": "Unknown folder."}
    if not os.path.isdir(d):
      return {"ok": False, "error": "The folder does not exist yet (the game has not created it)."}
    return {"ok": bool(open_path(d)), "path": d}

  def library_backup(self, path, name):
    pkg = self._lib_pkg(path)
    if name not in ("_sd", "_sd_mem") or not pkg or not os.path.isdir(os.path.join(pkg, name)):
      return {"ok": False, "error": f"Folder {name} was not found."}
    dest = self.pick_any_folder()
    if not dest:
      return {"ok": False, "cancelled": True}
    try:
      archive = os.path.join(dest, f"{name}-backup-{time.strftime('%Y%m%d-%H%M%S')}")
      shutil.make_archive(archive, "zip", root_dir=pkg, base_dir=name)
      return {"ok": True, "path": archive + ".zip"}
    except OSError as e:
      return {"ok": False, "error": str(e)}

  def library_reset_trophies(self, path):
    pkg = self._lib_pkg(path)
    d = os.path.join(pkg, "_trophy") if pkg else ""
    if not d or not os.path.isdir(d):
      return {"ok": False, "error": "Folder _trophy was not found."}
    try:
      shutil.rmtree(d)
      os.makedirs(d, exist_ok=True)
      return {"ok": True}
    except OSError as e:
      return {"ok": False, "error": str(e)}

  def library_trophies(self, path):
    pkg = self._lib_pkg(path)
    if not pkg or not os.path.isdir(os.path.join(pkg, "_trophy")):
      return None
    try:
      return clean(trophy_summary(pkg))
    except Exception as _exc:
      hidden_error("library_trophies", _exc)
      return None

  def pick_patch_file(self):
    kind = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
    res = webview.windows[0].create_file_dialog(kind, file_types=("Patch files (*.toml)", "All files (*.*)"))
    if not res:
      return None
    return res if isinstance(res, str) else res[0]

  def verify_patches(self, rl_values=None):
    """Verify the patch file of the settings dialog against the selected game folder."""
    rl = dict(self.cfg["relinker"])
    if isinstance(rl_values, dict):
      rl.update({k: str(v) for k, v in rl_values.items()})
    level, text = relink_options_mod.verify_patches(rl, self.folder)
    return {"level": level, "text": text}

  def suggest_patches(self, rl_values=None, list_all=False):
    """Ask tools/suggest_patches.py which catalog patch fits the selected game (nothing is applied)."""
    rl = dict(self.cfg["relinker"])
    if isinstance(rl_values, dict):
      rl.update({k: str(v) for k, v in rl_values.items()})
    level, text, patch = relink_options_mod.suggest_patches(rl, self.folder, bool(list_all))
    return {"level": level, "text": text, "patch": patch}

  def run_nid_patcher(self, rl_values=None):
    """Pick one or more native executables/libraries and patch their imports in place with nid_patcher."""
    rl = dict(self.cfg["relinker"])
    if isinstance(rl_values, dict):
      rl.update({k: str(v) for k, v in rl_values.items()})
    kind = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
    res = webview.windows[0].create_file_dialog(kind, allow_multiple=True, file_types=("All files (*.*)",))
    if not res:
      return {"level": "muted", "text": "Cancelled."}
    files = [res] if isinstance(res, str) else list(res)
    level, text = relink_options_mod.run_nid_patcher(rl, files)
    return {"level": level, "text": text}

  def verify_output(self, rl_values=None):
    """Ask for a converted Linux executable and check it against its <stem>.patches.toml manifest."""
    rl = dict(self.cfg["relinker"])
    if isinstance(rl_values, dict):
      rl.update({k: str(v) for k, v in rl_values.items()})
    kind = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
    res = webview.windows[0].create_file_dialog(kind, file_types=("All files (*.*)",))
    if not res:
      return {"level": "muted", "text": "Cancelled."}
    path = res if isinstance(res, str) else res[0]
    level, text = relink_options_mod.verify_output(rl, path)
    return {"level": level, "text": text}

  def relinker_version(self, rl_values=None):
    rl = dict(self.cfg["relinker"])
    if isinstance(rl_values, dict):
      rl.update({k: str(v) for k, v in rl_values.items()})
    level, text = relink_options_mod.relinker_version(rl)
    return {"level": level, "text": text}

  def relinker_flags(self, rl_values=None):
    """Options listed by `relinker --help` for the relinker in use (the interface disables the others)."""
    rl = dict(self.cfg["relinker"])
    if isinstance(rl_values, dict):
      rl.update({k: str(v) for k, v in rl_values.items()})
    try:
      return relinker_capabilities(rl)
    except Exception as exc:
      hidden_error("relinker_flags", exc)
      return {"known": False, "flags": []}

  def pick_output(self):
    res = webview.windows[0].create_file_dialog(getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
                                                or webview.FOLDER_DIALOG)
    return res[0] if res else None

  def pick_archive(self):
    kind = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
    res = webview.windows[0].create_file_dialog(kind, file_types=(ARCHIVE_DIALOG_FILTER, "All files (*.*)"))
    if res:
      self.folder = res if isinstance(res, str) else res[0]
    return self.folder

  pick_zip = pick_archive  # old name

  def game_name(self):
    """Name of the game: folder name, or (for a zip) its internal folder / the zip name."""
    if is_archive_source(self.folder):
      return archive_game_name(self.folder)
    return os.path.basename(os.path.normpath(os.path.abspath(self.folder)))

  def set_folder(self, path):
    self.folder = (path or "").strip().strip('"')
    return self.folder

  def make_bug_report(self, log_text):
    """Short, redacted text to paste in an issue."""
    cfg = {sec: dict(v) for sec, v in self.cfg.items()}
    target = cfg["relinker"]["target"]
    pkg = self.game_pkg
    game = os.path.basename(pkg.rstrip("/\\")) if pkg else ""
    return build_bug_report(cfg, target, game, pkg, log_text or "", self.game_exit,
                              run_report=run_report_text(self.game_exe_dir, self.game_started))

  def make_report(self, log_text):
    """Test report (GAME_TESTING.md points 1-8) as plain text; the UI saves it to a file."""
    cfg = {sec: dict(v) for sec, v in self.cfg.items()}
    target = cfg["relinker"]["target"]
    pkg = self.game_pkg
    game = ""
    if not pkg and self.folder:
      game = self.game_name()
      pkg = package_dir(cfg["settings"]["output_dir"] or DEFAULT_OUTPUT_DIR, game, target)
    elif pkg:
      game = os.path.basename(pkg.rstrip("/\\"))
    return build_report(cfg, target, game, pkg, log_text or "", self.game_exit)

  def save_log(self, text, filename="anyps5-log.txt"):
    kind = getattr(getattr(webview, "FileDialog", None), "SAVE", None) or webview.SAVE_DIALOG
    res = webview.windows[0].create_file_dialog(kind, save_filename=filename)
    if res:
      path = res if isinstance(res, str) else res[0]
      with open(path, "w", encoding="utf-8") as f:
        f.write(text)
      return True
    return False

  # ---------- status ----------
  def status(self):
    rl, target = self.cfg["relinker"], self.cfg["relinker"]["target"]
    set_active_relinker_cfg(rl)
    if repo_unwrapper_path(rl):
      unw = ("tools/" + UNWRAP_SCRIPT_NAME + " (GitHub repo)  ✔", "ok")
    elif unwrapper_status():
      unw = (os.path.basename(unwrapper_status()) + "  ✔", "ok")
    elif rl.get("relinker_source") == "github":
      unw = ("will be taken from the GitHub repo  ⚠", "warn")
    elif rl.get("relinker_source") == "release":
      unw = ("will be downloaded from the GitHub release  ⚠", "warn")
    else:
      unw = ("not found in the app data folder  ✖", "err")
    li = libs_info(target, rl)
    libs = ((libs_label(li) + "  ✔", "ok") if li else
            (("will be built from the GitHub repo  ⚠", "warn") if wants_built_libs(rl)
             else ("will be downloaded from the GitHub release  ⚠", "warn") if rl.get("relinker_source") == "release"
             else (f"no {LIBS_ZIP_PREFIX}*.zip in the app data folder  ✖", "err")))
    rows = []
    zinfo = scan_archive(self.folder) if is_archive_source(self.folder) else None
    if zinfo and zinfo.get("error"):
      rows += [["eboot", "eboot.bin", "cannot read the archive  ✖  " + zinfo["error"], "err"],
               ["modules", "Modules (sce_module/prx)", "—", "muted"], ["dirs", "sce_sys", "—", "muted"]]
      zinfo = {"skip": True}
    info = scan_game(self.folder, target) if self.folder and not zinfo else None
    if zinfo and zinfo.get("skip"):
      pass
    elif zinfo:
      rows.append(["eboot", "eboot.bin", *(("inside the archive  ✔  extracted automatically", "ok") if zinfo["eboot"]
                                           else ("not found in the archive  ✖", "err"))])
      if zinfo["module_dirs"]:
        rows.append(["modules", "Modules (sce_module/prx)",
                     f"{', '.join(zinfo['module_dirs'])}: {zinfo['modules_total']} files (in the archive)  ✔", "ok"])
      else:
        rows.append(["modules", "Modules (sce_module/prx)", "none found in the archive  ✖", "err"])
      rows.append(["dirs", "sce_sys", "present  ✔" if zinfo["dirs"] else "not found  ✖",
                   "ok" if zinfo["dirs"] else "err"])
    elif info is None:
      for k, t in (("eboot", "eboot.bin"), ("modules", "Modules (sce_module/prx)"), ("dirs", "sce_sys")):
        rows.append([k, t, "—", "muted"])
    else:
      texts = {"elf": ("ELF  ✔  ready", "ok"), "self": ("SELF  ⚠  needs unwrapping", "warn"),
               "unknown": ("unknown format  ✖", "err"), "missing": ("not found  ✖", "err")}
      rows.append(["eboot", "eboot.bin", *texts[info["eboot"]]])
      if info["module_error"]:
        rows.append(["modules", "Modules (sce_module/prx)", info["module_error"] + "  ✖", "err"])
      else:
        d = f"{', '.join(info['module_dirs'])}: {info['modules_total']} files"
        if info["modules_self"]:
          rows.append(["modules", "Modules (sce_module/prx)", d + f", {info['modules_self']} SELF to unwrap  ⚠", "warn"])
        else:
          rows.append(["modules", "Modules (sce_module/prx)", d + "  ✔", "ok"])
      rows.append(["dirs", "sce_sys", "present  ✔" if info["sce_sys"] else "not found  ✖",
                   "ok" if info["sce_sys"] else "err"])
    rows += [["unwrapper", "unwrap_self.py", *unw], ["relinker", "Relinker", *relinker_status(rl)],
             ["libs", "prx-*.zip", *libs]]
    rows.append(["cpu", "CPU", *cpu_status(target, yes(self.cfg["settings"]["intel"]), yes(rl["rosetta"]), yes(rl.get("to_avx", "no")))])
    rows.append(["gpu", "GPU", *gpu_status(self.cfg.get("runtime", {}).get("gpu", ""))])
    mem = memory_status()
    if mem and target == "windows":
      rows.append(["memory", "Memory", *mem])
    nv = nvidia_status()
    if nv:
      rows.append(["nvidia", "NVIDIA driver", *nv])
    ud = udmabuf_status()
    if ud and target not in ("windows", "macos"):
      rows.append(["udmabuf", "/dev/udmabuf", *ud])
    return clean(rows)

  def gpu_names(self):
    """Detected hardware GPUs (lowercase), offered as suggestions in the ANYPS5_GPU field."""
    return [g["name"].lower() for g in gpu_info()["gpus"] if not g["software"]]

  def space(self):
    if is_archive_source(self.folder):
      z = scan_archive(self.folder)
      if not z:
        return None
      if z.get("error"):
        return {"text": z["error"], "level": "err"}
      free = shutil.disk_usage(existing_parent(DATA_DIR)).free
      need = z["size"] * 2 + 64 * 1024 * 1024  # extracted copy + game copy (rough estimate)
      return {"text": f"~{format_size(need)} needed (archive extraction + package) · {format_size(free)} free"
                      + ("  ✔" if free >= need else "  ✖  not enough space"),
              "level": "ok" if free >= need else "err"}
    if not self.folder or not os.path.isdir(self.folder):
      return None
    s, rl = self.cfg["settings"], self.cfg["relinker"]
    try:
      r = space_requirements(self.folder, rl["target"], s["mode"], s["output_dir"] or DEFAULT_OUTPUT_DIR,
                               yes(s["auto_unwrap"]), not yes(s["keep_game"]))
    except Exception as _exc:
      hidden_error("space", _exc)
      return {"text": "unable to calculate", "level": "warn"}
    avail = r["free"] + r["reusable"]
    t = f"~{format_size(r['needed'])} needed · {format_size(avail)} free"
    return {"text": t + ("  ✔" if avail >= r["needed"] else "  ✖  not enough space"),
            "level": "ok" if avail >= r["needed"] else "err"}

  # ---------- actions ----------
  def prepare(self, action):
    """Checks + the confirmations the Tk version asked with dialogs. -> {error, confirms[], tools[]}"""
    res = {"error": None, "confirms": [], "tools": []}
    if action in ("play", "download"):
      return res
    zipsrc = is_archive_source(self.folder)
    if action != "build" and not (self.folder and (os.path.isdir(self.folder) or zipsrc)):
      res["error"] = "Select the game folder or an archive of the game."
      return res
    if zipsrc and action != "build":
      z = scan_archive(self.folder)
      if z.get("error"):
        res["error"] = "Cannot read the archive: " + z["error"]
        return res
    if action not in ("build", "restore"):
      problems = archive_game_problems(scan_archive(self.folder)) if zipsrc else folder_game_problems(self.folder)
      if problems:
        res["error"] = (("The archive" if zipsrc else "The folder") + " does not look like a PS5 game, missing: "
                        + "; ".join(problems) + ".")
        return res
    cfg = self.cfg
    rl = cfg["relinker"]
    if action == "dry_run":
      if zipsrc:
        res["error"] = "The preview works on a game folder, not on an archive: extract it and select the folder."
      return res
    if action == "restore":
      if zipsrc:
        res["error"] = "Restore backup works on a game folder, not on an archive: select the folder."
        return res
      
      if not os.path.isdir(get_backup_dir(os.path.basename(os.path.normpath(self.folder)))):
        res["error"] = "There are no backups for this game."
      else:
        res["confirms"].append(["Restore backup", "The current files in the game (eboot.bin and modules) will be "
                                "OVERWRITTEN with the original backups. Proceed?"])
      return res
    if action == "build":
      p = source_preflight(dict(rl))
      if p and not p.startswith(("git", "cmake", "git and")):
        res["error"] = p
        return res
    elif cfg["settings"]["mode"] == "unwrap":
      return res
    if rl.get("relinker_source") == "github":
      missing = missing_build_tools(rl)
      if missing:
        names = ", ".join(TOOL_NAMES[t] for t in missing)
        if os.name != "nt" or not shutil.which("winget"):
          res["error"] = f"To build from source you need: {names}. Install them with your package manager / by hand."
          return res
        res["confirms"].append(["Install missing tools", f"Missing: {names}. Install them now, automatically "
                                "(winget)? Windows may ask for administrator permission."])
        res["tools"] = missing
    if action == "start":
      base = cfg["settings"]["output_dir"] or DEFAULT_OUTPUT_DIR
      pkg = package_dir(base, self.game_name(), rl["target"])
      if os.path.exists(pkg):
        res["confirms"].append(["Folder already exists", f"'{os.path.basename(pkg)}' already exists in {base}. Overwrite it?"])
      if not yes(cfg["settings"]["keep_game"]):
        res["confirms"].append(["Move the game", "'Keep PS5 game' is OFF: the game files will be MOVED out of "
                                f"{self.folder}. Each file is deleted only after it has been copied and verified; "
                                "if the operation fails or is stopped, the files are put back. Continue?"])
    return res

  def run(self, action, tools=None):
    if self.running:
      return False
    folder = self.folder
    cfg = {s: dict(v) for s, v in self.cfg.items()}
    rl, mode = cfg["relinker"], cfg["settings"]["mode"]
    tools = tools or []
    errs = []

    def log0(k, m):
      if k == "err":
        errs.append(str(m))
      self.q.put((k, m))
      if k == "step" and not self.cancel_ev.is_set() and action not in ("play", "download", "build", "restore", "dry_run"):
        sub = discord_substate(str(m))  # Discord: show which phase of the conversion is running
        if sub:
          self._rpc_set("Unwrapping game" if mode == "unwrap" else "Converting game", sub, timer=True)
    if action == "play":
      return self._play(cfg, log0)
    if action == "download":
      title = "Downloading the GitHub release"
      job = lambda log: (set_active_relinker_cfg(rl),
                         ensure_release_assets(rl, log, self.cancel_ev, rl["target"]))[1]
      mode_download = True
    else:
      mode_download = False
    if mode_download:
      pass
    elif action == "restore":
      title, job = "Restoring backup", lambda log: do_restore(folder, log, self.cancel_ev)
    elif action == "dry_run":
      title, job = "Conversion preview (dry run)", lambda log: do_dry_run(folder, cfg, log, self.cancel_ev, self.holder)
    elif action == "build":
      def job(log):
        if tools and not install_build_tools(tools, log, self.cancel_ev, self.holder):
          return False
        return ensure_source_relinker(rl, log, self.cancel_ev, self.holder) is not None
      title = "Building the relinker from source"
    elif mode == "unwrap":
      title, job = "Mode: Unwrap only", lambda log: do_unwrap(folder, cfg, log, self.cancel_ev, self.holder)
    else:
      def job(log):
        if tools and not install_build_tools(tools, log, self.cancel_ev, self.holder):
          return False
        return do_relink(folder, cfg, log, self.cancel_ev, self.holder)
      title = ("Mode: Convert PS5 game to PC + automatic unwrap" if yes(cfg["settings"]["auto_unwrap"])
               else "Mode: Convert PS5 game to PC") + " · " + TARGETS[rl["target"]]["label"].split(" (")[0]
    if action in ("start", "unwrap_only") and is_archive_source(folder):
      zip_path, inner = folder, job

      def job(log):
        nonlocal folder
        root = extract_game_archive(zip_path, log, self.cancel_ev)
        if not root:
          return False
        folder = root
        ok = False
        try:
          ok = inner(log)
          return ok
        finally:
          if mode == "unwrap":
            log("info", f"Unwrap-only on an archive works on the extracted copy, kept in: {root}")
          else:
            cleanup_extracted(root, log)
    self.running, self.result, self.cancelled = True, None, False
    self.cancel_ev.clear()
    self.holder = {}
    log0("step", title)
    if action == "download":
      self._rpc_set("Downloading tools", "Preparing release", timer=True)
    elif action == "build":
      self._rpc_set("Building converter (relinker)", "Compiling from source", timer=True)
    elif action == "restore":
      self._rpc_set("Restoring backup", "Restoring")
    elif action == "dry_run":
      self._rpc_set("Previewing a conversion", "Dry run", timer=True)
    elif mode == "unwrap":
      self._rpc_set("Unwrapping game", "Preparing", timer=True)
    else:
      self._rpc_set("Converting game", "Preparing", timer=True)

    def work():
      t0, ok = time.time(), False
      seen0 = len(_HIDDEN_ERRORS)
      try:
        ok = bool(job(log0))
      except Cancelled:
        log0("warn", "Operation cancelled.")
      except Exception as e:
        log0("err", f"Unexpected error: {e}")
      dt = time.time() - t0
      self.cancelled = self.cancel_ev.is_set()
      if self.cancel_ev.is_set():
        log0("warn", f"Cancelled after {dt:.0f}s.")
      else:
        log0("ok" if ok else "err", f"Operation completed ({dt:.0f}s)." if ok else f"Completed with errors ({dt:.0f}s).")
      quiet = _HIDDEN_ERRORS[seen0:] if len(_HIDDEN_ERRORS) > seen0 else []
      for _t, _w, _x in quiet[:8]:  # problems the app handled by itself during this operation
        log0("detail", f"Handled without stopping ({_w}): {_x}")
      if len(quiet) > 8:
        log0("detail", f"... and {len(quiet) - 8} more in hidden-errors.log (data folder).")
      if yes(cfg["settings"].get("notify", "yes")) and not self.cancel_ev.is_set():
        try:
          game = archive_game_name(folder) if is_archive_source(folder) else (
            os.path.basename(os.path.normpath(folder)) if folder else "")
        except Exception as _exc:
          hidden_error("work", _exc)
          game = ""
        ntitle, ntext = build_notification(title, game, ok, dt, errs)
        notify_desktop(ntitle if os.name == "nt" else "AnyPS5 · " + ntitle, ntext)
      self.result, self.running = ok, False
      self._rpc_idle()

    threading.Thread(target=work, daemon=True).start()
    return True

  def _pick_exe_path(self):
    kind = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
    d = getattr(self, "last_exe_dir", "")
    if not (d and os.path.isdir(d)):
      d = self.cfg["settings"]["output_dir"] or DEFAULT_OUTPUT_DIR
    res = webview.windows[0].create_file_dialog(
      kind, directory=d, file_types=("Game executable (*.exe;*.elf)", "All files (*.*)"))
    if not res:
      return None
    path = res if isinstance(res, str) else res[0]
    self.last_exe_dir = os.path.dirname(path)
    return path

  def pick_play_folder(self):
    """Choose the converted PC game package folder used by Play."""
    try:
      kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or webview.FOLDER_DIALOG
      # Start from the app's output folder (where converted games are created) to speed up the search
      start = self.cfg["settings"].get("output_dir") or DEFAULT_OUTPUT_DIR
      if not os.path.isdir(start):
        os.makedirs(start, exist_ok=True) if not self.cfg["settings"].get("output_dir") else None
      if not os.path.isdir(start):
        start = SCRIPT_DIR
      try:
        res = webview.windows[0].create_file_dialog(kind, directory=os.path.abspath(start))
      except TypeError:  # very old pywebview without the "directory" parameter
        res = webview.windows[0].create_file_dialog(kind)
      if not res:
        return self.play_folder
      # pywebview backends differ: some return a string, others a list of paths.
      path = res if isinstance(res, str) else res[0]
      if path and os.path.isdir(path):
        self.play_folder = os.path.abspath(path)
      elif path:
        return ""
    except Exception as e:
      log("err", f"Unable to choose converted PC game folder: {e}")
      return ""
    return self.play_folder

  def set_play_folder(self, path):
    self.play_folder = os.path.abspath((path or "").strip().strip('"')) if (path or "").strip() else ""
    return self.play_folder

  def _selected_converted_game(self):
    """Resolve the converted PC package, independently of the PS5 source path."""
    cfg = self.cfg
    target = cfg["relinker"]["target"]
    if self.play_folder:
      pkg = os.path.abspath(self.play_folder)
      game = os.path.basename(os.path.normpath(pkg))
    else:
      if not self.folder:
        raise FileNotFoundError("Set the PS5 source path for Convert, and choose the converted PC game folder for Play.")
      game = archive_game_name(self.folder) if is_archive_source(self.folder) else os.path.basename(os.path.normpath(os.path.abspath(self.folder)))
      base = cfg["settings"]["output_dir"] or DEFAULT_OUTPUT_DIR
      pkg = package_dir(base, game, target)
    exe = os.path.join(pkg, TARGETS[target]["exe"])
    if not os.path.isfile(exe):
      raise FileNotFoundError(f"Converted executable not found: {exe}. Select the converted PC game folder in Play, or convert/relink the PS5 source first.")
    return pkg, target, exe, game

  def play_exe(self):
    """Launch the executable in the converted package derived from Game path."""
    if self.game_proc is not None and self.game_proc.poll() is None:
      self.q.put(("warn", "The game is already running."))
      return False
    cfg = {s: dict(v) for s, v in self.cfg.items()}
    try:
      pkg, target, exe, game = self._selected_converted_game()
      self.game_proc = launch_game(pkg, target, cfg, exe=exe, log=lambda k, m: self.q.put((k, m)))
      self.q.put(("ok", f"Game started (PID {self.game_proc.pid}) from {pkg}"))
      self.game_pkg, self.game_exit = pkg, None
      self.game_exe_dir, self.game_started = os.path.dirname(exe), time.time()
      self._rpc_set("Playing a converted game", "Running with AnyPS5", timer=True)
    except (OSError, ValueError) as e:
      self.q.put(("err", f"Unable to launch the game: {e}"))
      return False
    return True

  def shortcut_exe(self):
    """Create a shortcut for the converted executable derived from Game path."""
    cfg = {s: dict(v) for s, v in self.cfg.items()}
    try:
      pkg, target, exe, game = self._selected_converted_game()
      create_shortcut(pkg, os.path.basename(exe), target, cfg["runtime"],
                        game, lambda k, m: self.q.put((k, m)), SCRIPT_DIR)
    except (OSError, ValueError) as e:
      self.q.put(("err", f"Unable to create shortcut: {e}"))
      return False
    return True

  def _play(self, cfg, log):
    if not self.folder:
      log("err", "Select the game folder first.")
      return False
    base = cfg["settings"]["output_dir"] or DEFAULT_OUTPUT_DIR
    target = cfg["relinker"]["target"]
    pkg = package_dir(base, self.game_name(), target)
    if self.game_proc is not None and self.game_proc.poll() is None:
      log("warn", "The game is already running.")
      return False
    try:
      self.game_proc = launch_game(pkg, target, cfg, log=log)
      log("ok", f"Game started (PID {self.game_proc.pid}) from {pkg}")
      self.game_pkg, self.game_exit = pkg, None
      self.game_exe_dir = os.path.dirname(os.path.join(pkg, TARGETS[target]["exe"]))
      self.game_started = time.time()
      self._rpc_set("Playing a converted game", "Running with AnyPS5", timer=True)
    except FileNotFoundError:
      log("err", f"Run the Relink for this game first. Expected: {pkg}")
    except OSError as e:
      log("err", f"Unable to launch the game: {e}")
    return True

  def poll(self):
    lines, pct = [], "keep"
    p = self.game_proc
    if p is not None and self.game_exit is None and p.poll() is not None:
      self.game_exit = normalize_exit_code(p.returncode)
      self.q.put(("ok" if self.game_exit == 0 else "warn",
                  "Game ended, exit code " + explain_exit_code(self.game_exit)))
      self._report_summary_to_log()
      self._rpc_idle()
    while not self.q.empty():
      k, m = self.q.get()
      if k == "percent":
        pct = m
      else:
        lines.append([k, str(m)])
    return {"lines": lines, "pct": pct, "running": self.running, "ok": self.result,
            "cancelled": bool(getattr(self, "cancelled", False))}

  def _report_summary_to_log(self):
    """After the game closes: read anyps5-report.json / anyps5-recipe.json and write the summary in the log."""
    try:
      r = read_run_report(self.game_exe_dir, self.game_started)
      if r["report"] is None:
        self.q.put(("warn", r["error"] or "Run report: none found. It is not written when the game crashes or is "
                    "closed by force, or when it is off (APS5_REPORT=0)."))
        return
      self.q.put(("info", "Run report (" + REPORT_NAME + "):"))
      for line in summarize_run_report(r["report"], r["recipe"]):
        self.q.put(("info", "  " + line))
    except Exception as e:
      self.q.put(("warn", f"Run report: could not be summarised ({e})."))

  def cancel(self):
    self.cancel_ev.set()
    p = self.holder.get("proc")
    try:
      if p is not None and p.poll() is None:
        kill_tree(p)
    except Exception as _exc:
      hidden_error("cancel", _exc)
      pass
    self.q.put(("warn", "Cancellation requested…"))
    self._rpc_idle()  # Discord back to "In the launcher / Idle" right away

  # ---------- settings ----------
  def check_input(self, text):
    return [f"Line {n}: {m}" for n, m in validate_input_ini(text)[:6]]

  def reset_input(self):
    return INPUT_TEMPLATE

  # ---------- import tools into the app data folder (Local files option) ----------
  def _tool_dest(self, name):
    """(relative destination inside the data folder, None) for a file the program knows, else (None, reason)."""
    low = name.lower()
    if low == UNWRAP_SCRIPT_NAME:
      return UNWRAP_SCRIPT_NAME, None
    if low.startswith(RELINKER_PREFIX):
      if os.name == "nt" and not low.endswith(".exe"):
        return None, "not a Windows relinker (.exe)"
      if os.name != "nt" and (low.endswith(".exe") or low.endswith(NOT_BINARY_EXT)):
        return None, "not a relinker for this system"
      return name, None
    if low.startswith(LIBS_ZIP_PREFIX) and low.endswith(".zip"):
      return name, None
    if low in ("import_audit.py", "pad_input.py"):
      return os.path.join("tools", name), None
    if low == INPUT_INI_NAME:
      return INPUT_INI_NAME, None
    if low.endswith((".ttf", ".otf", ".ttc")):
      return os.path.join(FONTS_DIR_NAME, name), None
    if low.startswith("nid_patcher"):
      if os.name == "nt" and not low.endswith(".exe"):
        return None, "not a Windows nid_patcher (.exe)"
      if os.name != "nt" and (low.endswith(".exe") or low.endswith(NOT_BINARY_EXT)):
        return None, "not a nid_patcher for this system"
      return name, None
    return None, "not a file the program uses"

  def pick_tool_files(self):
    """Choose files to copy into the data folder. Returns the plan: [{src, name, dest, exists, skip}]."""
    kind = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
    res = webview.windows[0].create_file_dialog(
      kind, allow_multiple=True,
      file_types=("Tool files (*.py;*.zip;*.exe;*.ini;*.ttf;*.otf;*.ttc)", "All files (*.*)"))
    if not res:
      return []
    paths = [res] if isinstance(res, str) else list(res)
    plan = []
    for src in paths:
      name = os.path.basename(src)
      rel, why = self._tool_dest(name)
      item = {"src": src, "name": name, "skip": why or "", "dest": rel or "", "exists": False}
      if rel:
        dest = os.path.join(DATA_DIR, rel)
        item["exists"] = os.path.isfile(dest)
        try:
          if item["exists"] and os.path.samefile(src, dest):
            item["skip"] = "already in the app data folder"
        except OSError:
          pass
      plan.append(item)
    return plan

  def import_tool_files(self, plan, replace):
    """Copy the files of the plan into the data folder. Existing ones are overwritten only if `replace`."""
    done, kept, errors = [], [], []
    for it in plan or []:
      if it.get("skip") or not it.get("dest"):
        continue
      rel, why = self._tool_dest(os.path.basename(it["src"]))  # never trust the destination coming from the page
      if not rel:
        continue
      dest = os.path.join(DATA_DIR, rel)
      if os.path.isfile(dest) and not replace:
        kept.append(rel)
        continue
      try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".part"
        shutil.copyfile(it["src"], tmp)
        if os.name != "nt" and rel.lower().startswith((RELINKER_PREFIX, "nid_patcher")):
          os.chmod(tmp, os.stat(tmp).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(tmp, dest)
        done.append(rel)
      except OSError as e:
        errors.append(f"{rel}: {e}")
    unwrapper_mod._unwrapper = None  # unwrap_self.py may have been replaced: reload it on next use
    _libs_cache.clear()
    for item in [("ok", "Imported into the app data folder: " + ", ".join(done)) if done else None,
                 ("info", "Kept existing: " + ", ".join(kept)) if kept else None,
                 ("err", "Import failed: " + "; ".join(errors)) if errors else None]:
      if item:
        self.q.put(item)
    return {"done": done, "kept": kept, "errors": errors}

  def import_input(self):
    """Pick an input .ini made by someone else: its text goes into the editor (nothing is written yet)."""
    kind = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
    res = webview.windows[0].create_file_dialog(kind, file_types=("Input config (*.ini;*.txt)", "All files (*.*)"))
    if not res:
      return None
    path = res if isinstance(res, str) else res[0]
    try:
      if os.path.getsize(path) > 256 * 1024:
        return {"error": "The file is too big to be an input configuration."}
      with open(path, encoding="utf-8-sig") as fh:
        text = fh.read()
    except (OSError, UnicodeDecodeError) as e:
      return {"error": f"Unable to read the file: {e}"}
    errs = [f"Line {n}: {m}" for n, m in validate_input_ini(text)[:6]]
    return {"name": os.path.basename(path), "text": text.replace("\r\n", "\n"), "errors": errs}

  def export_input(self, text):
    """Save the input configuration currently shown in the editor to an .ini file chosen by the user."""
    kind = getattr(getattr(webview, "FileDialog", None), "SAVE", None) or webview.SAVE_DIALOG
    res = webview.windows[0].create_file_dialog(kind, save_filename=INPUT_INI_NAME,
                                                file_types=("Input config (*.ini)", "All files (*.*)"))
    if not res:
      return None
    path = res if isinstance(res, str) else res[0]
    try:
      with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text.rstrip() + "\n")
    except OSError as e:
      return {"error": f"Unable to save the file: {e}"}
    return {"name": os.path.basename(path)}

  def export_section(self, tab, values):
    """Save the options of one settings section (Conversion, Source build, Runtime) to a .json file."""
    kind = getattr(getattr(webview, "FileDialog", None), "SAVE", None) or webview.SAVE_DIALOG
    fname = "anyps5-" + re.sub(r"[^a-z0-9]+", "-", str(tab).lower()).strip("-") + ".json"
    res = webview.windows[0].create_file_dialog(kind, save_filename=fname,
                                                file_types=("Settings (*.json)", "All files (*.*)"))
    if not res:
      return None
    path = res if isinstance(res, str) else res[0]
    try:
      with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"anyps5_settings": tab, "values": values}, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    except OSError as e:
      return {"error": f"Unable to save the file: {e}"}
    return {"name": os.path.basename(path)}

  def import_section(self):
    """Pick a .json made by export_section: its content is returned to the UI (nothing is written yet)."""
    kind = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
    res = webview.windows[0].create_file_dialog(kind, file_types=("Settings (*.json)", "All files (*.*)"))
    if not res:
      return None
    path = res if isinstance(res, str) else res[0]
    try:
      if os.path.getsize(path) > 256 * 1024:
        return {"error": "The file is too big to be a settings file."}
      with open(path, encoding="utf-8-sig") as fh:
        data = json.load(fh)
    except (OSError, UnicodeDecodeError, ValueError) as e:
      return {"error": f"Unable to read the file: {e}"}
    if not isinstance(data, dict) or not isinstance(data.get("values"), dict) or "anyps5_settings" not in data:
      return {"error": "This is not an AnyPS5 settings file."}
    return {"name": os.path.basename(path), "tab": str(data["anyps5_settings"]), "values": data["values"]}

  def delete_input(self):
    try:
      if os.path.isfile(INPUT_INI_FILE):
        os.remove(INPUT_INI_FILE)
      return True
    except OSError as e:
      return str(e)

  def save_settings(self, values, input_text, write_input):
    if self.relocated:
      return {"error": "The storage folder was changed: close and reopen the app first."}
    new = {s: dict(v) for s, v in self.cfg.items()}
    for s, items in values.items():
      new[s].update({k: str(v) for k, v in items.items()})
    problem = (validate_relink_options(yes(new["settings"]["intel"]), new["relinker"])
               or validate_runtime_options(new["runtime"], new["relinker"]))
    if problem:
      return {"error": problem}
    if write_input:
      body = input_text.rstrip() + "\n"
      has = any(__import__("re").split(r"[#;]", l, 1)[0].strip() for l in body.splitlines())
      try:
        os.makedirs(DATA_DIR, exist_ok=True)
        if has:
          with open(INPUT_INI_FILE, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        elif os.path.isfile(INPUT_INI_FILE):
          os.remove(INPUT_INI_FILE)
      except OSError as e:
        return {"error": f"Unable to save {INPUT_INI_NAME}: {e}"}
    self.cfg = new
    save_config(new)
    self._rpc_refresh()
    return {"error": None}
