"""Windows shutdown / sleep / display-off notifications. No dependencies.

Why this exists: nothing told the engine the PC was going down. pythonw has no
console to receive CTRL_SHUTDOWN_EVENT, and Tk's own window only maps session
end onto "exit now", so the process was simply killed with every device still
holding its last frame - or, for the Fifine pair, dropping straight back to
their onboard effect.

Windows only delivers these to a real top-level window whose thread pumps
messages (a message-only window does not get WM_QUERYENDSESSION or the power
broadcasts), so PowerWatcher creates a hidden one on its own thread.

Events passed to the callback, which runs on the watcher thread and may block
for a moment - Windows waits for the reply before it carries on:

  "shutdown"            WM_QUERYENDSESSION: logoff, restart or shutdown is starting
  "shutdown_cancelled"  WM_ENDSESSION(FALSE): something vetoed it, carry on
  "suspend"             PBT_APMSUSPEND: going to sleep / hibernate
  "resume"              PBT_APMRESUMEAUTOMATIC: back from sleep
  "display_off"         the monitor has been switched off by power management
  "display_on"          ... and back on (dimmed counts as on)
"""
import ctypes, threading, uuid
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_POWERBROADCAST = 0x0218

PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
PBT_POWERSETTINGCHANGE = 0x8013

DEVICE_NOTIFY_WINDOW_HANDLE = 0

# GUID_CONSOLE_DISPLAY_STATE: Data is a DWORD, 0 off / 1 on / 2 dimmed
GUID_CONSOLE_DISPLAY_STATE = "6fe69556-704a-47a0-8f24-c28d936fda47"

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, s):
        b = uuid.UUID(s).bytes_le
        return cls.from_buffer_copy(b)


class POWERBROADCAST_SETTING(ctypes.Structure):
    _fields_ = [("PowerSetting", GUID), ("DataLength", wintypes.DWORD),
                ("Data", ctypes.c_ubyte * 1)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.RegisterClassW.restype = wintypes.ATOM
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                   wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.RegisterPowerSettingNotification.restype = wintypes.HANDLE
user32.RegisterPowerSettingNotification.argtypes = [wintypes.HANDLE, ctypes.POINTER(GUID),
                                                    wintypes.DWORD]
user32.UnregisterPowerSettingNotification.argtypes = [wintypes.HANDLE]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]


class PowerWatcher(threading.Thread):
    """Hidden window on its own thread, forwarding power events to `callback`."""

    _class_n = 0

    def __init__(self, callback, log=print):
        super().__init__(daemon=True, name="power-watcher")
        self.callback = callback
        self.log = log
        self.hwnd = None
        self._proc = WNDPROC(self._wndproc)     # keep a reference - ctypes won't
        self._display_guid = GUID.parse(GUID_CONSOLE_DISPLAY_STATE)
        self._notify = None
        self._ready = threading.Event()

    def _emit(self, event):
        try:
            self.callback(event)
        except Exception as e:
            self.log(f"power: {event} handler failed: {type(e).__name__}: {e}")

    def _wndproc(self, hwnd, msg, wp, lp):
        if msg == WM_QUERYENDSESSION:
            # Do the work now, not in WM_ENDSESSION: by then Tk may already be
            # tearing the process down from its own thread. Windows waits for
            # this reply, and the blackout takes well under a second.
            self._emit("shutdown")
            return 1                            # never veto a shutdown
        if msg == WM_ENDSESSION:
            if not wp:
                self._emit("shutdown_cancelled")
            return 0
        if msg == WM_POWERBROADCAST:
            if wp == PBT_APMSUSPEND:
                self._emit("suspend")
            elif wp in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                self._emit("resume")
            elif wp == PBT_POWERSETTINGCHANGE and lp:
                s = ctypes.cast(lp, ctypes.POINTER(POWERBROADCAST_SETTING)).contents
                if bytes(s.PowerSetting) == bytes(self._display_guid) and s.DataLength >= 1:
                    self._emit("display_off" if s.Data[0] == 0 else "display_on")
            return 1
        if msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def run(self):
        hinst = kernel32.GetModuleHandleW(None)
        PowerWatcher._class_n += 1
        name = f"RGBSyncPowerWatcher{PowerWatcher._class_n}"
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = name
        if not user32.RegisterClassW(ctypes.byref(wc)):
            self.log(f"power: RegisterClass failed ({ctypes.get_last_error()})")
            self._ready.set()
            return
        # WS_OVERLAPPED, never shown: top-level so it gets the broadcasts
        self.hwnd = user32.CreateWindowExW(0, name, "RGB Sync power", 0,
                                           0, 0, 0, 0, None, None, hinst, None)
        if not self.hwnd:
            self.log(f"power: CreateWindow failed ({ctypes.get_last_error()})")
            self._ready.set()
            return
        self._notify = user32.RegisterPowerSettingNotification(
            self.hwnd, ctypes.byref(self._display_guid), DEVICE_NOTIFY_WINDOW_HANDLE)
        self._ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        if self._notify:
            user32.UnregisterPowerSettingNotification(self._notify)
            self._notify = None
        self.hwnd = None

    def wait_ready(self, timeout=2.0):
        return self._ready.wait(timeout)

    def stop(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)


if __name__ == "__main__":
    import time
    w = PowerWatcher(lambda e: print(time.strftime("%H:%M:%S"), e))
    w.start()
    w.wait_ready()
    print("watching - lock the screen, sleep the display, or Ctrl+C")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        w.stop()
