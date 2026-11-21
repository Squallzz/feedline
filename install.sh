#!/bin/sh
# Feedline installer (Linux with systemd). Run from the folder holding these files:  sudo ./install.sh [PRINTER_HOST] [--with-autoadd]
# With no PRINTER_HOST it asks for the address, checks that Moonraker answers there, and only then saves it.
# Needs only python3 (3.8+). Safe to re-run: an existing config.json is never overwritten.
set -eu

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo ./install.sh)"; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || { echo "python 3.8 or newer is required"; exit 1; }

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/feedline
PRINTER=""; AUTOADD=0
for arg in "$@"; do
  case "$arg" in --with-autoadd) AUTOADD=1 ;; *) PRINTER="$arg" ;; esac
done

install -d -m 755 "$DEST" "$DEST/graphics"
install -m 644 "$SRC/feedline.html" "$DEST/index.html"
install -m 644 "$SRC/feedline_server.py" "$DEST/feedline_server.py"
[ -d "$SRC/graphics" ] && cp -r "$SRC/graphics/." "$DEST/graphics/"

# --- choose printer: ask for the printer's address, check it, never take a placeholder from the README ---
EXAMPLE_IP=192.168.1.50
have_tty() { ( : </dev/tty ) 2>/dev/null; }
ask() {                       # ask "question" -> answer in $REPLY, read from the terminal (never from a pasted script or pipe)
  printf "%s " "$1" >/dev/tty; read -r REPLY </dev/tty || REPLY=""
}
confirm() { have_tty || return 1; ask "$1 [y/N]"; case "$REPLY" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac; }
moonraker_answers() {         # does http://HOST:7125/server/info answer?
  python3 - "$1" <<'PY' 2>/dev/null
import sys, urllib.request
try:
    urllib.request.urlopen("http://%s:7125/server/info" % sys.argv[1], timeout=5).read(1)
except Exception:
    sys.exit(1)
PY
}
choose_printer() {
  while :; do
    cand="$PRINTER"; PRINTER=""
    if [ -z "$cand" ]; then
      have_tty || { echo "No printer address given and no terminal to ask on. Run:  sudo ./install.sh PRINTER_ADDRESS"; exit 1; }
      ask "Address (IP or host name) of your printer, where Moonraker runs:"; cand="$REPLY"
    fi
    if ! printf '%s' "$cand" | grep -Eq '^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$'; then
      echo "'$cand' is not a host name or IP address (no http://, no port, no spaces)."
      have_tty || exit 1; continue
    fi
    if [ "$cand" = "$EXAMPLE_IP" ] && ! confirm "$EXAMPLE_IP is only the example address from the README. Is your printer really there?"; then
      echo "Not using the example address."; have_tty || exit 1; continue
    fi
    if moonraker_answers "$cand"; then PRINTER="$cand"; return; fi
    echo "Moonraker did not answer at http://$cand:7125 (is the printer on, and is this the right address?)."
    if have_tty; then confirm "Use $cand anyway?" && { PRINTER="$cand"; return; }; continue; fi
    echo "No terminal to ask on: continuing with $cand."; PRINTER="$cand"; return
  done
}
# --- end choose printer ---

if [ ! -f "$DEST/config.json" ]; then
  choose_printer
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
if [ "$AUTOADD" -eq 1 ]; then
  install -d -m 755 "$DEST/tools"
  install -m 644 "$SRC/tools/spoolman_autoadd.py" "$DEST/tools/spoolman_autoadd.py"
  install -m 644 "$SRC/tools/feedline-autoadd.service" /etc/systemd/system/feedline-autoadd.service
  systemctl daemon-reload
  if python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("spoolman") else 1)' "$DEST/config.json"; then
    systemctl enable --now feedline-autoadd
    echo "Spoolman auto-add helper is running (journalctl -u feedline-autoadd -f)."
  else
    echo "Auto-add helper installed but NOT started: set \"spoolman\" (e.g. \"192.168.1.20:7912\") in $DEST/config.json, then: systemctl enable --now feedline-autoadd"
  fi
fi
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "Feedline is running. Open it in a browser: http://${IP:-THIS_MACHINE}:8081/"
echo "Settings live in $DEST/config.json; after editing it run: systemctl restart feedline"
echo "Without a service manager:  python3 $DEST/feedline_server.py --root $DEST --state /var/lib/feedline"
