#!/bin/sh
# Container entrypoint: turns FEEDLINE_* environment variables into the same command-line flags install.sh's service uses.
#   server   (default) the dashboard
#   autoadd  the Snapmaker U1 Spoolman auto-add helper
#   anything else is run as given (e.g. "sh" to look around)
# Extra arguments after "server" / "autoadd" are passed on, e.g.  docker run ... feedline server --bind 127.0.0.1
set -eu

DATA="${FEEDLINE_DATA:-/data}"
CONFIG="${FEEDLINE_CONFIG:-$DATA/config.json}"

cmd="${1:-server}"
[ $# -gt 0 ] && shift

case "$cmd" in
  server)
    if [ -z "${FEEDLINE_PRINTER:-}" ] && ! python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("printer") else 1)' "$CONFIG" 2>/dev/null; then
      echo "Feedline: no printer set. Pass -e FEEDLINE_PRINTER=<printer IP or host name>, or put \"printer\" in $CONFIG" >&2
      echo "          (an example config is at /app/config.example.json)." >&2
      exit 1
    fi
    set -- --root /app --state "$DATA" --config "$CONFIG" --port "${FEEDLINE_PORT:-8081}" "$@"
    [ -n "${FEEDLINE_PRINTER:-}" ] && set -- "$@" --printer "$FEEDLINE_PRINTER"
    [ -n "${FEEDLINE_SPOOLMAN:-}" ] && set -- "$@" --spoolman "$FEEDLINE_SPOOLMAN"
    [ -n "${FEEDLINE_PRINTERS:-}" ] && set -- "$@" --printers "$FEEDLINE_PRINTERS"
    exec python3 /app/feedline_server.py "$@"
    ;;
  autoadd)
    set -- --config "$CONFIG" --state "$DATA" "$@"
    [ -n "${FEEDLINE_PRINTER:-}" ] && set -- "$@" --printer "$FEEDLINE_PRINTER"
    [ -n "${FEEDLINE_SPOOLMAN:-}" ] && set -- "$@" --spoolman "$FEEDLINE_SPOOLMAN"
    exec python3 /app/tools/spoolman_autoadd.py "$@"
    ;;
  *)
    exec "$cmd" "$@"
    ;;
esac
