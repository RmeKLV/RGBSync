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
