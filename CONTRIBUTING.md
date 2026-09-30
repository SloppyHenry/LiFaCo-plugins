# Contributing a plugin

1. Read the [plugin guide](docs/plugin-guide.md) and build your plugin with `lifacoctl plugin new` / `dev` / `validate`.
2. Add it as `plugins/<id>/` (only `plugin.toml`, `plugin.py`, an optional `README.md` and a `lib/` folder with
   pure-Python dependencies).
3. Open a pull request. Reviewers look at:
   * **Permissions** – as few as possible. `network` only if the plugin really needs it, `usb` only with the exact
     IDs, `i2c` only with a strong reason.
   * **Hardware safety** – no commands that write to a device's flash/EEPROM or "save" settings unless unavoidable
     and documented. Prefer a list of tested devices over probing.
   * **Honesty** – the description states what is supported and what is untested.
   * **Tests** – a test with a simulated device (see `tests/`) is welcome and often required for protocol code.
4. After the merge CI publishes it to the catalog.

Plugins in this repository are MIT licensed unless they state another open-source license in `plugin.toml`.
