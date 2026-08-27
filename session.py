"""Windows session state: is the workstation locked, is the display asleep.

OpenInputDesktop() fails while the secure desktop (lock screen / UAC) is up,
which is the standard way to detect a locked session without needing a window
to receive WTS notifications.
"""
import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32")

DESKTOP_SWITCHDESKTOP = 0x0100

user32.OpenInputDesktop.restype = wintypes.HANDLE
user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
user32.CloseDesktop.argtypes = [wintypes.HANDLE]
user32.GetLastInputInfo.argtypes = [ctypes.c_void_p]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def is_locked():
    """True when the lock screen (or another secure desktop) has input focus."""
    h = user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
    if not h:
        return True
    user32.CloseDesktop(h)
    return False


def idle_seconds():
    """Seconds since the last keyboard or mouse input, system-wide."""
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    return max(0.0, (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) / 1000.0)


def should_pause(idle_timeout=0):
    """(paused, reason). idle_timeout of 0 disables the idle check."""
    if is_locked():
        return True, "locked"
    if idle_timeout and idle_seconds() >= idle_timeout:
        return True, "idle"
    return False, ""
