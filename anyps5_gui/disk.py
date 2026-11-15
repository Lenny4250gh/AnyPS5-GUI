"""Disk space needed by an operation, and the desktop notification when it ends."""

import os
import shutil
import subprocess
import sys
import threading

from .settings import DATA_DIR, EXECUTABLE_NAME, FONTS_DIR_NAME, hidden_error
from .util import dir_size, existing_parent, file_state, format_duration, format_size, package_dir
from .files import find_extra_module_dirs, game_targets, libs_info


def space_requirements(folder, target, mode, output_base, auto=True, move=False):
  """Estimate of the space the operation needs and of what is free on the destination drive."""
  parts = []
  extra = find_extra_module_dirs(folder)
  files = [p for _n, p in game_targets(folder, True, extra) if os.path.isfile(p)]
  if mode == "unwrap":
    sizes = [os.path.getsize(p) for p in files if file_state(p) == "self"]
    parts.append(("backups of the originals", sum(sizes)))
    parts.append(("temporary file", max(sizes, default=0)))
    where, reusable = DATA_DIR, 0
  else:
    eboot = os.path.join(folder, EXECUTABLE_NAME)
    fonts = os.path.join(DATA_DIR, FONTS_DIR_NAME)
    if move:
      # moved file by file: the peak is only the biggest file (same drive) or the whole game on the
      # destination drive while the source shrinks at the same time
      same = False
      try:
        same = os.stat(folder).st_dev == os.stat(existing_parent(output_base)).st_dev
      except OSError:
        pass
      biggest = max((os.path.getsize(os.path.join(r, f)) for r, _d, fs in os.walk(folder) for f in fs
                     if os.path.isfile(os.path.join(r, f))), default=0)
      parts.append(("game files moved on the same drive: only the biggest file counts" if same
                    else "game files moved to another drive: full size", biggest if same else dir_size(folder)))
    else:
      parts.append(("game files copy: full size", dir_size(folder)))
    libs = libs_info(target)
    parts.append(("libs folder", libs["size"] if libs else 0))
    if os.path.isdir(fonts):
      parts.append(("fonts", dir_size(fonts)))
    parts.append(("new executable", os.path.getsize(eboot) if os.path.isfile(eboot) else 0))
    if auto:
      parts.append(("temporary file", max((os.path.getsize(p) for p in files), default=0)))
    where = output_base
    pkg = package_dir(output_base, os.path.basename(os.path.normpath(folder)), target)
    reusable = dir_size(pkg) if os.path.isdir(pkg) else 0  # freed when the old package is replaced
  subtotal = sum(b for _l, b in parts)
  margin = max(subtotal // 20, 64 * 1024 * 1024)
  parts.append(("safety margin", margin))
  free = shutil.disk_usage(existing_parent(where)).free
  return {"parts": parts, "needed": subtotal + margin, "free": free,
          "reusable": reusable, "where": where}


def build_notification(op, game, ok, elapsed, errors):
  """(title, text) of the end-of-operation notification."""
  op = op or "Operation"
  if ok:
    took = "in under 1s" if elapsed < 1 else "in " + format_duration(elapsed)
    return f"{op} completed", f"{game} · {took}" if game else took
  lines = [game]
  seen = []
  for e in reversed(errors):  # last distinct errors, skipping the generic summary line
    e = e.strip()
    if e and not e.startswith("Completed with errors") and e not in seen:
      seen.append(e)
    if len(seen) == 3:
      break
  lines += [(e if len(e) <= 160 else e[:157] + "...") for e in reversed(seen)]
  lines.append("See the log for details.")
  return f"{op} failed", "\n".join(lines)


def notify_desktop(title, text):
  """System notification (Windows balloon/toast, macOS Notification Center, Linux notify-send).
  Never raises and never blocks: the notification is sent from a background thread."""
  def send():
    try:
      if os.name == "nt":
        # Native Windows toast shown under its own app name ("AnyPS5 GUI"): an AppUserModelID
        # is registered under HKCU (no admin rights) so Windows does not label it "Windows PowerShell".
        toast = (
          "$id='AnyPS5.GUI';"
          "$k='HKCU:\\Software\\Classes\\AppUserModelId\\'+$id;"
          "if(!(Test-Path $k)){New-Item $k -Force | Out-Null};"
          "New-ItemProperty $k -Name DisplayName -Value 'AnyPS5 GUI' -PropertyType String -Force | Out-Null;"
          "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime] | Out-Null;"
          "[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime] | Out-Null;"
          "$x=New-Object Windows.Data.Xml.Dom.XmlDocument;"
          "$x.LoadXml('<toast><visual><binding template=\"ToastGeneric\"><text></text><text></text></binding></visual></toast>');"
          "$t=$x.GetElementsByTagName('text');"
          "$t.Item(0).InnerText=$env:NTF_TITLE; $t.Item(1).InnerText=$env:NTF_TEXT;"
          "$n=New-Object Windows.UI.Notifications.ToastNotification($x);"
          "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show($n)")
        balloon = (
          "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
          "$n = New-Object System.Windows.Forms.NotifyIcon;"
          "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true;"
          "$n.BalloonTipTitle = 'AnyPS5 GUI - ' + $env:NTF_TITLE; $n.BalloonTipText = $env:NTF_TEXT;"
          "$n.ShowBalloonTip(8000); Start-Sleep -Seconds 9; $n.Dispose()")
        penv = dict(os.environ, NTF_TITLE=title[:60], NTF_TEXT=text[:250])
        for script in (toast, balloon):  # the old balloon only if the toast fails
          res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                                "-ExecutionPolicy", "Bypass", "-Command", script],
                               env=penv, capture_output=True, timeout=30,
                               creationflags=subprocess.CREATE_NO_WINDOW)
          if res.returncode == 0:
            break
      elif sys.platform == "darwin":
        script = ('on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run')
        subprocess.run(["osascript", "-e", script, title, text], capture_output=True, timeout=15)
      else:
        # Linux: try the common notification front ends in order, stop at the first that works
        attempts = []
        if shutil.which("notify-send"):
          attempts.append(["notify-send", "-a", "AnyPS5 GUI", title, text])
        if shutil.which("gdbus"):  # talks to org.freedesktop.Notifications directly (no extra package)
          attempts.append(["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
                           "--object-path", "/org/freedesktop/Notifications",
                           "--method", "org.freedesktop.Notifications.Notify",
                           "AnyPS5 GUI", "0", "", title, text, "[]", "{}", "8000"])
        if shutil.which("kdialog"):
          attempts.append(["kdialog", "--title", title, "--passivepopup", text, "8"])
        if shutil.which("zenity"):
          attempts.append(["zenity", "--notification", "--text", title + "\n" + text])
        for cmd in attempts:
          if subprocess.run(cmd, capture_output=True, timeout=15).returncode == 0:
            break
    except Exception as _exc:
      hidden_error("send", _exc)
      pass
  threading.Thread(target=send, daemon=True).start()


def log_space(req, log):
  """Write the estimate in the log. Returns True if there is enough space."""
  detail = ", ".join(f"{label} {format_size(b)}" for label, b in req["parts"] if b)
  avail = req["free"] + req["reusable"]
  ok = avail >= req["needed"]
  log("detail", f"Disk space required: about {format_size(req['needed'])} ({detail})")
  note = f" (includes {format_size(req['reusable'])} that will be freed by replacing the old output folder)" if req["reusable"] else ""
  log("ok" if ok else "err",
      f"Free space at {req['where']}: {format_size(avail)}{note}"
      + ("" if ok else f" - NOT ENOUGH, {format_size(req['needed'] - avail)} more needed"))
  return ok
