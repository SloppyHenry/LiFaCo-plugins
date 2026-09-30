"""LiFaCo plugin SDK – a single file that uses only the Python standard library.

A plugin is a folder with two files: `plugin.toml` (name, version, permissions, settings) and `plugin.py`:

    from lifaco_plugin import Plugin, Device, run

    class MyPlugin(Plugin):
        def discover(self):
            return [Device("strip", "Desk strip", type="strip", leds=30)]

        def set_colors(self, device_id, colors):      # colors: one (r, g, b) per LED, values 0-255
            ...send them to the hardware...

    run(MyPlugin)

`discover()` is the only thing you must write. Add `set_colors()` if the hardware can show a colour per LED
(LiFaCo then renders its own effects for it) and/or list hardware effects in `Device(modes=[...])` and handle them in
`set_mode()`. Try it without LiFaCo:  python3 plugin.py --dev [setting=value ...] [--color ff8800]

Full guide: docs/plugin-guide.md (in the LiFaCo-plugins repository).
"""

import json
import os
import sys
import threading
import traceback

API = 1
DEVICE_TYPES = ("mainboard", "gpu", "ram", "keyboard", "mouse", "cooler", "fan", "strip", "headset", "case", "other")


class Zone:
    """A named group of LEDs on a device (for example one fan header or one ring)."""

    def __init__(self, name, leds):
        self.name = str(name)
        self.leds = int(leds)

    def to_dict(self):
        return {"name": self.name, "leds": self.leds}


class Mode:
    """A hardware effect the device can run on its own (for example "Rainbow wave").

    colors:     how many colours the effect uses (0 = none, 1 = one, 2 = two …)
    speed:      the effect has an adjustable speed
    brightness: the effect has an adjustable brightness
    """

    def __init__(self, name, colors=0, speed=False, brightness=False):
        self.name = str(name)
        self.colors = int(colors)
        self.speed = bool(speed)
        self.brightness = bool(brightness)

    def to_dict(self):
        return {"name": self.name, "colors": self.colors, "speed": self.speed, "brightness": self.brightness}


class Device:
    """One controllable LED device.

    id:            unique and stable within your plugin (use a serial number or address, not a list index)
    name:          shown to the user
    type:          one of mainboard, gpu, ram, keyboard, mouse, cooler, fan, strip, headset, case, other
    leds / zones:  give either `leds=30` (one zone) or `zones=[Zone("Ring", 16), Zone("Logo", 1)]`
    modes:         list of Mode – hardware effects, optional
    frame_timeout: seconds after which the device forgets colours that are not refreshed (LiFaCo then resends
                   them more often). Leave out if the device keeps its colours.
    """

    def __init__(self, id, name, type="other", leds=None, zones=None, modes=None, vendor="", frame_timeout=None):
        if zones is None:
            zones = [Zone("All", leds)] if leds else []
        self.id = str(id)
        self.name = str(name)
        self.type = type if type in DEVICE_TYPES else "other"
        self.zones = list(zones)
        self.modes = list(modes or [])
        self.vendor = str(vendor)
        self.frame_timeout = frame_timeout

    @property
    def led_count(self):
        return sum(z.leds for z in self.zones)

    def to_dict(self, direct):
        d = {"id": self.id, "name": self.name, "type": self.type, "vendor": self.vendor,
             "zones": [z.to_dict() for z in self.zones], "modes": [m.to_dict() for m in self.modes],
             "direct": bool(direct and self.zones)}
        if self.frame_timeout:
            d["frame_timeout"] = float(self.frame_timeout)
        return d


class Plugin:
    """Base class. Override what your hardware can do."""

    settings = {}      # the user's values for the [[settings]] in plugin.toml
    data_dir = ""      # a folder only this plugin can write to (for caches and state)

    def setup(self):
        """Called once after the settings are known. Open connections here."""

    def discover(self):
        """Return a list of Device. Called at start, after 'Rescan' and after devices_changed()."""
        raise NotImplementedError("discover() is missing")

    def set_colors(self, device_id, colors):
        """Show one (r, g, b) colour per LED (all zones in order, values 0-255). Called up to 20 times a second."""
        raise NotImplementedError

    def set_mode(self, device_id, mode, colors, speed, brightness):
        """Run a hardware effect. mode is the Mode name; colors a list of (r, g, b); speed and brightness are 0.0-1.0."""
        raise NotImplementedError

    def close(self):
        """LiFaCo is shutting the plugin down. Close connections."""

    # --- helpers you can call ---------------------------------------------------------
    def log(self, *parts):
        """Write to the LiFaCo log (journalctl -u fancontrol-linux)."""
        print(*parts, file=sys.stderr, flush=True)

    def devices_changed(self):
        """Tell LiFaCo that devices appeared or disappeared; it calls discover() again."""
        _send({"event": "devices_changed"})


# --- helpers for colours ---------------------------------------------------------------

def hex_to_rgb(text):
    """'ff8800' or '#ff8800' -> (255, 136, 0)"""
    text = text.lstrip("#")
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))


def clamp(v, lo=0, hi=255):
    return max(lo, min(hi, int(v)))


# --- protocol (you normally never touch this) -------------------------------------------

_out = None
_out_lock = threading.Lock()


def _send(message):
    """Thread-safe: devices_changed() may be called from a thread of your own."""
    global _out
    with _out_lock:
        if _out is None:
            fd = int(os.environ.get("LIFACO_PROTO_FD", "1"))
            _out = os.fdopen(os.dup(fd), "w", buffering=1, encoding="utf-8")
        _out.write(json.dumps(message, separators=(",", ":")) + "\n")
        _out.flush()


def _rgb_list(colors):
    return [(clamp(c[0]), clamp(c[1]), clamp(c[2])) for c in colors]


def _handle(plugin, method, params):
    if method == "init":
        plugin.settings = params.get("settings") or {}
        plugin.data_dir = params.get("data_dir", "")
        plugin.setup()
        return None
    if method == "discover":
        direct = type(plugin).set_colors is not Plugin.set_colors
        return {"devices": [d.to_dict(direct) for d in plugin.discover()]}
    if method == "set_colors":
        plugin.set_colors(params["device"], _rgb_list(params["colors"]))
        return None
    if method == "set_mode":
        plugin.set_mode(params["device"], params["mode"], _rgb_list(params.get("colors") or []),
                        float(params.get("speed", 0.5)), float(params.get("brightness", 1.0)))
        return None
    if method == "close":
        plugin.close()
        return None
    raise ValueError(f"Unknown method: {method}")


def _serve(plugin):
    for line in sys.stdin:
        try:
            request = json.loads(line)
            rid, method = request.get("id"), request["method"]
        except (ValueError, KeyError, AttributeError):
            continue
        try:
            reply = {"id": rid, "result": _handle(plugin, method, request.get("params") or {})}
        except NotImplementedError:
            reply = {"id": rid, "error": f"The plugin does not support '{method}'"}
        except Exception as e:  # noqa: BLE001 – reported to LiFaCo, the plugin keeps running
            if os.environ.get("LIFACO_PLUGIN_DEBUG"):
                traceback.print_exc(file=sys.stderr)     # full details only when the service runs with --verbose
            else:
                print(f"{type(e).__name__}: {e}", file=sys.stderr, flush=True)
            reply = {"id": rid, "error": f"{type(e).__name__}: {e}"}
        _send(reply)
        if method == "close":
            break


def _dev(plugin):
    """python3 plugin.py --dev [key=value ...] [--color ff8800]: run the plugin on its own and show what it finds."""
    args = sys.argv[2:]
    color = None
    settings = {}
    if "--color" in args:
        i = args.index("--color")
        color = hex_to_rgb(args[i + 1])
        del args[i:i + 2]
    for a in args:
        key, _, value = a.partition("=")
        settings[key] = value
    try:
        import tomllib
        with open("plugin.toml", "rb") as f:
            for s in tomllib.load(f).get("settings", []):
                settings.setdefault(s["key"], s.get("default", ""))
    except (ImportError, OSError, ValueError):
        pass
    plugin.settings = settings
    plugin.data_dir = os.environ.get("TMPDIR", "/tmp")
    plugin.setup()
    devices = plugin.discover()
    direct = type(plugin).set_colors is not Plugin.set_colors
    if not devices:
        print("No devices found.")
    for d in devices:
        print(f"{d.name}  [{d.type}, id={d.id}, {d.led_count} LEDs, "
              f"{'direct colours' if direct and d.zones else 'no direct colours'}, {len(d.modes)} modes]")
        if color and direct and d.zones:
            plugin.set_colors(d.id, [color] * d.led_count)
            print(f"  -> set all LEDs to {color}")
    plugin.close()


def run(plugin_class):
    """Start the plugin. Call this as the last line of plugin.py."""
    plugin = plugin_class()
    if sys.argv[1:2] == ["--dev"]:
        _dev(plugin)
    else:
        _serve(plugin)
