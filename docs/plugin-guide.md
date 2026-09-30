# Writing a LiFaCo lighting plugin

A plugin teaches LiFaCo to talk to one kind of lighting hardware: an LED strip, a keyboard, a fan hub, a mainboard.
It is a folder with two files and usually 30–100 lines of Python. You do not need to know anything about LiFaCo's
internals, GTK or the daemon.

**Five minutes to a working plugin**

```bash
lifacoctl plugin new my-strip        # creates my-strip/plugin.toml and my-strip/plugin.py
cd my-strip
lifacoctl plugin dev . --color ff8800   # runs it on its own: shows the devices and sets an orange colour
# ... edit plugin.py until it drives your hardware ...
lifacoctl plugin validate .          # checks everything
lifacoctl plugin install .           # installs it into the running LiFaCo (or use "Add a plugin from a file")
```

You need the `lifacoctl` command that comes with LiFaCo (no service is needed for `new`, `dev`, `validate`, `pack`).
Without LiFaCo you can run `python3 plugin.py --dev` with `sdk/lifaco_plugin.py` on your `PYTHONPATH`.

## The two files

### plugin.toml – what the plugin is

```toml
id = "my-strip"              # lowercase letters, digits, "-"; must equal the folder name
name = "My strip"            # shown in the plugin list
version = "0.1.0"            # major.minor.patch – raise it with every release
api = 1                      # plugin API version (this guide describes 1)
description = "LED strips on my desk"
author = "Your name"
license = "MIT"
tags = ["led strip", "usb"]  # words people can search for in the catalog

[permissions]                # everything not listed here is blocked
network = true               # use the network
usb = ["1462:7d25"]          # open these USB devices (vendor:product, see `lsusb`)
i2c = false                  # mainboard SMBus – see "Permissions" below before using this

[[settings]]                 # LiFaCo builds the settings form from these
key = "host"
label = "Address"
type = "text"                # text | password | number | switch | choice
default = "192.168.1.50"
help = "IP address of the strip"
```

Setting types: `text`, `password`, `switch` (true/false), `number` (`min`, `max`, `step`) and `choice`
(`choices = ["a", "b"]`). You read the user's values from `self.settings["host"]`.

Optional: `entry = "plugin.py"` (main file), `homepage`, `[requires] commands = ["some-program"]` (LiFaCo warns if
a program you call is missing).

### plugin.py – what the plugin does

```python
from lifaco_plugin import Device, Plugin, run

class MyStrip(Plugin):
    def setup(self):
        self.host = self.settings["host"]          # optional: open connections here

    def discover(self):                            # required
        return [Device("strip1", "Desk strip", type="strip", leds=60)]

    def set_colors(self, device_id, colors):       # optional: one (r, g, b) per LED
        send_to_hardware(self.host, colors)

    def close(self):                               # optional
        pass

run(MyStrip)                                       # always the last line
```

That is the whole contract:

| Method | Required | When LiFaCo calls it |
|---|---|---|
| `discover()` | yes | at start, on "Rescan", after `devices_changed()`. Return a list of `Device`. |
| `set_colors(device_id, colors)` | if the hardware can show a colour per LED | up to 20× per second while an effect runs. `colors` is a list of `(r, g, b)`, values 0–255, all zones in order. |
| `set_mode(device_id, mode, colors, speed, brightness)` | if the device has its own effects | when the user picks one of the device's effects. `speed` and `brightness` are 0.0–1.0. |
| `setup()` / `close()` | no | once after start / before stopping. |

With `set_colors` LiFaCo draws the effects itself (static, breathing, rainbow, colour that follows a temperature)
and streams the frames to you. With `modes` and `set_mode` the hardware runs its own effects (WLED's 100+ effects,
a keyboard's built-in waves). A plugin can offer both.

### Describing devices

```python
Device(
    id="strip1",                 # unique AND stable (serial number, MAC, address – not a list index)
    name="Desk strip",
    type="strip",                # mainboard gpu ram keyboard mouse cooler fan strip headset case other
    leds=60,                     # or zones=[Zone("Ring", 16), Zone("Logo", 1)]
    modes=[Mode("Rainbow wave", speed=True, brightness=True), Mode("Pulse", colors=1, speed=True)],
    frame_timeout=None,          # seconds after which the device forgets un-refreshed colours (see below)
)
```

* `zones` split the LEDs of one device (fan header 1, fan header 2 …). `set_colors` still receives one flat list:
  zone 1's LEDs, then zone 2's, and so on.
* `Mode(colors=n)` says how many colours the effect uses, `speed` and `brightness` say whether the sliders apply.
* `frame_timeout`: some devices (WLED) go back to their own effect if no colours arrive for a few seconds. Set the
  number of seconds and LiFaCo re-sends the current colours often enough. Leave it out for devices that keep colours.

### Helpers in the SDK

`self.log("text")` writes to LiFaCo's log (`journalctl -u fancontrol-linux`). `self.data_dir` is a folder only your
plugin can write to. `self.devices_changed()` tells LiFaCo that devices appeared or disappeared (call it from a
thread of your own, for example when a network connection reports a new device). `hex_to_rgb("ff8800")`, `clamp()`.

Do not `print()` – use `self.log()`. Exceptions are reported to the user and the plugin keeps running.

## Permissions and how plugins are isolated

Plugins run in **their own process without root privileges**, as the system user `lifaco-plugins`. They cannot read
other users' files or the LiFaCo configuration and cannot write to their own code folder. What they can reach:

| Permission | What it allows | What the user sees |
|---|---|---|
| none | pure computation, its own data folder | "No special access" |
| `network = true` | sockets (TCP/UDP) to other machines. Without it the plugin has **no network at all**. | "Use the network" |
| `usb = ["vvvv:pppp"]` | access to exactly those USB devices (hidraw and libusb nodes), granted through a udev rule | "Access the USB device …" |
| `i2c = true` | the mainboard's SMBus | a warning that careless writes can damage hardware |

The user approves the list when they switch a plugin on. If an update asks for more, the approval is asked again.

**i2c and hardware safety.** Writing to the wrong SMBus address can permanently damage RAM or mainboard controllers.
If you use `i2c`, talk only to addresses you have positively identified, never write to flash/EEPROM or "save"
commands, and prefer volatile settings. The same applies to USB controllers: some (for example certain MSI
Mystic Light boards) can be damaged by commands that store settings in flash. Send only commands that change the
current colour or effect, never "save to device" commands, and consider a list of tested product IDs.

Bundle pure-Python libraries by copying them into a `lib/` folder next to `plugin.py`; it is on the import path.
Compiled libraries are not supported.

## Files on the system

| What | Where |
|---|---|
| Installed plugins | `/var/lib/fancontrol-linux/plugins/<id>/` |
| A plugin's own data | `/var/lib/fancontrol-linux/plugin-data/<id>/` (`self.data_dir`) |
| Which plugins are on, settings, device effects | `/etc/fancontrol-linux/lighting.json` |

## Sharing your plugin

* **With yourself or friends:** `lifacoctl plugin pack my-strip` makes `my-strip-0.1.0.zip`. Install it with
  *Settings → LED devices → Add a plugin from a file* or `lifacoctl plugin install my-strip-0.1.0.zip`.
  LiFaCo tells the user that nobody has checked such a plugin.
* **In the catalog:** open a pull request that adds `plugins/<id>/` to this repository (see
  [CONTRIBUTING.md](../CONTRIBUTING.md)). After review, everybody can find it with the search in *Settings → LED
  devices*.

## The protocol underneath (for the curious)

`lifaco_plugin.py` is a thin layer over a simple protocol: LiFaCo starts `plugin.py`, sends one JSON request per
line on the plugin's standard input, and the plugin answers with one JSON line on the pipe whose file descriptor
number is in `$LIFACO_PROTO_FD` (standard output is redirected to the log so a stray `print` cannot break the
protocol). A request looks like

    {"id": 7, "method": "set_colors", "params": {"device": "strip1", "colors": [[255,0,0], [0,255,0]]}}

and the answer is `{"id": 7, "result": null}` or `{"id": 7, "error": "what went wrong"}`. The methods are `init`,
`discover`, `set_colors`, `set_mode` and `close`. A plugin may send `{"event": "devices_changed"}` at any time.
The entry point must be a Python file, but it may start programs in other languages (declare them under
`[requires] commands`).

## Checklist before you publish

* `lifacoctl plugin validate .` shows no warnings
* the `id` is unique in the catalog (`lifacoctl plugin search`)
* you ask for the fewest permissions that work
* your device ids are stable
* `set_colors` returns quickly (it is called 20× a second; slow calls make LiFaCo skip frames)
* the description says which devices are supported – and which are not
