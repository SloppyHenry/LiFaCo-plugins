#!/usr/bin/env python3
"""Build dist/<id>-<version>.zip for every plugin and index.json for the LiFaCo plugin catalog.

The ZIPs are reproducible (fixed timestamps, sorted files), so the checksum only changes when a plugin does.
Run:  python3 tools/build_index.py        (Python 3.11+)
"""

import hashlib
import io
import json
import os
import re
import stat
import sys
import tomllib
import zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
FIXED_TIME = (2020, 1, 1, 0, 0, 0)


def build_zip(folder, pid):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(folder):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__" and not d.startswith("."))
            for name in sorted(files):
                if name.endswith((".pyc", ".pyo")) or name.startswith("."):
                    continue
                full = os.path.join(root, name)
                info = zipfile.ZipInfo(f"{pid}/{os.path.relpath(full, folder)}", FIXED_TIME)
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                with open(full, "rb") as f:
                    zf.writestr(info, f.read())
    return buf.getvalue()


def main():
    plugins, problems = [], []
    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    for pid in sorted(os.listdir(os.path.join(ROOT, "plugins"))):
        folder = os.path.join(ROOT, "plugins", pid)
        if not os.path.isdir(folder):
            continue
        try:
            with open(os.path.join(folder, "plugin.toml"), "rb") as f:
                m = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as e:
            problems.append(f"{pid}: plugin.toml: {e}")
            continue
        if m.get("id") != pid or not ID_RE.match(pid):
            problems.append(f"{pid}: 'id' must equal the folder name")
        if not VERSION_RE.match(str(m.get("version", ""))):
            problems.append(f"{pid}: 'version' must look like 1.0.0")
        for key in ("name", "description"):
            if not m.get(key):
                problems.append(f"{pid}: '{key}' is missing")
        if not os.path.isfile(os.path.join(folder, m.get("entry", "plugin.py"))):
            problems.append(f"{pid}: entry file is missing")
        if problems:
            continue
        data = build_zip(folder, pid)
        name = f"{pid}-{m['version']}.zip"
        for old in os.listdir(os.path.join(ROOT, "dist")):
            if old.startswith(pid + "-") and old.endswith(".zip") and old != name:
                os.remove(os.path.join(ROOT, "dist", old))
        with open(os.path.join(ROOT, "dist", name), "wb") as f:
            f.write(data)
        perms = m.get("permissions", {})
        plugins.append({"id": pid, "name": m["name"], "version": m["version"], "api": m.get("api", 1),
                        "description": m["description"], "author": m.get("author", ""), "tags": m.get("tags", []),
                        "homepage": m.get("homepage", ""),
                        "permissions": {"network": bool(perms.get("network")), "usb": perms.get("usb", []),
                                        "i2c": bool(perms.get("i2c"))},
                        "download": f"dist/{name}", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    with open(os.path.join(ROOT, "index.json"), "w") as f:
        json.dump({"format": 1, "plugins": plugins}, f, indent=2)
        f.write("\n")
    for p in plugins:
        print(f"{p['id']:12} {p['version']:8} {p['size']:6} bytes  {p['sha256'][:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
