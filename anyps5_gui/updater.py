"""Update check and self-update of AnyPS5 GUI."""

import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile

from .settings import APP_VERSION, DATA_DIR, hidden_error, LEGACY_DIR_NAME
from .config import host_target
from .release import _download_file, fetch_release_info


GUI_REPO_OWNER = "Lenny4250gh"
GUI_REPO_NAME = "AnyPS5-GUI"
GUI_RELEASES_URL = "https://github.com/%s/%s/releases/latest" % (GUI_REPO_OWNER, GUI_REPO_NAME)
GUI_HOME_URL = "https://github.com/%s/%s#readme" % (GUI_REPO_OWNER, GUI_REPO_NAME)  # README, download section


def _version_tuple(v):
  return tuple(int(n) for n in re.findall(r"\d+", v or ""))


def check_gui_update():
  """{'version', 'current'} if a newer release exists on GitHub, else None. Never raises."""
  try:
    info = fetch_release_info(GUI_REPO_OWNER, GUI_REPO_NAME, "")
    tag = info["tag"]
    if _version_tuple(tag) > _version_tuple(APP_VERSION):
      return {"version": tag, "current": APP_VERSION, "packaged": bool(getattr(sys, "frozen", False)),
              "notes": (info.get("notes") or "")[:6000]}
  except Exception as _exc:
    hidden_error("check_gui_update", _exc)
    pass  # offline, rate limit, no release yet: stay silent
  return None

# ---------- GUI self-update (download the release asset, replace the app, restart) ----------
_OS_TOKENS = {"windows": {"win", "windows", "win32", "win64"},
              "macos": {"mac", "macos", "osx", "darwin", "macosx"},
              "linux": {"linux", "appimage"}}
_ARCH_ARM = {"arm64", "aarch64", "armv8"}
_ARCH_X64 = {"x64", "x86_64", "amd64", "win64"}
_SKIP_EXT = (".sha256", ".sha512", ".sig", ".asc", ".txt", ".md", ".json", ".blockmap", ".msi",
             ".deb", ".rpm", ".yml", ".yaml", ".pem", ".sbom", ".spdx")
_OK_EXT = {"windows": (".exe", ".zip"),
           "macos": (".zip", ".dmg", ".tar.gz", ".tgz"),
           "linux": (".appimage", ".zip", ".tar.gz", ".tgz", "")}


class UpdateUnavailable(Exception):
  """Automatic update not possible here: the GUI falls back to the release page."""


def _host_arch():
  import platform
  m = platform.machine().lower()
  return "arm" if (m in _ARCH_ARM or m.startswith("arm")) else "x64"


def pick_gui_asset(assets, host=None, arch=None):
  """Asset of the release built for this system (None if there is none)."""
  host, arch = host or host_target(), arch or _host_arch()
  best, best_score = None, -1
  for a in assets:
    n = a["name"].lower()
    if n.endswith(_SKIP_EXT):
      continue
    if host == "windows" and re.search(r"setup|install", n):
      continue  # a wizard cannot replace the exe silently
    tokens = set(re.split(r"[^a-z0-9]+", n))
    oses = {k for k, v in _OS_TOKENS.items() if tokens & v}
    ext = next((e for e in (".tar.gz", ".tgz", ".appimage", ".exe", ".zip", ".dmg") if n.endswith(e)), "")
    if not ext and "." in n.rsplit("-", 1)[-1]:
      continue  # unknown extension
    score = 0
    if oses:
      if host not in oses:
        continue
      score += 10
    elif not ((host == "windows" and ext == ".exe") or (host == "linux" and ext == "")):
      continue
    if ext not in _OK_EXT[host]:
      continue
    a_arm, a_x64 = bool(tokens & _ARCH_ARM), bool(tokens & _ARCH_X64)
    if a_arm or a_x64:
      if (a_arm and arch == "arm") or (a_x64 and arch == "x64"):
        score += 3
      elif host != "macos":
        continue  # wrong CPU (macOS x64 still runs through Rosetta: kept, lower score)
    score += {".exe": 2, ".appimage": 2, ".zip": 1}.get(ext, 0)
    if score > best_score:
      best, best_score = a, score
  return best


def _safe_extract_zip(path, dest):
  base = os.path.realpath(dest)
  with zipfile.ZipFile(path) as z:
    for info in z.infolist():
      target = os.path.realpath(os.path.join(dest, info.filename))
      if target != base and not target.startswith(base + os.sep):
        raise OSError("unsafe path in the archive: " + info.filename)
    z.extractall(dest)
    for info in z.infolist():  # keep the executable bit
      mode = (info.external_attr >> 16) & 0o777
      if mode and not info.is_dir():
        try:
          os.chmod(os.path.join(dest, info.filename), mode | 0o600)
        except OSError:
          pass


def _safe_extract_tar(path, dest):
  import tarfile
  base = os.path.realpath(dest)
  with tarfile.open(path) as t:
    for m in t.getmembers():
      target = os.path.realpath(os.path.join(dest, m.name))
      if target != base and not target.startswith(base + os.sep):
        raise OSError("unsafe path in the archive: " + m.name)
    t.extractall(dest)


def _single_root(folder):
  """Descends while a folder contains only one folder."""
  while True:
    items = [i for i in os.listdir(folder) if not i.startswith("__MACOSX") and i != ".DS_Store"]
    if len(items) == 1 and os.path.isdir(os.path.join(folder, items[0])) and not items[0].endswith(".app"):
      folder = os.path.join(folder, items[0])
    else:
      return folder


def _find_main_exe(root, host, cur_name):
  files = [f for f in os.listdir(root) if os.path.isfile(os.path.join(root, f))]
  if host == "windows":
    files = [f for f in files if f.lower().endswith(".exe")]
  else:
    files = [f for f in files if not f.lower().endswith((".txt", ".md", ".so", ".json", ".png", ".ini"))]
  if not files:
    return None
  if cur_name in files:
    return cur_name
  return max(files, key=lambda f: os.path.getsize(os.path.join(root, f)))


def _run_detached(cmd, marker=None):
  env = dict(os.environ)
  if marker:
    env["ANYPS5_UPDATE_MARKER"] = marker  # inherited by the new app, which deletes it once its window is up
  env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"  # the new onefile exe must not reuse our temp folder
  for k in ("_MEIPASS2", "_PYI_APPLICATION_HOME_DIR", "_PYI_ARCHIVE_FILE", "_PYI_PARENT_PROCESS_LEVEL"):
    env.pop(k, None)
  if os.name == "nt":
    # NO_WINDOW + NEW_PROCESS_GROUP only. DETACHED_PROCESS must NOT be added: Windows then ignores
    # NO_WINDOW and cmd.exe opens a visible console (the black terminal during the update).
    flags = 0x00000200 | 0x08000000  # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE, in case a console is created anyway
    subprocess.Popen(cmd, creationflags=flags, startupinfo=si, close_fds=True, env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  else:
    subprocess.Popen(cmd, start_new_session=True, close_fds=True, env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


_BAT = """@echo off
chcp 65001 >nul
if exist "{splash}" start "" /b powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{splash}"
set "MAIN={main}"
set "DEST={dest}"
set "APPDIR={appdir}"
set "PAYLOAD={payload}"
set "STAGE={stage}"
set /a N=0
:retry
move /y "%MAIN%" "%DEST%" >nul 2>&1
if not errorlevel 1 goto moved
set /a N+=1
if %N% GEQ 120 goto fail
ping -n 2 127.0.0.1 >nul
goto retry
:moved
if exist "%PAYLOAD%\\_internal" if exist "%APPDIR%\\_internal" rmdir /s /q "%APPDIR%\\_internal"
xcopy "%PAYLOAD%\\*" "%APPDIR%\\" /e /y /i /q >nul 2>&1
:fail
rmdir /s /q "%STAGE%" >nul 2>&1
set "PYINSTALLER_RESET_ENVIRONMENT=1"
start "" /b "%DEST%"
(goto) 2>nul & del "%~f0"
"""

_PS_SPLASH = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
$marker = '@@MARKER@@'
$dest = '@@DEST@@'
$W = 420; $H = 132
$t0 = Get-Date
$f = New-Object System.Windows.Forms.Form
$f.Text = 'AnyPS5 GUI'
$f.FormBorderStyle = 'None'
$f.StartPosition = 'CenterScreen'
$f.TopMost = $true
$f.ShowInTaskbar = $false
$f.ClientSize = New-Object System.Drawing.Size($W, $H)
$f.BackColor = [System.Drawing.Color]::FromArgb(17, 19, 18)
$f.GetType().GetProperty('DoubleBuffered', [Reflection.BindingFlags]'Instance,NonPublic').SetValue($f, $true, $null)

function New-RoundPath($x, $y, $w, $h, $r) {
  $gp = New-Object System.Drawing.Drawing2D.GraphicsPath
  $d = $r * 2
  $gp.AddArc($x, $y, $d, $d, 180, 90)
  $gp.AddArc($x + $w - $d, $y, $d, $d, 270, 90)
  $gp.AddArc($x + $w - $d, $y + $h - $d, $d, $d, 0, 90)
  $gp.AddArc($x, $y + $h - $d, $d, $d, 90, 90)
  $gp.CloseFigure()
  return $gp
}
$f.Region = New-Object System.Drawing.Region((New-RoundPath 0 0 $W $H 18))

$white = [System.Drawing.Color]::FromArgb(240, 240, 240)
$dim = [System.Drawing.Color]::FromArgb(150, 154, 152)
$line = [System.Drawing.Color]::FromArgb(52, 56, 54)
$track = [System.Drawing.Color]::FromArgb(40, 43, 42)
$fTitle = New-Object System.Drawing.Font('Segoe UI Semibold', 13)
$fSub = New-Object System.Drawing.Font('Segoe UI', 9.5)
$bWhite = New-Object System.Drawing.SolidBrush($white)
$bDim = New-Object System.Drawing.SolidBrush($dim)
$bTrack = New-Object System.Drawing.SolidBrush($track)
$penLine = New-Object System.Drawing.Pen($line, 1)
$penIcon = New-Object System.Drawing.Pen($white, 2.4)
$penIcon.StartCap = 'Round'; $penIcon.EndCap = 'Round'; $penIcon.LineJoin = 'Round'
$penIconDim = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(70, 240, 240, 240), 2)

$f.Add_Paint({
  param($sender, $e)
  $g = $e.Graphics
  $g.SmoothingMode = 'AntiAlias'
  $g.TextRenderingHint = 'ClearTypeGridFit'
  $sec = ((Get-Date) - $t0).TotalSeconds
  # border
  $bp = New-RoundPath 0.5 0.5 ($W - 1) ($H - 1) 18
  $g.DrawPath($penLine, $bp)
  # icon: circle with a bobbing download arrow
  $g.DrawEllipse($penIconDim, 28, 30, 44, 44)
  $bob = [Math]::Sin($sec * 4) * 2.5
  $g.DrawLine($penIcon, 50, (41 + $bob), 50, (56 + $bob))
  $g.DrawLine($penIcon, 43, (50 + $bob), 50, (57 + $bob))
  $g.DrawLine($penIcon, 57, (50 + $bob), 50, (57 + $bob))
  $g.DrawLine($penIcon, 43, 64, 57, 64)
  # texts
  $g.DrawString('Updating AnyPS5 GUI', $fTitle, $bWhite, 88, 30)
  $g.DrawString('The app will restart by itself.', $fSub, $bDim, 89, 56)
  # indeterminate bar
  $tx = 28; $ty = 100; $tw = $W - 56; $th = 6; $sw = 120
  $tp = New-RoundPath $tx $ty $tw $th 3
  $g.FillPath($bTrack, $tp)
  $g.SetClip($tp)
  $pos = (($sec * 0.55) % 1.0)
  $ease = 0.5 - 0.5 * [Math]::Cos($pos * 2 * [Math]::PI)
  $sx = $tx - $sw + (($tw + $sw) * $pos)
  $sp = New-RoundPath $sx $ty $sw $th 3
  $g.FillPath($bWhite, $sp)
  $g.ResetClip()
})

$tm = New-Object System.Windows.Forms.Timer
$tm.Interval = 33
$tm.Add_Tick({
  $f.Invalidate()
  $el = ((Get-Date) - $t0).TotalSeconds
  $done = $false
  if ($el -gt 3 -and -not (Test-Path -LiteralPath $marker)) { $done = $true }
  if (-not $done -and $el -gt 2 -and ([int]($el * 30) % 15 -eq 0)) {
    # fallback for builds that do not clear the marker: a NEW process of the app with a visible window
    try {
      $np = Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.MainWindowHandle -ne 0 -and $_.StartTime -gt $t0 -and $_.Path -eq $dest }
      if ($np) { $done = $true }
    } catch {}
  }
  if ($done -or $el -gt 90) { $tm.Stop(); $f.Close() }
})
$tm.Start()
[void]$f.ShowDialog()
Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
"""

_SH_LINUX = """#!/bin/sh
MAIN="$1"; DEST="$2"; APPDIR="$3"; PAYLOAD="$4"; STAGE="$5"; shift 5
i=0
while [ $i -lt 90 ]; do
  alive=0
  for p in "$@"; do kill -0 "$p" 2>/dev/null && alive=1; done
  [ $alive -eq 0 ] && break
  sleep 1; i=$((i+1))
done
sleep 1
MARK="$ANYPS5_UPDATE_MARKER"
SPL=""
MSG="Updating AnyPS5 GUI... The app will restart by itself."
if command -v zenity >/dev/null 2>&1; then
  (sleep 120) | zenity --progress --pulsate --no-cancel --auto-close --title="AnyPS5 GUI" --text="$MSG" --width=380 >/dev/null 2>&1 &
  SPL=$!
elif command -v kdialog >/dev/null 2>&1; then
  kdialog --title "AnyPS5 GUI" --passivepopup "$MSG" 60 >/dev/null 2>&1 &
  SPL=$!
elif command -v xmessage >/dev/null 2>&1; then
  xmessage -center "$MSG" >/dev/null 2>&1 &
  SPL=$!
elif command -v notify-send >/dev/null 2>&1; then
  notify-send "AnyPS5 GUI" "$MSG" >/dev/null 2>&1
fi
if mv -f "$MAIN" "$DEST"; then
  chmod +x "$DEST"
  if [ -d "$PAYLOAD/_internal" ]; then rm -rf "$APPDIR/_internal"; fi
  cp -a "$PAYLOAD/." "$APPDIR/" 2>/dev/null
fi
rm -rf "$STAGE"
export PYINSTALLER_RESET_ENVIRONMENT=1
setsid "$DEST" >/dev/null 2>&1 &
if [ -n "$MARK" ]; then
  j=0
  while [ -e "$MARK" ] && [ $j -lt 90 ]; do sleep 1; j=$((j+1)); done
fi
[ -n "$SPL" ] && kill "$SPL" 2>/dev/null
rm -f "$MARK" "$0"
"""

_SH_MAC = """#!/bin/sh
NEWAPP="$1"; OLDAPP="$2"; KEEP="$3"; STAGE="$4"; shift 4
i=0
while [ $i -lt 90 ]; do
  alive=0
  for p in "$@"; do kill -0 "$p" 2>/dev/null && alive=1; done
  [ $alive -eq 0 ] && break
  sleep 1; i=$((i+1))
done
sleep 1
MARK="$ANYPS5_UPDATE_MARKER"
osascript -e 'display dialog "Updating AnyPS5 GUI..." & return & return & "The app will reopen by itself." with title "AnyPS5 GUI" buttons {} giving up after 120 with icon note' >/dev/null 2>&1 &
SPL=$!
SAVE="$STAGE/keep"
if [ -n "$KEEP" ] && [ -e "$OLDAPP/$KEEP" ]; then mkdir -p "$SAVE" && mv "$OLDAPP/$KEEP" "$SAVE/data"; fi
BACKUP="$OLDAPP.old"
rm -rf "$BACKUP"
if mv "$OLDAPP" "$BACKUP"; then
  if ditto "$NEWAPP" "$OLDAPP"; then
    rm -rf "$BACKUP"
  else
    rm -rf "$OLDAPP"; mv "$BACKUP" "$OLDAPP"
  fi
fi
if [ -n "$KEEP" ] && [ -e "$SAVE/data" ]; then rm -rf "$OLDAPP/$KEEP"; mv "$SAVE/data" "$OLDAPP/$KEEP"; fi
xattr -dr com.apple.quarantine "$OLDAPP" 2>/dev/null
rm -rf "$STAGE"
export PYINSTALLER_RESET_ENVIRONMENT=1
open -n "$OLDAPP"
if [ -n "$MARK" ]; then
  j=0
  while [ -e "$MARK" ] && [ $j -lt 90 ]; do sleep 1; j=$((j+1)); done
fi
kill "$SPL" 2>/dev/null
rm -f "$MARK" "$0"
"""


def _mac_bundle():
  """Path of the running .app bundle, or None."""
  p = os.path.realpath(sys.executable)
  while p and p != os.path.dirname(p):
    if p.endswith(".app"):
      return p
    p = os.path.dirname(p)
  return None


def _mac_extract_app(dl, stage):
  """Unpacks zip / tar.gz / dmg into stage and returns the path of the .app inside."""
  out = os.path.join(stage, "x")
  os.makedirs(out, exist_ok=True)
  n = dl.lower()
  if n.endswith(".zip"):
    subprocess.run(["ditto", "-x", "-k", dl, out], check=True)  # keeps symlinks and permissions
  elif n.endswith((".tar.gz", ".tgz")):
    _safe_extract_tar(dl, out)
  elif n.endswith(".dmg"):
    mnt = os.path.join(stage, "mnt")
    os.makedirs(mnt, exist_ok=True)
    subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noverify", "-mountpoint", mnt, dl],
                   check=True, stdout=subprocess.DEVNULL)
    try:
      apps = [a for a in os.listdir(mnt) if a.endswith(".app")]
      if not apps:
        raise OSError("no .app inside the dmg")
      subprocess.run(["ditto", os.path.join(mnt, apps[0]), os.path.join(out, apps[0])], check=True)
    finally:
      subprocess.run(["hdiutil", "detach", "-force", mnt], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  else:
    raise OSError("unsupported archive: " + dl)
  for root, dirs, _ in os.walk(out):
    for d in dirs:
      if d.endswith(".app"):
        return os.path.join(root, d)
    if root.count(os.sep) - out.count(os.sep) >= 2:
      dirs[:] = []
  raise OSError("no .app found in the downloaded file")


class _PausableCancel(threading.Event):
  """Cancel flag that blocks the downloading thread while `paused` is set (used by the GUI update)."""
  def __init__(self):
    super().__init__()
    self.paused = threading.Event()

  def is_set(self):
    while self.paused.is_set() and not super().is_set():
      time.sleep(0.1)
    return super().is_set()


def prepare_and_launch_update(asset, status, cancel):
  """Downloads `asset`, stages it and starts the external installer script. The caller must then
  quit the app. Raises UpdateUnavailable when the update cannot be applied automatically."""
  import tempfile
  host = host_target()
  if not getattr(sys, "frozen", False):
    raise UpdateUnavailable("not running from a packaged executable")
  pids = sorted({os.getpid(), os.getppid()} - {0, 1})

  if host == "macos":
    bundle = _mac_bundle()
    if not bundle:
      raise UpdateUnavailable("the app is not inside a .app bundle")
    if not os.access(os.path.dirname(bundle), os.W_OK):
      raise UpdateUnavailable("no write permission in " + os.path.dirname(bundle))
    stage = tempfile.mkdtemp(prefix="anyps5-update-")
  else:
    dest = os.environ.get("APPIMAGE") if host == "linux" and os.environ.get("APPIMAGE") else sys.executable
    dest = os.path.abspath(dest)
    appdir = os.path.dirname(dest)
    if not os.access(appdir, os.W_OK):
      raise UpdateUnavailable("no write permission in " + appdir)
    stage = os.path.join(DATA_DIR, "update")
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage, exist_ok=True)

  errors = []

  def log(kind, msg):
    if kind == "percent":
      status(pct=msg if msg is not None else 0)
    elif kind == "progress" and msg:
      status(text=msg)
    elif kind == "err":
      errors.append(msg)
  dl = os.path.join(stage, "download-" + re.sub(r"[^\w.\-]", "_", asset["name"]))
  status(state="downloading", pct=0, text="Downloading " + asset["name"] + "…")
  if not _download_file(asset["url"], dl, asset["size"], asset["sha256"], log, cancel, asset["name"]):
    shutil.rmtree(stage, ignore_errors=True)
    raise RuntimeError(errors[-1] if errors else "download failed")

  status(state="installing", pct=None, text="Preparing the installation…")
  mfd, marker = tempfile.mkstemp(prefix="anyps5-upd-", suffix=".run")  # splash stays up while this exists
  os.close(mfd)
  if host == "macos":
    newapp = _mac_extract_app(dl, stage)
    keep = ""
    ud = os.path.realpath(DATA_DIR)
    if ud.startswith(os.path.realpath(bundle) + os.sep):  # settings live inside the bundle: carry them over
      keep = os.path.relpath(ud, os.path.realpath(bundle))
    fd, script = tempfile.mkstemp(prefix="anyps5-upd-", suffix=".sh")
    with os.fdopen(fd, "w") as f:
      f.write(_SH_MAC)
    os.chmod(script, 0o700)
    _run_detached(["/bin/sh", script, newapp, bundle, keep, stage] + [str(p) for p in pids], marker)
    return

  # Windows / Linux: single file, or an archive with the file (and maybe its folder)
  low = dl.lower()
  if low.endswith(".zip") or low.endswith((".tar.gz", ".tgz")):
    ex = os.path.join(stage, "payload")
    os.makedirs(ex, exist_ok=True)
    (_safe_extract_zip if low.endswith(".zip") else _safe_extract_tar)(dl, ex)
    payload = _single_root(ex)
    shutil.rmtree(os.path.join(payload, LEGACY_DIR_NAME), ignore_errors=True)  # never touch user data
  else:
    payload = os.path.join(stage, "payload")
    os.makedirs(payload, exist_ok=True)
    shutil.move(dl, os.path.join(payload, os.path.basename(dest)))
  main = _find_main_exe(payload, host, os.path.basename(dest))
  if not main:
    shutil.rmtree(stage, ignore_errors=True)
    try:
      os.remove(marker)
    except OSError:
      pass
    raise RuntimeError("no executable found in the downloaded file")
  if host == "linux":
    os.chmod(os.path.join(payload, main), 0o755)
  main_path = os.path.join(payload, main)

  if host == "windows":
    fd, script = tempfile.mkstemp(prefix="anyps5-upd-", suffix=".bat")
    esc = lambda p: p.replace("%", "%%")
    sfd, splash = tempfile.mkstemp(prefix="anyps5-splash-", suffix=".ps1")
    with os.fdopen(sfd, "w", encoding="utf-8-sig") as f:  # BOM: PowerShell 5 reads UTF-8 paths correctly
      f.write(_PS_SPLASH.replace("@@MARKER@@", marker.replace("'", "''")).replace("@@DEST@@", dest.replace("'", "''")))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
      f.write(_BAT.format(main=esc(main_path), dest=esc(dest), appdir=esc(appdir),
                          payload=esc(payload), stage=esc(stage), splash=esc(splash)).replace("\n", "\r\n"))
    _run_detached(["cmd.exe", "/c", script], marker)
  else:
    fd, script = tempfile.mkstemp(prefix="anyps5-upd-", suffix=".sh")
    with os.fdopen(fd, "w") as f:
      f.write(_SH_LINUX)
    os.chmod(script, 0o700)
    _run_detached(["/bin/sh", script, main_path, dest, appdir, payload, stage] + [str(p) for p in pids], marker)
