"""ASUS XG248Q (0B05:186E) Aura backlight driver.

Protocol per SignalRGB's ASUS_Monitor_Controller.js, which lists PID 0x186E.
8-byte FEATURE reports, report ID 0x03. Three backlight LEDs.
Register order is R, B, G (green and blue swapped), and the unlock/apply trio
is sent at the START of a frame, with value 0x01.

Each feature report costs ~1.15ms, so a naive full update is 12 writes / 13.9ms.
We track what the device already holds and only send registers that changed,
which drops a typical frame to zero or a handful of writes.
"""
import time
from aura_lib import hid, open_dev, send


class XG248Q:
    VID, PID, LEDS = 0x0B05, 0x186E, 3
    REFRESH_SEC = 2.0          # resend everything occasionally, as a safety net

    def __init__(self):
        self.dev, self.path = open_dev(self.VID, self.PID, up=0xFFA0, us=0x0001)
        if not self.dev:
            raise RuntimeError("XG248Q Aura interface not found")
        self._held = {}        # register -> value we believe the monitor holds
        self._last_full = 0.0
        self.writes = 0

    def _cmd(self, reg, val):
        self.writes += 1
        return send(self.dev, [0x03, 0x02, 0xA1, 0x80, reg, val, 0x00, 0x00], 8)

    def set_colors(self, colors, force=False):
        regs = {}
        for i, (r, g, b) in enumerate(colors[:self.LEDS]):
            regs[i * 3 + 0] = r      # R
            regs[i * 3 + 1] = b      # B  <- swapped
            regs[i * 3 + 2] = g      # G

        now = time.time()
        if force or (now - self._last_full) > self.REFRESH_SEC:
            changed = regs
            self._last_full = now
        else:
            changed = {k: v for k, v in regs.items() if self._held.get(k) != v}

        if not changed:
            return 0

        self._cmd(0x20, 0x01)
        self._cmd(0x30, 0x01)
        self._cmd(0xA0, 0x01)
        for reg, val in changed.items():
            self._cmd(reg, val)
        self._held.update(changed)
        return len(changed)

    def fill(self, r, g, b):
        return self.set_colors([(r, g, b)] * self.LEDS, force=True)

    def close(self):
        if self.dev:
            hid.hid_close(self.dev)
            self.dev = None
