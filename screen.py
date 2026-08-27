"""Screen capture via GDI StretchBlt -> small RGB grid. No dependencies.

The grab costs ~12.5ms regardless of grid size (it reads back from the GPU),
so CaptureThread runs it on its own thread and publishes the latest frame.
That lets device writes overlap with capture instead of queueing behind it.
"""
import ctypes, threading, time
from ctypes import wintypes

user32, gdi32 = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")
try:
    ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

SRCCOPY, HALFTONE, BI_RGB = 0x00CC0020, 4, 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _srgb_to_linear(v):
    v /= 255.0
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(v):
    v = max(0.0, min(1.0, v))
    v = v * 12.92 if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055
    return int(255.0 * v + 0.5)


_LIN_SCALE = 65535
# sRGB byte -> linear light, and a reverse table fine enough to avoid banding
TO_LINEAR = [int(_srgb_to_linear(v) * _LIN_SCALE + 0.5) for v in range(256)]
_INV_N = 4096
TO_SRGB = bytes(_linear_to_srgb(i / (_INV_N - 1)) for i in range(_INV_N))


class Sampler:
    """Region averaging over a raw BGRA grid. Pure data - safe on any thread.

    Three quality modes:
      "mean"   - arithmetic mean of sRGB bytes. Cheapest, but averaging gamma-
                 encoded values mixes colours darker and muddier than real light.
      "linear" - convert to linear light, average, convert back. Physically how
                 light actually mixes; small bright things stop vanishing.
      "vivid"  - linear, but each cell weighted by its saturation, so a colourful
                 object on a drab background drives the colour instead of being
                 averaged into grey. `vividness` sets how hard it leans.

    Call prepare(raw) once per frame before the region() calls for that frame.
    """

    def __init__(self, cols, rows, mode="vivid", vividness=4.0):
        self.cols, self.rows = cols, rows
        self.mode = mode
        self.vividness = vividness
        self._lr = None

    def prepare(self, raw):
        """Precompute linear values + weights once, amortised over every device."""
        if self.mode == "mean" or raw is None:
            self._lr = None
            return
        n = self.cols * self.rows
        lr = [0] * n; lg = [0] * n; lb = [0] * n
        L = TO_LINEAR
        k = self.vividness if self.mode == "vivid" else 0.0
        w = [1.0] * n if not k else [0.0] * n
        for i in range(n):
            j = i << 2
            b = raw[j]; g = raw[j + 1]; r = raw[j + 2]
            lr[i] = L[r]; lg[i] = L[g]; lb[i] = L[b]
            if k:
                mx = r if r > g else g
                if b > mx: mx = b
                mn = r if r < g else g
                if b < mn: mn = b
                w[i] = 1.0 + k * ((mx - mn) / mx) if mx else 1.0
        self._lr, self._lg, self._lb, self._w = lr, lg, lb, w

    def region(self, raw, x0, y0, x1, y1):
        c, r = self.cols, self.rows
        x0 = max(0, min(c - 1, int(x0))); x1 = max(x0 + 1, min(c, int(x1)))
        y0 = max(0, min(r - 1, int(y0))); y1 = max(y0 + 1, min(r, int(y1)))

        if self._lr is None:                       # plain sRGB mean
            rr = gg = bb = 0
            n = (x1 - x0) * (y1 - y0)
            for y in range(y0, y1):
                base = y * c
                for x in range(x0, x1):
                    i = (base + x) * 4
                    rr += raw[i + 2]; gg += raw[i + 1]; bb += raw[i]
            return (rr // n, gg // n, bb // n)

        lr, lg, lb, wt = self._lr, self._lg, self._lb, self._w
        ar = ag = ab = 0.0
        wsum = 0.0
        for y in range(y0, y1):
            base = y * c
            for x in range(x0, x1):
                i = base + x
                w = wt[i]
                ar += lr[i] * w; ag += lg[i] * w; ab += lb[i] * w
                wsum += w
        if wsum <= 0:
            return (0, 0, 0)
        s = _INV_N - 1
        return (TO_SRGB[min(s, int(ar / wsum * s / _LIN_SCALE))],
                TO_SRGB[min(s, int(ag / wsum * s / _LIN_SCALE))],
                TO_SRGB[min(s, int(ab / wsum * s / _LIN_SCALE))])


class ScreenGrabber:
    """Downsamples the primary monitor to a cols x rows BGRA grid."""

    def __init__(self, cols=48, rows=27):
        self.cols, self.rows = cols, rows
        self.sw = user32.GetSystemMetrics(0)
        self.sh = user32.GetSystemMetrics(1)
        self.hdc_screen = user32.GetDC(0)
        self.hdc_mem = gdi32.CreateCompatibleDC(self.hdc_screen)
        self.hbm = gdi32.CreateCompatibleBitmap(self.hdc_screen, cols, rows)
        gdi32.SelectObject(self.hdc_mem, self.hbm)
        gdi32.SetStretchBltMode(self.hdc_mem, HALFTONE)

        self.bmi = BITMAPINFO()
        h = self.bmi.bmiHeader
        h.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        h.biWidth, h.biHeight = cols, -rows        # negative => top-down
        h.biPlanes, h.biBitCount, h.biCompression = 1, 32, BI_RGB
        self.buf = ctypes.create_string_buffer(cols * rows * 4)

    def grab(self):
        gdi32.StretchBlt(self.hdc_mem, 0, 0, self.cols, self.rows,
                         self.hdc_screen, 0, 0, self.sw, self.sh, SRCCOPY)
        gdi32.GetDIBits(self.hdc_mem, self.hbm, 0, self.rows,
                        self.buf, ctypes.byref(self.bmi), 0)
        return self.buf.raw

    def close(self):
        try:
            gdi32.DeleteObject(self.hbm)
            gdi32.DeleteDC(self.hdc_mem)
            user32.ReleaseDC(0, self.hdc_screen)
        except Exception:
            pass


class CaptureThread(threading.Thread):
    """Grabs continuously; `latest()` returns the most recent frame.

    The grabber is created inside run() so its GDI objects belong to this thread.
    """

    def __init__(self, cols=48, rows=27, max_fps=120):
        super().__init__(daemon=True)
        self.cols, self.rows = cols, rows
        self.max_fps = max_fps
        self._raw = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self.fps = 0.0
        self.screen_size = (0, 0)

    def run(self):
        g = ScreenGrabber(self.cols, self.rows)
        self.screen_size = (g.sw, g.sh)
        n, t0 = 0, time.perf_counter()
        try:
            while not self._stop.is_set():
                t = time.perf_counter()
                try:
                    raw = g.grab()
                except Exception:
                    time.sleep(0.2)
                    continue
                with self._lock:
                    self._raw = raw
                self._ready.set()
                n += 1
                if n >= 20:
                    el = time.perf_counter() - t0
                    self.fps = n / el if el else 0.0
                    n, t0 = 0, time.perf_counter()
                dt = time.perf_counter() - t
                time.sleep(max(0.0, 1.0 / self.max_fps - dt))
        finally:
            g.close()

    def latest(self):
        with self._lock:
            return self._raw

    def wait_ready(self, timeout=3.0):
        return self._ready.wait(timeout)

    def stop(self):
        self._stop.set()
