"""Screen capture via DXGI Desktop Duplication, downscaled on the GPU.

Why: the GDI path (screen.ScreenGrabber) costs ~12-14 ms per grab on a 1440p
desktop - DWM copies the whole composited frame back from the GPU, then
StretchBlt HALFTONE shrinks it on the CPU. Measured with bench.py that was
~20% of a core at 30/s, and the readback is exactly what a game can feel.

Desktop Duplication hands us the desktop as a GPU texture. We copy it into a
texture with a mip chain, let the GPU build the mips (each level a 2x2 box
average of the one above), and read back only the small level that covers the
sampling grid - a few KB instead of the whole screen. When nothing on screen
has changed, AcquireNextFrame times out and the previous frame is reused at
no cost at all.

Plain ctypes COM, no dependencies. Vtable slots below were counted from the
Windows SDK interface definitions (via Wine's dxgi.idl / dxgi1_2.idl /
d3d11.idl), inherited methods included. A wrong slot crashes the process
instead of raising, so do not edit them without recounting.

Output is identical in shape to ScreenGrabber.grab(): cols*rows*4 bytes, BGRA,
top-down - so Sampler, Letterbox and everything downstream are unchanged.
"""
import ctypes, uuid, struct
from ctypes import c_void_p, POINTER, byref

# Explicit widths, not wintypes/c_long: those follow the platform's C `long`,
# and every one of these is 32-bit in the Windows ABI.
c_uint = ctypes.c_uint32
c_long = ctypes.c_int32     # HRESULT, LONG, BOOL

dxgi = ctypes.WinDLL("dxgi")
d3d11 = ctypes.WinDLL("d3d11")

# ---------------------------------------------------------------- constants --
DXGI_FORMAT_B8G8R8A8_UNORM = 87
DXGI_FORMAT_R16G16B16A16_FLOAT = 10
# Bytes per texel for the desktop formats we accept. DuplicateOutput is meant
# to hand back BGRA8, but with Windows 11 Auto Color Management (or HDR) on,
# DWM composes in FP16 scRGB and frames can arrive as R16G16B16A16_FLOAT -
# seen on the XG248Q, intermittently. We follow whatever arrives.
_BPP = {DXGI_FORMAT_B8G8R8A8_UNORM: 4, DXGI_FORMAT_R16G16B16A16_FLOAT: 8}

# scRGB linear value that is SDR white. 1.0 on an SDR display under Auto Color
# Management; with HDR on, Windows maps SDR white to its "SDR content
# brightness" level, which can be 2-3x this, and bright areas would clip.
SDR_WHITE = 1.0

_LUT_N = 4096
_TO_SRGB = bytes(
    int(255.0 * (v * 12.92 if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055) + 0.5)
    for v in (i / (_LUT_N - 1) for i in range(_LUT_N)))
D3D11_SDK_VERSION = 7
D3D_DRIVER_TYPE_UNKNOWN = 0
D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20
D3D11_USAGE_DEFAULT, D3D11_USAGE_STAGING = 0, 3
D3D11_BIND_SHADER_RESOURCE, D3D11_BIND_RENDER_TARGET = 0x08, 0x20
D3D11_CPU_ACCESS_READ = 0x20000
D3D11_RESOURCE_MISC_GENERATE_MIPS = 0x1
D3D11_MAP_READ = 1
DXGI_MODE_ROTATION_UNSPECIFIED, DXGI_MODE_ROTATION_IDENTITY = 0, 1

DXGI_ERROR_NOT_FOUND = 0x887A0002
DXGI_ERROR_ACCESS_LOST = 0x887A0026
DXGI_ERROR_WAIT_TIMEOUT = 0x887A0027

# vtable slots (IUnknown = 0..2, IDXGIObject adds 3..6)
_QI, _RELEASE = 0, 2
_FACTORY1_ENUMADAPTERS1 = 12
_ADAPTER_ENUMOUTPUTS = 7
_OUTPUT_GETDESC = 7
_OUTPUT1_DUPLICATEOUTPUT = 22
_DUPL_ACQUIRENEXTFRAME = 8
_DUPL_RELEASEFRAME = 14
_DEVICE_CREATETEXTURE2D = 5
_DEVICE_CREATESRV = 7
_CTX_MAP, _CTX_UNMAP = 14, 15
_CTX_COPYSUBRESOURCEREGION = 46
_CTX_GENERATEMIPS = 54
_TEX2D_GETDESC = 10


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, s):
        return cls.from_buffer_copy(uuid.UUID(s).bytes_le)


IID_IDXGIFactory1 = GUID.of("770aae78-f26f-4dba-a829-253c83d1b387")
IID_IDXGIOutput1 = GUID.of("00cddea8-939b-4b83-a340-a685226666cc")
IID_ID3D11Texture2D = GUID.of("6f15aaf2-d208-4e89-9ab4-489535d34f9c")


class RECT(ctypes.Structure):
    _fields_ = [("left", c_long), ("top", c_long), ("right", c_long), ("bottom", c_long)]


class DXGI_OUTPUT_DESC(ctypes.Structure):
    _fields_ = [("DeviceName", ctypes.c_uint16 * 32),     # WCHAR[32]
                ("DesktopCoordinates", RECT),
                ("AttachedToDesktop", c_long),
                ("Rotation", c_uint),
                ("Monitor", c_void_p)]


class DXGI_SAMPLE_DESC(ctypes.Structure):
    _fields_ = [("Count", c_uint), ("Quality", c_uint)]


class D3D11_TEXTURE2D_DESC(ctypes.Structure):
    _fields_ = [("Width", c_uint), ("Height", c_uint), ("MipLevels", c_uint),
                ("ArraySize", c_uint), ("Format", c_uint),
                ("SampleDesc", DXGI_SAMPLE_DESC), ("Usage", c_uint),
                ("BindFlags", c_uint), ("CPUAccessFlags", c_uint),
                ("MiscFlags", c_uint)]


class DXGI_OUTDUPL_FRAME_INFO(ctypes.Structure):
    _fields_ = [("LastPresentTime", ctypes.c_longlong),
                ("LastMouseUpdateTime", ctypes.c_longlong),
                ("AccumulatedFrames", c_uint),
                ("RectsCoalesced", c_long),
                ("ProtectedContentMaskedOut", c_long),
                ("PointerX", c_long), ("PointerY", c_long),
                ("PointerVisible", c_long),
                ("TotalMetadataBufferSize", c_uint),
                ("PointerShapeBufferSize", c_uint)]


class D3D11_MAPPED_SUBRESOURCE(ctypes.Structure):
    _fields_ = [("pData", c_void_p), ("RowPitch", c_uint), ("DepthPitch", c_uint)]


dxgi.CreateDXGIFactory1.restype = c_long
dxgi.CreateDXGIFactory1.argtypes = [POINTER(GUID), POINTER(c_void_p)]
d3d11.D3D11CreateDevice.restype = c_long
d3d11.D3D11CreateDevice.argtypes = [c_void_p, c_uint, c_void_p, c_uint, c_void_p, c_uint,
                                    c_uint, POINTER(c_void_p), POINTER(c_uint),
                                    POINTER(c_void_p)]


def _call(obj, slot, restype, *args):
    """Call COM method `slot` on interface pointer `obj` (a plain int/c_void_p).

    `args` are (ctype, value) pairs so every argument is passed with the exact
    width the method expects - ctypes' default int conversion would truncate
    pointers on 64-bit.
    """
    vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(restype, c_void_p, *[t for t, _ in args])
    return proto(vtbl[slot])(obj, *[v for _, v in args])


def _hr(hr, what):
    if hr < 0:
        raise OSError(f"{what} failed: 0x{hr & 0xFFFFFFFF:08X}")


def _release(obj):
    if obj:
        _call(obj, _RELEASE, c_uint)


def _qi(obj, iid):
    out = c_void_p()
    _hr(_call(obj, _QI, c_long, (POINTER(GUID), byref(iid)),
              (POINTER(c_void_p), byref(out))), "QueryInterface")
    return out.value


class DXGIGrabber:
    """Same interface as screen.ScreenGrabber: grab() -> BGRA bytes, close()."""

    backend = "dxgi"

    def __init__(self, cols=48, rows=27):
        self.cols, self.rows = cols, rows
        self.factory = self.adapter = self.output1 = None
        self.device = self.context = self.dup = None
        self.mip_tex = self.srv = self.staging = None
        self._last = None
        try:
            self._open_output()
            self._open_device()
            self._duplicate()
            self._make_textures()
        except Exception:
            self.close()
            raise

    # ------------------------------------------------------------- setup --
    def _open_output(self):
        """Find the primary monitor (the one whose desktop rect starts at 0,0)
        and the adapter driving it - not necessarily adapter 0 on a laptop or
        with two GPUs."""
        f = c_void_p()
        _hr(dxgi.CreateDXGIFactory1(byref(IID_IDXGIFactory1), byref(f)), "CreateDXGIFactory1")
        self.factory = f.value
        ai = 0
        while True:
            ad = c_void_p()
            hr = _call(self.factory, _FACTORY1_ENUMADAPTERS1, c_long,
                       (c_uint, ai), (POINTER(c_void_p), byref(ad)))
            if (hr & 0xFFFFFFFF) == DXGI_ERROR_NOT_FOUND:
                break
            _hr(hr, "EnumAdapters1")
            oi = 0
            while True:
                out = c_void_p()
                hr = _call(ad.value, _ADAPTER_ENUMOUTPUTS, c_long,
                           (c_uint, oi), (POINTER(c_void_p), byref(out)))
                if (hr & 0xFFFFFFFF) == DXGI_ERROR_NOT_FOUND:
                    break
                _hr(hr, "EnumOutputs")
                desc = DXGI_OUTPUT_DESC()
                _hr(_call(out.value, _OUTPUT_GETDESC, c_long,
                          (POINTER(DXGI_OUTPUT_DESC), byref(desc))), "IDXGIOutput::GetDesc")
                r = desc.DesktopCoordinates
                if desc.AttachedToDesktop and r.left == 0 and r.top == 0:
                    if desc.Rotation not in (DXGI_MODE_ROTATION_UNSPECIFIED,
                                             DXGI_MODE_ROTATION_IDENTITY):
                        _release(out.value); _release(ad.value)
                        raise OSError("primary display is rotated - using GDI capture")
                    try:
                        self.output1 = _qi(out.value, IID_IDXGIOutput1)
                    finally:
                        _release(out.value)
                    self.adapter = ad.value
                    self.sw, self.sh = r.right - r.left, r.bottom - r.top
                    return
                _release(out.value)
                oi += 1
            _release(ad.value)
            ai += 1
        raise OSError("primary display not found among DXGI outputs")

    def _open_device(self):
        dev, ctx, fl = c_void_p(), c_void_p(), c_uint()
        _hr(d3d11.D3D11CreateDevice(self.adapter, D3D_DRIVER_TYPE_UNKNOWN, None,
                                    D3D11_CREATE_DEVICE_BGRA_SUPPORT, None, 0,
                                    D3D11_SDK_VERSION, byref(dev), byref(fl), byref(ctx)),
            "D3D11CreateDevice")
        self.device, self.context = dev.value, ctx.value

    def _duplicate(self):
        d = c_void_p()
        _hr(_call(self.output1, _OUTPUT1_DUPLICATEOUTPUT, c_long,
                  (c_void_p, self.device), (POINTER(c_void_p), byref(d))),
            "DuplicateOutput")
        self.dup = d.value

    def _make_textures(self, fmt=DXGI_FORMAT_B8G8R8A8_UNORM):
        """Mip chain + staging texture in `fmt` (the desktop's own format -
        CopySubresourceRegion needs the two to match)."""
        # smallest mip level that still covers the sampling grid
        lvl = 0
        while ((self.sw >> (lvl + 1)) >= self.cols and (self.sh >> (lvl + 1)) >= self.rows):
            lvl += 1
        self.level = lvl
        self.mw, self.mh = max(1, self.sw >> lvl), max(1, self.sh >> lvl)
        self.fmt = None         # set last: a half-built set must never be used

        desc = D3D11_TEXTURE2D_DESC(
            Width=self.sw, Height=self.sh, MipLevels=lvl + 1, ArraySize=1,
            Format=fmt, SampleDesc=DXGI_SAMPLE_DESC(1, 0),
            Usage=D3D11_USAGE_DEFAULT,
            BindFlags=D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET,
            CPUAccessFlags=0, MiscFlags=D3D11_RESOURCE_MISC_GENERATE_MIPS)
        t = c_void_p()
        _hr(_call(self.device, _DEVICE_CREATETEXTURE2D, c_long,
                  (POINTER(D3D11_TEXTURE2D_DESC), byref(desc)), (c_void_p, None),
                  (POINTER(c_void_p), byref(t))), "CreateTexture2D(mips)")
        self.mip_tex = t.value

        v = c_void_p()
        _hr(_call(self.device, _DEVICE_CREATESRV, c_long,
                  (c_void_p, self.mip_tex), (c_void_p, None),
                  (POINTER(c_void_p), byref(v))), "CreateShaderResourceView")
        self.srv = v.value

        sdesc = D3D11_TEXTURE2D_DESC(
            Width=self.mw, Height=self.mh, MipLevels=1, ArraySize=1,
            Format=fmt, SampleDesc=DXGI_SAMPLE_DESC(1, 0),
            Usage=D3D11_USAGE_STAGING, BindFlags=0,
            CPUAccessFlags=D3D11_CPU_ACCESS_READ, MiscFlags=0)
        s = c_void_p()
        _hr(_call(self.device, _DEVICE_CREATETEXTURE2D, c_long,
                  (POINTER(D3D11_TEXTURE2D_DESC), byref(sdesc)), (c_void_p, None),
                  (POINTER(c_void_p), byref(s))), "CreateTexture2D(staging)")
        self.staging = s.value

        # Nearest-sample the mip onto the grid. Each mip texel is already the
        # average of a (2^lvl)^2 block, and the mip is less than 2x the grid
        # in each direction, so every grid cell is mostly covered.
        self._xs = [min(self.mw - 1, int((x + 0.5) * self.mw / self.cols))
                    for x in range(self.cols)]
        self._ys = [min(self.mh - 1, int((y + 0.5) * self.mh / self.rows))
                    for y in range(self.rows)]
        self.fmt = fmt

    # ------------------------------------------------------------- frame --
    def grab(self):
        if self.dup is None:
            try:
                self._duplicate()
            except OSError:
                # secure desktop (UAC / lock screen) or a mode switch in
                # progress - keep showing the last frame and try again later
                return self._last or bytes(self.cols * self.rows * 4)

        info = DXGI_OUTDUPL_FRAME_INFO()
        res = c_void_p()
        timeout = 0 if self._last is not None else 200
        hr = _call(self.dup, _DUPL_ACQUIRENEXTFRAME, c_long, (c_uint, timeout),
                   (POINTER(DXGI_OUTDUPL_FRAME_INFO), byref(info)),
                   (POINTER(c_void_p), byref(res)))
        code = hr & 0xFFFFFFFF
        if code == DXGI_ERROR_WAIT_TIMEOUT:
            # nothing new on screen: the last frame is still the right answer
            if self._last is None:
                # ...unless there has never been one. Duplication only reports
                # changes, so a desktop that is perfectly still at startup
                # yields no frame at all and the lights would sit black until
                # something moved. One GDI grab fills the gap.
                self._last = self._gdi_prime()
            return self._last or bytes(self.cols * self.rows * 4)
        if code == DXGI_ERROR_ACCESS_LOST:
            _release(self.dup)
            self.dup = None
            return self._last or bytes(self.cols * self.rows * 4)
        _hr(hr, "AcquireNextFrame")

        try:
            if info.LastPresentTime == 0 and self._last is not None:
                return self._last                # only the mouse moved
            tex = _qi(res.value, IID_ID3D11Texture2D)
            try:
                d = D3D11_TEXTURE2D_DESC()
                _call(tex, _TEX2D_GETDESC, None, (POINTER(D3D11_TEXTURE2D_DESC), byref(d)))
                if d.Format not in _BPP or d.Width != self.sw or d.Height != self.sh:
                    raise OSError(f"desktop texture is format {d.Format}, "
                                  f"{d.Width}x{d.Height} - expected "
                                  f"{self.sw}x{self.sh} BGRA8 or FP16 "
                                  f"(resolution changed?)")
                if d.Format != self.fmt or not self.mip_tex:
                    self._free_textures()
                    self._make_textures(d.Format)
                _call(self.context, _CTX_COPYSUBRESOURCEREGION, None,
                      (c_void_p, self.mip_tex), (c_uint, 0),
                      (c_uint, 0), (c_uint, 0), (c_uint, 0),
                      (c_void_p, tex), (c_uint, 0), (c_void_p, None))
            finally:
                _release(tex)
        finally:
            _release(res.value)
            _call(self.dup, _DUPL_RELEASEFRAME, c_long)

        _call(self.context, _CTX_GENERATEMIPS, None, (c_void_p, self.srv))
        _call(self.context, _CTX_COPYSUBRESOURCEREGION, None,
              (c_void_p, self.staging), (c_uint, 0),
              (c_uint, 0), (c_uint, 0), (c_uint, 0),
              (c_void_p, self.mip_tex), (c_uint, self.level), (c_void_p, None))

        m = D3D11_MAPPED_SUBRESOURCE()
        _hr(_call(self.context, _CTX_MAP, c_long, (c_void_p, self.staging), (c_uint, 0),
                  (c_uint, D3D11_MAP_READ), (c_uint, 0),
                  (POINTER(D3D11_MAPPED_SUBRESOURCE), byref(m))), "Map")
        bpp = _BPP[self.fmt]
        try:
            pitch = m.RowPitch
            rows = [ctypes.string_at(m.pData + y * pitch, self.mw * bpp) for y in self._ys]
        finally:
            _call(self.context, _CTX_UNMAP, None, (c_void_p, self.staging), (c_uint, 0))
        xs = self._xs
        if self.fmt == DXGI_FORMAT_B8G8R8A8_UNORM:
            self._last = b"".join(row[x * 4:x * 4 + 4] for row in rows for x in xs)
        else:
            self._last = self._fp16_to_bgra(rows, xs)
        return self._last

    def _gdi_prime(self):
        try:
            from screen import ScreenGrabber
            g = ScreenGrabber(self.cols, self.rows)
            try:
                return g.grab()
            finally:
                g.close()
        except Exception:
            return None

    @staticmethod
    def _fp16_to_bgra(rows, xs):
        """scRGB half floats (linear, RGBA) -> sRGB BGRA bytes, as GDI gives.

        The mips were averaged in linear light, which is what this format is.
        """
        lut, top, k = _TO_SRGB, _LUT_N - 1, (_LUT_N - 1) / SDR_WHITE
        out = bytearray()
        for row in rows:
            for x in xs:
                r, g, b, _a = struct.unpack_from("<4e", row, x * 8)
                out += bytes((lut[min(top, max(0, int(b * k + 0.5)))],
                              lut[min(top, max(0, int(g * k + 0.5)))],
                              lut[min(top, max(0, int(r * k + 0.5)))], 255))
        return bytes(out)

    def _free_textures(self):
        for name in ("staging", "srv", "mip_tex"):
            obj = getattr(self, name, None)
            if obj:
                _release(obj)
                setattr(self, name, None)
        self.fmt = None

    def close(self):
        for name in ("dup", "staging", "srv", "mip_tex", "context", "device",
                     "output1", "adapter", "factory"):
            obj = getattr(self, name, None)
            if obj:
                try:
                    _release(obj)
                except Exception:
                    pass
                setattr(self, name, None)


if __name__ == "__main__":
    import time
    g = DXGIGrabber()
    print(f"primary {g.sw}x{g.sh}, mip level {g.level} = {g.mw}x{g.mh} -> {g.cols}x{g.rows}")
    raw = g.grab()
    t, n, fresh = time.perf_counter(), 0, 0
    while time.perf_counter() - t < 3.0:
        t1 = time.perf_counter()
        raw = g.grab()
        n += 1
        time.sleep(max(0.0, 1 / 30 - (time.perf_counter() - t1)))
    i = (g.rows // 2 * g.cols + g.cols // 2) * 4
    print(f"{n} grabs in 3s; centre pixel RGB = {raw[i+2]}, {raw[i+1]}, {raw[i]}")
    g.close()
