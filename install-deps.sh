#!/usr/bin/env bash
set -euo pipefail
sudo apt update
sudo apt install -y \
  python3-numpy python3-scipy python3-gi python3-gi-cairo \
  gir1.2-gtk-4.0 gir1.2-adw-1 python3-cairo \
  alsa-utils
