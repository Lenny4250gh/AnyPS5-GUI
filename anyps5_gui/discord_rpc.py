"""Discord Rich Presence (optional: needs pypresence)."""

import threading
import time

from .updater import GUI_HOME_URL


try:
  from pypresence import Presence
except ImportError:
  Presence = None

DISCORD_CLIENT_ID = "1563249221094875246"  # discord.com/developers/applications


class DiscordRPC:
  def __init__(self):
    self.rpc = None
    self.start_ts = None
    self.lock = threading.Lock()  # updates come from several threads: one at a time
    self._last = None  # (details, state, timer) last sent: identical updates are skipped
    self.enabled = lambda: True  # set by Api from the "no_discord_rpc" setting

  def _drop(self):
    """Remove the presence and close the connection (a fresh one is opened on the next update)."""
    r, self.rpc = self.rpc, None
    self.start_ts = None
    self._last = None
    if r is not None:
      try:
        r.clear()
      except Exception:
        pass
      try:
        r.close()
      except Exception:
        pass

  def _connect(self):
    if Presence is None or DISCORD_CLIENT_ID.startswith("YOUR_"):
      return False
    if self.rpc is None:
      try:
        r = Presence(DISCORD_CLIENT_ID)
        r.connect()
        self.rpc = r
      except Exception:
        self.rpc = None  # Discord not running: ignore
        return False
    return True

  def update(self, details, state=None, timer=False):
    with self.lock:
      if not self.enabled():
        self._drop()
        return
      key = (details, state, bool(timer))
      if key == self._last and self.rpc is not None:
        return  # same status as before: nothing to send (Discord rate-limits updates)
      for _ in range(2):  # a second try reconnects from scratch if the old connection went stale
        try:
          if not self._connect():
            return
          if timer and not self.start_ts:
            self.start_ts = int(time.time())
          elif not timer:
            self.start_ts = None
          self.rpc.update(
              details=details,
              state=state,
              start=self.start_ts,
              large_image="logo",
              large_text="AnyPS5 GUI",
              large_url=GUI_HOME_URL,
              buttons=[
                  {
                      "label": "Get AnyPS5 GUI",
                      "url": GUI_HOME_URL
                  }
              ]
          )
          self._last = key
          return
        except Exception:
          self._drop()

  def clear(self):
    with self.lock:
      self._drop()
