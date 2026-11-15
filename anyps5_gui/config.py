"""Loading and saving config.ini, and small helpers for its values."""

import configparser
import os
import sys

from .settings import (
    CONFIG_FILE, DATA_DIR, DEFAULT_REPO_URL, DEFAULTS, hidden_error, LEGACY_CONFIG_FILE, RELINKER_SOURCES,
    TARGETS
)
from .system_checks import cpu_info


def first_run_defaults():
  """(target, intel, rosetta) that fit this computer, used only when there is no config file yet:
  target = the host system · intel on for Intel CPUs · rosetta on for Apple silicon (implies intel)."""
  target, intel, rosetta = host_target(), "no", "no"
  try:
    c = cpu_info()
    if c["arch"] == "apple":  # Apple silicon (or an x86 process under Rosetta)
      target, rosetta = "macos", "yes"
    elif c["vendor"] == "intel":
      intel = "yes"
  except Exception as _exc:
    hidden_error("first_run_defaults", _exc)
    pass
  return target, intel, rosetta


def load_config():
  cfg = configparser.ConfigParser(interpolation=None)
  source = CONFIG_FILE
  if not os.path.isfile(CONFIG_FILE) and os.path.isfile(LEGACY_CONFIG_FILE):
    source = LEGACY_CONFIG_FILE  # migrate settings from the old name
  first_run = not os.path.isfile(source)  # no config yet: pick the options that fit this computer
  try:
    cfg.read(source, encoding="utf-8")
  except Exception as _exc:
    hidden_error("load_config", _exc)
    pass
  values = {}
  for section, defaults in DEFAULTS.items():
    values[section] = {}
    for key, default in defaults.items():
      values[section][key] = cfg.get(section, key, fallback=default).strip().strip('"')
  s = values["settings"]
  if s["output_dir"].lower() in ("d", "default"):
    s["output_dir"] = ""
  if s["mode"] not in ("unwrap", "relink"):
    s["mode"] = "relink"
  if first_run:
    values["relinker"]["target"], values["settings"]["intel"], values["relinker"]["rosetta"] = first_run_defaults()
  if values["relinker"]["target"] not in TARGETS:
    values["relinker"]["target"] = "windows"
  if values["relinker"]["relinker_source"] not in RELINKER_SOURCES:
    values["relinker"]["relinker_source"] = "release"
  if not values["relinker"]["repo_url"]:
    values["relinker"]["repo_url"] = DEFAULT_REPO_URL
  return values


def save_config(values):
  cfg = configparser.ConfigParser(interpolation=None)
  for section, items in values.items():
    cfg[section] = {k: (v if v != "" or section != "settings" else "default")
                    for k, v in items.items()}
  cfg["settings"]["output_dir"] = values["settings"]["output_dir"] or "default"
  try:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
      f.write("# settings: output_dir (default = 'Converted games' folder next to the program), intel yes/no,\n"
              "#   auto_unwrap yes/no, mode unwrap/relink, window_size WIDTHxHEIGHT (empty = default)\n"
              "# relinker: relinker options (see AnyPS5 USAGE.md); relinker_source prebuilt/github/release,\n"
              "#   release_tag / release_update for the release source (empty tag = latest release),\n"
              "#   repo_url / repo_ref / repo_target / repo_update for the GitHub source build\n"
              "# runtime: environment variables used to launch the game\n")
      cfg.write(f)
  except Exception as _exc:
    hidden_error("save_config", _exc)
    pass


def yes(value):
  return str(value).strip().lower() in ("yes", "true", "1")


def yn(flag):
  return "yes" if flag else "no"


def host_target():
  """Target system that matches the computer the GUI runs on."""
  if os.name == "nt":
    return "windows"
  if sys.platform == "darwin":
    return "macos"
  return "linux"
