"""Screen capture via Windows Graphics Capture (the API OBS and the Windows
snipping tools use), downscaled on the GPU exactly like dxgi_capture.py.

Why it exists: Desktop Duplication (dxgi_capture.py) can cost a game its
direct display path. WGC is the newer capture pipeline and does not ask DWM to
hand a duplicated output to us the same way. Whether it is lighter for a given
game is measured, not assumed - see the README ("capture": "wgc").

Everything after "a desktop texture arrived" is inherited from DXGIGrabber
(copy into a mip chain, GPU builds the mips, read back a few KB), so Sampler,
Letterbox and the rest are unchanged. Plain ctypes COM/WinRT again.

IIDs and method order were read from Windows' own WinRT metadata. WinRT
interfaces derive from IInspectable (IUnknown + 3), so their first method is
vtable slot 6; the two classic COM interfaces here (the interop factory and
IDirect3DDxgiInterfaceAccess) derive from IUnknown, so theirs is slot 3.
A wrong slot crashes the process instead of raising - recount before editing.

Needs Windows 10 2004+ (free-threaded frame pool); the yellow capture border
is switched off where Windows 11 allows it.
"""
import ctypes, time
from ctypes import c_void_p, POINTER, byref

import dxgi_capture as dx
from dxgi_capture import (GUID, c_uint, c_long, _call, _hr, _release, _qi,
                          DXGIGrabber)

combase = ctypes.WinDLL("combase")
combase.RoInitialize.argtypes = [c_uint]
combase.RoInitialize.restype = c_long
combase.WindowsCreateString.argtypes = [ctypes.c_wchar_p, c_uint, POINTER(c_void_p)]
combase.WindowsCreateString.restype = c_long
combase.WindowsDeleteString.argtypes = [c_void_p]
combase.RoGetActivationFactory.argtypes = [c_void_p, POINTER(GUID), POINTER(c_void_p)]
combase.RoGetActivationFactory.restype = c_long
dx.d3d11.CreateDirect3D11DeviceFromDXGIDevice.argtypes = [c_void_p, POINTER(c_void_p)]
dx.d3d11.CreateDirect3D11DeviceFromDXGIDevice.restype = c_long

IID_IDXGIDevice = GUID.of("54ec77fa-1377-44e6-8c32-88fd5f44c84c")
IID_IGraphicsCaptureItemInterop = GUID.of("3628E81B-3CAC-4C60-B7F4-23CE0E0C3356")
IID_IGraphicsCaptureItem = GUID.of("79c3f95b-31f7-4ec2-a464-632ef5d30760")
IID_FramePoolStatics2 = GUID.of("589b103f-6bbc-5df5-a991-02e28b3b66d5")
IID_IGraphicsCaptureSession2 = GUID.of("2c39ae40-7d2e-5044-804e-8b6799d4cf9e")
IID_IGraphicsCaptureSession3 = GUID.of("f2cdd966-22ae-5ea1-9596-3a289344c3be")
IID_IClosable = GUID.of("30d5a829-7fa4-4026-83bb-d75bae4ea99e")
IID_DxgiInterfaceAccess = GUID.of("A9B3D012-3DF2-4EE3-B8D1-8695F457D3C1")

CLS_ITEM = "Windows.Graphics.Capture.GraphicsCaptureItem"
CLS_POOL = "Windows.Graphics.Capture.Direct3D11CaptureFramePool"

# DirectXPixelFormat.B8G8R8A8UIntNormalized == DXGI_FORMAT_B8G8R8A8_UNORM
PIXEL_FORMAT_BGRA8 = 87
POOL_BUFFERS = 2

# vtable slots
_ITEMINTEROP_CREATEFORMONITOR = 4          # IUnknown-derived: 3 = CreateForWindow
_POOLSTATICS2_CREATEFREETHREADED = 6
_POOL_TRYGETNEXTFRAME, _POOL_CREATESESSION = 7, 10
_SESSION_STARTCAPTURE = 6
_SESSION2_PUT_CURSOR, _SESSION3_PUT_BORDER = 7, 7
_FRAME_GET_SURFACE, _FRAME_GET_CONTENTSIZE = 6, 8
_CLOSABLE_CLOSE = 6
_DXGIACCESS_GETINTERFACE = 3


class SizeInt32(ctypes.Structure):
    _fields_ = [("Width", ctypes.c_int32), ("Height", ctypes.c_int32)]


def _factory(class_name, iid):
    """RoGetActivationFactory for a runtime class -> interface pointer."""
    hs = c_void_p()
    _hr(combase.WindowsCreateString(class_name, len(class_name), byref(hs)),
        "WindowsCreateString")
    try:
        out = c_void_p()
        _hr(combase.RoGetActivationFactory(hs, byref(iid), byref(out)),
            f"RoGetActivationFactory({class_name.rsplit('.', 1)[-1]})")
        return out.value
    finally:
        combase.WindowsDeleteString(hs)


class WGCGrabber(DXGIGrabber):
    """Same interface as DXGIGrabber/ScreenGrabber: grab() -> BGRA bytes."""

    backend = "wgc"

    def __init__(self, cols=48, rows=27):
        self.item = self.pool = self.session = self.d3ddev = None
        super().__init__(cols, rows)

    # DXGIGrabber.__init__ calls this where it would start Desktop Duplication.
    def _duplicate(self):
        combase.RoInitialize(1)             # MTA; S_FALSE / already-initialised is fine
        dxgi_dev = _qi(self.device, IID_IDXGIDevice)
        try:
            d3d = c_void_p()
            _hr(dx.d3d11.CreateDirect3D11DeviceFromDXGIDevice(dxgi_dev, byref(d3d)),
                "CreateDirect3D11DeviceFromDXGIDevice")
            self.d3ddev = d3d.value
        finally:
            _release(dxgi_dev)

        interop = _factory(CLS_ITEM, IID_IGraphicsCaptureItemInterop)
        try:
            item = c_void_p()
            _hr(_call(interop, _ITEMINTEROP_CREATEFORMONITOR, c_long,
                      (c_void_p, self.hmon), (POINTER(GUID), byref(IID_IGraphicsCaptureItem)),
                      (POINTER(c_void_p), byref(item))), "CreateForMonitor")
            self.item = item.value
        finally:
            _release(interop)

        statics = _factory(CLS_POOL, IID_FramePoolStatics2)
        try:
            pool = c_void_p()
            _hr(_call(statics, _POOLSTATICS2_CREATEFREETHREADED, c_long,
                      (c_void_p, self.d3ddev), (ctypes.c_int32, PIXEL_FORMAT_BGRA8),
                      (ctypes.c_int32, POOL_BUFFERS),
                      (SizeInt32, SizeInt32(self.sw, self.sh)),
                      (POINTER(c_void_p), byref(pool))), "CreateFreeThreaded")
            self.pool = pool.value
        finally:
            _release(statics)

        ses = c_void_p()
        _hr(_call(self.pool, _POOL_CREATESESSION, c_long, (c_void_p, self.item),
                  (POINTER(c_void_p), byref(ses))), "CreateCaptureSession")
        self.session = ses.value
        # Cosmetic, best effort: no cursor in the frame, no yellow border.
        # Older Windows lacks these interfaces; capture works without them.
        for iid, slot in ((IID_IGraphicsCaptureSession2, _SESSION2_PUT_CURSOR),
                          (IID_IGraphicsCaptureSession3, _SESSION3_PUT_BORDER)):
            try:
                q = _qi(self.session, iid)
            except OSError:
                continue
            try:
                _call(q, slot, c_long, (c_long, 0))
            finally:
                _release(q)
        _hr(_call(self.session, _SESSION_STARTCAPTURE, c_long), "StartCapture")

    # ------------------------------------------------------------- frame --
    def _next_frame(self):
        """Newest queued frame (older ones dropped), or None if nothing new."""
        newest = None
        for _ in range(POOL_BUFFERS + 1):
            f = c_void_p()
            _hr(_call(self.pool, _POOL_TRYGETNEXTFRAME, c_long,
                      (POINTER(c_void_p), byref(f))), "TryGetNextFrame")
            if not f.value:
                break
            if newest:
                self._close_frame(newest)
            newest = f.value
        return newest

    @staticmethod
    def _close_frame(frame):
        try:
            c = _qi(frame, IID_IClosable)
            try:
                _call(c, _CLOSABLE_CLOSE, c_long)
            finally:
                _release(c)
        finally:
            _release(frame)

    def grab(self):
        frame = self._next_frame()
        if frame is None:
            if self._last is None:
                # a still desktop produces no frames at all; see DXGIGrabber.grab
                for _ in range(10):
                    time.sleep(0.02)
                    frame = self._next_frame()
                    if frame:
                        break
                if frame is None:
                    self._last = self._gdi_prime()
                    return self._last or bytes(self.cols * self.rows * 4)
            else:
                return self._last
        try:
            size = SizeInt32()
            _hr(_call(frame, _FRAME_GET_CONTENTSIZE, c_long,
                      (POINTER(SizeInt32), byref(size))), "get_ContentSize")
            if size.Width != self.sw or size.Height != self.sh:
                raise OSError(f"capture content is {size.Width}x{size.Height}, "
                              f"expected {self.sw}x{self.sh} (resolution changed?)")
            surf = c_void_p()
            _hr(_call(frame, _FRAME_GET_SURFACE, c_long, (POINTER(c_void_p), byref(surf))),
                "get_Surface")
            try:
                acc = _qi(surf.value, IID_DxgiInterfaceAccess)
                try:
                    tex = c_void_p()
                    _hr(_call(acc, _DXGIACCESS_GETINTERFACE, c_long,
                              (POINTER(GUID), byref(dx.IID_ID3D11Texture2D)),
                              (POINTER(c_void_p), byref(tex))), "GetInterface(Texture2D)")
                    try:
                        self._copy_in(tex.value)
                    finally:
                        _release(tex.value)
                finally:
                    _release(acc)
            finally:
                _release(surf.value)
        finally:
            self._close_frame(frame)
        return self._readback()

    def close(self):
        # session and pool first, so Windows stops capturing before the device goes
        for name in ("session", "pool"):
            obj = getattr(self, name, None)
            if obj:
                try:
                    c = _qi(obj, IID_IClosable)
                    try:
                        _call(c, _CLOSABLE_CLOSE, c_long)
                    finally:
                        _release(c)
                except Exception:
                    pass
                try:
                    _release(obj)
                except Exception:
                    pass
                setattr(self, name, None)
        for name in ("item", "d3ddev"):
            obj = getattr(self, name, None)
            if obj:
                try:
                    _release(obj)
                except Exception:
                    pass
                setattr(self, name, None)
        super().close()


if __name__ == "__main__":
    g = WGCGrabber()
    print(f"primary {g.sw}x{g.sh}, mip level {g.level} = {g.mw}x{g.mh} -> {g.cols}x{g.rows}")
    t, n = time.perf_counter(), 0
    raw = g.grab()
    while time.perf_counter() - t < 3.0:
        t1 = time.perf_counter()
        raw = g.grab()
        n += 1
        time.sleep(max(0.0, 1 / 30 - (time.perf_counter() - t1)))
    i = (g.rows // 2 * g.cols + g.cols // 2) * 4
    print(f"{n} grabs in 3s; centre pixel RGB = {raw[i+2]}, {raw[i+1]}, {raw[i]}")
    g.close()
