#!/usr/bin/env bash
# Copies the SDK from a LiFaCo checkout: tools/sync-sdk.sh [path to LiFaCo, default ../fancontrol-linux]
set -euo pipefail
cd "$(dirname "$0")/.."
SRC="${1:-../fancontrol-linux}/fancontrol_linux/lighting/sdk/lifaco_plugin.py"
cp "$SRC" sdk/lifaco_plugin.py
echo "SDK updated from $SRC"
