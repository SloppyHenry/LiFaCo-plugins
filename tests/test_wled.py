import importlib.util
import json
import os
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "sdk"))

INFO = {"name": "Desk", "mac": "aabbccddeeff", "leds": {"count": 10}}
STATE = {"on": True, "bri": 128, "seg": [{"id": 0, "start": 0, "stop": 6, "n": "Left"},
                                          {"id": 1, "start": 6, "stop": 10}]}
EFFECTS = ["Solid", "Blink", "RSVD", "Rainbow"]
FXDATA = ["", "!,Duty cycle;!,!;!;01", "", "!,!;;!;01"]
posted = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._reply({"/json/info": INFO, "/json/state": STATE, "/json/eff": EFFECTS, "/json/fxdata": FXDATA}[self.path])

    def do_POST(self):
        posted.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self._reply({"success": True})


def load_plugin():
    spec = importlib.util.spec_from_file_location("wled_plugin", os.path.join(ROOT, "plugins/wled/plugin.py"))
    src = open(spec.origin).read().replace("run(Wled)", "")
    module = importlib.util.module_from_spec(spec)
    exec(compile(src, spec.origin, "exec"), module.__dict__)
    return module


class WledTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        cls.udp.bind(("127.0.0.1", 0))
        cls.udp.settimeout(2)
        cls.mod = load_plugin()
        cls.mod.UDP_PORT = cls.udp.getsockname()[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.udp.close()

    def tearDown(self):
        for p in getattr(self, 'made', []):
            p.close()

    def plugin(self, **settings):
        p = self.mod.Wled()
        p.settings = dict({"hosts": f"127.0.0.1:{self.server.server_port}", "scan": False, "timeout": 3}, **settings)
        p.setup()
        self.made = getattr(self, 'made', []) + [p]
        return p

    def test_discover(self):
        p = self.plugin()
        devices = p.discover()
        self.assertEqual(len(devices), 1)
        d = devices[0]
        self.assertEqual((d.id, d.name, d.led_count), ("aabbccddeeff", "Desk", 10))
        self.assertEqual([z.name for z in d.zones], ["Left", "Segment 1"])
        self.assertEqual([m.name for m in d.modes], ["Solid", "Blink", "Rainbow"])       # reserved slot dropped
        self.assertEqual([m.colors for m in d.modes], [3, 2, 0])

    def test_unreachable_host_is_skipped(self):
        p = self.plugin(hosts="127.0.0.1:9")
        self.assertEqual(p.discover(), [])

    def test_direct_colors_use_drgb(self):
        p = self.plugin()
        p.discover()
        self.mod.Wled.set_colors(p, "aabbccddeeff", [(1, 2, 3)] * 10)
        packet = self.udp.recv(2000)
        self.assertEqual(packet[:2], bytes([2, 3]))
        self.assertEqual(packet[2:], bytes([1, 2, 3]) * 10)

    def test_many_leds_use_dnrgb_chunks(self):
        p = self.plugin()
        p.discover()
        p.strips["aabbccddeeff"]["leds"] = 600
        p.set_colors("aabbccddeeff", [(9, 9, 9)] * 600)
        first, second = self.udp.recv(2000), self.udp.recv(2000)
        self.assertEqual(first[:4], bytes([4, 3, 0, 0]))
        self.assertEqual(len(first), 4 + 489 * 3)
        self.assertEqual(second[:4], bytes([4, 3, 489 >> 8, 489 & 255]))
        self.assertEqual(len(second), 4 + 111 * 3)

    def test_hardware_mode_posts_state(self):
        p = self.plugin()
        p.discover()
        posted.clear()
        p.set_mode("aabbccddeeff", "Rainbow", [(255, 0, 0)], 0.5, 1.0)
        body = posted[-1]
        self.assertEqual((body["on"], body["bri"], body["live"]), (True, 255, False))
        self.assertEqual([s["id"] for s in body["seg"]], [0, 1])
        self.assertEqual(body["seg"][0]["fx"], 3)
        self.assertEqual(body["seg"][0]["col"], [[255, 0, 0]])


if __name__ == "__main__":
    unittest.main()
