"""OpenRGB plugin against a small fake server that speaks protocol 4 (built from the SDK documentation).

This checks framing, parsing and serialisation. It cannot prove compatibility with a real OpenRGB server.
"""
import importlib.util
import os
import queue
import socket
import struct
import sys
import threading
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "sdk"))


def s(text):
    raw = text.encode() + b"\0"
    return struct.pack("<H", len(raw)) + raw


def mode(name, value, flags, smin, smax, cmin, cmax, speed, bright, direction, cmode, colors, bmin=0, bmax=0):
    out = s(name) + struct.pack("<i11I", value, flags, smin, smax, bmin, bmax, cmin, cmax, speed, bright, direction,
                                cmode)
    return out + struct.pack("<H", len(colors)) + b"".join(bytes([*c, 0]) for c in colors)


def controller():
    body = struct.pack("<I", 0) + s("ASUS Aura") + s("ASUS") + s("desc") + s("v1") + s("SER1") + s("HID: /dev/hidraw3")
    body += struct.pack("<Hi", 2, 0)
    body += mode("Direct", 0, 1 << 5, 0, 0, 0, 0, 0, 0, 0, 1, [])
    body += mode("Breathing", 1, (1 << 0) | (1 << 4), 10, 2, 1, 2, 5, 40, 0, 2, [(255, 0, 0)], 0, 100)
    body += struct.pack("<H", 1) + s("Header") + struct.pack("<IIII", 1, 0, 8, 3) + struct.pack("<H", 0)
    body += struct.pack("<H", 0)                       # protocol 4: no segments
    body += struct.pack("<H", 3) + b"".join(s(f"LED {i}") + struct.pack("<I", i) for i in range(3))
    body += struct.pack("<H", 3) + bytes(12)
    return struct.pack("<I", 4 + len(body)) + body


class FakeServer:
    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.packets = queue.Queue()
        self.conn = None
        threading.Thread(target=self.serve, daemon=True).start()

    def reply(self, packet_id, payload, device=0):
        self.conn.sendall(b"ORGB" + struct.pack("<III", device, packet_id, len(payload)) + payload)

    def serve(self):
        self.conn, _ = self.sock.accept()
        f = self.conn.makefile("rb")
        while True:
            header = f.read(16)
            if len(header) < 16:
                return
            dev, pid, size = struct.unpack("<III", header[4:])
            payload = f.read(size)
            self.packets.put((dev, pid, payload))
            if pid == 40:
                self.reply(40, struct.pack("<I", min(6, struct.unpack("<I", payload)[0])))
            elif pid == 0:
                self.reply(0, struct.pack("<I", 1))
            elif pid == 1:
                self.reply(1, controller(), dev)


def load_plugin():
    path = os.path.join(ROOT, "plugins/openrgb/plugin.py")
    src = open(path).read().replace("run(OpenRgb)", "")
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("orgb", loader=None))
    exec(compile(src, path, "exec"), mod.__dict__)
    return mod


class OpenRgbTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.mod = load_plugin()
        self.p = self.mod.OpenRgb()
        self.p.settings = {"host": "127.0.0.1", "port": self.server.port}
        self.p.setup()

    def tearDown(self):
        self.p.close()

    def sent(self, pid):
        while True:
            dev, got, payload = self.server.packets.get(timeout=2)
            if got == pid:
                return dev, payload

    def test_discover_parses_controller(self):
        devices = self.p.discover()
        self.assertEqual(self.p.version, 4)
        d = devices[0]
        self.assertEqual((d.name, d.vendor, d.type, d.led_count), ("ASUS Aura", "ASUS", "mainboard", 3))
        self.assertEqual([m.name for m in d.modes], ["Breathing"])       # "Direct" is used for per-LED colours
        self.assertEqual((d.modes[0].colors, d.modes[0].speed, d.modes[0].brightness), (2, True, True))
        self.assertEqual(d.id, "ASUS Aura@HID: /dev/hidraw3")

    def test_set_colors_switches_to_direct_once_and_pads(self):
        d = self.p.discover()[0]
        self.p.set_colors(d.id, [(1, 2, 3), (4, 5, 6)])
        self.p.set_colors(d.id, [(7, 8, 9)] * 3)
        self.assertEqual(self.sent(1100)[0], 0)
        dev, payload = self.sent(1050)
        size, count = struct.unpack("<IH", payload[:6])
        self.assertEqual((size, count), (len(payload), 3))
        self.assertEqual(payload[6:], bytes([1, 2, 3, 0, 4, 5, 6, 0, 0, 0, 0, 0]))
        self.assertTrue(self.sent(1050))
        self.assertTrue(self.server.packets.empty() or self.server.packets.get()[1] != 1100)

    def test_set_mode_serialises_full_mode_and_never_saves(self):
        d = self.p.discover()[0]
        self.p.set_mode(d.id, "Breathing", [(0, 255, 0)], 1.0, 0.5)
        dev, payload = self.sent(1101)
        size, index = struct.unpack("<Ii", payload[:8])
        self.assertEqual((size, index), (len(payload), 1))
        r = self.mod.Reader(payload[8:])
        self.assertEqual(r.text(), "Breathing")
        value, flags, smin, smax, bmin, bmax, cmin, cmax, speed, bright = r.unpack("iIIIIIIIII")
        self.assertEqual((speed, bright), (2, 50))                        # 100 % speed = the fast end, even if inverted
        r.unpack("II")
        self.assertEqual(r.colors(r.unpack("H")), [(0, 255, 0)])
        while not self.server.packets.empty():
            self.assertNotEqual(self.server.packets.get()[1], 1102)       # never "save mode"


if __name__ == "__main__":
    unittest.main()
