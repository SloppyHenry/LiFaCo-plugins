"""OpenRGB: talks to an OpenRGB server over its SDK protocol (TCP, port 6742).

The plugin asks for protocol version 4, which OpenRGB 0.9 and 1.0 both speak. It never sends "save mode":
colours and effects stay in the device's volatile memory and are gone after a power cycle. That is deliberate –
writing to a controller's flash is what damaged some mainboards in the past.
"""

import queue
import socket
import struct
import threading

from lifaco_plugin import Device, Mode, Plugin, Zone, run

MAGIC = b"ORGB"
CLIENT_PROTOCOL = 4
REQUEST_CONTROLLER_COUNT, REQUEST_CONTROLLER_DATA, REQUEST_PROTOCOL_VERSION = 0, 1, 40
SET_CLIENT_NAME, DEVICE_LIST_UPDATED = 50, 100
UPDATE_LEDS, SET_CUSTOM_MODE, UPDATE_MODE = 1050, 1100, 1101
FLAG_SPEED, FLAG_BRIGHTNESS, FLAG_PER_LED = 1 << 0, 1 << 4, 1 << 5
COLOR_MODE_PER_LED, COLOR_MODE_SPECIFIC = 1, 2
DEVICE_TYPES = {0: "mainboard", 1: "ram", 2: "gpu", 3: "cooler", 4: "strip", 5: "keyboard", 6: "mouse", 7: "other",
                8: "headset", 9: "headset", 10: "other", 11: "other", 12: "other", 13: "other", 14: "case",
                15: "other", 16: "other", 17: "other"}


class Reader:
    """Walks through the bytes of one server reply."""

    def __init__(self, data):
        self.data, self.pos = data, 0

    def unpack(self, fmt):
        values = struct.unpack_from("<" + fmt, self.data, self.pos)
        self.pos += struct.calcsize("<" + fmt)
        return values if len(values) > 1 else values[0]

    def text(self):
        length = self.unpack("H")
        raw = self.data[self.pos:self.pos + length]
        self.pos += length
        return raw.rstrip(b"\0").decode("utf-8", "replace")

    def colors(self, count):
        out = [tuple(self.data[self.pos + 4 * i:self.pos + 4 * i + 3]) for i in range(count)]
        self.pos += 4 * count
        return out


def parse_controller(data, version):
    r = Reader(data)
    r.unpack("I")                                   # total size
    c = {"type": r.unpack("I"), "name": r.text()}
    c["vendor"] = r.text() if version >= 1 else ""
    r.text()                                        # description
    r.text()                                        # version
    c["serial"], c["location"] = r.text(), r.text()
    count, c["active_mode"] = r.unpack("H"), r.unpack("i")
    c["modes"] = []
    for _ in range(count):
        m = {"name": r.text(), "value": r.unpack("i"), "flags": r.unpack("I"),
             "speed_min": r.unpack("I"), "speed_max": r.unpack("I")}
        m["bright_min"], m["bright_max"] = r.unpack("II") if version >= 3 else (0, 0)
        m["colors_min"], m["colors_max"], m["speed"] = r.unpack("I"), r.unpack("I"), r.unpack("I")
        m["brightness"] = r.unpack("I") if version >= 3 else 0
        m["direction"], m["color_mode"] = r.unpack("I"), r.unpack("I")
        m["colors"] = r.colors(r.unpack("H"))
        c["modes"].append(m)
    c["zones"] = []
    for _ in range(r.unpack("H")):
        z = {"name": r.text(), "type": r.unpack("I"), "leds_min": r.unpack("I"), "leds_max": r.unpack("I"),
             "leds": r.unpack("I")}
        matrix = r.unpack("H")
        if matrix:
            height, width = r.unpack("II")
            r.pos += 4 * height * width
        if version >= 4:
            for _s in range(r.unpack("H")):
                r.text()
                r.unpack("III")
        if version >= 5:
            r.unpack("I")
        c["zones"].append(z)
    c["led_count"] = r.unpack("H")
    return c


def build_mode(m, version):
    """A mode block as OpenRGB expects it in 'update mode'."""
    def text(s):
        raw = s.encode() + b"\0"
        return struct.pack("<H", len(raw)) + raw
    out = text(m["name"]) + struct.pack("<iIII", m["value"], m["flags"], m["speed_min"], m["speed_max"])
    if version >= 3:
        out += struct.pack("<II", m["bright_min"], m["bright_max"])
    out += struct.pack("<III", m["colors_min"], m["colors_max"], m["speed"])
    if version >= 3:
        out += struct.pack("<I", m["brightness"])
    out += struct.pack("<IIH", m["direction"], m["color_mode"], len(m["colors"]))
    for r, g, b in m["colors"]:
        out += bytes([r, g, b, 0])
    return out


class OpenRgb(Plugin):
    def setup(self):
        self.sock = None
        self.replies = queue.Queue()
        self.send_lock = threading.Lock()
        self.version = 0
        self.controllers = {}                       # our device id -> (index, controller)
        self.custom_mode = set()

    # --- protocol -----------------------------------------------------------------
    def _connect(self):
        if self.sock:
            return
        host, port = self.settings.get("host", "127.0.0.1"), int(self.settings.get("port", 6742))
        try:
            self.sock = socket.create_connection((host, port), timeout=5)
        except OSError as e:
            raise RuntimeError(f"Cannot connect to the OpenRGB server at {host}:{port} ({e}). "
                               "Start OpenRGB with the --server option.") from None
        self.sock.settimeout(None)
        threading.Thread(target=self._read_loop, args=(self.sock,), daemon=True).start()
        self._send(SET_CLIENT_NAME, b"LiFaCo\0")
        self.version = min(CLIENT_PROTOCOL, self._request(REQUEST_PROTOCOL_VERSION, struct.pack("<I", CLIENT_PROTOCOL),
                                                          expect=REQUEST_PROTOCOL_VERSION, unpack="I"))

    def _send(self, packet_id, payload=b"", device=0):
        with self.send_lock:
            self.sock.sendall(MAGIC + struct.pack("<III", device, packet_id, len(payload)) + payload)

    def _read_loop(self, sock):
        try:
            while True:
                header = self._read_exact(sock, 16)
                if header[:4] != MAGIC:
                    raise OSError("bad packet")
                device, packet_id, size = struct.unpack("<III", header[4:])
                payload = self._read_exact(sock, size)
                if packet_id == DEVICE_LIST_UPDATED:
                    self.devices_changed()
                else:
                    self.replies.put((packet_id, payload))
        except OSError:
            pass
        finally:
            if sock is self.sock:
                self.sock = None
                self.replies.put((None, b""))

    @staticmethod
    def _read_exact(sock, n):
        data = b""
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                raise OSError("connection closed")
            data += chunk
        return data

    def _request(self, packet_id, payload=b"", device=0, expect=None, unpack=None):
        while not self.replies.empty():
            self.replies.get_nowait()
        self._send(packet_id, payload, device)
        try:
            got, data = self.replies.get(timeout=10)
        except queue.Empty:
            raise RuntimeError("The OpenRGB server did not answer") from None
        if got is None:
            raise RuntimeError("The connection to the OpenRGB server was lost")
        return struct.unpack("<" + unpack, data[:4])[0] if unpack else data

    # --- plugin API ---------------------------------------------------------------
    def discover(self):
        self._connect()
        count = self._request(REQUEST_CONTROLLER_COUNT, unpack="I")
        self.controllers, seen, devices = {}, {}, []
        for index in range(count):
            data = self._request(REQUEST_CONTROLLER_DATA, struct.pack("<I", self.version), device=index)
            c = parse_controller(data, self.version)
            base = f"{c['name']}@{c['location'] or c['serial']}"
            seen[base] = seen.get(base, 0) + 1
            did = base if seen[base] == 1 else f"{base}#{seen[base]}"
            c["direct_mode"] = next((m for m in c["modes"] if m["flags"] & FLAG_PER_LED), None)
            self.controllers[did] = (index, c)
            modes = [Mode(m["name"], colors=m["colors_max"] if m["color_mode"] == COLOR_MODE_SPECIFIC else 0,
                          speed=bool(m["flags"] & FLAG_SPEED), brightness=bool(m["flags"] & FLAG_BRIGHTNESS))
                     for m in c["modes"] if m is not c["direct_mode"]]
            zones = [Zone(z["name"], z["leds"]) for z in c["zones"]]
            if not c["zones"] and c["led_count"]:
                zones = [Zone("All", c["led_count"])]
            devices.append(Device(did, c["name"], type=DEVICE_TYPES.get(c["type"], "other"), zones=zones, modes=modes,
                                  vendor=c["vendor"]))
        self.custom_mode.clear()
        return devices

    def set_colors(self, device_id, colors):
        index, c = self.controllers[device_id]
        if not c["direct_mode"]:
            raise RuntimeError("This device has no direct mode")
        self._connect()
        if device_id not in self.custom_mode:
            self._send(SET_CUSTOM_MODE, b"", device=index)
            self.custom_mode.add(device_id)
        colors = (list(colors) + [(0, 0, 0)] * c["led_count"])[:c["led_count"]]     # exactly one colour per LED
        body = struct.pack("<H", len(colors)) + b"".join(bytes([r, g, b, 0]) for r, g, b in colors)
        self._send(UPDATE_LEDS, struct.pack("<I", 4 + len(body)) + body, device=index)

    def set_mode(self, device_id, mode, colors, speed, brightness):
        index, c = self.controllers[device_id]
        base = next(m for m in c["modes"] if m["name"] == mode)
        m = dict(base)
        if m["flags"] & FLAG_SPEED:
            # speed_min is the slowest setting; on some devices it is numerically the larger number
            m["speed"] = int(m["speed_min"] + (m["speed_max"] - m["speed_min"]) * speed)
        if m["flags"] & FLAG_BRIGHTNESS:
            m["brightness"] = int(m["bright_min"] + (m["bright_max"] - m["bright_min"]) * brightness)
        if m["color_mode"] == COLOR_MODE_SPECIFIC and colors:
            m["colors"] = colors[:m["colors_max"]]
            while len(m["colors"]) < m["colors_min"]:
                m["colors"].append(m["colors"][-1])
        block = build_mode(m, self.version)
        self._connect()
        self._send(UPDATE_MODE, struct.pack("<Ii", 8 + len(block), c["modes"].index(base)) + block, device=index)
        self.custom_mode.discard(device_id)

    def close(self):
        sock, self.sock = self.sock, None
        if sock:
            sock.close()


run(OpenRgb)
