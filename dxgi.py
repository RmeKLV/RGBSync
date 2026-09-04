"""Screen capture via DXGI Desktop Duplication. No dependencies beyond ctypes.

This exists because the GDI path (screen.ScreenGrabber) is unusable while a game
is running. StretchBlt out of the screen DC costs ~17ms a grab on a 1080p
desktop no matter how small the destination is - the price is the readback of
the whole desktop framebuffer, not the resampling - and doing that continuously
forces DWM to keep compositing, which drops a fullscreen game off the
independent-flip path. The frame counter stays high and the game feels like it
is running at a fraction of the monitor's refresh.

Desktop Duplication is the API Windows provides for exactly this. Two things
make it cheap:

  * It is event-driven. AcquireNextFrame blocks until the desktop actually
    changes and returns DXGI_ERROR_WAIT_TIMEOUT otherwise, so a still screen
    costs no CPU at all rather than 57 full-frame readbacks a second.
  * The downscale happens on the GPU. The desktop image is copied into a
    mip-chained texture, GenerateMips does the box filter, and only the small
    mip is read back - a few KB instead of 8MB.

The grid it returns is therefore a mip level, not an arbitrary size: the
smallest power-of-two reduction that is still at least as fine as the requested
grid (1920x1080 asked for 48x27 gives 60x33). Callers read `cols`/`rows` back
off the object rather than assuming they got what they asked for.

Everything here is hand-rolled COM: `m()` binds a vtable slot by index, so the
index tables below are load-bearing. They come from d3d11.h / dxgi.h and only
change if Microsoft breaks ABI, which it cannot.
"""
import ctypes
from ctypes import POINTER, byref, c_void_p, c_uint, c_int

d3d11 = ctypes.WinDLL("d3d11")

# --------------------------------------------------------------------- COM --


class GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16),
                ("d3", ctypes.c_uint16), ("d4", ctypes.c_ubyte * 8)]

    def __init__(self, s):
        super().__init__()
        p = s.split("-")
        self.d1 = int(p[0], 16)
        self.d2 = int(p[1], 16)
        self.d3 = int(p[2], 16)
        self.d4 = (ctypes.c_ubyte * 8)(*bytes.fromhex(p[3] + p[4]))


IID_IDXGIDevice  = GUID("54ec77fa-1377-44e6-8c32-88fd5f44c84c")
IID_IDXGIOutput1 = GUID("00cddea8-939b-4b83-a340-a685226666cc")
IID_ID3D11Tex2D  = GUID("6f15aaf2-d208-4e89-9ab4-489535d34f9c")

# HRESULTs we react to rather than treat as fatal
E_WAIT_TIMEOUT  = -2005270489      # 0x887A0027 nothing changed on screen
E_ACCESS_LOST   = -2005270490      # 0x887A0026 mode switch / another duplicator
E_ACCESS_DENIED = -2005270485      # 0x887A002B secure desktop (UAC, lock screen)
E_NOT_FOUND     = -2005270526      # 0x887A0002 no such output

# vtable slots. IUnknown is 0..2 everywhere; IDXGIObject adds 3..6.
_QUERY_INTERFACE = 0
_RELEASE         = 2
_DXGIDEV_GETADAPTER   = 7
_ADAPTER_ENUMOUTPUTS  = 7
_OUTPUT_GETDESC       = 7
_OUTPUT1_DUPLICATE    = 22
_DUPL_GETDESC         = 7
_DUPL_ACQUIRENEXT     = 8
_DUPL_RELEASEFRAME    = 14
_DEV_CREATETEXTURE2D  = 5
_DEV_CREATESRV        = 7
_CTX_MAP              = 14
_CTX_UNMAP            = 15
_CTX_COPYSUBREGION    = 46
_CTX_GENERATEMIPS     = 54


def m(ptr, index, *argtypes):
    """Bind vtable slot `index` of `ptr` as an HRESULT-returning method."""
    vtbl = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0]
    return ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, *argtypes)(vtbl[index])


def mv(ptr, index, *argtypes):
    """Same, for the slots that return void."""
    vtbl = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0]
    return ctypes.WINFUNCTYPE(None, c_void_p, *argtypes)(vtbl[index])


class DXGIError(OSError):
    def __init__(self, what, hr):
        self.hr = hr
        super().__init__(f"{what} failed: 0x{hr & 0xFFFFFFFF:08X}")


def _check(hr, what):
    if hr < 0:
        raise DXGIError(what, hr)
    return hr


def _qi(ptr, iid):
    out = c_void_p()
    _check(m(ptr, _QUERY_INTERFACE, POINTER(GUID), POINTER(c_void_p))(
        ptr, byref(iid), byref(out)), "QueryInterface")
    return out


def _release(ptr):
    if ptr:
        mv(ptr, _RELEASE)(ptr)


# ----------------------------------------------------------------- structs --


class TEX2D_DESC(ctypes.Structure):
    _fields_ = [("Width", c_uint), ("Height", c_uint), ("MipLevels", c_uint),
                ("ArraySize", c_uint), ("Format", c_uint),
                ("SampleCount", c_uint), ("SampleQuality", c_uint),
                ("Usage", c_uint), ("BindFlags", c_uint),
                ("CPUAccessFlags", c_uint), ("MiscFlags", c_uint)]


class SRV_DESC(ctypes.Structure):
    _fields_ = [("Format", c_uint), ("ViewDimension", c_uint),
                ("MostDetailedMip", c_uint), ("MipLevels", c_uint),
                ("_pad", c_uint * 2)]


class FRAME_INFO(ctypes.Structure):
    _fields_ = [("LastPresentTime", ctypes.c_longlong),
                ("LastMouseUpdateTime", ctypes.c_longlong),
                ("AccumulatedFrames", c_uint), ("RectsCoalesced", c_int),
                ("ProtectedContentMaskedOut", c_int),
                ("PointerPositionX", c_int), ("PointerPositionY", c_int),
                ("PointerVisible", c_int),
                ("TotalMetadataBufferSize", c_uint),
                ("PointerShapeBufferSize", c_uint)]


class MAPPED(ctypes.Structure):
    _fields_ = [("pData", c_void_p), ("RowPitch", c_uint), ("DepthPitch", c_uint)]


class BOX(ctypes.Structure):
    _fields_ = [("left", c_uint), ("top", c_uint), ("front", c_uint),
                ("right", c_uint), ("bottom", c_uint), ("back", c_uint)]


class OUTDUPL_DESC(ctypes.Structure):
    """DXGI_OUTDUPL_DESC - ModeDesc.Width/Height first, which is all we read."""
    _fields_ = [("Width", c_uint), ("Height", c_uint),
                ("RefreshNum", c_uint), ("RefreshDen", c_uint),
                ("Format", c_uint), ("ScanlineOrdering", c_uint),
                ("Scaling", c_uint), ("Rotation", c_uint),
                ("DesktopImageInSystemMemory", c_int)]


DXGI_FORMAT_B8G8R8A8_UNORM = 87
D3D_DRIVER_TYPE_HARDWARE = 1
D3D11_SDK_VERSION = 7
BIND_SHADER_RESOURCE = 0x8
BIND_RENDER_TARGET = 0x20
MISC_GENERATE_MIPS = 0x1
USAGE_STAGING = 3
CPU_ACCESS_READ = 0x20000
SRV_DIMENSION_TEXTURE2D = 4
MAP_READ = 1

# BGRA is what the desktop is already in, so no format conversion happens
# anywhere in the chain - the mip filter and the readback both stay on the fast
# path, and the bytes land in the same order screen.Sampler already expects.


class DuplicationGrabber:
    """Primary-monitor capture, downsampled on the GPU to ~cols x rows BGRA.

    `grab()` returns None when the desktop has not changed since the last call,
    which is the normal case on a still screen and is not an error. Read the
    real grid off `cols`/`rows` after construction - see the module docstring.
    """

    def __init__(self, cols=48, rows=27, timeout_ms=100):
        self.timeout_ms = timeout_ms
        self._want = (cols, rows)
        self.dev = self.ctx = self.dupl = None
        self.out0 = self.out1 = self.adapter = self.dxgidev = None
        self.mtex = self.srv = self.stag = None
        self._acquire = self._release_frame = None   # bound by _start_duplication
        self._holding = False          # a frame is acquired and not yet released
        try:
            self._create_device()
            self._pick_output()
            self._create_textures()
            self._start_duplication()
        except Exception:
            self.close()
            raise

    # --------------------------------------------------------------- setup --
    def _create_device(self):
        dev, ctx, fl = c_void_p(), c_void_p(), c_uint()
        d3d11.D3D11CreateDevice.restype = ctypes.c_long
        _check(d3d11.D3D11CreateDevice(
            None, D3D_DRIVER_TYPE_HARDWARE, None, 0, None, 0,
            D3D11_SDK_VERSION, byref(dev), byref(fl), byref(ctx)),
            "D3D11CreateDevice")
        self.dev, self.ctx = dev, ctx
        self.dxgidev = _qi(dev, IID_IDXGIDevice)
        adapter = c_void_p()
        _check(m(self.dxgidev, _DXGIDEV_GETADAPTER, POINTER(c_void_p))(
            self.dxgidev, byref(adapter)), "GetAdapter")
        self.adapter = adapter

    def _pick_output(self):
        """The output holding the desktop origin - i.e. the primary monitor.

        EnumOutputs order is not primary-first, and on a hybrid-graphics machine
        the render adapter may own no outputs at all; that raises and the caller
        falls back to GDI.
        """
        best = None
        i = 0
        while True:
            out = c_void_p()
            hr = m(self.adapter, _ADAPTER_ENUMOUTPUTS, c_uint, POINTER(c_void_p))(
                self.adapter, i, byref(out))
            if hr == E_NOT_FOUND:
                break
            _check(hr, "EnumOutputs")
            l, t, r, b = self._output_rect(out)
            if best is None:
                best = (out, l, t, r, b)
            elif l == 0 and t == 0:
                _release(best[0])
                best = (out, l, t, r, b)
            else:
                _release(out)
            i += 1
        if best is None:
            raise DXGIError("EnumOutputs", E_NOT_FOUND)
        self.out0, l, t, r, b = best
        self.sw, self.sh = r - l, b - t
        self.out1 = _qi(self.out0, IID_IDXGIOutput1)

    @staticmethod
    def _output_rect(out):
        # DXGI_OUTPUT_DESC: WCHAR[32] name, then RECT at offset 64
        desc = (ctypes.c_byte * 96)()
        _check(m(out, _OUTPUT_GETDESC, c_void_p)(out, byref(desc)), "GetDesc")
        return tuple(ctypes.cast(ctypes.byref(desc, 64), POINTER(c_int * 4))[0])

    def _release_textures(self):
        for name in ("srv", "mtex", "stag"):
            p = getattr(self, name, None)
            if p:
                _release(p)
            setattr(self, name, None)

    def _create_textures(self):
        cols, rows = self._want
        mip = 0
        while (self.sw >> (mip + 1)) >= cols and (self.sh >> (mip + 1)) >= rows:
            mip += 1
        self.mip = mip
        self.cols = max(1, self.sw >> mip)
        self.rows = max(1, self.sh >> mip)

        create_tex = m(self.dev, _DEV_CREATETEXTURE2D,
                       POINTER(TEX2D_DESC), c_void_p, POINTER(c_void_p))

        # MipLevels 0 => full chain. GENERATE_MIPS needs both bind flags.
        d = TEX2D_DESC(self.sw, self.sh, 0, 1, DXGI_FORMAT_B8G8R8A8_UNORM,
                       1, 0, 0, BIND_SHADER_RESOURCE | BIND_RENDER_TARGET,
                       0, MISC_GENERATE_MIPS)
        mtex = c_void_p()
        _check(create_tex(self.dev, byref(d), None, byref(mtex)), "CreateTexture2D(mip)")
        self.mtex = mtex

        sd = SRV_DESC(DXGI_FORMAT_B8G8R8A8_UNORM, SRV_DIMENSION_TEXTURE2D,
                      0, 0xFFFFFFFF, (0, 0))
        srv = c_void_p()
        _check(m(self.dev, _DEV_CREATESRV, c_void_p, POINTER(SRV_DESC),
                 POINTER(c_void_p))(self.dev, mtex, byref(sd), byref(srv)),
               "CreateShaderResourceView")
        self.srv = srv

        s = TEX2D_DESC(self.cols, self.rows, 1, 1, DXGI_FORMAT_B8G8R8A8_UNORM,
                       1, 0, USAGE_STAGING, 0, CPU_ACCESS_READ, 0)
        stag = c_void_p()
        _check(create_tex(self.dev, byref(s), None, byref(stag)),
               "CreateTexture2D(staging)")
        self.stag = stag

        self.buf = ctypes.create_string_buffer(self.cols * self.rows * 4)
        self._row_bytes = self.cols * 4

        self._copy_sub = mv(self.ctx, _CTX_COPYSUBREGION, c_void_p, c_uint,
                            c_uint, c_uint, c_uint, c_void_p, c_uint, POINTER(BOX))
        self._gen_mips = mv(self.ctx, _CTX_GENERATEMIPS, c_void_p)
        self._map = m(self.ctx, _CTX_MAP, c_void_p, c_uint, c_uint, c_uint,
                      POINTER(MAPPED))
        self._unmap = mv(self.ctx, _CTX_UNMAP, c_void_p, c_uint)

    def _start_duplication(self):
        """Take the duplication, and resize to the mode it reports.

        The duplication's own desc is the authority on how big the desktop is
        now: a game taking exclusive fullscreen at a lower resolution changes
        the mode out from under us, and the textures - and the grid, since the
        mip level depends on the size - have to follow it. Callers re-read
        cols/rows afterwards.
        """
        dupl = c_void_p()
        _check(m(self.out1, _OUTPUT1_DUPLICATE, c_void_p, POINTER(c_void_p))(
            self.out1, self.dev, byref(dupl)), "DuplicateOutput")
        self.dupl = dupl
        self._holding = False
        self._acquire = m(dupl, _DUPL_ACQUIRENEXT, c_uint,
                          POINTER(FRAME_INFO), POINTER(c_void_p))
        self._release_frame = m(dupl, _DUPL_RELEASEFRAME)

        d = OUTDUPL_DESC()
        mv(dupl, _DUPL_GETDESC, POINTER(OUTDUPL_DESC))(dupl, byref(d))
        if d.Width and d.Height and (d.Width, d.Height) != (self.sw, self.sh):
            self.sw, self.sh = d.Width, d.Height
            self._release_textures()
            self._create_textures()

    def _drop_duplication(self):
        if self.dupl:
            if self._holding:
                try:
                    self._release_frame(self.dupl)
                except Exception:
                    pass
                self._holding = False
            _release(self.dupl)
        self.dupl = None
        # binding a released vtable would be a use-after-free, so clear them
        self._acquire = self._release_frame = None

    def _restart_duplication(self):
        """Re-take the duplication after ACCESS_LOST.

        Every fullscreen mode switch invalidates it - which is to say, every
        time a game starts, exits or alt-tabs. Recreating is expected, not an
        error. If DuplicateOutput itself fails (it does, transiently, while a
        mode change is in flight) the duplication stays dropped and the next
        grab() retries; that is why grab() re-takes it rather than assuming it
        is there.
        """
        self._drop_duplication()
        self._start_duplication()

    # ---------------------------------------------------------------- grab --
    def grab(self, timeout_ms=None):
        """One frame as cols*rows*4 BGRA bytes, or None if nothing has changed.

        Blocks up to timeout_ms waiting for the desktop to change, so calling
        this in a tight loop is a sleep rather than a spin.
        """
        if timeout_ms is None:
            timeout_ms = self.timeout_ms
        if self.dupl is None:            # a previous restart did not get it back
            self._start_duplication()
        fi, res = FRAME_INFO(), c_void_p()
        hr = self._acquire(self.dupl, timeout_ms, byref(fi), byref(res))
        if hr == E_WAIT_TIMEOUT:
            return None
        if hr == E_ACCESS_LOST:
            # leaves self.dupl None if it cannot be re-taken yet; the next call
            # tries again
            self._restart_duplication()
            return None
        if hr == E_ACCESS_DENIED:
            # secure desktop is up; nothing to capture until it goes away
            return None
        _check(hr, "AcquireNextFrame")
        self._holding = True
        try:
            tex = _qi(res, IID_ID3D11Tex2D)
            try:
                self._copy_sub(self.ctx, self.mtex, 0, 0, 0, 0, tex, 0, None)
            finally:
                _release(tex)
        finally:
            _release(res)
            # Hold no longer than the copy: keeping a frame checked out stalls
            # the desktop's own presents.
            self._release_frame(self.dupl)
            self._holding = False

        self._gen_mips(self.ctx, self.srv)
        self._copy_sub(self.ctx, self.stag, 0, 0, 0, 0, self.mtex, self.mip, None)

        mp = MAPPED()
        _check(self._map(self.ctx, self.stag, 0, MAP_READ, 0, byref(mp)), "Map")
        try:
            if mp.RowPitch == self._row_bytes:
                ctypes.memmove(self.buf, mp.pData, self._row_bytes * self.rows)
            else:
                for y in range(self.rows):
                    ctypes.memmove(ctypes.byref(self.buf, y * self._row_bytes),
                                   mp.pData + y * mp.RowPitch, self._row_bytes)
        finally:
            self._unmap(self.ctx, self.stag, 0)
        return self.buf.raw

    def close(self):
        try:
            self._drop_duplication()
        except Exception:
            pass
        for name in ("srv", "mtex", "stag", "dupl", "out1", "out0",
                     "adapter", "dxgidev", "ctx", "dev"):
            p = getattr(self, name, None)
            if p:
                try:
                    _release(p)
                except Exception:
                    pass
            setattr(self, name, None)
