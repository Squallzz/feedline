#!/usr/bin/env python3
"""spoolman_autoadd: watch a Snapmaker U1's loaded RFID tags and add unknown ones to Spoolman.

A helper for Feedline's Spoolman view (the dashboard itself only reads Spoolman). Python 3.8+, standard library only.
Run it on any machine that can reach the printer's Moonraker (TCP 7125) and Spoolman.

For each loaded channel whose tag UID is not stored in the `card_uids` field of any Spoolman spool it:
  1. creates the vendor / filament / spool from the tag's data (UID saved in `card_uids`), and
  2. runs SET_SPOOL_ID for that channel so the printer binds the tag to the new spool (held back while printing).
It never deletes or edits existing spools. Spoolman needs the text extra field `card_uids` for spools.

  python3 spoolman_autoadd.py --config /opt/feedline/config.json --state /var/lib/feedline
  python3 spoolman_autoadd.py --printer 192.168.1.50 --spoolman http://192.168.1.20:7912
  python3 spoolman_autoadd.py ... --dry-run --once     only report what it would add (no changes anywhere)
"""
import argparse
import json
import logging
import os
import time
import urllib.parse
import urllib.request

log = logging.getLogger("autoadd")


# ---------------------------------------------------------------- printer side
def uid_hex(uid):
    return "".join("%02X" % b for b in uid)


def moonraker(host, path, post=False):
    req = urllib.request.Request("http://%s:7125/%s" % (host, path), method="POST" if post else "GET")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def read_printer(host):
    """The tags the printer currently sees, one row per channel."""
    rows = []
    for ch, c in enumerate(moonraker(host, "printer/objects/query?filament_detect")["result"]["status"]["filament_detect"]["info"]):
        if not c or not c.get("CARD_UID"):
            continue
        rows.append(dict(
            channel=ch, uid=uid_hex(c["CARD_UID"]), vendor=c.get("VENDOR", ""), brand=c.get("MANUFACTURER", ""),
            material=c.get("MAIN_TYPE", ""), sub=c.get("SUB_TYPE", ""), color="#%06X" % c.get("RGB_1", 0),
            weight=c.get("WEIGHT"), diameter=c.get("DIAMETER"), bed=c.get("BED_TEMP"),
            nozzle_lo=c.get("HOTEND_MIN_TEMP"), nozzle_hi=c.get("HOTEND_MAX_TEMP")))
    return rows


def printing(host):
    st = moonraker(host, "printer/objects/query?print_stats")["result"]["status"]["print_stats"]["state"]
    return st in ("printing", "paused")


def bind(host, channel, spool_id):
    script = "SET_SPOOL_ID CHANNEL=%d SPOOL_ID=%d" % (channel, spool_id)
    moonraker(host, "printer/gcode/script?script=" + urllib.parse.quote(script), post=True)
    log.info("ran %s", script)


# ---------------------------------------------------------------- Spoolman side
def sm_call(base, path, body=None, method=None):
    req = urllib.request.Request(
        base.rstrip("/") + "/api/v1/" + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method=method or ("GET" if body is None else "POST"))
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def spool_uids(spool):
    raw = ((spool.get("extra") or {}).get("card_uids") or "").strip().strip('"')
    return {u.strip().upper() for u in raw.split(",") if u.strip()}


def find_spool(base, uid):
    for s in sm_call(base, "spool?allow_archived=true"):
        if uid.upper() in spool_uids(s):
            return s["id"]
    return None


def add_to_spoolman(base, t):
    """Create vendor / filament / spool for tag row `t`. Returns the new spool id."""
    vname = t.get("vendor") or t.get("brand") or "Unknown"
    vendor = next((v for v in sm_call(base, "vendor") if v["name"] == vname), None) or sm_call(base, "vendor", {"name": vname})
    hexc = (t.get("color") or "#808080").lstrip("#").upper()
    weight = t.get("weight") or 1000
    material = t.get("material") or "Unknown"
    brand = t.get("brand") or vname
    fname = " ".join(x for x in (t.get("sub"), material, "-", brand, hexc, "%dg" % weight) if x)
    fil = next((f for f in sm_call(base, "filament") if f["name"] == fname and (f.get("vendor") or {}).get("id") == vendor["id"]), None)
    if fil is None:
        lo, hi = t.get("nozzle_lo"), t.get("nozzle_hi")
        body = dict(name=fname, vendor_id=vendor["id"], material=material, density=1.24,
                    diameter=(t.get("diameter") or 175) / 100.0, weight=weight, color_hex=hexc)
        if lo or hi:
            body["settings_extruder_temp"] = ((lo or hi) + (hi or lo)) // 2
        if t.get("bed"):
            body["settings_bed_temp"] = t["bed"]
        fil = sm_call(base, "filament", body)
    sp = sm_call(base, "spool", dict(filament_id=fil["id"], initial_weight=weight, remaining_weight=weight,
                                     extra={"card_uids": '"%s"' % t["uid"].upper()}))
    return sp["id"]


# ---------------------------------------------------------------- the loop
def one_pass(host, spoolman, pending, backoff, dry):
    now = time.time()
    for row in read_printer(host):
        uid = row["uid"]
        if not row.get("material") or uid in pending or backoff.get(uid, 0) > now:
            continue
        try:
            known = find_spool(spoolman, uid)
            if known is not None:
                backoff[uid] = now + 30                       # known tag; look again later in case the spool was swapped
                continue
            if dry:
                log.info("DRY RUN: tag %s (%s %s %s) is not in Spoolman; would add it", uid, row["brand"], row["material"], row["sub"])
                backoff[uid] = now + 30
                continue
            sid = add_to_spoolman(spoolman, row)
        except Exception as e:  # noqa: BLE001
            log.warning("tag %s: Spoolman error: %s (retrying in 60 s)", uid, e)
            backoff[uid] = now + 60
            continue
        log.info("tag %s (%s %s %s) added to Spoolman as spool #%d", uid, row["brand"], row["material"], row["sub"], sid)
        pending[uid] = (row["channel"], sid)
    for uid, (channel, sid) in list(pending.items()):          # bind new spools to their channels once the printer is idle
        try:
            if printing(host):
                continue
            bind(host, channel, sid)
            del pending[uid]
            backoff[uid] = now + 30
        except Exception as e:  # noqa: BLE001
            log.warning("tag %s: could not bind spool #%d to channel %d: %s", uid, sid, channel, e)


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def with_scheme(v):
    return v if "://" in v else "http://" + v


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--printer", help="Moonraker host of the U1 (default: \"printer\" in --config)")
    ap.add_argument("--spoolman", help="Spoolman address (default: the one set in Feedline's Settings, else \"spoolman\" in --config)")
    ap.add_argument("--config", help="Feedline config.json")
    ap.add_argument("--state", help="Feedline state folder (to use the Spoolman address set in Settings)")
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between checks")
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--dry-run", action="store_true", help="only report what would be added; change nothing")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = load_json(a.config) if a.config else {}
    host = a.printer or cfg.get("printer")
    sm = a.spoolman or (load_json(os.path.join(a.state, "spoolman.json")).get("url") if a.state else None) or cfg.get("spoolman")
    if not host or not sm:
        ap.error("need the printer and Spoolman: pass --printer and --spoolman, or --config (and --state)")
    sm = with_scheme(sm)
    log.info("watching %s -> %s every %.0fs%s", host, sm, a.interval, " (dry run)" if a.dry_run else "")
    pending, backoff = {}, {}
    while True:
        try:
            one_pass(host, sm, pending, backoff, a.dry_run)
        except Exception as e:  # noqa: BLE001
            log.warning("pass failed: %s", e)
        if a.once:
            return
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
