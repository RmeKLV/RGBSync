"""Screen-ambient sync engine.

  OpenRGB SDK  -> motherboard, Razer DeathAdder V2
  direct HID   -> SteelSeries Apex 3 TKL, ASUS XG248Q backlight,
                  Fifine BM38 arm, Fifine AM8 Pro mic

The keyboard takes whichever path is available: OpenRGB if it has the device,
otherwise apex3tkl.py talks to it directly. OpenRGB is normally installed as a
LocalSystem service, and a service scans for devices once at boot and never
again, so "OpenRGB has it" is the less likely of the two.

Two things run on their own timers alongside the frame loop, both because a
device can stop responding without anything reporting an error:

  * Direct mode is re-asserted every few seconds. Vendor software pulls devices
    back onto its own profile; OpenRGB keeps accepting colour writes afterwards
    and returns success, so the device reads as healthy while sitting unlit.
  * Missing devices are retried. Plugging something in, or starting OpenRGB
    late, no longer means restarting the app.

Each device samples its own rectangle of the screen (CFG["regions"]); edit those
visually with rgbsync_gui.py -> "Screen layout".

Capture runs on its own thread so device writes overlap with it rather than
queueing behind it.
"""
import time, sys, json, os, signal, atexit

from screen import CaptureThread, Sampler
from orgb import OpenRGBClient
from xg248q import XG248Q
from fifine import BM38, AM8Pro
from apex3tkl import Apex3TKL
from letterbox import Letterbox
import logbook
import session

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

GRID_C, GRID_R = 48, 27

# axis: "x" left-to-right gradient | "y" top-to-bottom | "avg" one flat colour
#       | "grid" 6x3 canvas (AM8 Pro ring)
DEFAULT_REGIONS = {
    "mobo":    {"x": 0.00, "y": 0.00, "w": 1.00, "h": 1.00, "axis": "avg", "reverse": False},
    "kbd":     {"x": 0.00, "y": 0.62, "w": 1.00, "h": 0.38, "axis": "x",   "reverse": False},
    "mouse":   {"x": 0.00, "y": 0.00, "w": 1.00, "h": 1.00, "axis": "avg", "reverse": False},
    "monitor": {"x": 0.00, "y": 0.00, "w": 1.00, "h": 1.00, "axis": "x",   "reverse": False},
    "bm38":    {"x": 0.00, "y": 0.00, "w": 1.00, "h": 0.45, "axis": "x",   "reverse": False},
    "am8":     {"x": 0.00, "y": 0.00, "w": 1.00, "h": 1.00, "axis": "grid", "reverse": False},
}

DEVICE_KEYS = ("mobo", "kbd", "mouse", "monitor", "bm38", "am8")

# Per-device look. link=True follows the master sliders; set it False to give
# a device its own saturation/gamma/brightness. gain is RGB white balance.
def _dev_defaults(gain=(1.0, 1.0, 1.0)):
    return {"link": True, "saturation": 1.45, "gamma": 0.85,
            "brightness": 1.00, "gain": list(gain)}


DEFAULT_DEVICE = {k: _dev_defaults() for k in DEVICE_KEYS}
DEFAULT_DEVICE["monitor"]["gain"] = [0.80, 1.00, 1.00]   # its white reads pink

DEFAULTS = {
    "fps":        45,
    "quality":    "vivid",     # "mean" | "linear" | "vivid" - see screen.Sampler
    "vividness":  4.0,
    "grid":       [48, 27],    # capture grid; costs ~nothing until regions get small
    "letterbox":  False,       # crop black bars in films (heuristic - see letterbox.py)
    "pause_on_lock": True,     # blackout while the session is locked
    "idle_timeout": 0,         # seconds of no input before blackout; 0 = never
    "smoothing":  0.35,
    "saturation": 1.45,
    "gamma":      0.85,
    "brightness": 1.00,
    "black_floor": 6,          # screen levels below this have no trustworthy hue
    "monitor_hz": 30,
    "mode_refresh_sec": 5,     # re-assert Direct mode this often; 0 = never.
                               # Vendor software (Synapse) steals devices back
                               # out of Direct and OpenRGB reports no error.
    "rescan_sec": 20,          # re-look for missing devices this often; 0 = never
    "kbd_brightness": 16,      # Apex 3 TKL master level, 0-16, when driven by HID
    "enabled": {k: True for k in DEVICE_KEYS},
    "device":  json.loads(json.dumps(DEFAULT_DEVICE)),
    "regions": json.loads(json.dumps(DEFAULT_REGIONS)),
}

CFG = json.loads(json.dumps(DEFAULTS))

AM8_POSITIONS = [(0, 1), (0, 0), (0, 0), (2, 0), (5, 0), (5, 0),
                 (5, 1), (5, 2), (5, 2), (2, 2), (0, 2), (0, 2)]

ORGB_KEYS = ("mobo", "kbd", "mouse")
HID_KEYS = ("monitor", "bm38", "am8")


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        # migrate pre-per-device configs: {"gain": {"orgb": [...], ...}}
        old_gain = saved.pop("gain", None)
        if isinstance(old_gain, dict):
            bucket = {"mobo": "orgb", "kbd": "orgb", "mouse": "orgb",
                      "monitor": "monitor", "bm38": "bm38", "am8": "am8"}
            for dk, bk in bucket.items():
                if bk in old_gain:
                    CFG["device"][dk]["gain"] = list(old_gain[bk])
        for k, v in saved.items():
            if k in ("regions", "device") and isinstance(v, dict):
                for dk, dv in v.items():
                    if dk in CFG[k] and isinstance(dv, dict):
                        CFG[k][dk].update(dv)
            elif isinstance(v, dict) and isinstance(CFG.get(k), dict):
                CFG[k].update(v)
            elif k in CFG:
                CFG[k] = v
    except FileNotFoundError:
        pass
    except Exception as e:
        print("config load failed:", e)
    return CFG


def save_config():
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(CFG, f, indent=2)


def reset_regions():
    CFG["regions"] = json.loads(json.dumps(DEFAULT_REGIONS))


def reset_tuning():
    CFG["device"] = json.loads(json.dumps(DEFAULT_DEVICE))


def settings_for(key):
    """(saturation, gamma, brightness, gain) for a device, master or its own."""
    d = CFG["device"].get(key) or _dev_defaults()
    if d.get("link", True):
        return CFG["saturation"], CFG["gamma"], CFG["brightness"], d.get("gain", [1, 1, 1])
    return (d.get("saturation", CFG["saturation"]),
            d.get("gamma", CFG["gamma"]),
            d.get("brightness", CFG["brightness"]),
            d.get("gain", [1, 1, 1]))


def grade(c, key):
    """Colour grading for one LED, in floats.

    Quantisation is deliberately not done here - see Smoother. After the gamma
    curve a dark screen only reaches output levels 0-2, so each channel is
    effectively one bit and the discarded fraction is the whole difference
    between a neutral dark grey and a saturated corner: (8,7,9) on screen is a
    dark blue-grey, but rounded here it leaves as (1,0,1), magenta. Keep the
    fraction and let Smoother spend it.
    """
    r, g, b = c
    s, gm, br, gain = settings_for(key)

    # Near-black hue is capture noise, not content - a region averaging (3,2,2)
    # has no colour worth reproducing. Fade to grey below the floor so gamma
    # cannot amplify that noise into a visible tint.
    floor = CFG.get("black_floor", 0)
    mx = max(r, g, b)
    if floor > 0 and mx < floor:
        t = mx / float(floor)
        m = (r + g + b) / 3.0
        r, g, b = (m + (v - m) * t for v in (r, g, b))

    grey = (r + g + b) / 3.0
    out = []
    for v, k in zip((r, g, b), gain):
        v = max(0.0, min(255.0, grey + (v - grey) * s))
        v = 255.0 * (v / 255.0) ** gm
        out.append(max(0.0, min(255.0, v * br * k)))
    return tuple(out)


def enhance(c, key):
    """grade() rounded to 8-bit, for callers that just want a plain colour."""
    return tuple(int(round(v)) for v in grade(c, key))


class Smoother:
    """Exponential smoothing kept in float, rounded on the way out.

    The state must not be rounded. Rounding inside the feedback loop is a
    permanent bias, not a rounding error: feed the old int() version a constant
    (9,8,7) and it settles on (8,7,6) and stays there forever.

    Output is rounded, not truncated, and deliberately NOT temporally dithered.
    Dithering does reproduce sub-LSB colour exactly on average, but it only ever
    engages at the bottom of the range - and down there one code is a ~50%
    brightness step, not a 1% one, so on a still image it reads as the LED
    blinking. Stability wins; rounding alone already kills the hue flip that
    truncation caused.
    """

    def __init__(self, n):
        self.n = n
        self.reset()

    def reset(self):
        """Drop smoothing state (used on blackout)."""
        self.prev = [(0.0, 0.0, 0.0)] * self.n

    def __call__(self, colors):
        a = CFG["smoothing"]
        if len(colors) != len(self.prev):
            self.n = len(colors)
            self.reset()
        prev = self.prev
        nxt, out = [], []
        for i, c in enumerate(colors):
            p = prev[i]
            s = (p[0] * a + c[0] * (1 - a),
                 p[1] * a + c[1] * (1 - a),
                 p[2] * a + c[2] * (1 - a))
            nxt.append(s)
            out.append(tuple(0 if v < 0 else (255 if v > 255 else int(round(v)))
                            for v in s))
        self.prev = nxt
        return out


class Engine:
    MAX_FAULTS = 5          # consecutive write failures before a device is dropped

    def __init__(self, log=print):
        self.log = log
        self.running = False
        self.fps_actual = 0.0
        self.worst_ms = 0.0
        self.preview = {}
        self.found = {}
        self.faults = {}
        self.dead = set()
        self.last_raw = None
        self.paused = False
        self.pause_reason = ""
        self._last_session_check = 0.0

        grid = CFG.get("grid") or [GRID_C, GRID_R]
        self.C, self.R = int(grid[0]), int(grid[1])
        self.grab = Sampler(self.C, self.R, CFG.get("quality", "vivid"),
                            CFG.get("vividness", 4.0))
        self.letterbox = Letterbox(self.C, self.R)
        self.content = (0, 0, self.C, self.R)
        self.cap = CaptureThread(self.C, self.R)
        self.cap.start()
        if not self.cap.wait_ready(3.0):
            self.log("WARNING: no screen frame yet")
        else:
            w, h = self.cap.screen_size
            self.log(f"capture : {w}x{h} -> {self.C}x{self.R} "
                     f"({CFG.get('quality', 'vivid')})")

        # OpenRGB is optional: the monitor, the keyboard and both Fifine devices
        # are driven over raw HID and work fine without it.
        self.orgb = None
        self.mobo = self.kbd = self.mouse = None
        self.kbd_hid = None
        self._connect_orgb(first=True)
        self._discover_orgb()

        self.monitor = self._try("monitor", "XG248Q backlight (3)", XG248Q)
        # 0x000C is correct despite the strings: the BM38 exposes iface 1 on
        # usage_page 0x000C reporting product 'IAP' and iface 0 on 0x00FF
        # reporting 'LED'. The LEDs answer on the 'IAP' one - verified on
        # hardware. Do not "fix" this to 0x00FF.
        self.bm38 = self._try("bm38", "Fifine BM38 arm (48)", lambda: BM38(0x000C))
        self.am8 = self._try("am8", "Fifine AM8 Pro (12)", AM8Pro)
        self._discover_kbd_hid()

        self.counts = {
            "mobo": self.mobo.led_count if self.mobo else 4,
            "kbd": self.kbd.led_count if self.kbd else Apex3TKL.LEDS,
            "mouse": self.mouse.led_count if self.mouse else 2,
            "monitor": 3, "bm38": 48, "am8": 12,
        }
        self.sm = {k: Smoother(n) for k, n in self.counts.items()}
        self._last_mon = 0.0
        self._last_mode_refresh = time.time()
        self._last_rescan = time.time()
        missing = [k for k in DEVICE_KEYS if not self.found.get(k)]
        if missing:
            self.log(f"missing : {', '.join(missing)} - retrying every "
                     f"{CFG.get('rescan_sec', 20)}s")
        atexit.register(self._atexit)

    # ------------------------------------------------------------ discovery --
    def _connect_orgb(self, first=False):
        """Open the OpenRGB SDK socket. Returns True if we have one."""
        if self.orgb:
            return True
        try:
            self.orgb = OpenRGBClient(name="rgb-screen-sync")
            if not first:
                self.log("OpenRGB : connected")
            return True
        except Exception as e:
            if first:
                self.log(f"OpenRGB : not available ({e})")
                self.log("OpenRGB : motherboard/mouse will be skipped "
                         "(keyboard falls back to direct HID)")
            return False

    # Several plausible names per device: OpenRGB renames controllers between
    # releases, and a rename should degrade to "keeps working", not "not found".
    ORGB_NAMES = {
        "mobo":  [("ASUS", "B760"), ("TUF", "B760"), ("B760",)],
        "kbd":   [("Apex 3",), ("Apex",)],
        "mouse": [("Deathadder",), ("Death Adder",)],
    }

    def _find_orgb(self, key):
        for pattern in self.ORGB_NAMES[key]:
            d = self.orgb.find(*pattern)
            if d:
                return d
        return None

    def _discover_orgb(self):
        """(Re)bind the OpenRGB-backed devices and put them in Direct mode."""
        if not self.orgb:
            for key in ORGB_KEYS:
                self.found.setdefault(key, None)
            return
        for key in ORGB_KEYS:
            if getattr(self, key) is not None:
                continue
            d = self._find_orgb(key)
            setattr(self, key, d)
            if d:
                mode = self.orgb.set_direct(d)
                self.found[key] = f"{d.name} ({d.led_count} LEDs)"
                self.dead.discard(key)
                self.faults[key] = 0
                self.log(f"OpenRGB : {self.found[key]} -> {mode}")
            else:
                self.found[key] = None

    def _discover_kbd_hid(self):
        """Keyboard fallback: talk to the Apex 3 TKL ourselves.

        Only when OpenRGB does not have it - if both paths were live they would
        fight over the same interface. OpenRGB installed as a LocalSystem
        service detects once at boot and never rescans, so this is the usual
        case rather than the exotic one.
        """
        if self.kbd is not None or self.kbd_hid is not None:
            return
        try:
            self.kbd_hid = Apex3TKL(brightness=CFG.get("kbd_brightness", 16))
            self.found["kbd"] = f"Apex 3 TKL, direct HID ({Apex3TKL.LEDS} zones)"
            self.dead.discard("kbd")
            self.faults["kbd"] = 0
            self.log(f"HID     : {self.found['kbd']}")
        except Exception as e:
            self.found["kbd"] = None
            self.log(f"HID     : Apex 3 TKL unavailable ({e})")

    def rescan(self):
        """Retry everything that is missing. Safe to call at any time."""
        self._last_rescan = time.time()
        before = {k: bool(self.found.get(k)) for k in DEVICE_KEYS}
        for key in list(self.dead):
            self.dead.discard(key)
            self.faults[key] = 0
        if not self.orgb:
            self._connect_orgb()
        self._discover_orgb()
        if self.monitor is None:
            self.monitor = self._try("monitor", "XG248Q backlight (3)", XG248Q)
        if self.bm38 is None:
            self.bm38 = self._try("bm38", "Fifine BM38 arm (48)", lambda: BM38(0x000C))
        if self.am8 is None:
            self.am8 = self._try("am8", "Fifine AM8 Pro (12)", AM8Pro)
        self._discover_kbd_hid()
        gained = [k for k in DEVICE_KEYS if self.found.get(k) and not before[k]]
        for k in gained:
            n = self._count_for(k)
            if self.counts.get(k) != n:
                self.counts[k] = n
                self.sm[k] = Smoother(n)
        if gained:
            self.log(f"rescan  : picked up {', '.join(gained)}")
        return gained

    def _count_for(self, key):
        d = getattr(self, key, None)
        if key in ORGB_KEYS and d is not None:
            return d.led_count
        return self.counts.get(key, 1)

    def _refresh_modes(self):
        """Re-assert Direct on the OpenRGB devices.

        Vendor software - Razer Synapse in particular - quietly pulls a device
        back out of Direct and onto its own profile. Nothing surfaces: OpenRGB
        keeps accepting UPDATELEDS over the socket and returns no error, so the
        device still reads as present and enabled while sitting there unlit.
        That is why stopping and starting the app "fixes" it - a fresh Engine
        re-sends the mode. Send it periodically instead.
        """
        every = CFG.get("mode_refresh_sec", 5)
        if not every:
            return
        if time.time() - self._last_mode_refresh < every:
            return
        self._last_mode_refresh = time.time()
        if self.orgb:
            for key in ORGB_KEYS:
                d = self.dev(key)
                if d and self.on(key):
                    try:
                        self.orgb.set_direct(d)
                    except Exception as e:
                        self.log(f"{key}: mode refresh failed: "
                                 f"{type(e).__name__}: {e}")
        # the keyboard's own brightness is moved by Fn+F11/F12, so re-assert it
        if self.kbd_hid and self.on("kbd"):
            try:
                self.kbd_hid.brightness = None
                self.kbd_hid.set_brightness(CFG.get("kbd_brightness", 16))
            except Exception:
                pass

    def _atexit(self):
        try:
            self.blackout()
        except Exception:
            pass

    def _try(self, key, label, ctor):
        try:
            d = ctor()
            self.found[key] = label
            self.log(f"HID     : {label}")
            return d
        except Exception as e:
            self.found[key] = None
            self.log(f"HID     : {label} unavailable ({e})")
            return None

    def on(self, key):
        return CFG["enabled"].get(key, True) and key not in self.dead

    def dev(self, key):
        return getattr(self, key, None)

    # ------------------------------------------------------------ sampling --
    def sample(self, raw, key, n):
        reg = CFG["regions"].get(key, DEFAULT_REGIONS[key])
        cx0, cy0, cx1, cy1 = self.content       # real picture, bars excluded
        cw, ch = cx1 - cx0, cy1 - cy0
        gx0 = cx0 + reg["x"] * cw
        gy0 = cy0 + reg["y"] * ch
        gw = max(1e-6, reg["w"]) * cw
        gh = max(1e-6, reg["h"]) * ch
        axis = reg.get("axis", "x")

        if axis == "avg":
            out = [self.grab.region(raw, gx0, gy0, gx0 + gw, gy0 + gh)] * n
        elif axis == "grid":
            cell = [[self.grab.region(raw,
                                      gx0 + x * gw / 6, gy0 + y * gh / 3,
                                      gx0 + (x + 1) * gw / 6, gy0 + (y + 1) * gh / 3)
                     for y in range(3)] for x in range(6)]
            out = [cell[x][y] for (x, y) in AM8_POSITIONS][:n]
            out += [out[-1]] * (n - len(out))
        elif axis == "y":
            out = [self.grab.region(raw, gx0, gy0 + i * gh / n,
                                    gx0 + gw, gy0 + (i + 1) * gh / n) for i in range(n)]
        else:
            out = [self.grab.region(raw, gx0 + i * gw / n, gy0,
                                    gx0 + (i + 1) * gw / n, gy0 + gh) for i in range(n)]

        if reg.get("reverse"):
            out = list(reversed(out))
        return out

    def _colors(self, raw, key):
        cols = self.sm[key]([grade(c, key) for c in self.sample(raw, key, self.counts[key])])
        self.preview[key] = cols
        return cols

    def _guard(self, key, fn):
        """Run one device's update; isolate it so a failure can't kill the loop."""
        try:
            fn()
            self.faults[key] = 0
        except Exception as e:
            self.faults[key] = self.faults.get(key, 0) + 1
            if self.faults[key] == 1:
                self.log(f"{key}: {type(e).__name__}: {e}")
            if self.faults[key] >= self.MAX_FAULTS:
                self.dead.add(key)
                self.found[key] = None
                self.log(f"{key}: disabled after {self.MAX_FAULTS} failures")

    def frame(self):
        raw = self.cap.latest()
        if raw is None:
            time.sleep(0.01)
            return
        self.last_raw = raw

        if CFG.get("letterbox"):
            was = self.letterbox.active
            self.content = self.letterbox.update(raw)
            if self.letterbox.active != was:
                self.log(f"letterbox: {self.letterbox.describe()}")
        elif self.letterbox.active or self.content != (0, 0, self.C, self.R):
            self.letterbox.reset()
            self.content = (0, 0, self.C, self.R)

        self.grab.mode = CFG.get("quality", "vivid")
        self.grab.vividness = CFG.get("vividness", 4.0)
        self.grab.prepare(raw)

        for key in ORGB_KEYS:
            d = self.dev(key)
            if d and self.on(key):
                self._guard(key, lambda d=d, k=key: self.orgb.update_leds(d, self._colors(raw, k)))

        if self.kbd_hid and self.on("kbd"):
            self._guard("kbd", lambda: self.kbd_hid.set_colors(self._colors(raw, "kbd")))

        if self.monitor and self.on("monitor") and \
                (time.time() - self._last_mon) > 1.0 / max(1, CFG["monitor_hz"]):
            self._guard("monitor", lambda: self.monitor.set_colors(self._colors(raw, "monitor")))
            self._last_mon = time.time()

        for key in ("bm38", "am8"):
            d = self.dev(key)
            if d and self.on(key):
                self._guard(key, lambda d=d, k=key: d.set_colors(self._colors(raw, k)))

    def run(self):
        self.running = True
        n, t0, worst = 0, time.perf_counter(), 0.0
        while self.running:
            t = time.perf_counter()
            if self._check_session():
                time.sleep(0.5)
                continue
            try:
                self.frame()
                self._refresh_modes()
                self._maybe_rescan()
            except Exception as e:
                self.log(f"frame error: {type(e).__name__}: {e}")
                if not self._reconnect():
                    break
            dt = time.perf_counter() - t
            worst = max(worst, dt)
            n += 1
            if n % 15 == 0:
                el = time.perf_counter() - t0
                self.fps_actual = n / el if el else 0.0
                self.worst_ms = worst * 1000
                if n >= 300:
                    n, t0, worst = 0, time.perf_counter(), 0.0
            time.sleep(max(0.0, 1.0 / max(1, CFG["fps"]) - dt))
        self.blackout()

    def _maybe_rescan(self):
        every = CFG.get("rescan_sec", 20)
        if not every:
            return
        if time.time() - self._last_rescan < every:
            return
        if all(self.found.get(k) for k in DEVICE_KEYS):
            self._last_rescan = time.time()
            return
        self.rescan()

    def blackout_key(self, key):
        """Turn one device off without the caller knowing how it is driven."""
        if key in ORGB_KEYS and self.dev(key) and self.orgb:
            d = self.dev(key)
            self.orgb.update_leds(d, [(0, 0, 0)] * d.led_count)
        elif key == "kbd" and self.kbd_hid:
            self.kbd_hid.fill(0, 0, 0)
        else:
            d = self.dev(key)
            if d:
                d.fill(0, 0, 0)
        sm = self.sm.get(key)
        if sm:
            sm.reset()

    def _reconnect(self):
        """OpenRGB restarted or went away - try to pick it back up."""
        if self.orgb is None:
            return True                      # nothing to reconnect to; HID keeps running
        self.log("reconnecting to OpenRGB...")
        self.orgb = None
        self.mobo = self.mouse = None
        if self.kbd_hid is None:
            self.kbd = None
        for attempt in range(3):
            time.sleep(1.0)
            try:
                if not self._connect_orgb():
                    continue
                self._discover_orgb()
                self.log("reconnected")
                return True
            except Exception:
                self.orgb = None
                continue
        # Losing OpenRGB is not fatal any more: the monitor, the keyboard and
        # both Fifine devices are ours directly, so keep those running.
        if self.kbd_hid or self.monitor or self.bm38 or self.am8:
            self.log("reconnect failed - continuing on direct-HID devices only")
            for key in ORGB_KEYS:
                if getattr(self, key) is None and not (key == "kbd" and self.kbd_hid):
                    self.found[key] = None
            return True
        self.log("reconnect failed - stopping")
        return False

    def _check_session(self):
        """Blackout while locked or idle. Returns True if we should skip this frame."""
        now = time.time()
        if now - self._last_session_check < 1.0:
            return self.paused
        self._last_session_check = now
        if not CFG.get("pause_on_lock", True) and not CFG.get("idle_timeout", 0):
            if self.paused:
                self.paused, self.pause_reason = False, ""
            return False
        try:
            pause, reason = session.should_pause(
                CFG.get("idle_timeout", 0) if CFG.get("pause_on_lock", True) else 0)
            if not CFG.get("pause_on_lock", True):
                pause = reason == "idle" and pause
        except Exception:
            return False
        if pause and not self.paused:
            self.paused, self.pause_reason = True, reason
            self.log(f"paused ({reason}) - lights off")
            try: self.blackout()
            except Exception: pass
        elif not pause and self.paused:
            self.paused, self.pause_reason = False, ""
            self.log("resumed")
            self._last_mode_refresh = 0.0
            self._refresh_modes()
        return self.paused

    def stop(self):
        self.running = False

    def blackout(self):
        for sm in getattr(self, "sm", {}).values():
            sm.reset()
        for key in ORGB_KEYS:
            d = self.dev(key)
            if d and self.orgb:
                try:
                    self.orgb.update_leds(d, [(0, 0, 0)] * d.led_count)
                except Exception:
                    pass
        time.sleep(0.08)
        if self.kbd_hid:
            try:
                self.kbd_hid.fill(0, 0, 0)
            except Exception:
                pass
        for key in HID_KEYS:
            d = self.dev(key)
            if d:
                try:
                    d.fill(0, 0, 0)
                except Exception:
                    pass

    def close(self):
        self.blackout()
        self.cap.stop()
        if self.kbd_hid:
            try:
                self.kbd_hid.close()
            except Exception:
                pass
            self.kbd_hid = None
        for key in HID_KEYS:
            d = self.dev(key)
            if d:
                try:
                    d.close()
                except Exception:
                    pass
        if self.orgb:
            try:
                self.orgb.close()
            except Exception:
                pass


if __name__ == "__main__":
    load_config()
    logbook.session_header("cli")
    logbook.snapshot()
    print("device discovery:")
    eng = Engine(log=logbook.tee(print))
    signal.signal(signal.SIGINT, lambda *a: eng.stop())
    if "--once" in sys.argv:
        eng.frame()
        print("single frame pushed")
    else:
        print(f"\nrunning at {CFG['fps']} fps - Ctrl+C to stop\n")
        eng.run()
    eng.close()
    print("done.")
