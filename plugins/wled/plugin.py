"""WLED: discovery and hardware effects over WLED's JSON API, live colours over UDP realtime (DRGB / DNRGB)."""

import json
import re
import socket
import subprocess
import urllib.request

from lifaco_plugin import Device, Mode, Plugin, Zone, run

UDP_PORT = 21324
MAX_DRGB = 489                       # LEDs per UDP packet (keeps the datagram below the usual 1472 bytes)
HOST_RE = re.compile(r"^[A-Za-z0-9._:-]{1,120}$")


def http_json(host, path, body=None, timeout=3.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://{host}{path}", data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read(4 * 1024 * 1024))


def scan_mdns():
    """Addresses of WLED devices announced over mDNS (empty if avahi-browse is not installed)."""
    try:
        out = subprocess.run(["avahi-browse", "-rtp", "_wled._tcp"], capture_output=True, text=True, timeout=8).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    hosts = []
    for line in out.splitlines():
        parts = line.split(";")
        if parts[0] == "=" and len(parts) > 7 and parts[2] == "IPv4" and parts[7] not in hosts:
            hosts.append(parts[7])
    return hosts


def color_slots(fxdata):
    """How many colours an effect uses, from WLED's effect metadata ("sliders;colors;palette;flags").

    No metadata at all means WLED's default (three colour slots); an empty colour field means the effect
    takes no colours (it only uses a palette).
    """
    if not fxdata:
        return 3
    fields = fxdata.split(";")
    if len(fields) < 2:
        return 3
    return min(3, len([s for s in fields[1].split(",") if s.strip() and s.strip() != "-"]))


class Wled(Plugin):
    def setup(self):
        self.strips = {}
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.timeout = int(self.settings.get("timeout", 3))

    def _hosts(self):
        hosts = [h.strip().removeprefix("http://").strip("/") for h in str(self.settings.get("hosts", "")).split(",")]
        hosts = [h for h in hosts if HOST_RE.match(h)]
        if self.settings.get("scan", True):
            hosts += [h for h in scan_mdns() if h not in hosts]
        return hosts

    def discover(self):
        self.strips = {}
        devices = []
        for host in self._hosts():
            try:
                info = http_json(host, "/json/info")
                state = http_json(host, "/json/state")
                names = http_json(host, "/json/eff")
                try:
                    fxdata = http_json(host, "/json/fxdata")
                except (OSError, ValueError):
                    fxdata = []
            except (OSError, ValueError) as e:
                self.log(f"{host}: not reachable ({e})")
                continue
            total = int(info.get("leds", {}).get("count", 0))
            segments = sorted((s for s in state.get("seg", []) if s.get("stop", 0) > s.get("start", 0)),
                              key=lambda s: s["start"])
            contiguous = segments and segments[0]["start"] == 0 and all(
                a["stop"] == b["start"] for a, b in zip(segments, segments[1:])) and segments[-1]["stop"] == total
            zones = ([Zone(s.get("n") or f"Segment {s['id']}", s["stop"] - s["start"]) for s in segments]
                     if contiguous else [Zone("All", total)])
            modes = []
            for i, name in enumerate(names):
                if name in ("RSVD", "-") or name.startswith("RSVD"):
                    continue
                slots = color_slots(fxdata[i] if i < len(fxdata) else "")
                modes.append(Mode(name, colors=slots, speed=True, brightness=True))
                modes[-1].fx = i
            did = str(info.get("mac") or host)
            self.strips[did] = {"host": host, "leds": total, "fx": {m.name: m.fx for m in modes},
                                "segments": [s["id"] for s in state.get("seg", [])] or [0]}
            devices.append(Device(did, info.get("name") or "WLED", type="strip", zones=zones, modes=modes,
                                  vendor="WLED", frame_timeout=self.timeout))
        return devices

    def set_colors(self, device_id, colors):
        strip = self.strips[device_id]
        flat = bytes(c for rgb in colors for c in rgb)
        addr = (strip["host"].rsplit(":", 1)[0] if strip["host"].count(":") == 1 else strip["host"], UDP_PORT)
        if len(colors) <= MAX_DRGB + 1:
            self.sock.sendto(bytes([2, self.timeout]) + flat, addr)
            return
        for start in range(0, len(colors), MAX_DRGB):
            chunk = flat[start * 3:(start + MAX_DRGB) * 3]
            self.sock.sendto(bytes([4, self.timeout, start >> 8, start & 255]) + chunk, addr)

    def set_mode(self, device_id, mode, colors, speed, brightness):
        strip = self.strips[device_id]
        seg = {"fx": strip["fx"][mode], "sx": int(speed * 255), "ix": 128}
        if colors:
            seg["col"] = [list(c) for c in colors[:3]]
        body = {"on": True, "live": False, "bri": max(1, int(brightness * 255)),
                "seg": [dict(seg, id=i) for i in strip["segments"]]}
        http_json(strip["host"], "/json/state", body)

    def close(self):
        self.sock.close()


run(Wled)
