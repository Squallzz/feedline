#!/bin/sh
# Feedline installer (Linux with systemd). Run from the folder holding these files:  sudo ./install.sh [PRINTER_HOST]
# Needs only python3 (3.8+). Safe to re-run: an existing config.json is never overwritten.
set -eu

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo ./install.sh)"; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || { echo "python 3.8 or newer is required"; exit 1; }

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/feedline

install -d -m 755 "$DEST" "$DEST/graphics"
install -m 644 "$SRC/feedline.html" "$DEST/index.html"
install -m 644 "$SRC/feedline_server.py" "$DEST/feedline_server.py"
[ -d "$SRC/graphics" ] && cp -r "$SRC/graphics/." "$DEST/graphics/"

if [ ! -f "$DEST/config.json" ]; then
  PRINTER="${1:-}"
  [ -n "$PRINTER" ] || { printf "Moonraker host or IP of your printer: "; read -r PRINTER; }
  python3 - "$SRC/config.example.json" "$DEST/config.json" "$PRINTER" <<'PY'
import json, sys
src, dst, printer = sys.argv[1:4]
cfg = json.load(open(src))
cfg["printer"] = printer
json.dump(cfg, open(dst, "w"), indent=2)
PY
  echo "Created $DEST/config.json"
fi
# the service runs as a dynamic user that no group can be assigned to, so the file must be world-readable
chmod 644 "$DEST/config.json"

install -m 644 "$SRC/feedline.service" /etc/systemd/system/feedline.service
systemctl daemon-reload
systemctl enable --now feedline
sleep 1
systemctl --no-pager --lines=3 status feedline || true
echo
echo "Feedline is running on port 8081.  Edit $DEST/config.json and run: systemctl restart feedline"
echo "Without a service manager:  python3 $DEST/feedline_server.py --root $DEST --state /var/lib/feedline"
