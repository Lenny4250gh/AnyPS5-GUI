"""anyps5-input.ini: allowed names and validation (INPUT_MAPPING.md)."""

import math
import re


INPUT_ACTIONS = {a.lower() for a in (
  "Cross Circle Triangle Square L1 R1 L2 R2 L3 R3 Options Up Right Down Left "
  "LeftStickLeft LeftStickRight LeftStickUp LeftStickDown "
  "RightStickLeft RightStickRight RightStickUp RightStickDown "
  "TouchLeft TouchRight StickModifier Shake ToggleMouse ToggleFullscreen "
  "TogglePerformanceOverlay Screenshot").split()}
KEY_ONLY_ACTIONS = {"togglefullscreen", "toggleperformanceoverlay", "screenshot"}  # INPUT_MAPPING.md: keyboard keys only
# Controller button remapping: "SDLButton:button = Button" (INPUT_MAPPING.md, "Controller button remapping")
SDL_BUTTONS = {"a", "b", "x", "y", "back", "guide", "start", "leftstick", "rightstick", "leftshoulder", "rightshoulder",
               "dpup", "dpdown", "dpleft", "dpright", "misc1", "paddle1", "paddle2", "paddle3", "paddle4", "touchpad"}
PAD_BUTTON_NAMES = {a.lower() for a in (
  "Cross Circle Triangle Square L1 R1 L2 R2 L3 R3 Options Up Right Down Left TouchPad").split()}
MOUSE_BUTTONS = {"left", "middle", "right", "x1", "x2"}
# name: (minimum, maximum, minimum_excluded, integer)
INPUT_SETTINGS = {
  "leftstickdeadzone": (0, 90, False, True),
  "rightstickdeadzone": (0, 90, False, True),
  "mousesensitivity": (0, 10, True, False),
  "mouseverticalscale": (0.1, 10, False, False),
  "mousecurve": (0.2, 5, False, False),
  "mousedeadzoneoffset": (0, 0.9, False, False),
  "mousemaxspeed": (0.1, 10, False, False),
  "mousesmoothingms": (0, 500, False, False),
  "mousedecayms": (1, 1000, False, False),
  "stickmodifierscale": (0.05, 1, False, False),
  "keyboardrampms": (0, 2000, False, False),
  "triggerrampms": (0, 2000, False, False),
}

INPUT_TEMPLATE = """# anyps5-input.ini - AnyPS5 keyboard, mouse and controller button mapping
# Format:  Action = Type:Value     (# or ; start a comment)
# The first line of an action replaces the default keys, later ones add to them.
# Actions that are not listed keep their default keys.
# Controller buttons can be remapped with "SDLButton:button = Button" (for example SDLButton:a = Circle,
# or "= None" to disable the button); each SDL button can appear once.
#
# Example below (active): Cross on F or Space, left stick on IJKL
#
# Settings (each at most once):
"""


def validate_input_ini(text):
  """Check the file according to INPUT_MAPPING.md. Returns a list of (line, message)."""
  errors = []
  seen_settings = set()
  seen_sdl = set()
  for number, raw in enumerate(text.lstrip("\ufeff").splitlines(), 1):
    line = re.split(r"[#;]", raw, 1)[0].strip()
    if not line:
      continue
    if "=" not in line:
      errors.append((number, "missing '=': expected 'Action = Type:Value'"))
      continue
    key, value = (p.strip() for p in line.split("=", 1))
    low = key.lower()
    if ":" in key and key.split(":", 1)[0].strip().lower() == "sdlbutton":
      sdl = key.split(":", 1)[1].strip()
      sdl_low = sdl.lower()
      if sdl_low not in SDL_BUTTONS:
        errors.append((number, f"unknown SDL controller button '{sdl}' (for example a, b, x, y, back, start, "
                               "leftshoulder, dpup, paddle1, touchpad)"))
      elif sdl_low in seen_sdl:
        errors.append((number, f"SDL controller button '{sdl}' is set more than once"))
      else:
        seen_sdl.add(sdl_low)
        if value.lower() != "none" and value.lower() not in PAD_BUTTON_NAMES:
          errors.append((number, f"SDLButton:{sdl}: '{value}' is not a single PlayStation button "
                                 "(Cross, Circle, L1, R2, Options, TouchPad... or None)"))
      continue
    if low in INPUT_SETTINGS:
      if low in seen_settings:
        errors.append((number, f"'{key}' appears more than once"))
        continue
      seen_settings.add(low)
      lo, hi, lo_excl, integer = INPUT_SETTINGS[low]
      try:
        num = int(value) if integer else float(value)
        if not math.isfinite(num):
          raise ValueError(value)
      except ValueError:
        errors.append((number, f"'{key}': non-numeric value{' (an integer is required)' if integer else ''}"))
        continue
      if num < lo or num > hi or (lo_excl and num <= lo):
        errors.append((number, f"'{key}': out of range ({'>' if lo_excl else ''}{lo} .. {hi})"))
      continue
    if low not in INPUT_ACTIONS:
      errors.append((number, f"unknown action or setting: '{key}'"))
      continue
    if ":" not in value:
      errors.append((number, "expected 'Type:Value' (KEY, MOUSE or WHEEL)"))
      continue
    kind, name = (p.strip() for p in value.split(":", 1))
    kind = kind.upper()
    if not name:
      errors.append((number, "empty value after the colon"))
    elif kind == "MOUSE" and name.lower() not in MOUSE_BUTTONS:
      errors.append((number, f"MOUSE:{name} is invalid (Left, Middle, Right, X1, X2)"))
    elif kind == "WHEEL" and name.lower() not in ("up", "down"):
      errors.append((number, f"WHEEL:{name} is invalid (Up or Down)"))
    elif kind not in ("KEY", "MOUSE", "WHEEL"):
      errors.append((number, f"unknown type '{kind}' (KEY, MOUSE, WHEEL)"))
    elif low in KEY_ONLY_ACTIONS and kind != "KEY":
      errors.append((number, f"'{key}' accepts only keyboard keys (KEY:...)"))
  return errors
