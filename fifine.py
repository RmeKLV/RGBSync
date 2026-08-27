"""Fifine BM38 / AM8 Pro RGB driver.

Protocol recovered from fifine Genie's own Electron plugins:
  resources/app/dist/plugins/Feifan/{BM38,AM8Pro}.js
Genie drives these over WebHID sendReport(reportId=0, data); the hidapi
equivalent is hid_write() with a leading 0x00 report-ID byte.

Identical consecutive frames are skipped (with a periodic forced refresh so the
device can never be left holding a stale frame).
"""
import time

# hidapi loading, enumeration and the stream-device base class are shared with
# the other raw-HID drivers - see hidlib. `hid` is re-exported because
# ambient.py and prereqs.py import it from here.
from hidlib import hid, DevInfo, open_first, StreamDevice   # noqa: F401

FIFINE_VID = 0x3142


def _open(vid, pid, usage_page, usage=None):
    return open_first(vid, pid, usage_page=usage_page, usage=usage)


class FifineDevice(StreamDevice):
    # MAX_FPS / REFRESH_SEC: never write faster than MAX_FPS. Genie declares a
    # rate per device via Fps() and does not exceed it; pushing harder makes the
    # controllers hiccup. These values are Genie's; each subclass overrides.
    MAX_FPS = 20
    REFRESH_SEC = 2.0


class BM38(FifineDevice):
    """Boom arm. 48 LEDs. Packet: [0x41, 48, 48*RGB (LED order reversed), 0x01]
    split into 64-byte chunks."""
    PID = 0x0038
    LEDS = 48
    MAX_FPS = 20               # Genie's BM38.js: Fps() { return 20; }
    REFRESH_SEC = 0.0          # never skip - it goes dark without a steady stream

    def __init__(self, usage_page=0x000C):
        self.dev, self.path = _open(FIFINE_VID, self.PID, usage_page)
        if not self.dev:
            raise RuntimeError(f"BM38 not found on usage_page 0x{usage_page:04X}")
        self._init_state()

    def set_colors(self, colors, force=False):
        if not self._changed(colors, force):
            return 0
        packet = [0x41, self.LEDS]
        for (r, g, b) in reversed(colors):          # plugin does .reverse()
            packet += [r, g, b]
        packet.append(0x01)
        n = 0
        for i in range(0, len(packet), 64):
            self._write(packet[i:i + 64], 64)
            n += 1
        return n

    def fill(self, r, g, b):
        return self.set_colors([(r, g, b)] * self.LEDS, force=True)


class AM8Pro(FifineDevice):
    """Microphone. 12 LEDs. Packet: [A5 5A FE 26 0B 0C] + 12*RGB + [0x16], padded to 256.

    Its controller drops the LEDs when the host stops sending, so this device
    must be written every frame - Genie streams to it at 30fps for the same
    reason. Skipping unchanged frames makes it blink.
    """
    PID = 0x01A8
    LEDS = 12
    MAX_FPS = 30               # Genie's AM8Pro.js: Fps() { return 30; }
    REFRESH_SEC = 0.0          # never skip - it goes dark without a steady stream

    def __init__(self):
        self.dev, self.path = _open(FIFINE_VID, self.PID, 0xFF00, 0x55AA)
        if not self.dev:
            raise RuntimeError("AM8 Pro control interface not found")
        self._init_state()

    def set_colors(self, colors, force=False):
        if not self._changed(colors, force):
            return 0
        packet = [0xA5, 0x5A, 0xFE, 0x26, 0x0B, 0x0C]
        for (r, g, b) in colors:
            packet += [r, g, b]
        packet.append(0x16)
        self._write(packet, 256)
        return 1

    def fill(self, r, g, b):
        return self.set_colors([(r, g, b)] * self.LEDS, force=True)
