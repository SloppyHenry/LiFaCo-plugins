"""WLED: discovery and hardware effects over WLED's JSON API, live colours over UDP realtime (DRGB / DNRGB)."""

import json
import re
import select
import socket
import struct
import threading
import time
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


MDNS_GROUP, MDNS_PORT = "224.0.0.251", 5353
SERVICE = "_wled._tcp.local"


def _mdns_query(name, unicast=False):
    """A DNS question for PTR records of `name` (optionally with the "answer me directly" bit)."""
    qname = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"
    return struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + qname + struct.pack(">HH", 12, 0x8001 if unicast else 1)


def _read_name(data, pos):
    """Name at `pos` (with DNS compression); returns (name, position after it)."""
    labels, end, jumps = [], None, 0
    while True:
        length = data[pos]
        if length & 0xC0 == 0xC0:
            if end is None:
                end = pos + 2
            pos = ((length & 0x3F) << 8) | data[pos + 1]
            jumps += 1
            if jumps > 20:
                raise ValueError("name loop")
            continue
        pos += 1
        if length == 0:
            return ".".join(labels), (end if end is not None else pos)
        labels.append(data[pos:pos + length].decode("utf-8", "replace"))
        pos += length


def mdns_answers(data):
    """(type, name, rdata-position, rdata-length) of every record in an mDNS packet."""
    _id, _flags, qd, an, ns, ar = struct.unpack_from(">HHHHHH", data, 0)
    pos = 12
    for _ in range(qd):
        _name, pos = _read_name(data, pos)
        pos += 4
    out = []
    for _ in range(an + ns + ar):
        name, pos = _read_name(data, pos)
        rtype, _cls, _ttl, length = struct.unpack_from(">HHIH", data, pos)
        pos += 10
        out.append((rtype, name.lower(), pos, length))
        pos += length
    return out


def wled_address(data, sender):
    """The IPv4 address of a WLED device in an mDNS answer, or None if the packet is about something else."""
    try:
        records = mdns_answers(data)
        if not any(t == 12 and n == SERVICE for t, n, _p, _l in records):
            return None
        targets = set()
        for t, _n, p, _l in records:
            if t == 33:                                   # SRV: priority, weight, port, target
                targets.add(_read_name(data, p + 6)[0].lower())
        for t, n, p, length in records:
            if t == 1 and length == 4 and (n in targets or not targets):
                return socket.inet_ntoa(data[p:p + 4])
    except (IndexError, struct.error, ValueError):
        return None
    return sender                                          # the device answered itself, without an address record


def _group_socket():
    """A socket on the mDNS port that hears the group (shared with avahi and other programs), or None."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("", MDNS_PORT))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                        socket.inet_aton(MDNS_GROUP) + socket.inet_aton("0.0.0.0"))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        return sock
    except OSError:
        sock.close()
        return None


def scan_mdns(wait=2.5):
    """Addresses of WLED devices announced over mDNS (multicast DNS, as used by WLED and the WLED app).

    WLED (ESP8266/ESP32 mDNS) only answers ordinary queries sent from port 5353, and answers them to the group –
    so the question goes out from a socket on that port. A second socket asks with the "answer me directly" bit,
    for systems where port 5353 cannot be shared.
    """
    socks = []
    try:
        ask = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ask.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        socks.append(ask)
        group = _group_socket()
        if group:
            socks.append(group)
        hosts = []
        deadline, resend = time.monotonic() + wait, 0.0
        while (left := deadline - time.monotonic()) > 0:
            if time.monotonic() >= resend:                 # ask three times: multicast gets lost on Wi-Fi
                ask.sendto(_mdns_query(SERVICE, unicast=True), (MDNS_GROUP, MDNS_PORT))
                if group:
                    group.sendto(_mdns_query(SERVICE), (MDNS_GROUP, MDNS_PORT))
                resend = time.monotonic() + wait / 3
            ready, _w, _x = select.select(socks, [], [], min(left, max(0.05, resend - time.monotonic())))
            for s in ready:
                data, (sender, _port) = s.recvfrom(9000)
                address = wled_address(data, sender)
                if address and address not in hosts:
                    hosts.append(address)
        return hosts
    except OSError:
        return []
    finally:
        for s in socks:
            s.close()


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


RESCAN_SECONDS = 60


class Wled(Plugin):
    def setup(self):
        self.strips = {}
        self.found = set()                    # addresses the last search returned
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.timeout = int(self.settings.get("timeout", 3))
        self.closing = threading.Event()
        if self.settings.get("scan", True):
            threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self):
        """Search again now and then: devices that are switched on later appear without a manual rescan."""
        while not self.closing.wait(RESCAN_SECONDS):
            found = set(scan_mdns())
            if found - self.found:
                self.devices_changed()

    def _hosts(self):
        hosts = [h.strip().removeprefix("http://").strip("/") for h in str(self.settings.get("hosts", "")).split(",")]
        hosts = [h for h in hosts if HOST_RE.match(h)]
        if self.settings.get("scan", True):
            found = scan_mdns()
            self.found = set(found)
            hosts += [h for h in found if h not in hosts]
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
        self.closing.set()
        self.sock.close()


run(Wled)
