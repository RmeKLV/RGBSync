"""SteelSeries Apex 3 TKL (1038:1622) 8-zone keyboard driver.

Protocol per OpenRGB's SteelSeriesApex8ZoneController (GPL-2.0-or-later),
Controllers/SteelSeriesController/SteelSeriesApex8ZoneController.{h,cpp}, whose
header carries the reverse-engineering notes this is built from.

Why this exists at all: OpenRGB is usually installed as a LocalSystem service,
and a service runs one detection pass at boot - a keyboard that enumerates late
is simply absent from its device list until the next reboot, with no rescan.
Talking to the keyboard ourselves removes OpenRGB from the keyboard's path the
same way it is already removed from the monitor's and both Fifine devices'.

  interface 1, usage_page 0xFFC0, usage 0x0001   (OpenRGB's own detector tuple)

Wire format - hid_write, 65 bytes: report-ID 0x00 then 64 payload bytes.

  0x21 MASK R1 G1 B1 ... R8 G8 B8   set zone colours; MASK is a bitmask of
                                    zones to apply, 0xFF = all eight
  0x22 0xFF                         hand the keyboard back to Rainbow Wave
  0x23 LEVEL                        master brightness, 0x00..0x10

Zone 1 is the leftmost of eight columns, so an "x" axis region maps straight on.

The keyboard has no persistent lighting memory: a USB power cycle drops it back
to Rainbow Wave. Nothing to undo on exit beyond going dark, and REFRESH_SEC
keeps a stale frame from sticking if a write is ever lost.
"""
from hidlib import hid, open_first, StreamDevice

SS_VID = 0x1038


class Apex3TKL(StreamDevice):
    PID = 0x1622
    LEDS = 8
    BRIGHT_MAX = 0x10

    # The keyboard keeps whatever frame it was last given, so unchanged frames
    # can be skipped - unlike the Fifine pair, which go dark without a stream.
    MAX_FPS = 30
    REFRESH_SEC = 2.0

    PKT = 64                    # payload bytes after the report-ID

    CMD_COLOR = 0x21
    CMD_RAINBOW = 0x22
    CMD_BRIGHT = 0x23

    def __init__(self, brightness=BRIGHT_MAX):
        self.dev, self.path = open_first(SS_VID, self.PID,
                                         usage_page=0xFFC0, usage=0x0001,
                                         interface=1)
        if not self.dev:
            raise RuntimeError("Apex 3 TKL LED interface not found "
                               "(1038:1622 iface 1, usage page 0xFFC0)")
        self._init_state()
        self.brightness = None
        self.set_brightness(brightness)

    def set_brightness(self, level):
        """Master brightness, 0x00..0x10. Also moved by the keyboard's own
        Fn+F11/F12, which is why it is re-asserted rather than assumed."""
        level = max(0, min(self.BRIGHT_MAX, int(level)))
        if level == self.brightness:
            return 0
        self._write([self.CMD_BRIGHT, level], self.PKT)
        self.brightness = level
        return 1

    def set_colors(self, colors, force=False):
        colors = list(colors)[:self.LEDS]
        colors += [(0, 0, 0)] * (self.LEDS - len(colors))
        if not self._changed(colors, force):
            return 0
        packet = [self.CMD_COLOR, 0xFF]
        for (r, g, b) in colors:
            packet += [r & 0xFF, g & 0xFF, b & 0xFF]
        self._write(packet, self.PKT)
        return 1

    def fill(self, r, g, b):
        return self.set_colors([(r, g, b)] * self.LEDS, force=True)

    def rainbow_wave(self):
        """Restore the keyboard's own effect - its state after a power cycle."""
        return self._write([self.CMD_RAINBOW, 0xFF], self.PKT)


if __name__ == "__main__":
    import sys, time
    kb = Apex3TKL()
    print("opened:", kb.path)
    if "--rainbow" in sys.argv:
        kb.rainbow_wave()
        print("handed back to Rainbow Wave")
    else:
        for name, col in (("red", (255, 0, 0)), ("green", (0, 255, 0)),
                          ("blue", (0, 0, 255)), ("white", (255, 255, 255))):
            kb.fill(*col)
            print("fill", name)
            time.sleep(1.2)
        print("left-to-right ramp")
        kb.set_colors([(255 - i * 30, i * 30, 128) for i in range(8)], force=True)
        time.sleep(2.0)
        kb.fill(0, 0, 0)
        print("off")
    kb.close()
