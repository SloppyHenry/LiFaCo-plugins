# LiFaCo plugins

Lighting plugins for [LiFaCo](https://github.com/SloppyHenry/LiFaCo) (Linux fan control and RGB lighting).
LiFaCo's *Settings → LED devices* page searches this catalog and installs plugins from here.

| Plugin | Controls | Notes |
|---|---|---|
| [`wled`](plugins/wled) | LED strips and matrices running [WLED](https://kno.wled.ge) | JSON API for WLED's own effects, UDP realtime (DRGB/DNRGB) for LiFaCo's effects |
| [`openrgb`](plugins/openrgb) | Mainboards, GPUs, RAM, keyboards, mice, coolers, fans – via an [OpenRGB](https://openrgb.org) server | needs `openrgb --server` running with hardware access; never sends "save mode" |
| [`virtual`](plugins/virtual) | Pretend devices | for trying things out, tests and as a minimal example |

Planned: `liquidctl` (AIO coolers, fan hubs), `openrazer`, `msi-mystic-light` (safe subset, tested boards only).
Ideas and requests: open an issue.

## Write your own

A plugin is a folder with `plugin.toml` and `plugin.py`. **[Read the guide](docs/plugin-guide.md)** or start right away:

```bash
lifacoctl plugin new my-plugin
cd my-plugin && lifacoctl plugin dev .
```

## How the catalog works

* `plugins/<id>/` holds the source of each plugin.
* `python3 tools/build_index.py` builds `dist/<id>-<version>.zip` (reproducible) and `index.json` with a SHA-256 for
  each package. CI does this on every push to `main`.
* LiFaCo downloads `index.json` over HTTPS, shows a search, and verifies the checksum of every package before it
  installs it. Plugins run in their own unprivileged process and only get the permissions the user approves.

Point LiFaCo at a different catalog (your own fork or a test server) with the environment variable
`FANCONTROL_CATALOG_URL` for the service, or `"catalog_url"` in `/etc/fancontrol-linux/lighting.json`.

## Tests

```bash
python3 -m unittest discover -s tests
```

The tests use simulated devices (a fake WLED controller, a fake OpenRGB server). The OpenRGB plugin's protocol
handling is written from the OpenRGB SDK documentation and has been tested against that simulation; please report
what you see with a real server.

## SDK copy

`sdk/lifaco_plugin.py` is a copy of `fancontrol_linux/lighting/sdk/lifaco_plugin.py` in the LiFaCo repository, so
that plugin authors and the tests here do not need LiFaCo installed. Update it with `tools/sync-sdk.sh`.

## License

MIT, like LiFaCo. Each plugin states its own license in `plugin.toml`.
