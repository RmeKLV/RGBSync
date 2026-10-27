"""Shared hidapi plumbing.

One DLL handle for the whole app, plus the base class for devices that get a
colour frame streamed to them every capture tick.

Split out of fifine.py once the SteelSeries keyboard needed the same machinery:
rate limiting, skip-if-unchanged, and a periodic forced refresh are properties
of "streaming colours at a USB device", not of any one vendor.
"""
import os, time, ctypes
from ctypes import (c_char_p, c_wchar_p, c_ushort, c_int, c_size_t,
                    POINTER, Structure, c_void_p)

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_hidapi():
    for p in (os.path.join(_HERE, "hidapi.dll"),
              r"C:\Program Files\OpenRGB\hidapi.dll",
              "hidapi.dll"):
        try:
            return ctypes.CDLL(p)
        except OSError:
            continue
    raise OSError("hidapi.dll not found (expected next to this script)")


hid = _load_hidapi()


class DevInfo(Structure):
    pass


DevInfo._fields_ = [
    ("path", c_char_p), ("vendor_id", c_ushort), ("product_id", c_ushort),
    ("serial_number", c_wchar_p), ("release_number", c_ushort),
    ("manufacturer_string", c_wchar_p), ("product_string", c_wchar_p),
    ("usage_page", c_ushort), ("usage", c_ushort), ("interface_number", c_int),
    ("next", POINTER(DevInfo)),
]

hid.hid_enumerate.restype = POINTER(DevInfo)
hid.hid_enumerate.argtypes = [c_ushort, c_ushort]
hid.hid_free_enumeration.argtypes = [POINTER(DevInfo)]
hid.hid_open_path.restype = c_void_p
hid.hid_open_path.argtypes = [c_char_p]
hid.hid_write.restype = c_int
hid.hid_write.argtypes = [c_void_p, c_char_p, c_size_t]
hid.hid_read_timeout.restype = c_int
hid.hid_read_timeout.argtypes = [c_void_p, c_char_p, c_size_t, c_int]
hid.hid_send_feature_report.restype = c_int
hid.hid_send_feature_report.argtypes = [c_void_p, c_char_p, c_size_t]
hid.hid_get_feature_report.restype = c_int
hid.hid_get_feature_report.argtypes = [c_void_p, c_char_p, c_size_t]
hid.hid_error.restype = c_wchar_p
hid.hid_error.argtypes = [c_void_p]
hid.hid_close.argtypes = [c_void_p]
hid.hid_init()


def describe(vid, pid=0):
    """Every HID interface matching vid/pid, as plain dicts. Diagnostics only.

    A device missing from here is missing from Windows; a device present here
    but missing from OpenRGB is OpenRGB's problem, not ours. That distinction
    is the whole reason this ends up in the log.
    """
    out = []
    head = hid.hid_enumerate(vid, pid)
    cur = head
    while cur:
        d = cur.contents
        out.append({
            "vid": d.vendor_id, "pid": d.product_id,
            "interface": d.interface_number,
            "usage_page": d.usage_page, "usage": d.usage,
            "product": d.product_string or "",
            "path": d.path.decode(errors="replace"),
        })
        cur = d.next
    if head:
        hid.hid_free_enumeration(head)
    return out


def open_first(vid, pid, usage_page=None, usage=None, interface=None):
    """Open the first interface matching every constraint given. -> (handle, path)."""
    head = hid.hid_enumerate(vid, pid)
    cur = head
    try:
        while cur:
            d = cur.contents
            if ((usage_page is None or d.usage_page == usage_page)
                    and (usage is None or d.usage == usage)
                    and (interface is None or d.interface_number == interface)):
                h = hid.hid_open_path(d.path)
                if h:
                    return h, d.path.decode(errors="replace")
            cur = d.next
    finally:
        if head:
            hid.hid_free_enumeration(head)
    return None, None


class StreamDevice:
    """A device we push a full colour frame to, over and over.

    MAX_FPS: never write faster than this. Vendor software declares a rate per
    device and does not exceed it; pushing harder makes controllers hiccup.

    REFRESH_SEC: seconds after which an unchanged frame is re-sent anyway.
    0.0 means "never skip" - needed by devices that go dark when the host stops
    talking to them.
    """
    MAX_FPS = 20
    REFRESH_SEC = 2.0

    def _init_state(self):
        self._last = None
        self._last_sent = 0.0
        self.writes = 0

    def _changed(self, colors, force):
        now = time.time()
        # rate limit first - it outranks "never skip"
        if not force and self.MAX_FPS and (now - self._last_sent) < 1.0 / self.MAX_FPS:
            return False
        if (force or self.REFRESH_SEC <= 0.0 or self._last != colors
                or (now - self._last_sent) >= self.REFRESH_SEC):
            self._last = list(colors)
            self._last_sent = now
            return True
        return False

    def _write(self, payload, report_len):
        """hid_write with leading report-ID 0, payload zero-padded to report_len."""
        buf = bytes([0x00]) + bytes(payload) + b"\x00" * (report_len - len(payload))
        self.writes += 1
        r = hid.hid_write(self.dev, buf, len(buf))
        if r < 0:
            raise IOError(f"hid_write failed: {hid.hid_error(self.dev)}")
        return r

    def close(self):
        if getattr(self, "dev", None):
            hid.hid_close(self.dev)
            self.dev = None


class Streamer:
    """Feeds one StreamDevice from its own thread, at the device's own rate.

    For devices that go dark - or fall back to their onboard effect - the
    moment frames stop arriving (the Fifine pair, REFRESH_SEC 0.0). Sending
    from the engine's frame loop tied their timing to it: with the loop at
    30 fps a frame landing a fraction early hit the MAX_FPS limit and was
    dropped, the BM38 (20/s) ended up at 15/s with 66 ms gaps, and a game
    holding the CPU stretched the gaps further - each one a visible blink.

    Here the engine only hands over the latest colours (set_colors) and this
    thread sends whatever is current every 1/MAX_FPS on fixed deadlines, the
    way Genie does. It runs at raised priority inside our below-normal
    process: a few small USB writes a second, so it can't cost a game anything,
    but it won't be starved by one either.

    Drop-in for the device: set_colors / fill / close, everything else passes
    through. A write error is re-raised from the next set_colors call so the
    engine's fault counting still sees it.
    """

    THREAD_PRIORITY_HIGHEST = 2

    def __init__(self, dev, name="stream"):
        import threading
        self.dev = dev
        self._lock = threading.Lock()       # one writer on the handle at a time
        self._colors = None
        self._error = None
        self._halt = threading.Event()
        self.period = 1.0 / max(1, dev.MAX_FPS)
        self.late = 0                       # sends that missed their deadline by a full period
        self._thread = threading.Thread(target=self._run, daemon=True, name=name)
        self._thread.start()

    def __getattr__(self, name):            # LEDS, path, writes, set_light ...
        return getattr(self.dev, name)

    def set_colors(self, colors, force=False):
        err, self._error = self._error, None
        if err is not None:
            raise err
        self._colors = list(colors)
        return 0

    def fill(self, r, g, b):
        """Write now (blackout must not wait for the next tick), then keep streaming it."""
        colors = [(r, g, b)] * self.dev.LEDS
        self._colors = colors
        with self._lock:
            if self.dev.dev:
                return self.dev.set_colors(colors, force=True)
        return 0

    def set_light(self, on):
        with self._lock:
            return self.dev.set_light(on)

    def _run(self):
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.GetCurrentThread.restype = ctypes.c_void_p
            k32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
            k32.SetThreadPriority(k32.GetCurrentThread(), self.THREAD_PRIORITY_HIGHEST)
        except Exception:
            pass
        nxt = time.perf_counter()
        while not self._halt.is_set():
            colors = self._colors
            if colors is not None:
                try:
                    with self._lock:
                        if self.dev.dev:
                            self.dev.set_colors(colors, force=True)
                except Exception as e:
                    self._error = e
            nxt += self.period
            now = time.perf_counter()
            if now > nxt + self.period:     # fell behind (suspend, stall): resync
                self.late += 1
                nxt = now
            else:
                self._halt.wait(max(0.0, nxt - now))

    def close(self):
        self._halt.set()
        self._thread.join(timeout=1.0)
        with self._lock:
            self.dev.close()
