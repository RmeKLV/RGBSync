"""Minimal OpenRGB SDK client - enumerate devices, set modes, push per-LED colours."""
import socket, struct

PKT_CONTROLLER_COUNT   = 0
PKT_CONTROLLER_DATA    = 1
PKT_PROTOCOL_VERSION   = 40
PKT_SET_CLIENT_NAME    = 50
PKT_UPDATELEDS         = 1050
PKT_SETCUSTOMMODE      = 1100
PKT_UPDATEMODE         = 1101

MODE_FLAG_HAS_PER_LED_COLOR = 0x20


class Device:
    def __init__(self, idx, name, dev_type, led_count, zones, modes, active_mode=0):
        self.index, self.name, self.type = idx, name, dev_type
        self.led_count, self.zones = led_count, zones
        self.modes = modes          # list of (name, flags, raw_blob)
        self.active_mode = active_mode

    def active_mode_name(self):
        if 0 <= self.active_mode < len(self.modes):
            return self.modes[self.active_mode][0]
        return f"?{self.active_mode}"

    def mode_index(self, name):
        for i, (n, _f, _b) in enumerate(self.modes):
            if n.lower() == name.lower():
                return i
        return None

    def direct_mode_index(self):
        """'Direct' by name, else the first mode advertising per-LED colour."""
        i = self.mode_index("Direct")
        if i is not None:
            return i
        for i, (_n, f, _b) in enumerate(self.modes):
            if f & MODE_FLAG_HAS_PER_LED_COLOR:
                return i
        return None

    def __repr__(self):
        return f"<{self.index}: {self.name} ({self.led_count} LEDs)>"


class OpenRGBClient:
    PROTO = 3

    def __init__(self, host="127.0.0.1", port=6742, name="ambient-sync"):
        self.sock = socket.create_connection((host, port), 5)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._send(0, PKT_SET_CLIENT_NAME, name.encode() + b"\x00")
        self._send(0, PKT_PROTOCOL_VERSION, struct.pack("<I", self.PROTO))
        self.proto = min(self.PROTO, struct.unpack("<I", self._recv_expect(PKT_PROTOCOL_VERSION)[2])[0])
        self.devices = self._enumerate()

    # ------------------------------------------------------------ transport --
    def _send(self, dev, pid, data=b""):
        self.sock.sendall(b"ORGB" + struct.pack("<III", dev, pid, len(data)) + data)

    def _recv_exact(self, n):
        b = b""
        while len(b) < n:
            c = self.sock.recv(n - len(b))
            if not c: raise ConnectionError("OpenRGB closed the connection")
            b += c
        return b

    def _recv(self):
        h = self._recv_exact(16)
        dev, pid, size = struct.unpack("<III", h[4:])
        return dev, pid, (self._recv_exact(size) if size else b"")

    def _recv_expect(self, want_pid, tries=8):
        """OpenRGB pushes unsolicited packets; skip anything we did not ask for."""
        for _ in range(tries):
            dev, pid, data = self._recv()
            if pid == want_pid:
                return dev, pid, data
        raise ConnectionError(f"no packet {want_pid} after {tries} tries")

    # ------------------------------------------------------------- parsing --
    def _enumerate(self):
        self._send(0, PKT_CONTROLLER_COUNT)
        count = struct.unpack("<I", self._recv_expect(PKT_CONTROLLER_COUNT)[2])[0]
        out = []
        for i in range(count):
            self._send(i, PKT_CONTROLLER_DATA, struct.pack("<I", self.proto))
            data = self._recv_expect(PKT_CONTROLLER_DATA)[2]
            o = [0]
            def u32():
                v = struct.unpack_from("<I", data, o[0])[0]; o[0] += 4; return v
            def i32():
                v = struct.unpack_from("<i", data, o[0])[0]; o[0] += 4; return v
            def u16():
                v = struct.unpack_from("<H", data, o[0])[0]; o[0] += 2; return v
            def st():
                n = u16(); v = data[o[0]:o[0]+n-1].decode("utf-8", "replace"); o[0] += n; return v

            u32(); dev_type = i32(); name = st()
            if self.proto >= 1: st()          # vendor
            st(); st(); st(); st()            # description, version, serial, location

            nmodes = u16(); active_mode = i32()
            modes = []
            for _ in range(nmodes):
                start = o[0]
                mname = st(); i32(); flags = u32(); u32(); u32()
                if self.proto >= 3: u32(); u32()
                u32(); u32(); u32()
                if self.proto >= 3: u32()
                u32(); u32()
                ncolors = u16()               # keep as its own statement:
                o[0] += ncolors * 4           # `o[0] += u16()*4` loses the read's advance
                modes.append((mname, flags, data[start:o[0]]))

            zones = []
            for _ in range(u16()):
                zn = st(); i32(); u32(); u32(); lc = u32()
                ml = u16()
                if ml: o[0] += ml * 4
                zones.append((zn, lc))
            led_count = u16()
            out.append(Device(i, name, dev_type, led_count, zones, modes, active_mode))
        return out

    def read_active_mode(self, dev):
        """Re-read one controller and return the mode it is actually in now.

        The device can be moved out of Direct by vendor software behind our
        back; UPDATELEDS keeps succeeding regardless, so this is the only way
        to see it happen.
        """
        fresh = self._enumerate()
        for d in fresh:
            if d.index == dev.index:
                dev.active_mode = d.active_mode
                return d.active_mode_name()
        return None

    # -------------------------------------------------------------- control --
    def find(self, *keywords):
        for d in self.devices:
            n = d.name.lower()
            if all(k.lower() in n for k in keywords):
                return d
        return None

    def update_mode(self, dev, index):
        """Actually push the mode to the hardware.

        SETCUSTOMMODE (1100) only flips OpenRGB's internal active_mode and never
        calls DeviceUpdateMode(), so devices that need a real mode switch (Razer's
        custom-frame effect, for one) stay dark no matter what colours you send.
        """
        blob = dev.modes[index][2]
        payload = struct.pack("<Ii", 4 + 4 + len(blob), index) + blob
        self._send(dev.index, PKT_UPDATEMODE, payload)

    def set_direct(self, dev):
        i = dev.direct_mode_index()
        if i is None:
            return None
        self.update_mode(dev, i)
        return dev.modes[i][0]

    def set_custom_mode(self, dev):
        self._send(dev.index, PKT_SETCUSTOMMODE)

    def update_leds(self, dev, colors):
        n = len(colors)
        data = struct.pack("<IH", 6 + 4 * n, n)
        data += b"".join(struct.pack("<BBBB", r, g, b, 0) for (r, g, b) in colors)
        self._send(dev.index, PKT_UPDATELEDS, data)

    def close(self):
        try: self.sock.close()
        except Exception: pass
