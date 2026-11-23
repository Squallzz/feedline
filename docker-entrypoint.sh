#!/bin/sh
# Container entrypoint: turns FEEDLINE_* environment variables into the same command-line flags install.sh's service uses.
#   server   (default) the dashboard
#   autoadd  the Snapmaker U1 Spoolman auto-add helper
#   anything else is run as given (e.g. "sh" to look around)
# Extra arguments after "server" / "autoadd" are passed on, e.g.  docker run ... feedline server --bind 127.0.0.1
set -eu

DATA="${FEEDLINE_DATA:-/data}"
CONFIG="${FEEDLINE_CONFIG:-$DATA/config.json}"

# Extra certificate authorities to trust (a home or company CA, so https:// cameras, Spoolman and printers are accepted):
#   - every *.crt / *.pem / *.cer file in /certs (mount a folder there; FEEDLINE_CA_DIR picks another folder), and
#   - FEEDLINE_CA_CERTS: certificate file paths (comma-separated), or the PEM text itself.
# They are added to the system's CAs in a bundle under /tmp, which Python then uses through SSL_CERT_FILE.
# A file that holds no valid certificate stops the container, rather than leaving https connections failing later.
if bundle=$(python3 - <<'PY'
import glob, os, re, ssl, sys

say = lambda m: print("Feedline: " + m, file=sys.stderr)
ca_dir, spec = os.environ.get("FEEDLINE_CA_DIR") or "/certs", (os.environ.get("FEEDLINE_CA_CERTS") or "").strip()
pems, sources = [], []

def add(text, src):
    blocks = re.findall(r"-----BEGIN CERTIFICATE-----\s.+?-----END CERTIFICATE-----", text, re.S)
    if not blocks:
        say("no PEM certificate in %s" % src)
        sys.exit(1)
    for b in blocks:
        try:
            ssl.PEM_cert_to_DER_cert(b)
        except ValueError:
            say("a certificate in %s cannot be read" % src)
            sys.exit(1)
    pems.extend(blocks)
    sources.append("%s (%d)" % (src, len(blocks)))

files = sorted(f for ext in ("crt", "pem", "cer") for f in glob.glob(os.path.join(ca_dir, "*." + ext)))
if "BEGIN CERTIFICATE" in spec:
    add(spec.replace("\\n", "\n"), "FEEDLINE_CA_CERTS")
else:
    files += [p.strip() for p in spec.split(",") if p.strip()]
for f in files:
    try:
        with open(f, encoding="utf-8", errors="replace") as fh:
            add(fh.read(), f)
    except OSError as e:
        say("cannot read CA certificate %s: %s" % (f, e.strerror))
        sys.exit(1)
if not pems:
    sys.exit(0)                                    # nothing extra: the system CAs as they are

paths = ssl.get_default_verify_paths()
system = next((p for p in (paths.cafile, paths.openssl_cafile, "/etc/ssl/certs/ca-certificates.crt") if p and os.path.isfile(p)), None)
out = "/tmp/feedline-ca/ca-bundle.pem"
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w", encoding="utf-8") as fh:
    if system:
        with open(system, encoding="utf-8") as s:
            fh.write(s.read().rstrip("\n") + "\n")
    fh.write("\n".join(pems) + "\n")
ssl.create_default_context(cafile=out)             # the whole bundle must load
say("trusting %d extra CA certificate(s): %s" % (len(pems), ", ".join(sources)))
print(out)
PY
); then
  [ -n "$bundle" ] && export SSL_CERT_FILE="$bundle"
else
  exit 1
fi

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
