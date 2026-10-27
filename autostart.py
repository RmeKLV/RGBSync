"""Start-at-login toggle, via the per-user HKCU Run key.

Per-user only - it touches nothing outside HKEY_CURRENT_USER and needs no admin
rights. Removing the value fully undoes it.
"""
import os
import sys
import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "RGBScreenSync"

HERE = os.path.dirname(os.path.abspath(__file__))
GUI = os.path.join(HERE, "rgbsync_gui.py")


def _pythonw():
    """pythonw.exe runs without a console window; fall back to python.exe."""
    exe = sys.executable or "python.exe"
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.exists(cand) else exe


def command():
    return f'"{_pythonw()}" "{GUI}" --autostart'


def is_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            val, _ = winreg.QueryValueEx(k, VALUE_NAME)
            return bool(val)
    except FileNotFoundError:
        return False
    except OSError:
        return False


def current_command():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            val, _ = winreg.QueryValueEx(k, VALUE_NAME)
            return val
    except OSError:
        return None


def enable():
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, command())
    return command()


def disable():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, VALUE_NAME)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def repair():
    """Point an existing Run entry at this copy of the app. -> old command if
    it was changed, else None.

    The entry stores an absolute path. Move the folder and it keeps naming
    the old one, so Windows silently starts nothing at login - while
    is_enabled() still says yes, because the value exists. Only ever
    rewrites an entry that is already there; never turns autostart on.
    """
    old = current_command()
    if not old or old == command():
        return None
    enable()
    return old


def set_enabled(on):
    return enable() if on else (disable() or None)
