"""Checks of the computer: CPU (AVX2/FMA/F16C...), GPU, memory, NVIDIA driver, udmabuf."""

import os
import re
import shutil
import subprocess
import sys

from .settings import hidden_error


# ── CPU compatibility check ──
# AnyPS5 runs the game natively and relies on what the PS5's Zen 2 CPU offers without checking.
CPU_REQUIRED = (("avx2", "AVX2"), ("fma", "FMA"), ("f16c", "F16C"), ("bmi1", "BMI1"),
                ("bmi2", "BMI2"), ("lzcnt", "LZCNT"), ("movbe", "MOVBE"))
_CPU_INFO = None


def _sysctl(name):
  try:
    return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=5).stdout.strip()
  except Exception as _exc:
    hidden_error("_sysctl", _exc)
    return ""


def cpu_info():
  """{arch, vendor ('amd'/'intel'/'other'), name, features:set of CPU_REQUIRED keys or None if unknown, rosetta}."""
  global _CPU_INFO
  if _CPU_INFO is not None:
    return _CPU_INFO
  import platform
  machine = platform.machine().lower()
  info = {"arch": "x64" if machine in ("x86_64", "amd64", "x64") else machine,
          "vendor": "other", "name": "", "features": None, "rosetta": False}
  try:
    if sys.platform == "darwin":
      info["rosetta"] = _sysctl("sysctl.proc_translated") == "1"
      if info["rosetta"] or machine.startswith("arm"):
        info["arch"] = "apple"
        info["name"] = _sysctl("machdep.cpu.brand_string") or "Apple silicon"
      else:
        text = " ".join(_sysctl(k) for k in ("machdep.cpu.features", "machdep.cpu.leaf7_features",
                                             "machdep.cpu.extfeatures")).lower().split()
        info["features"] = {k for k, _ in CPU_REQUIRED if k in text}
        info["avx"] = "avx1.0" in text.split()  # --to-avx needs at least AVX
        info["name"] = _sysctl("machdep.cpu.brand_string")
        info["vendor"] = "intel" if "intel" in info["name"].lower() else "other"
    elif os.name == "nt":
      import ctypes
      import winreg
      with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0") as k:
        v = winreg.QueryValueEx(k, "VendorIdentifier")[0]
        info["name"] = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
      info["vendor"] = "amd" if "AMD" in v else "intel" if "Intel" in v else "other"
      # Windows only reports AVX2 (feature 40). Every x86 CPU with AVX2 from Haswell/Excavator on also has
      # FMA, F16C, BMI1/2, LZCNT and MOVBE, so AVX2 stands for the whole set.
      if ctypes.windll.kernel32.IsProcessorFeaturePresent(40):
        info["features"] = {k for k, _ in CPU_REQUIRED}
      else:
        info["features"] = set()
    else:
      flags, vendor, name = set(), "", ""
      with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
        for line in f:
          key, _, val = line.partition(":")
          key, val = key.strip(), val.strip()
          if key == "flags" and not flags:
            flags = set(val.split())
          elif key == "vendor_id" and not vendor:
            vendor = val
          elif key == "model name" and not name:
            name = val
      if flags:
        flags.add("lzcnt") if "abm" in flags else None  # lzcnt is reported as abm
        info["features"] = {k for k, _ in CPU_REQUIRED if k in flags}
      info["vendor"] = "amd" if "AMD" in vendor else "intel" if "Intel" in vendor else "other"
      info["name"] = name
  except Exception as _exc:
    hidden_error("cpu_info", _exc)
    pass
  _CPU_INFO = info
  return info


def cpu_status(target, intel, rosetta, to_avx=False):
  """(text, level) for the requirements list. level: ok / warn / err / muted."""
  c = cpu_info()
  name = c["name"] or c["arch"]
  if c["arch"] == "apple":
    if target == "macos" and rosetta:
      return (f"{name}  ✔  Rosetta (--to-rosetta on)", "ok")
    return (f"{name}  ⚠  Apple silicon: use the macOS target with --to-rosetta", "warn")
  if c["arch"] != "x64":
    return (f"{c['arch']}  ✖  not supported: AnyPS5 needs an x86-64 CPU", "err")
  if c["features"] is None:
    return (f"{name}  (extensions could not be read)", "muted")
  missing = [t for k, t in CPU_REQUIRED if k not in c["features"]]
  if missing and to_avx and target == "macos" and c.get("avx"):
    # --to-avx lowers AVX2, BMI1/2, LZCNT and MOVBE; F16C and FMA are not lowered (USAGE.md)
    if "F16C" in missing or "FMA" in missing:
      return (f"{name}  ⚠  --to-avx on: AVX2 code is emulated (slow); {', '.join(m for m in ('FMA', 'F16C') if m in missing)} "
              "are not lowered, so some games may still fail", "warn")
    return (f"{name}  ✔  --to-avx on (missing {', '.join(missing)}: emulated, slower)", "ok")
  if missing and c.get("avx") and target == "macos" and not to_avx and c["vendor"] == "intel":
    return (f"{name}  ✖  missing {', '.join(missing)}: AnyPS5 stops at start. This CPU has AVX: turn on --to-avx", "err")
  if missing:
    return (f"{name}  ✖  missing {', '.join(missing)}: AnyPS5 stops at start", "err")
  if c["vendor"] == "intel":
    if intel or rosetta:
      return (f"{name}  ✔  Intel, --to-intel on (titles that generate AMD-only code at runtime may still fail)", "ok")
    return (f"{name}  ⚠  Intel: turn on --to-intel (titles that generate AMD-only code at runtime may still fail)", "warn")
  return (f"{name}  ✔  {', '.join(t for _, t in CPU_REQUIRED)}", "ok")


_GPU_INFO = None
_SOFT_GPU = re.compile(r"llvmpipe|lavapipe|swiftshader|softpipe|software rasterizer|microsoft basic render", re.I)


def _run_text(cmd, timeout=15):
  try:
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace", **kw).stdout
  except Exception as _exc:
    hidden_error("_run_text", _exc)
    return ""


def gpu_info():
  """[{name, software, vendor ('amd'/'nvidia'/'intel'/'other')}] from Vulkan; falls back to the OS list."""
  global _GPU_INFO
  if _GPU_INFO is not None:
    return _GPU_INFO
  names = []
  if shutil.which("vulkaninfo"):
    out = _run_text(["vulkaninfo", "--summary"])
    names = [m.strip() for m in re.findall(r"deviceName\s*=\s*(.+)", out)]
  vulkan = bool(names)
  if not names:
    if os.name == "nt":
      out = _run_text(["powershell", "-NoProfile", "-Command",
                       "(Get-CimInstance Win32_VideoController).Name"])
      names = [x.strip() for x in out.splitlines() if x.strip()]
    elif sys.platform == "darwin":
      out = _run_text(["system_profiler", "SPDisplaysDataType"])
      names = [m.strip() for m in re.findall(r"Chipset Model:\s*(.+)", out)]
    else:
      out = _run_text(["lspci"])
      names = [l.split(": ", 1)[1].strip() for l in out.splitlines()
               if re.search(r"VGA|3D|Display", l) and ": " in l]
  gpus, seen = [], set()
  for n in names:
    if n in seen:
      continue
    seen.add(n)
    low = n.lower()
    vendor = ("amd" if re.search(r"amd|radeon|advanced micro", low) else
              "nvidia" if re.search(r"nvidia|geforce|rtx|gtx|quadro", low) else
              "intel" if "intel" in low else "other")
    gpus.append({"name": n, "software": bool(_SOFT_GPU.search(n)), "vendor": vendor})
  _GPU_INFO = {"gpus": gpus, "vulkan": vulkan}
  return _GPU_INFO


def gpu_status(selected=""):
  """(text, level) for the requirements list. `selected` is the ANYPS5_GPU filter (part of the device name)."""
  info = gpu_info()
  gpus = info["gpus"]
  if sys.platform.startswith("linux"):
    import ctypes.util
    if not ctypes.util.find_library("vulkan"):
      return ("Vulkan loader (libvulkan.so.1) not found  ✖  install your distribution's Vulkan loader and GPU driver", "err")
  if not gpus:
    return ("no GPU found  (install the Vulkan driver / vulkan-tools to detect it)", "warn")
  hw = [g for g in gpus if not g["software"]]
  sel = selected.strip().lower()
  if sel:
    hit = [g for g in gpus if sel in g["name"].lower()]
    if not hit:
      return (f"ANYPS5_GPU “{selected.strip()}” matches no device: {'; '.join(g['name'] for g in gpus)}", "err")
    if hit[0]["software"]:
      return (f"{hit[0]['name']}  ⚠  software renderer selected: the title runs, but very slowly", "warn")
    return (f"{hit[0]['name']}  ✔  (selected with ANYPS5_GPU)", "ok")
  if not hw:
    return (f"{gpus[0]['name']}  ⚠  software renderer (no GPU driver): the title runs, but very slowly", "warn")
  names = "; ".join(g["name"] for g in hw)
  text = names + ("" if info["vulkan"] else "  (Vulkan not verified)")
  if len(hw) > 1:
    return (text + "  ⚠  several GPUs: set ANYPS5_GPU to pick one", "warn")
  tip = ""
  if os.name == "nt" and hw[0]["vendor"] == "amd":
    tip = "  · if the first run ends in DEVICE_LOST, run again (driver cache: %LOCALAPPDATA%\\AMD\\VkCache)"
  return (text + "  ✔" + tip, "ok" if info["vulkan"] else "muted")


DIRECT_MEMORY_MIB = 13824  # USAGE.md: up to this much direct memory is committed per title on Windows


def memory_status():
  """(text, level) for Windows: free commit (RAM + page file) against the title's direct memory. None elsewhere."""
  if os.name != "nt":
    return None
  try:
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
      _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                  ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                  ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                  ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                  ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(m)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
      return None
  except Exception as _exc:
    hidden_error("memory_status", _exc)
    return None
  mib = 1024 * 1024
  limit, free, ram = m.ullTotalPageFile // mib, m.ullAvailPageFile // mib, m.ullTotalPhys // mib
  base = f"RAM {ram} MiB · commit limit {limit} MiB · {free} MiB free to commit"
  if limit < DIRECT_MEMORY_MIB:
    return (base + f"  ✖  below the {DIRECT_MEMORY_MIB} MiB direct memory: increase the page file", "err")
  if free < DIRECT_MEMORY_MIB:
    return (base + f"  ⚠  less than {DIRECT_MEMORY_MIB} MiB: close other programs or increase the page file", "warn")
  return (base + "  ✔", "ok")


BAD_NVIDIA_DRIVERS = {"610.47": "known crash in nvgpucomp64.dll (see USAGE.md)"}


def nvidia_status():
  """(text, level) for the NVIDIA driver, or None when there is no NVIDIA GPU / nvidia-smi."""
  if not any(g["vendor"] == "nvidia" for g in gpu_info()["gpus"]):
    return None
  exe = shutil.which("nvidia-smi")
  if not exe:
    return ("NVIDIA GPU found, driver version could not be read (nvidia-smi missing)", "muted")
  ver = _run_text([exe, "--query-gpu=driver_version", "--format=csv,noheader"]).strip().splitlines()
  ver = ver[0].strip() if ver else ""
  if not ver:
    return ("NVIDIA driver version could not be read", "muted")
  if ver in BAD_NVIDIA_DRIVERS:
    return (f"driver {ver}  ✖  {BAD_NVIDIA_DRIVERS[ver]}: install another driver version", "err")
  return (f"driver {ver}  ✔", "ok")


def udmabuf_status():
  """(text, level) for /dev/udmabuf on Linux (not needed with the proprietary NVIDIA driver). None elsewhere."""
  if not sys.platform.startswith("linux"):
    return None
  path = "/dev/udmabuf"
  if not os.path.exists(path):
    return ("/dev/udmabuf not present  (only needed with Mesa drivers: sudo modprobe udmabuf)", "warn")
  if os.access(path, os.R_OK | os.W_OK):
    return ("/dev/udmabuf accessible  ✔", "ok")
  return ("/dev/udmabuf not accessible  ✖  run: sudo usermod -aG kvm $USER  (then log in again) "
          "or: sudo setfacl -m u:$USER:rw /dev/udmabuf", "err")
