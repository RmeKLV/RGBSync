"""Is a game in front? Used to release the screen capture while one is.

Desktop Duplication (dxgi_capture.py) makes Windows keep the desktop
compositor in the loop for the monitor it is attached to. A game then can't
take the direct, low-latency path to the display, and it feels like fewer Hz
even though the fps counter looks fine. The only fix is not to be attached
while the game runs.

Two signals, either is enough:
  * SHQueryUserNotificationState says a Direct3D app owns the screen
    (QUNS_RUNNING_D3D_FULL_SCREEN) - exclusive-fullscreen games.
  * The foreground window belongs to an executable in `game_processes` -
    borderless/optimised fullscreen games Windows reports as merely "busy".
    A fullscreen film or browser is "busy" too, so that state alone is NOT used:
    ambient lighting should keep following a film.
"""
import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32")

QUNS_RUNNING_D3D_FULL_SCREEN = 3
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]


def d3d_fullscreen():
    """True while a Direct3D application is running exclusive fullscreen."""
    state = ctypes.c_int(0)
    try:
        if shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return False
    except OSError:
        return False
    return state.value == QUNS_RUNNING_D3D_FULL_SCREEN


def foreground_exe():
    """Lower-case file name of the foreground window's program, or ''."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(520)
        n = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return ""
        return buf.value.replace("/", "\\").rsplit("\\", 1)[-1].lower()
    finally:
        kernel32.CloseHandle(h)


def game_active(processes=()):
    """True when a game appears to own the screen. `processes`: exe names."""
    if d3d_fullscreen():
        return True
    if processes:
        names = {p.lower() for p in processes}
        return foreground_exe() in names
    return False


if __name__ == "__main__":
    import time
    for _ in range(10):
        print("d3d fullscreen:", d3d_fullscreen(), "| foreground:", foreground_exe())
        time.sleep(1)
