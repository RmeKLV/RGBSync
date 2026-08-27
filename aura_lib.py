"""Feature-report helpers for the ASUS Aura devices.

The hidapi handle and its argtypes live in hidlib so every driver in the app
shares one DLL load and one hid_init().
"""
import ctypes

from hidlib import hid, DevInfo, open_first   # noqa: F401


def open_dev(vid, pid, up=None, us=None, iface=None):
    return open_first(vid, pid, usage_page=up, usage=us, interface=iface)

def send(dev, data, size=8):
    b = bytes(data) + b"\x00" * (size - len(data))
    return hid.hid_send_feature_report(dev, b, size)

def get(dev, report_id, size=8):
    buf = ctypes.create_string_buffer(size)
    buf[0] = report_id
    r = hid.hid_get_feature_report(dev, buf, size)
    return r, buf.raw[:size]
