"""Prerequisite checks for RGB Screen Sync, plus the safe subset of fixes.

Design note: installing OpenRGB goes through winget (the OS package manager,
which does its own trust checks and prompts) or the official download page.
Nothing here silently downloads or executes a binary.
"""
import os, sys, socket, shutil, subprocess, ctypes, glob, hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
OPENRGB_DIRS = [
    r"C:\Program Files\OpenRGB",
    r"C:\Program Files (x86)\OpenRGB",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\OpenRGB"),
]
DOWNLOAD_URL = "https://openrgb.org/releases.html"
WINGET_ID = "OpenRGB.OpenRGB"

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"

# Matched against process image names only, case-insensitively. Keep these
# specific: "Close it" force-kills every match, and plain "Signal" used to hit
# the Signal messenger too.
CONFLICTS = [
    ("fifine Genie", "Fifine Genie", "repaints the mic and boom arm 20x a second"),
    ("SignalRgb",    "SignalRGB",    "drives the same devices and will fight for them"),
    ("Synapse",      "Razer Synapse", "can override the mouse lighting"),
    ("ArmouryCrate", "Armoury Crate", "can override the motherboard and monitor"),
]

HIDAPI_SHA256 = "EBEB835E2B4530ED68843F19D6A2604C51772E3C26E7F542FDE194075F82D9B4"

HID_TARGETS = [
    (0x0B05, 0x186E, "ASUS XG248Q backlight"),
    (0x3142, 0x0038, "Fifine BM38 boom arm"),
    (0x3142, 0x01A8, "Fifine AM8 Pro mic"),
    (0x1038, 0x1622, "SteelSeries Apex 3 TKL"),
]


class Check:
    def __init__(self, name, status, detail, fix=None, fix_label=None):
        self.name, self.status, self.detail = name, status, detail
        self.fix, self.fix_label = fix, fix_label


# ------------------------------------------------------------------ helpers --
def _no_window():
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return si


def find_openrgb_dir():
    for d in OPENRGB_DIRS:
        if os.path.isfile(os.path.join(d, "OpenRGB.exe")):
            return d
    for pat in (r"C:\Program Files\OpenRGB*", r"C:\Program Files (x86)\OpenRGB*"):
        for d in glob.glob(pat):
            if os.path.isfile(os.path.join(d, "OpenRGB.exe")):
                return d
    return None


def sdk_alive(timeout=0.6):
    try:
        with socket.create_connection(("127.0.0.1", 6742), timeout):
            return True
    except OSError:
        return False


def running_processes():
    """Image names of every running process, e.g. ['explorer.exe', ...]."""
    try:
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True,
                             text=True, startupinfo=_no_window(), timeout=10).stdout
    except Exception:
        return []
    names = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if parts and parts[0]:
            names.append(parts[0])
    return names


def matching(names, image_match):
    m = image_match.lower()
    return sorted({n for n in names if m in n.lower()})


def elevate(ps_command):
    """Run one PowerShell command elevated - Windows shows its own UAC prompt."""
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "powershell.exe",
        f'-NoProfile -ExecutionPolicy Bypass -Command "{ps_command}"', None, 0)
    return rc > 32


# ------------------------------------------------------------------- fixes --
def fix_copy_hidapi():
    d = find_openrgb_dir()
    if not d:
        return False, "OpenRGB not installed - can't source hidapi.dll"
    src = os.path.join(d, "hidapi.dll")
    if not os.path.isfile(src):
        return False, f"no hidapi.dll in {d}"
    shutil.copy2(src, os.path.join(HERE, "hidapi.dll"))
    return True, "copied hidapi.dll into the RGBSync folder"


def fix_install_openrgb():
    """Hand off to winget in a visible console so you see and approve it."""
    try:
        subprocess.Popen(
            ["cmd", "/c", "start", "", "cmd", "/k",
             f"winget install --id {WINGET_ID} -e"],
            shell=False)
        return True, "winget opened in a console window - follow its prompts"
    except Exception as e:
        return False, f"could not launch winget: {e}"


def fix_open_download_page():
    os.startfile(DOWNLOAD_URL)
    return True, "opened the OpenRGB download page in your browser"


def fix_start_openrgb():
    if elevate("Start-Service -Name OpenRGB -ErrorAction Stop"):
        return True, "asked Windows to start the OpenRGB service (approve the UAC prompt)"
    d = find_openrgb_dir()
    if d:
        try:
            subprocess.Popen([os.path.join(d, "OpenRGB.exe"), "--server", "--startminimized"],
                             cwd=d)
            return True, "launched OpenRGB with its SDK server"
        except Exception as e:
            return False, f"could not launch OpenRGB: {e}"
    return False, "OpenRGB not found"


def fix_autostart_service():
    if elevate("Set-Service -Name OpenRGB -StartupType Automatic; Start-Service OpenRGB"):
        return True, "set the OpenRGB service to start automatically"
    return False, "could not change the service (UAC declined?)"


def make_close_fix(image_match, label):
    def _fix():
        try:
            names = matching(running_processes(), image_match)
            if not names:
                return True, f"{label} is not running"
            for n in names:
                subprocess.run(["taskkill", "/IM", n, "/F"], capture_output=True,
                               startupinfo=_no_window(), timeout=10)
            return True, f"closed {label}"
        except Exception as e:
            return False, f"could not close {label}: {e}"
    return _fix


# ------------------------------------------------------------------ checks --
def run_checks():
    checks = []
    tasks = running_processes()

    # 1. Python
    v = sys.version_info
    checks.append(Check(
        "Python", OK if v >= (3, 8) else FAIL,
        f"{v.major}.{v.minor}.{v.micro} at {sys.executable}"))

    # 2. Tkinter
    try:
        import tkinter  # noqa
        checks.append(Check("Tkinter (GUI toolkit)", OK, "available - bundled with Python"))
    except Exception as e:
        checks.append(Check("Tkinter (GUI toolkit)", FAIL,
                            f"missing ({e}) - reinstall Python with tcl/tk"))

    # 3. hidapi.dll
    local = os.path.join(HERE, "hidapi.dll")
    d = find_openrgb_dir()
    if os.path.isfile(local):
        try:
            with open(local, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest().upper()
        except OSError:
            digest = ""
        if digest == HIDAPI_SHA256:
            checks.append(Check("hidapi.dll", OK, "bundled, verified (hidapi 0.14.0 release)"))
        else:
            checks.append(Check("hidapi.dll", WARN,
                                f"bundled, but not the 0.14.0 release build (SHA-256 "
                                f"{digest[:16]}...) - fine if you put your own copy there"))
    elif d and os.path.isfile(os.path.join(d, "hidapi.dll")):
        checks.append(Check("hidapi.dll", WARN,
                            "using OpenRGB's copy - bundle it so OpenRGB isn't required",
                            fix_copy_hidapi, "Bundle it"))
    else:
        checks.append(Check("hidapi.dll", FAIL,
                            "not found - the monitor and Fifine devices need it",
                            fix_copy_hidapi, "Copy from OpenRGB"))

    # 4. OpenRGB installed  (optional - only for mobo / keyboard / mouse)
    if d:
        checks.append(Check("OpenRGB installed", OK, d))
    else:
        checks.append(Check("OpenRGB installed", WARN,
                            "not found - optional, but needed for motherboard/keyboard/mouse",
                            fix_install_openrgb, "Install via winget"))

    # 5. SDK server
    if sdk_alive():
        checks.append(Check("OpenRGB SDK server", OK, "reachable on 127.0.0.1:6742"))
    elif d:
        checks.append(Check("OpenRGB SDK server", WARN,
                            "not reachable - OpenRGB must run with its server enabled",
                            fix_start_openrgb, "Start OpenRGB"))
    else:
        checks.append(Check("OpenRGB SDK server", WARN, "not reachable (OpenRGB not installed)"))

    # 6a. How OpenRGB is running. A LocalSystem service detects devices once,
    #     at boot, and never rescans - so anything that enumerates late is
    #     missing from its list until the next reboot, with no error anywhere.
    if d:
        import logbook
        state = logbook.openrgb_service_state()
        if state.startswith("running"):
            checks.append(Check(
                "OpenRGB run mode", WARN,
                "running as a session-0 service - it detects devices once at boot "
                "and never rescans, so a late USB device stays missing. The "
                "keyboard does not depend on this; it is driven directly."))
        else:
            checks.append(Check("OpenRGB run mode", OK,
                                f"not a service ({state}) - detection follows the session"))

    # 6. Devices visible through the SDK
    if sdk_alive():
        try:
            from orgb import OpenRGBClient
            c = OpenRGBClient(name="prereq-check")
            names = [x.name for x in c.devices]
            c.close()
            checks.append(Check("OpenRGB devices", OK if names else WARN,
                                ", ".join(names) if names else "connected but no devices detected"))
            if not any("apex" in n.lower() for n in names):
                checks.append(Check(
                    "Keyboard path", INFO,
                    "OpenRGB does not list the Apex 3 TKL - RGB Sync drives it "
                    "over raw HID instead, which needs nothing from OpenRGB"))
        except Exception as e:
            checks.append(Check("OpenRGB devices", FAIL, f"{type(e).__name__}: {e}"))

    # 7. Direct-HID devices
    try:
        from fifine import hid
        for vid, pid, label in HID_TARGETS:
            cur = hid.hid_enumerate(vid, pid)
            if cur:
                hid.hid_free_enumeration(cur)
            checks.append(Check(label, OK if cur else WARN,
                                f"{vid:04X}:{pid:04X} " + ("detected" if cur else "not connected")))
    except Exception as e:
        checks.append(Check("Direct-HID devices", FAIL, f"hidapi unusable: {e}"))

    # 8. Screen capture - DXGI is what the engine uses; GDI is its fallback
    try:
        from dxgi_capture import DXGIGrabber
        g = DXGIGrabber(8, 8)
        sw, sh = g.sw, g.sh
        g.close()
        checks.append(Check("Screen capture", OK,
                            f"{sw}x{sh} via DXGI desktop duplication (GPU, fast path)"))
    except Exception as e:
        checks.append(Check("Screen capture (DXGI)", WARN,
                            f"unavailable ({e}) - the slower GDI capture below is used"))
        checks += _gdi_check()
    for image, label, why in CONFLICTS:
        hits = matching(tasks, image)
        if hits:
            checks.append(Check(f"Conflict: {label}", WARN,
                                f"running ({', '.join(hits)}) - {why}",
                                make_close_fix(image, label), "Close it"))
        else:
            checks.append(Check(f"Conflict: {label}", OK, "not running"))

    return checks


def _gdi_check():
    try:
        from screen import ScreenGrabber
        g = ScreenGrabber(8, 8)
        raw = g.grab()
        sw, sh = g.sw, g.sh
        g.close()
        lit = any(raw[i] for i in range(len(raw)))
        return [Check("Screen capture (GDI)", OK if lit else WARN,
                      f"{sw}x{sh}" + ("" if lit else " - frame came back black "
                                      "(exclusive-fullscreen app in front?)"))]
    except Exception as e:
        return [Check("Screen capture (GDI)", FAIL, f"{type(e).__name__}: {e}")]


def summarise(checks):
    fails = sum(1 for c in checks if c.status == FAIL)
    warns = sum(1 for c in checks if c.status == WARN)
    if fails:
        return FAIL, f"{fails} problem(s), {warns} warning(s)"
    if warns:
        return WARN, f"all essentials present, {warns} warning(s)"
    return OK, "everything checks out"


if __name__ == "__main__":
    cs = run_checks()
    width = max(len(c.name) for c in cs)
    for c in cs:
        mark = {OK: "[ok]  ", WARN: "[warn]", FAIL: "[FAIL]", INFO: "[info]"}[c.status]
        print(f"{mark} {c.name.ljust(width)}  {c.detail}")
    print("\n" + summarise(cs)[1])
