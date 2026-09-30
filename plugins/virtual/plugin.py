"""Virtual LED devices: the smallest useful plugin. Every colour LiFaCo sends is written to state.json."""

import json
import os

from lifaco_plugin import Device, Mode, Plugin, Zone, run


class Virtual(Plugin):
    def setup(self):
        self.count = int(self.settings.get("count", 2))
        self.state = {}

    def discover(self):
        devices = []
        for i in range(1, self.count + 1):
            if i == 2:
                # A device with zones and a hardware effect, like a keyboard or a fan hub.
                devices.append(Device("keyboard", "Virtual keyboard", type="keyboard",
                                      zones=[Zone("Keys", 87), Zone("Logo", 1)],
                                      modes=[Mode("Rainbow wave", speed=True, brightness=True),
                                             Mode("Pulse", colors=1, speed=True)]))
            else:
                devices.append(Device(f"strip{i}", f"Virtual strip {i}", type="strip", leds=30))
        return devices

    def set_colors(self, device_id, colors):
        self.state[device_id] = {"mode": None, "first": colors[0] if colors else None, "leds": len(colors)}
        self._save()

    def set_mode(self, device_id, mode, colors, speed, brightness):
        self.state[device_id] = {"mode": mode, "colors": colors, "speed": speed, "brightness": brightness}
        self._save()

    def _save(self):
        with open(os.path.join(self.data_dir, "state.json"), "w") as f:
            json.dump(self.state, f)


run(Virtual)
