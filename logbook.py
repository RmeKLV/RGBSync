"""Persistent log.

The GUI's log pane is six lines that vanish when the window closes, which is
useless for the failures that actually matter here - a device that was missing
at 9am, or an autostarted session that went wrong while nobody was watching.
Everything the engine reports goes to disk as well, with a startup snapshot of
what the machine looked like at the time.

Rotating, five files of 512 KB, so it can be left on forever.
"""
import os, sys, time, logging, threading
from logging.handlers import RotatingFileHandler

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")
LOG_PATH = os.path.join(LOG_DIR, "rgbsync.log")

MAX_BYTES = 512 * 1024
BACKUPS = 5

_lock = threading.Lock()
_logger = None


def _build():
    os.makedirs(LOG_DIR, exist_ok=True)
    lg = logging.getLogger("rgbsync")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    if not lg.handlers:
        h = RotatingFileHandler(LOG_PATH, maxBytes=MAX_BYTES,
                                backupCount=BACKUPS, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s  %(message)s",
                                         datefmt="%Y-%m-%d %H:%M:%S"))
        lg.addHandler(h)
    return lg


def logger():
    global _logger
    with _lock:
        if _logger is None:
            _logger = _build()
        return _logger


def write(msg):
    """Log one line. Never raises - logging must not be able to stop the sync."""
    try:
        logger().info(str(msg))
    except Exception:
        pass


def tee(also):
    """A log callback that writes to disk and forwards to `also` (the GUI pane)."""
    def _log(msg):
        write(msg)
        try:
            also(msg)
        except Exception:
            pass
    return _log


def session_header(what="session"):
    write("=" * 70)
    write(f"--- {what} start --- python {sys.version.split()[0]} "
          f"pid {os.getpid()} tz {time.strftime('%Z%z')}")


def snapshot():
    """Record what the machine looks like right now.

    This is the evidence the next 'my keyboard is missing again' report needs:
    whether Windows sees the device, whether OpenRGB does, and which of the two
    disagrees. Best effort - a probe that fails logs why and moves on.
    """
    import prereqs

    write("-- environment --")

    d = prereqs.find_openrgb_dir()
    write(f"OpenRGB install : {d or 'not found'}")

    svc = openrgb_service_state()
    write(f"OpenRGB service : {svc}")
    if svc.startswith("running"):
        write("   note: a LocalSystem service detects devices once at boot and "
              "does not rescan, so a late-enumerating USB device stays missing")

    if prereqs.sdk_alive():
        try:
            from orgb import OpenRGBClient
            c = OpenRGBClient(name="rgbsync-snapshot")
            names = [f"{x.name} ({x.led_count} LEDs)" for x in c.devices]
            c.close()
            write(f"OpenRGB SDK     : up, {len(names)} device(s)")
            for n in names:
                write(f"   - {n}")
        except Exception as e:
            write(f"OpenRGB SDK     : up but query failed: {type(e).__name__}: {e}")
    else:
        write("OpenRGB SDK     : not reachable on 127.0.0.1:6742")

    try:
        import hidlib
        for vid, pid, label in HID_INVENTORY:
            found = hidlib.describe(vid, pid)
            if not found:
                write(f"HID {vid:04X}:{pid:04X} : {label} - ABSENT from Windows")
                continue
            write(f"HID {vid:04X}:{pid:04X} : {label} - {len(found)} interface(s)")
            for i in found:
                write(f"   iface {i['interface']:>2}  usage {i['usage_page']:#06x}/"
                      f"{i['usage']:#06x}  {i['product']!r}")
    except Exception as e:
        write(f"HID enumeration failed: {type(e).__name__}: {e}")

    write("-- end environment --")


# Devices worth recording every session: the three driven over raw HID, plus
# the keyboard, which is the one that keeps going missing.
HID_INVENTORY = [
    (0x0B05, 0x186E, "ASUS XG248Q backlight"),
    (0x3142, 0x0038, "Fifine BM38 boom arm"),
    (0x3142, 0x01A8, "Fifine AM8 Pro mic"),
    (0x1038, 0x1622, "SteelSeries Apex 3 TKL"),
]


def openrgb_service_state():
    """'running (session 0 service)' / 'stopped' / 'not installed'."""
    import subprocess
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        out = subprocess.run(["sc.exe", "query", "OpenRGB"], capture_output=True,
                             text=True, startupinfo=si, timeout=8).stdout
    except Exception as e:
        return f"could not query ({e})"
    if "does not exist" in out or "1060" in out:
        return "not installed as a service"
    if "RUNNING" in out:
        return "running (LocalSystem, session 0)"
    if "STOPPED" in out:
        return "installed but stopped"
    return "unknown"


def path():
    return LOG_PATH


def open_folder():
    os.makedirs(LOG_DIR, exist_ok=True)
    os.startfile(LOG_DIR)


def tail(n=200):
    try:
        with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
            return f.readlines()[-n:]
    except FileNotFoundError:
        return []
