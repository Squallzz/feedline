#!/usr/bin/env python3
"""Feedline: serve the printer dashboard and stand between the browser and the printers, so a browser only ever
talks to this one host (no CORS, no extra firewall rules) and can only do what is listed here.

Pages and data (GET)
  /                              the page (index.html); /graphics/..., /packs/... its pictures
  /api/config                    what the page needs to know about the selected printer (name, model, graphics, features)
  /api/printers, /api/overview   the configured printers; one status row for each of them
  /api/macros                    the printer's macros and the tool-button macros
  /api/screen/probe              does the printer serve a remote touchscreen?
  /printer/objects/query|list, /server/gcode_store, /server/files/metadata, /server/history/..., /server/spoolman/status
                                 forwarded to the printer's Moonraker unchanged (read-only)
  /server/files/gcodes/.../.thumbs/x.png   slicer thumbnails
  /cam/snapshot, /screen/snapshot          one camera / touchscreen frame
  /api/v1/spool                  Spoolman's spool list
  /config/list, /config/read     the printer's config folder; /logs/export its log files

Commands (POST /control with the X-Feedline-Control header and a JSON body {"action": ...}; see ROUTES and do_POST)
  pause, resume, cancel, estop                        print controls, each only in the print states where it makes sense
  load, unload, park, pickup, run_macro               tool buttons and macros: idle only, one at a time per printer
  home, jog, goto, motors_off, set_lane               idle only
  temp, fan, light, tune                              any time; every value is range-checked
  save_config                                         edit a config file (backed up here first; can be switched off)
  save_settings, save_pack, delete_pack, add_printer, remove_printer, set_embed, set_spoolman      this dashboard's own settings

Which printer a request is for: the X-Printer header, else ?p=<id>, else the "printer" cookie, else the main one.

Nothing else is forwarded: no free-form G-code and no other Moonraker endpoints. Commands are built here from fixed
templates and checked values (see run_gcode); text from a browser never reaches the printer as G-code.
"""
import argparse
import http.server
import json
import logging
import os
import re
import socket
import ipaddress
import base64
import io
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile


# ---------------------------------------------------------------------------------------------------------------------
# Configuration. config.json (written by the admin, never by the web UI) says which printer this is and which macros
# the dashboard may run. settings.json (written by the UI) only holds cosmetic choices: the name and the graphics.
# ---------------------------------------------------------------------------------------------------------------------
DEFAULT_CFG = {
    "printer": None,                 # Moonraker host (required, here or with --printer)
    "spoolman": None,                # Spoolman address the server uses: "host:port" or a URL ("http://192.168.1.20:7912"); null = no Spoolman. The Settings tab can override it
    "name": None,                    # header text; the UI setting overrides it; default = the printer's hostname
    "provider": "auto",              # how filament slots are found: "auto" | "u1" (Snapmaker RFID) | "afc" (ArmoredTurtle AFC / BoxTurtle) | "qidibox" (QIDI Box) | "toolchanger" (klipper-toolchanger: StealthChanger, ktc-easy) | "extruders"
    "tools": None,                   # number of tools; null = count the extruders Klipper reports
    "macros": None,                  # tool buttons, e.g. {"load": "AUTO_FEEDING EXTRUDER={n} LOAD=1", "pickup": "T{n}"}; "" disables one.
                                     # {n} = tool number, {nz} = number or "" for tool 0. Actions not listed are auto-detected
    "auto_macros": True,             # find load/unload/park/pickup macros by name when not configured
    "light": None,                   # {"object": "led cavity_led", "on": "<gcode>", "off": "<gcode>"}; null = find an LED / light pin by name; false = no light button
    "screen": None,                  # {"snapshot": "/screen/snapshot", "interactive": "/screen/"} (printers with a touchscreen page)
    "webcam": None,                  # {"mode": "webrtc|mjpeg|snapshot", "live": "/url", "snapshot": "/url"}; null = ask Moonraker
    "tool_fans": None,               # [{"hotend": "heater_fan e0_nozzle_fan", "part": "fan"}, ...] per tool, for the overlays
    "chamber": None,                 # {"sensor": "temperature_sensor cavity", "fan": "fan_generic cavity_fan"}
    "tool_spools": None,             # {"0": 12, "1": 7}: fixed Spoolman spool per tool (provider "extruders")
    "temp_limits": {"extruder": 300, "heater_bed": 110, "heater_generic": 150},
    "z_offset_limit": 1.0,           # print tuning: baby-stepping never takes the Z offset further than this from 0 (mm)
    "graphics": {"printer": None, "toolchanger": None},
    "toolhead": None,                # look of the toolhead on a drawn printer: one of TOOLHEADS; null = generic (Settings can change it)
    "docks": None,                   # toolchanger: number of docks drawn (at least one per tool, up to 12); null = one per tool
    "motion": True,                  # drawn printer models: the toolhead and bed follow the printer's real position (Settings can turn it off)
    "allowed_hosts": [],             # extra Host names the dashboard answers to (IP addresses, single-label and .local/.lan/.ts.net names always work)
    "allow_embed": False,            # true = the page may be shown inside a frame on another site (e.g. a Home Assistant dashboard card)
    "mesh_bed_temp": 60,             # Calibrate bed mesh heats a cold bed to this (C) first and switches it off after; 0 = do not heat
    "allow_config_edit": True,      # false = the Files tab is read-only (config files can then not be changed from the browser)
    "printers": None,                # more printers besides "printer": ["host", {"name": "Voron", "host": "voron.lan", "provider": "auto"}];
                                     # shown like ones added in Settings > Printers, but cannot be removed there
}
HOST_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal", ".localdomain", ".ts.net")
# Drawn printer models (the page draws them; see MODELS in index.html). Original, simplified drawings in each model's
# shape and colors; the toolhead and bed follow the printer's real position. Only models that run Klipper + Moonraker.
MODELS = [
    ("qidi-plus4", "QIDI Plus4"), ("qidi-xmax3", "QIDI X-Max 3"), ("qidi-xplus3", "QIDI X-Plus 3"), ("qidi-q1pro", "QIDI Q1 Pro"),
    ("qidi-q2", "QIDI Q2"), ("qidi-xsmart3", "QIDI X-Smart 3"),
    ("creality-k1", "Creality K1 / K1C"), ("creality-k1max", "Creality K1 Max"), ("creality-k2plus", "Creality K2 Plus"),
    ("creality-ender3", "Creality Ender-3 (bed slinger)"),
    ("voron-24", "Voron 2.4"), ("voron-trident", "Voron Trident"), ("voron-v0", "Voron 0"), ("voron-switchwire", "Voron Switchwire"),
    ("ratrig-vcore", "RatRig V-Core"), ("sovol-sv08", "Sovol SV08"), ("sovol-sv06", "Sovol SV06 / SV07"),
    ("elegoo-neptune4", "Elegoo Neptune 4"), ("anycubic-kobra-s1", "Anycubic Kobra S1"), ("anycubic-kobra", "Anycubic Kobra 2 / 3"),
    ("flashforge-ad5m", "Flashforge Adventurer 5M"), ("prusa-mk", "Prusa MK3 / MK4 (Klipper)"), ("flsun-delta", "FLSUN delta (V400 / T1 / S1)"),
    ("corexy-box", "Enclosed CoreXY (any brand)"), ("corexy-frame", "Open-frame CoreXY (any brand)"),
    ("bedslinger", "Bed slinger (any brand)"), ("delta", "Delta (any brand)"),
]
MODEL_LABEL = dict(MODELS)


def guess_model(cfg, objs, info):
    """Which drawn model fits this printer: (id, reason), from what Klipper itself reports. Klipper has no "model" field,
    so this goes by the kinematics, the build volume, a few vendor-specific config sections and paths, and the host name.
    The brand hints are best effort (the user can always pick another graphic in Settings); the family is reliable."""
    def num(sec, key):
        try:
            return float((cfg.get(sec) or {}).get(key))
        except (TypeError, ValueError):
            return 0.0
    kin = str((cfg.get("printer") or {}).get("kinematics") or "").lower()
    x = num("stepper_x", "position_max")
    secs = {k.split(" ")[0].lower() for k in cfg}
    text = " ".join(str(info.get(k) or "") for k in ("hostname", "klipper_path", "config_file")).lower()
    names = " ".join(objs).lower()
    hint = lambda *words: any(w in text for w in words)
    size = "%.0f mm bed" % x if x else "unknown bed size"
    if "filament_detect" in objs:
        return "u1", "Snapmaker RFID filament detection"
    if "delta" in kin:
        return ("flsun-delta", "delta kinematics, FLSUN paths") if hint("flsun") else ("delta", "delta kinematics")
    if "corexy" in kin:
        if hint("qidi") or ("/home/mks/" in text and (secs & {"chamber_fan", "box_stepper", "multi_color_controller", "hall_filament_width_sensor"} or "heater_generic chamber" in names)):
            mid = "qidi-xsmart3" if x < 200 else "qidi-q1pro" if x < 258 else "qidi-q2" if x < 276 else "qidi-xplus3" if x < 292 else "qidi-plus4" if x < 316 else "qidi-xmax3"
            return mid, "CoreXY on a QIDI (MKS) board, " + size
        if hint("/usr/share/klipper", "/usr/data/", "creality") or secs & {"prtouch_v2", "prtouch_v3"}:
            return ("creality-k1" if x < 260 else "creality-k1max" if x < 330 else "creality-k2plus"), "CoreXY with Creality firmware, " + size
        if hint("sovol"):
            return ("sovol-sv08", "CoreXY with Sovol paths") if "quad_gantry_level" in secs else ("corexy-box", "CoreXY with Sovol paths")
        if hint("ratos", "ratrig", "vcore", "v-core") or "ratos" in names:
            return "ratrig-vcore", "CoreXY running RatOS"
        if hint("flashforge", "adventurer", "ad5m"):
            return "flashforge-ad5m", "CoreXY with Flashforge paths"
        if hint("/userdata/", "rinkhals", "anycubic", "kobra"):
            return "anycubic-kobra-s1", "CoreXY with Anycubic paths"
        if "quad_gantry_level" in secs:
            return "voron-24", "CoreXY with a flying gantry (quad_gantry_level), " + size
        if 0 < x <= 130:
            return "voron-v0", "small CoreXY, " + size
        if "stepper_z2" in cfg and "z_tilt" in secs:
            return "voron-trident", "CoreXY with three Z motors (z_tilt), " + size
        if re.search(r"chamber|enclosure", names):
            return "corexy-box", "CoreXY with a chamber sensor or heater"
        return "corexy-frame", "CoreXY"
    if "corexz" in kin:
        return ("creality-ender3", "CoreXZ with Creality firmware") if hint("/usr/share/klipper", "/usr/data/", "creality", "ender") else ("voron-switchwire", "CoreXZ")
    if kin:                                                    # cartesian and friends: the bed moves in Y
        if hint("neptune", "elegoo") or "/home/mks/" in text:
            return "elegoo-neptune4", "bed slinger on an Elegoo (MKS) board"
        if hint("/usr/share/klipper", "/usr/data/", "creality", "ender", "sonic"):
            return "creality-ender3", "bed slinger with Creality firmware"
        if hint("sovol"):
            return "sovol-sv06", "bed slinger with Sovol paths"
        if hint("/userdata/", "rinkhals", "anycubic", "kobra"):
            return "anycubic-kobra", "bed slinger with Anycubic paths"
        if hint("prusa", "mk3", "mk4"):
            return "prusa-mk", "bed slinger named like a Prusa"
        return "bedslinger", kin + " kinematics"
    return None, "the printer's configuration could not be read"


# toolhead looks the page can draw (HEADS in index.html); the names follow StealthChanger's list of supported toolheads
TOOLHEADS = ("generic", "stealthburner", "ministealthburner", "dragonburner", "rapidburner", "anthead", "yavoth", "a4t", "xol", "jabberwocky", "blackbird")


def thumb_path(filename, thumbs):
    """Path under /server/files/gcodes/ of a job's largest slicer thumbnail, or None. Only relative PNG paths are used
    (some firmware also lists one with an absolute path on the printer's disk)."""
    ok = [t for t in thumbs or [] if isinstance(t, dict) and isinstance(t.get("relative_path"), str) and t["relative_path"].endswith(".png")
          and not t["relative_path"].startswith(("/", "\\")) and ".." not in t["relative_path"].replace("\\", "/").split("/")]
    if not ok:
        return None
    best = max(ok, key=lambda t: t.get("width") or 0)
    folder = filename.rsplit("/", 1)[0] + "/" if "/" in filename else ""
    return "/server/files/gcodes/" + "/".join(urllib.parse.quote(seg, safe="") for seg in (folder + best["relative_path"]).split("/"))


BUILTIN_PACKS = {
    "printer": [{"id": "u1", "label": "Snapmaker U1 (drawn, live)"}] + [{"id": i, "label": l + " (drawn, live)"} for i, l in MODELS] + [{"id": "generic", "label": "Generic printer (image)"}],
    "toolchanger": [{"id": "u1", "label": "Spool wheel"}, {"id": "reel", "label": "Simple reel"}, {"id": "afc", "label": "3D spool"}, {"id": "none", "label": "No graphic"}],
}
THUMB_SEG = r"[\w .()\[\]+@,&'!~=\-]+"      # one folder / file name in a thumbnail path: the characters slicers put in file names, never / \ ? # %
PACK_ID = re.compile(r"[a-z0-9][a-z0-9_\-]{0,39}")
MIME_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/svg+xml": ".svg", "image/gif": ".gif"}
EXT_MIME = {v: k for k, v in MIME_EXT.items()}
MAX_PACK_IMAGE = 3 * 1024 * 1024
ACTIONS = ("load", "unload", "park", "pickup")
PWA_MANIFEST = {
    "name": "Feedline", "short_name": "Feedline", "description": "Klipper printer dashboard",
    "start_url": "/", "scope": "/", "display": "standalone", "background_color": "#0A0814", "theme_color": "#0A0814",
    "icons": [{"src": "/graphics/feedline-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
              {"src": "/graphics/feedline-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}]}
# Service worker: Chrome only offers "Install app" when one exists. It never caches anything (printer data must be live); when the
# server cannot be reached it shows a short offline page instead of the browser's error page.
PWA_SW = """self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  if (e.request.mode !== 'navigate') return;
  e.respondWith(fetch(e.request).catch(() => new Response(
    '<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><body style="background:#0A0814;color:#DEDCFA;font:16px system-ui;padding:2em;text-align:center"><h2>Feedline is not reachable</h2><p>Check that you are on the home network (or VPN) and that the server is running.</p><p><a style="color:#9580FF" href="">Try again</a></p>',
    {status: 503, headers: {'Content-Type': 'text/html; charset=utf-8'}})));
});
"""
CMD_DENY = re.compile(r"^(FIRMWARE_)?RESTART$|^SAVE_CONFIG$|^M112$|^SHUTDOWN|^EMERGENCY|^FIRMWARE|^FLASH", re.I)
TOOL_ARGS = ("TOOL", "T", "EXTRUDER", "N", "INDEX", "TOOLHEAD", "TOOL_NUMBER")
RECT_ANCHORS = ("bed", "chamber", "screen")
POINT_ANCHORS = ("chamberTemp", "partFan", "chamberFan", "light")


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


PRINTER_HOST = re.compile(r"[a-z0-9]([a-z0-9.\-]{0,251}[a-z0-9])?")


def printer_id(name_or_host, taken):
    """A printer id (PACK_ID shape) from its name or host, not clashing with the ids in `taken`."""
    base = re.sub(r"[^a-z0-9]+", "-", str(name_or_host).lower()).strip("-")[:30] or "printer"
    pid, n = base, 2
    while pid in taken:
        pid, n = "%s-%d" % (base, n), n + 1
    return pid


def parse_printers(spec, main_host=None):
    """Printers the admin set besides the main one: config.json "printers" (a list of hosts or {"name", "host", "provider"})
    or --printers / FEEDLINE_PRINTERS ("host, Name=host, ..."). Returns [{"id", "name", "host", "provider", "fixed": True}].
    Bad entries, repeats and the main printer itself are skipped with a log line; nothing is looked up on the network."""
    if isinstance(spec, str):
        items = []
        for part in spec.split(","):
            part = part.strip()
            if part:
                name, _, host = part.rpartition("=")
                items.append({"name": name.strip(), "host": host})
    else:
        items = [{"host": x} if isinstance(x, str) else x for x in (spec or [])]
    out, taken, hosts = [], {"main"}, {str(main_host or "").lower()}
    for it in items:
        if not isinstance(it, dict):
            logging.warning("printers: ignoring %r (expected a host or {\"host\": ...})", it)
            continue
        host = str(it.get("host") or "").strip().lower()
        if not PRINTER_HOST.fullmatch(host):
            logging.warning("printers: ignoring %r (not a host name or IP address: no http://, no port)", it.get("host"))
            continue
        if host in hosts:
            continue
        name = str(it.get("name") or "").strip()[:60] or host
        provider = str(it.get("provider") or "auto")
        pid = str(it.get("id") or "")
        if not PACK_ID.fullmatch(pid) or pid in taken:          # ?p=<id>: from the name, else the host's first label (whole IP)
            short = host if re.fullmatch(r"[0-9.]+", host) else host.split(".")[0]
            pid = printer_id(it.get("name") or short, taken)
        taken.add(pid)
        hosts.add(host)
        out.append({"id": pid, "name": name, "host": host, "provider": provider, "fixed": True})
    return out


def color_hex(c):
    """A filament color as "#RRGGBB": from "#rrggbb", "rrggbbaa" or a 24-bit number; None when there is none."""
    if isinstance(c, bool):
        return None
    if isinstance(c, int):
        return "#%06X" % (c & 0xFFFFFF)
    m = re.match(r"#?([0-9a-fA-F]{6})", str(c or "").strip())
    return "#" + m.group(1).upper() if m else None


def ex_name(i):
    return "extruder" if i == 0 else "extruder%d" % i


def filament_objects(provider, objs, tools=1):
    """The Klipper objects filament_slots needs for this filament system (asked for together on the Printers page)."""
    if provider == "u1":
        return ["toolhead", "filament_detect", "filament_feed left", "filament_feed right"]
    if provider == "afc":
        return ["AFC"] + [o for o in objs if re.match(r"AFC_(stepper|lane) \S", o)]
    if provider == "qidibox":
        return ["multi_color_controller", "save_variables", "box_extras", "filament_switch_sensor fila"]
    if provider == "toolchanger":
        return ["toolhead", "toolchanger"]
    return ["toolhead"] if tools > 1 else []


def filament_slots(provider, st, names=(), tools=1, table=None):
    """What each filament slot holds, compactly, for one card on the Printers page:
    [{"label", "title", "color", "loaded", "active"}] (title and color only when known). The same rules as the Home page,
    without Spoolman (which the Home page adds per spool). `names`: AFC lanes or QIDI Box slots; `table`: box_table()."""
    st, out = st or {}, []
    if provider == "qidibox":
        M = st.get("multi_color_controller") or {}
        V = (st.get("save_variables") or {}).get("variables") or {}
        sl = M.get("slots") or {}
        states, mats, T = sl.get("states") or {}, sl.get("materials") or {}, table or {}

        def state(n):
            v = states.get(n)
            v = V.get(n) if v is None else v
            try:
                return int(v or 0)
            except (TypeError, ValueError):
                return 0

        def material(n):
            if isinstance(mats.get(n), dict):
                return mats[n]
            if not T:
                return {}
            return {"filament": {"filament": (T.get("fila") or {}).get(V.get("filament_" + n))},
                    "color": (T.get("color") or {}).get(V.get("color_" + n)), "vendor": (T.get("vendor") or {}).get(V.get("vendor_" + n))}

        box_loaded = any(state(n) == 2 for n in names if n != "slot16")
        ex, be, sw = M.get("extruder") or {}, st.get("box_extras") or {}, st.get("filament_switch_sensor fila") or {}
        at_head = (ex["filament_detected"] if isinstance(ex.get("filament_detected"), bool)
                   else be["e_endstop_state"] == 1 if isinstance(be.get("e_endstop_state"), (int, float)) else bool(sw.get("filament_detected")))
        for n in names:
            ext, num = n == "slot16", int(re.sub(r"\D", "", n) or 0)
            m = material(n)
            f = m.get("filament") if isinstance(m.get("filament"), dict) else {}
            s = (2 if at_head and not box_loaded else 1 if f.get("filament") or f.get("type") else 0) if ext else state(n)
            if ext and not s:
                continue                                      # an external spool nobody set up says nothing
            out.append({"label": "Ext" if ext else "%d%s" % (num // 4 + 1, "ABCD"[num % 4]), "loaded": s > 0, "active": s == 2,
                        "title": str(f.get("filament") or f.get("type") or "") if s else "", "color": color_hex(m.get("color")) if s else None})
    elif provider == "afc":
        cur = (st.get("AFC") or {}).get("current_load")
        for n in names:
            L = st.get("AFC_stepper " + n) or st.get("AFC_lane " + n) or {}
            loaded = bool(L.get("prep") or L.get("load") or L.get("tool_loaded"))
            out.append({"label": n, "loaded": loaded, "active": bool(L.get("tool_loaded")) or cur == n,
                        "title": str(L.get("filament_name") or L.get("material") or "") if loaded else "", "color": color_hex(L.get("color")) if loaded else None})
    elif provider == "u1":
        info, cur = (st.get("filament_detect") or {}).get("info") or [], (st.get("toolhead") or {}).get("extruder")
        for i in range(tools):
            d = info[i] if i < len(info) and isinstance(info[i], dict) else {}
            ch = (st.get("filament_feed " + ("left" if i < 2 else "right")) or {}).get("extruder%d" % i)
            loaded = bool(ch.get("filament_detected")) if isinstance(ch, dict) else bool(d.get("MAIN_TYPE")) and d.get("MAIN_TYPE") != "NONE"
            typ = " ".join(str(d.get(k) or "") for k in ("MAIN_TYPE", "SUB_TYPE")).strip() if d.get("MAIN_TYPE") not in (None, "", "NONE") else ""
            out.append({"label": "T%d" % i, "loaded": loaded, "active": cur == ex_name(i), "title": typ if loaded else "",
                        "color": color_hex(d.get("RGB_1")) if loaded and d.get("RGB_1") is not None else None})
    elif provider == "toolchanger":
        n = (st.get("toolchanger") or {}).get("tool_number")
        for i in range(tools):
            out.append({"label": "T%d" % i, "loaded": True, "active": n == i, "title": "", "color": None})
    elif tools > 1:
        cur = (st.get("toolhead") or {}).get("extruder")
        for i in range(tools):
            out.append({"label": "T%d" % i, "loaded": True, "active": cur == ex_name(i), "title": "", "color": None})
    return out


def write_json_atomic(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
    os.replace(tmp, path)


class Handler(http.server.BaseHTTPRequestHandler):
    printer = spoolman = spoolman_cfg = root = None
    fixed_printers = []             # set by the admin (config.json "printers", --printers): see parse_printers
    state = "/var/lib/feedline"
    cfg = dict(DEFAULT_CFG)
    _cache = {}

    timeout = 30                    # a client that stops sending cannot hold a thread forever

    def log_message(self, *a):
        pass

    def reply(self, code, ctype, body, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if not self.embed_allowed():
            self.send_header("X-Frame-Options", "DENY")                 # no clickjacking of the control buttons
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def embed_allowed():
        """May other pages (e.g. a Home Assistant card) show the dashboard in a frame? Settings choice first, then config.json."""
        st = load_json(os.path.join(Handler.state, "server.json"), {})
        return bool(st["allow_embed"]) if "allow_embed" in st else bool(Handler.cfg.get("allow_embed"))

    def set_embed(self, req):
        v = req.get("allow")
        if v is not None and not isinstance(v, bool):
            return self.jreply(400, {"error": "bad value"})
        st = load_json(os.path.join(self.state, "server.json"), {})
        if v is None:
            st.pop("allow_embed", None)
        else:
            st["allow_embed"] = v
        os.makedirs(self.state, exist_ok=True)
        write_json_atomic(os.path.join(self.state, "server.json"), st)
        self.jreply(200, {"ok": True, "config": self.public_config()})

    def blocked(self):
        """True (after answering) when the request must not be served: unknown Host name (DNS rebinding)."""
        host = (self.headers.get("Host") or "").strip().lower()
        name = host[1:host.index("]")] if host.startswith("[") and "]" in host else host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        ok = bool(name) and ("." not in name or name.endswith(HOST_SUFFIXES) or name in [str(h).lower() for h in Handler.cfg.get("allowed_hosts") or []])
        if not ok:
            try:
                ipaddress.ip_address(name)
                ok = True
            except ValueError:
                pass
        if not ok:
            self.reply(421, "text/plain", b"unknown host name: add it to allowed_hosts in config.json")
            return True
        return False

    def jreply(self, code, obj):
        self.reply(code, "application/json", json.dumps(obj).encode())

    # ---- small helpers on the printer ----
    _down = {}                      # printer id -> time until which its Moonraker is treated as unreachable
    DOWN_FOR = 10                   # seconds

    def cached(self, key, ttl, fn):
        key = (self.pid, key)
        hit = Handler._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        if Handler._down.get(self.pid, 0) > time.time():
            # Moonraker did not answer a moment ago (printer off or unplugged): use what is known instead of waiting
            # for one timeout after another. Nothing is stored, so it is asked again as soon as the pause is over.
            return hit[1] if hit else None
        try:
            val = fn()
        except Exception as e:  # noqa: BLE001
            if isinstance(e, OSError) and not isinstance(e, urllib.error.HTTPError):      # no connection / timeout; an HTTP error is an answer
                Handler._down[self.pid] = time.time() + self.DOWN_FOR
            if not hit:
                return None                                   # nothing known yet: ask again next time, not after the full ttl
            val = hit[1]
        Handler._cache[key] = (time.time(), val)
        return val

    def mget(self, path, timeout=8):
        with urllib.request.urlopen("http://%s:7125%s" % (self.printer, path), timeout=timeout) as r:
            return json.load(r)

    def run_gcode(self, script, timeout=15):
        """Send one G-code script (one or more lines) to the printer and wait until Moonraker says it has finished.
        Callers build `script` from fixed commands and checked values only; this never sees text typed in a browser."""
        logging.info("running %s", script.replace("\n", " ; "))
        url = "http://%s:7125/printer/gcode/script?script=%s" % (self.printer, urllib.parse.quote(script))
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method="POST"), timeout=timeout) as r:
                r.read()
        except urllib.error.HTTPError as e:                   # Klipper refused or the macro failed: pass on what it said, not just "400 Bad Request"
            raise RuntimeError(self.moonraker_error(e)) from None

    @staticmethod
    def moonraker_error(e):
        """The message inside Moonraker's error reply ({"error": {"message": ...}}), or the plain HTTP error."""
        try:
            msg = json.loads(e.read(65536) or b"{}").get("error", {}).get("message")
        except Exception:  # noqa: BLE001
            msg = None
        return str(msg).strip()[:400] if msg else str(e)

    # ---- several printers: "main" comes from config.json; config.json "printers" / --printers add fixed ones; ----
    # ---- the others are added in the UI and kept in printers.json                                             ----
    pid = "main"

    def ui_printers(self):
        """Printers added in Settings > Printers (printers.json), the only ones the UI may add or remove."""
        out = load_json(os.path.join(self.state, "printers.json"), {}).get("printers", [])
        return [p for p in out if isinstance(p, dict) and PACK_ID.fullmatch(str(p.get("id", ""))) and p.get("host")]

    def extra_printers(self):
        """Every printer besides the main one: the admin's fixed ones first, then the UI's. A UI entry for a printer the
        admin has since fixed in config is hidden (the fixed one, with the same id when it was derived the same way, wins)."""
        fixed = list(Handler.fixed_printers)
        ids, hosts = {p["id"] for p in fixed}, {p["host"] for p in fixed} | {str(Handler.printer or "").lower()}
        return fixed + [p for p in self.ui_printers() if p["id"] not in ids and str(p["host"]).lower() not in hosts]

    def select_printer(self):
        want = self.headers.get("X-Printer") or ""
        if not want:                                          # ?p=<id>: for pictures, downloads and the page itself, which cannot send the header
            want = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("p", [""])[0]
        if not want:
            m = re.search(r"(?:^|;\s*)printer=([^;]*)", self.headers.get("Cookie", ""))
            want = urllib.parse.unquote(m.group(1)) if m else ""
        self.pid, self.printer, self.cfg = "main", Handler.printer, Handler.cfg
        for p in self.extra_printers():
            if p["id"] == want:
                c = dict(DEFAULT_CFG)
                c.update({"printer": p["host"], "name": p.get("name"), "provider": p.get("provider") or "auto", "spoolman": Handler.cfg.get("spoolman"), "spoolman_url": Handler.cfg.get("spoolman_url")})
                self.pid, self.printer, self.cfg = p["id"], p["host"], c
                break

    # ---- macros: detected from Klipper, customizable (only commands Klipper really has, never free-form G-code) ----
    def commands(self):
        got = self.cached("cmds", 60, lambda: {k.upper() for k in self.mget("/printer/gcode/help")["result"]})
        return got or set()

    def klipper_settings(self):
        """Klipper's parsed configuration (every section), or {} while the printer cannot be asked."""
        return self.cached("kcfg", 300, lambda: self.mget("/printer/objects/query?configfile=settings", 20)["result"]["status"]["configfile"]["settings"]) or {}

    def model(self):
        """(id, reason) of the drawn printer model that fits this printer; (None, reason) when it cannot be told."""
        def find():
            cfg = self.klipper_settings()
            if not cfg:
                raise ValueError("no configuration")          # not stored as an answer: asked again next time
            return guess_model(cfg, self.objects(), self.mget("/printer/info")["result"])
        return self.cached("model", 300, find) or (None, "the printer did not answer")

    def macro_info(self):
        def load():
            cfg = self.klipper_settings()
            if not cfg:
                raise ValueError("no configuration")
            out = {}
            for sec, v in cfg.items():
                if not sec.startswith("gcode_macro "):
                    continue
                params = []
                for m in re.finditer(r"params(?:\.|\[\s*['\"])([A-Za-z_]\w*)(?:['\"]\s*\])?(?:\s*\|\s*default\(([^)]*)\))?", str(v.get("gcode", ""))):
                    if m.group(1).upper() not in [p["name"] for p in params]:
                        params.append({"name": m.group(1).upper(), "default": (m.group(2) or "").strip("'\" ") or None})
                out[sec.split(" ", 1)[1].upper()] = {"description": v.get("description") or "", "params": params}
            return out
        return self.cached("minfo", 300, load) or {}

    def user_macros(self):
        info = self.macro_info()
        out = []
        for o in self.objects():
            if o.startswith("gcode_macro "):
                name = o.split(" ", 1)[1]
                if not name.startswith("_"):
                    i = info.get(name.upper(), {})
                    out.append({"name": name, "description": i.get("description", ""), "params": i.get("params", []),
                                "blocked": bool(CMD_DENY.match(name))})
        return sorted(out, key=lambda m: m["name"].upper())

    def template_ok(self, base):
        """True when every tool's expansion of a macro name (T{n}, PARK_EXTRUDER{nz}) is a command Klipper has."""
        cmds = self.commands()
        for i in range(self.slot_count()):
            nm = base.replace("{nz}", str(i) if i else "").replace("{n}", str(i)).upper()
            if nm not in cmds or CMD_DENY.match(nm):
                return False
        return True

    def base_ok(self, base):
        known = {d.split(" ", 1)[0] for d in self.detect_macros().values()}
        return base in known or self.template_ok(base)

    def tool_param(self, macro):
        ps = [p["name"] for p in (self.macro_info().get(macro.upper()) or {}).get("params", [])]
        for c in TOOL_ARGS:
            if c in ps:
                return c
        return None

    def detect_macros(self):
        if not self.cfg.get("auto_macros", True):
            return {}
        cmds, info, n, out = self.commands(), self.macro_info(), self.tool_count(), {}
        has = lambda name: name in cmds
        if self.provider() == "afc":                          # one card per lane: {lane} is the lane's name
            for act, c in (("load", "TOOL_LOAD LANE={lane}"), ("unload", "LANE_UNLOAD LANE={lane}"), ("park", "TOOL_UNLOAD LANE={lane}"), ("pickup", "CHANGE_TOOL LANE={lane}")):
                if has(c.split(" ")[0]):
                    out[act] = c
            return out
        if self.provider() == "qidibox":
            # One card per Box slot; {n} is the slot number. These are the commands QIDI's own Fluidd "Control Box" card
            # sends for Load and Unload: the Box firmware then does the whole sequence (heat, cut, retract, purge), not just
            # the bare feed that the T<n> / UNLOAD_T<n> macros do. The firmware registers them without a description, so
            # they are missing from Klipper's command list and cannot be checked for here; a printer that lacks them
            # answers "Unknown command", which the page shows.
            # "park" is the Eject button: E_BOX (Fluidd's "Uninstall") pulls a waiting filament back out of its slot.
            return {"load": "E_LOAD slot={n}", "unload": "E_UNLOAD slot={n}", "park": "E_BOX slot={n}"}
        if has("AUTO_FEEDING") and "filament_detect" in self.objects():      # Snapmaker U1: tools and parking are native commands, not macros
            return {"load": "AUTO_FEEDING EXTRUDER={n} LOAD=1", "unload": "AUTO_FEEDING EXTRUDER={n} UNLOAD=1", "pickup": "T{n}", "park": "PARK_EXTRUDER{nz}"}
        def with_tool(name, extra=""):
            p = self.tool_param(name) if n > 1 else None
            return name + (" %s={n}" % p if p else "") + extra
        tc = self.provider() == "toolchanger"
        if tc:                                                # klipper-toolchanger: T<n> exists for every numbered tool; UNSELECT_TOOL docks the one in use
            if self.template_ok("T{n}"):
                out["pickup"] = "T{n}"
            elif has("SELECT_TOOL"):
                out["pickup"] = "SELECT_TOOL T={n}"
            if has("UNSELECT_TOOL"):
                out["park"] = "UNSELECT_TOOL"
        # pick up a tool
        if tc:
            pass
        elif n > 1 and (self.template_ok("T{n}") or ("T%d" % n) in cmds):
            out["pickup"] = "T{n}"
        else:
            for c in ("SELECT_TOOL", "TOOL_PICKUP", "PICKUP_TOOL", "TOOL_SELECT", "SELECT_EXTRUDER"):
                if has(c) and self.tool_param(c):
                    out["pickup"] = with_tool(c)
                    break
            else:
                if n > 1:
                    out["pickup"] = "ACTIVATE_EXTRUDER EXTRUDER=extruder{nz}"
        # park a tool
        if tc:
            pass
        elif n > 1 and self.template_ok("PARK_EXTRUDER{nz}"):
            out["park"] = "PARK_EXTRUDER{nz}"
        else:
            for c in (("TOOL_DROPOFF", "PARK_TOOL", "DROPOFF_TOOL", "PARK_EXTRUDER") if n > 1 else ("PARK", "PARK_HEAD", "PARK_TOOLHEAD")):
                if has(c):
                    out["park"] = with_tool(c)
                    break
        # load / unload
        ps = {p["name"] for p in (info.get("AUTO_FEEDING") or {}).get("params", [])}
        if has("AUTO_FEEDING") and {"EXTRUDER", "LOAD", "UNLOAD"} <= ps:
            out["load"], out["unload"] = "AUTO_FEEDING EXTRUDER={n} LOAD=1", "AUTO_FEEDING EXTRUDER={n} UNLOAD=1"
        else:
            for act, names in (("load", ("LOAD_FILAMENT", "FILAMENT_LOAD", "M701", "LOAD_FILAMENT_MACRO")), ("unload", ("UNLOAD_FILAMENT", "FILAMENT_UNLOAD", "M702", "UNLOAD_FILAMENT_MACRO"))):
                for c in names:
                    if has(c):
                        out[act] = with_tool(c)
                        break
        return out

    def effective_macros(self):
        det, cfgm, ui = None, self.cfg.get("macros") or {}, self.settings().get("macros") or {}
        out = {}
        for a in ACTIONS:
            if a in ui:
                if ui[a]:
                    out[a] = {"template": ui[a], "source": "custom"}
            elif a in cfgm:
                if cfgm[a]:
                    out[a] = {"template": cfgm[a], "source": "config"}
            else:
                det = self.detect_macros() if det is None else det
                if det.get(a):
                    out[a] = {"template": det[a], "source": "detected"}
        return out

    def clean_macro(self, tmpl):
        tmpl = " ".join(str(tmpl or "").split())
        if not tmpl:
            return ""
        m = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*(?:\{nz\}|\{n\})?)((?: [A-Za-z_][A-Za-z0-9_]*=[\w.\-+/:{}]*)*)", tmpl)
        if not m or not self.base_ok(m.group(1)):
            raise ValueError("%r is not a command this printer has (check the name; arguments must look like NAME=value)" % tmpl)
        return tmpl

    def macro_command(self, req):
        name = str(req.get("name") or "")
        macro = next((m for m in self.user_macros() if m["name"] == name), None)
        if not macro or macro["blocked"]:
            raise ValueError("unknown or blocked macro")
        params = req.get("params") or {}
        if not isinstance(params, dict) or len(params) > 12:
            raise ValueError("bad parameters")
        parts = [name]
        for k, v in params.items():
            v = "" if v is None else str(v).strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(k)) or not re.fullmatch(r"[\w.\-+/:]{0,64}", v):
                raise ValueError("parameter %s: use letters, digits and . - + / : only" % k)
            if v != "":
                parts.append("%s=%s" % (k, v))
        return " ".join(parts)

    def settings_path(self):
        return os.path.join(self.state, "settings.json" if self.pid == "main" else "settings-%s.json" % self.pid)

    def printer_list(self):
        known = (Handler._cache.get(("main", "pinfo")) or (0, None))[1] or {}      # Klipper's own host name, once the main printer has been asked
        main = {"id": "main", "name": Handler.cfg.get("name") or known.get("hostname") or Handler.printer, "host": Handler.printer, "main": True}
        st = load_json(os.path.join(self.state, "settings.json"), {})
        if st.get("name"):
            main["name"] = st["name"]
        out = [main]
        for p in self.extra_printers():
            n = load_json(os.path.join(self.state, "settings-%s.json" % p["id"]), {}).get("name") or p.get("name") or p["host"]
            out.append({"id": p["id"], "name": n, "host": p["host"], "main": False, "fixed": bool(p.get("fixed"))})
        for p in out:
            p["active"] = p["id"] == self.pid
        return out

    _ovmeta = {}                    # printer id -> (file name, its slicer metadata): asked once per job

    def overview(self):
        """One row of status per printer for the Printers page. All printers are asked side by side with a short timeout,
        and one that just failed to answer is not asked again for a few seconds, so a printer that is off does not hold
        up the page. Read-only: nothing here sends a command."""
        rows = self.printer_list()
        cfgs = {"main": Handler.cfg}
        for p in self.extra_printers():
            cfgs[p["id"]] = dict(DEFAULT_CFG, printer=p["host"], name=p.get("name"), provider=p.get("provider") or "auto")

        def ask(row):
            h = object.__new__(Handler)                       # the same helpers, pointed at that printer
            h.pid, h.printer, h.cfg, h.headers = row["id"], row["host"], cfgs.get(row["id"], Handler.cfg), self.headers
            known = (Handler._cache.get((h.pid, "model")) or (0, None))[1]
            row.update(online=False, model=None)
            try:
                if Handler._down.get(h.pid, 0) <= time.time():
                    st = h.mget("/printer/objects/query?print_stats&virtual_sdcard&extruder&heater_bed&webhooks", 4)["result"]["status"]
                    known = h.model()
                else:
                    st = None
            except Exception as e:  # noqa: BLE001
                st = None
                if isinstance(e, OSError) and not isinstance(e, urllib.error.HTTPError):
                    Handler._down[h.pid] = time.time() + self.DOWN_FOR
            if known and known[0]:
                row["model"] = {"id": known[0], "label": "Snapmaker U1" if known[0] == "u1" else MODEL_LABEL.get(known[0])}
            if st is None:
                return
            ps, vs, info = st.get("print_stats") or {}, st.get("virtual_sdcard") or {}, (st.get("print_stats") or {}).get("info") or {}
            pair = lambda o: [o.get("temperature"), o.get("target")] if isinstance(o, dict) and "temperature" in o else None
            fn = str(ps.get("filename") or "")
            row.update(online=True, state=str(ps.get("state") or "unknown"), klippy=str((st.get("webhooks") or {}).get("state") or ""), file=fn,
                       progress=vs.get("progress"), elapsed=ps.get("print_duration"), nozzle=pair(st.get("extruder")), bed=pair(st.get("heater_bed")),
                       layer=[info.get("current_layer"), info.get("total_layer")] if info.get("total_layer") else None, estimate=None, thumb=None, camera=None)
            try:
                if fn:
                    had = Handler._ovmeta.get(h.pid)
                    if not had or had[0] != fn:
                        had = Handler._ovmeta[h.pid] = (fn, h.mget("/server/files/metadata?filename=" + urllib.parse.quote(fn), 4)["result"])
                    row.update(estimate=had[1].get("estimated_time"), thumb=thumb_path(fn, had[1].get("thumbnails")))
                cam, ui = h.webcam(), h.settings().get("camera")
                if cam.get("snapshot"):
                    row["camera"] = self.clean_view(ui) if isinstance(ui, dict) else cam["view"]
            except Exception:  # noqa: BLE001
                pass                                          # the extras are optional: the row still shows the state
            try:                                              # what each filament slot holds (Box slots, AFC lanes, U1 channels, tools)
                prov = h.provider()
                tools = len(h.tc_tools()) if prov == "toolchanger" else h.tool_count()
                names = h.afc_lanes() if prov == "afc" else h.qidi_slots() + ["slot16"] if prov == "qidibox" else []
                objs = filament_objects(prov, h.objects(), tools)
                fst = h.mget("/printer/objects/query?" + "&".join(urllib.parse.quote(o) for o in objs), 4)["result"]["status"] if objs else {}
                need_table = prov == "qidibox" and not ((fst.get("multi_color_controller") or {}).get("slots") or {}).get("materials")
                row.update(provider=prov, filament=filament_slots(prov, fst, names, tools, h.box_table() if need_table else None))
            except Exception:  # noqa: BLE001
                pass

        threads = [threading.Thread(target=ask, args=(r,), daemon=True) for r in rows]
        for t in threads:
            t.start()
        for t in threads:
            t.join(9)
        return {"printers": rows}

    def add_printer(self, req):
        name = str(req.get("name") or "").strip()[:60]
        host = str(req.get("host") or "").strip().lower()
        if not PRINTER_HOST.fullmatch(host):
            return self.jreply(400, {"error": "enter the printer's IP address or hostname (no http://, no port)"})
        try:
            addrs = {ipaddress.ip_address(a[4][0]) for a in socket.getaddrinfo(host, 7125, type=socket.SOCK_STREAM)}
        except (OSError, ValueError):
            return self.jreply(400, {"error": "cannot resolve " + host})
        if any(a.is_loopback or a.is_link_local or a.is_unspecified or a.is_multicast for a in addrs):
            return self.jreply(400, {"error": "that address is not allowed"})
        shown, lst = self.extra_printers(), self.ui_printers()
        if len(lst) >= 11 or any(p["host"] == host for p in shown) or host == str(Handler.printer).lower():
            return self.jreply(409, {"error": "too many printers, or that printer is already added"})
        try:
            with urllib.request.urlopen("http://%s:7125/server/info" % host, timeout=5) as r:
                if "result" not in json.load(r):
                    raise ValueError("not Moonraker")
        except Exception:  # noqa: BLE001
            return self.jreply(400, {"error": "no Moonraker answered at %s:7125" % host})
        detected = {"afc": False, "u1": False}
        try:                                                  # first look at what filament system the printer has
            with urllib.request.urlopen("http://%s:7125/printer/objects/list" % host, timeout=6) as r:
                objs = json.load(r)["result"]["objects"]
            detected["u1"] = "filament_detect" in objs
            detected["afc"] = "AFC" in objs and any(re.match(r"AFC_(stepper|lane) \S", o) for o in objs)
        except Exception:  # noqa: BLE001
            pass
        pid = printer_id(name or host, {p["id"] for p in shown + lst} | {"main"})
        lst.append({"id": pid, "name": name or host, "host": host, "provider": "u1" if detected["u1"] else "afc" if detected["afc"] else "auto"})
        write_json_atomic(os.path.join(self.state, "printers.json"), {"printers": lst})
        logging.info("added printer %s (%s)", pid, host)
        self.jreply(200, {"ok": True, "id": pid, "detected": detected, "printers": self.printer_list()})

    def remove_printer(self, req):
        pid = str(req.get("id") or "")
        if any(p["id"] == pid for p in Handler.fixed_printers):
            return self.jreply(403, {"error": "this printer is set in the server's config (\"printers\" in config.json, or FEEDLINE_PRINTERS); remove it there"})
        lst = self.ui_printers()
        if pid == "main" or pid not in [p["id"] for p in lst]:
            return self.jreply(404, {"error": "no such printer (the main printer is set in config.json)"})
        write_json_atomic(os.path.join(self.state, "printers.json"), {"printers": [p for p in lst if p["id"] != pid]})
        try:
            os.remove(os.path.join(self.state, "settings-%s.json" % pid))
        except OSError:
            pass
        logging.info("removed printer %s", pid)
        self.jreply(200, {"ok": True, "printers": self.printer_list()})

    def objects(self):
        return self.cached("objects", 20, lambda: self.mget("/printer/objects/list")["result"]["objects"]) or []

    def afc_lanes(self):
        """Lane names of an ArmoredTurtle AFC (BoxTurtle) install: objects "AFC" plus "AFC_stepper <lane>" / "AFC_lane <lane>"."""
        objs = self.objects()
        if "AFC" not in objs:
            return []
        return [o.split(" ", 1)[1] for o in objs if re.match(r"AFC_(stepper|lane) \S", o)]

    def qidi_slots(self):
        """Slot names of a QIDI Box (objects "box_stepper slot0" ...), in order; four per Box."""
        nums = sorted(int(m.group(1)) for m in (re.fullmatch(r"box_stepper slot(\d+)", o) for o in self.objects()) if m)
        return ["slot%d" % n for n in nums if n < 16]

    def tc_tools(self):
        """Tool objects of a klipper-toolchanger setup ("toolchanger" plus "tool T0" ...), as used by StealthChanger and ktc-easy."""
        objs = self.objects()
        return sorted(o for o in objs if re.fullmatch(r"tool \S+", o)) if "toolchanger" in objs else []

    def provider(self):
        objs = self.objects()
        p = self.settings().get("provider") or self.cfg.get("provider") or "auto"
        if (p == "afc" and not self.afc_lanes()) or (p == "u1" and "filament_detect" not in objs) or (p == "qidibox" and not self.qidi_slots()) or (p == "toolchanger" and not self.tc_tools()) or p not in ("u1", "afc", "qidibox", "toolchanger", "extruders"):
            p = "auto"
        if p != "auto":
            return p
        if "filament_detect" in objs:
            return "u1"
        return "afc" if self.afc_lanes() else "qidibox" if self.qidi_slots() else "toolchanger" if self.tc_tools() else "extruders"

    # ---- QIDI Box: what is in a slot (for spools without an RFID tag). The Box keeps a number per slot for the filament,
    # the color and the vendor in Klipper's saved variables; the numbers are rows of the printer's own filament table.
    BOX_TABLE = "officiall_filas_list.cfg"
    BOX_FIELDS = (("filament", "filament_slot%d", "fila"), ("color", "color_slot%d", "color"), ("vendor", "vendor_slot%d", "vendor"))

    def box_table(self):
        """The printer's filament table: {"fila": {id: name}, "color": {id: "#hex"}, "vendor": {id: name}}, or {}."""
        def load():
            out, sec = {"fila": {}, "color": {}, "vendor": {}}, None
            for line in self.fetch_config(self.BOX_TABLE)[0].decode("utf-8", "replace").splitlines():
                m = re.match(r"\[([^\]]+)\]", line)
                kv = re.match(r"\s*([^#;=\s\[][^=]*?)\s*=\s*(.*?)\s*$", line)
                if m:
                    sec = m.group(1).strip()
                elif kv and sec:
                    k, v = kv.group(1), kv.group(2)
                    if re.fullmatch(r"fila\d+", sec) and k == "filament":
                        out["fila"][int(sec[4:])] = v
                    elif sec == "colordict" and k.isdigit():
                        out["color"][int(k)] = v
                    elif sec == "vendor_list" and k.isdigit():
                        out["vendor"][int(k)] = v
            return out
        return self.cached("boxtable", 300, load) or {}

    def slot_script(self, req):
        """SAVE_VARIABLE lines for one "set_slot" request, or raises ValueError. Only numbers that are rows of the printer's
        own filament table are accepted, for a slot this printer has (16 is the external spool)."""
        if self.provider() != "qidibox":
            raise ValueError("this printer has no QIDI Box")
        slot = req.get("slot")
        if isinstance(slot, bool) or not isinstance(slot, int) or slot not in [int(s[4:]) for s in self.qidi_slots()] + [16]:
            raise ValueError("unknown slot")
        table, lines = self.box_table(), []
        for field, var, kind in self.BOX_FIELDS:
            v = req.get(field)
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, int) or v not in (table.get(kind) or {}):
                raise ValueError("unknown %s" % field)
            lines.append("SAVE_VARIABLE VARIABLE=%s VALUE=%d" % (var % slot, v))
        if not lines:
            raise ValueError("nothing to change")
        return "\n".join(lines)

    def box_slot_control(self, req):
        try:
            script = self.slot_script(req)
            state = self.print_state()
            if state in ("printing", "paused"):
                return self.jreply(409, {"error": "refused: printer is %s" % state})
            self.run_gcode(script, 20)
            self.jreply(200, {"ok": True, "cmd": script.replace("\n", " ; ")})
        except ValueError as e:
            self.jreply(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self.jreply(502, {"error": str(e)})

    def slot_count(self):
        """How many filament slots the tool buttons cover: Box slots on a QIDI Box, otherwise one per tool."""
        return len(self.qidi_slots()) if self.provider() == "qidibox" else self.tool_count()

    def tool_count(self):
        n = self.cfg.get("tools")
        if isinstance(n, int) and 0 < n <= 16:
            return n
        objs = self.objects()
        return max(1, sum(1 for o in objs if re.fullmatch(r"extruder\d*", o)))

    def settings(self):
        return load_json(self.settings_path(), {})

    def custom_packs(self, kind):
        out = []
        base = os.path.join(self.state, "packs", kind)
        try:
            names = sorted(os.listdir(base))
        except OSError:
            return out
        for pid in names:
            p = load_json(os.path.join(base, pid, "pack.json"), None)
            if isinstance(p, dict) and PACK_ID.fullmatch(pid):
                p["id"], p["kind"], p["custom"] = pid, kind, True
                out.append(p)
        return out

    def pack_ids(self, kind):
        return [p["id"] for p in BUILTIN_PACKS[kind]] + [p["id"] for p in self.custom_packs(kind)]

    def webcam(self):
        """Resolve the camera: explicit config first, otherwise the first enabled webcam Moonraker knows about."""
        host = "http://%s" % self.printer
        absu = lambda u: u if re.match(r"https?://", u or "") else (host + (u if (u or "").startswith("/") else "/" + (u or "")))
        w = self.cfg.get("webcam")
        if not w:
            def find():
                for c in self.mget("/server/webcams/list")["result"]["webcams"]:
                    if c.get("enabled", True):
                        return c
            c = self.cached("webcam", 60, find)
            if not c:
                return {"mode": "none", "live": None, "snapshot": None, "view": self.clean_view({})}
            svc = (c.get("service") or "").lower()
            su = c.get("stream_url") or ""
            mode = "webrtc" if "webrtc" in svc or su.rstrip("/").endswith("webrtc") else ("mjpeg" if "mjpeg" in svc or "stream" in su else "snapshot")
            return {"mode": mode, "live": absu(su) if su and mode != "snapshot" else None, "snapshot": absu(c.get("snapshot_url")) if c.get("snapshot_url") else None,
                    "view": self.clean_view(c)}
        return {"mode": w.get("mode") or ("webrtc" if w.get("live") else "snapshot"), "live": absu(w["live"]) if w.get("live") else None,
                "snapshot": absu(w["snapshot"]) if w.get("snapshot") else None, "view": self.clean_view(w)}

    @staticmethod
    def clean_view(c):
        """How the camera picture is turned: {"rotation": 0|90|180|270 (clockwise), "flipH", "flipV"}. Accepts Moonraker's
        webcam entry (rotation, flip_horizontal, flip_vertical) and the Settings choice (rotation, flipH, flipV)."""
        try:
            rot = int(c.get("rotation") or 0) % 360
        except (TypeError, ValueError):
            rot = 0
        return {"rotation": rot if rot in (0, 90, 180, 270) else 0,
                "flipH": bool(c.get("flipH", c.get("flip_horizontal"))), "flipV": bool(c.get("flipV", c.get("flip_vertical")))}

    @staticmethod
    def parse_spoolman(raw):
        """Accepts "host:port", "host" or a full URL ("http://host:7912", "https://spoolman.lan/prefix").
        Returns (scheme, netloc, hostname, prefix) or raises ValueError."""
        s = str(raw or "").strip()
        if not s:
            raise ValueError("empty")
        if "://" not in s:
            s = "http://" + s
        u = urllib.parse.urlsplit(s)
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError("enter a Spoolman address like http://192.168.1.20:7912")
        if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?|[0-9a-fA-F:]+", u.hostname):
            raise ValueError("bad host name")
        try:
            u.port
        except ValueError:
            raise ValueError("bad port")
        prefix = u.path.rstrip("/")
        if prefix and (not re.fullmatch(r"(/[A-Za-z0-9._~\-]+)+", prefix) or any(p in (".", "..") for p in prefix.split("/"))):
            raise ValueError("bad path")
        return u.scheme, u.netloc, u.hostname, prefix

    def spoolman_base(self):
        """Base URL this server uses to reach Spoolman (no trailing slash), or None."""
        if not self.spoolman:
            return None
        try:
            scheme, netloc, _, prefix = self.parse_spoolman(self.spoolman)
        except ValueError:
            return None
        return "%s://%s%s" % (scheme, netloc, prefix)

    def spoolman_url(self):
        """Where the browser should open Spoolman: config "spoolman_url", else the address above
        (with this dashboard's host name when Spoolman runs on the same machine as localhost)."""
        if not self.spoolman:
            return None
        u = self.cfg.get("spoolman_url")
        if u:
            return u.rstrip("/")
        try:
            scheme, netloc, host, prefix = self.parse_spoolman(self.spoolman)
        except ValueError:
            return None
        if host in ("127.0.0.1", "localhost", "::1"):
            me = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if me:
                port = netloc.rpartition(":")[2] if ":" in netloc and not netloc.endswith("]") else ""
                netloc = me + (":" + port if port.isdigit() else "")
        return "%s://%s%s" % (scheme, netloc, prefix)

    def set_spoolman(self, req):
        """Settings tab: point the dashboard at a Spoolman running elsewhere ("" = back to config.json)."""
        raw = str(req.get("url") or "").strip()
        path = os.path.join(self.state, "spoolman.json")
        if not raw:
            try:
                os.remove(path)
            except OSError:
                pass
            Handler.spoolman = Handler.spoolman_cfg
            return self.jreply(200, {"ok": True, "config": self.public_config()})
        try:
            scheme, netloc, host, prefix = self.parse_spoolman(raw)
            addrs = {ipaddress.ip_address(a[4][0]) for a in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
        except ValueError as e:
            return self.jreply(400, {"error": str(e)})
        except OSError:
            return self.jreply(400, {"error": "cannot resolve that host"})
        if any(a.is_link_local or a.is_unspecified or a.is_multicast for a in addrs):
            return self.jreply(400, {"error": "that address is not allowed"})
        base = "%s://%s%s" % (scheme, netloc, prefix)
        try:
            with urllib.request.urlopen(base + "/api/v1/info", timeout=6) as r:
                info = json.load(r)
            if not isinstance(info, dict) or "version" not in info:
                raise ValueError("not Spoolman")
        except Exception:  # noqa: BLE001
            return self.jreply(400, {"error": "no Spoolman answered at %s/api/v1/info (is it running, and reachable from this server?)" % base})
        write_json_atomic(path, {"url": base})
        Handler.spoolman = base
        logging.info("spoolman set to %s", base)
        self.jreply(200, {"ok": True, "version": info.get("version"), "config": self.public_config()})

    def public_config(self):
        st, c, host = self.settings(), self.cfg, self.printer
        prov = self.provider()
        gsel = {}
        mid, mwhy = ("u1", "Snapmaker RFID filament detection") if prov == "u1" else self.model()
        if mid not in self.pack_ids("printer"):
            mid = None
        picked = (st.get("graphics") or {}).get("printer") or (c.get("graphics") or {}).get("printer")
        for kind, dflt in (("printer", mid or "generic"), ("toolchanger", {"u1": "u1", "afc": "afc", "qidibox": "afc"}.get(prov, "reel"))):
            want = (st.get("graphics") or {}).get(kind) or (c.get("graphics") or {}).get(kind) or dflt
            gsel[kind] = want if want in self.pack_ids(kind) else dflt
        # "toolchanger" is the spool/slot style of the lane cards; "unit" is the separate BoxTurtle unit picture (afc | none)
        un = (st.get("graphics") or {}).get("unit") or (c.get("graphics") or {}).get("unit")
        gsel["unit"] = un if un in ("afc", "u1", "none") else ("afc" if prov == "afc" else "none")
        name = (st.get("name") or "").strip() or c.get("name")
        if not name:
            info = self.cached("pinfo", 300, lambda: self.mget("/printer/info")["result"])
            name = (info or {}).get("hostname") or "Klipper printer"
        eff = self.effective_macros()
        cam = self.webcam()
        scr = self.screen_cfg()
        light = self.light_cfg()
        ui_light = st.get("light")
        return {
            "options": {"embed": {"on": self.embed_allowed(), "source": "ui" if "allow_embed" in load_json(os.path.join(self.state, "server.json"), {}) else ("config" if c.get("allow_embed") else "none")},
                        "screen": {"on": bool(scr and scr.get("snapshot")), "source": "ui" if "screen" in st else ("config" if scr else "none")},
                        "light": {"mode": "none" if ui_light is False else ("pick" if isinstance(ui_light, dict) and self.light_for(ui_light.get("object")) else "auto"),
                                  "choices": [o for _, o, _, _ in self.light_choices()], "active": light.get("object")}},
            "name": name, "defaultName": c.get("name"), "graphics": gsel,
            "motion": bool(st["motion"]) if isinstance(st.get("motion"), bool) else bool(c.get("motion", True)),
            "toolhead": next((v for v in (st.get("toolhead"), c.get("toolhead")) if v in TOOLHEADS), "generic"),
            "docks": next((v for v in (st.get("docks"), c.get("docks")) if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 12), None),
            # the printer graphic follows the detected model unless one was picked in Settings or config.json
            "model": {"id": mid, "label": "Snapmaker U1" if mid == "u1" else MODEL_LABEL.get(mid), "why": mwhy, "auto": picked not in self.pack_ids("printer")},
            "packs": {k: BUILTIN_PACKS[k] + self.custom_packs(k) for k in BUILTIN_PACKS},
            "provider": prov, "toolCount": self.tool_count(), "printerHost": host,
            # QIDI Box: its slots, then "slot16", the printer's name for the external spool that bypasses the Box
            "lanes": self.afc_lanes() if prov == "afc" else self.qidi_slots() + ["slot16"] if prov == "qidibox" else [],
            "laneEdit": {k: v[0] in self.commands() for k, v in self.LANE_EDIT.items()} if prov == "afc" else {},
            "filamentSystems": {"selected": st.get("provider") or "auto", "available": {"u1": "filament_detect" in self.objects(), "afc": bool(self.afc_lanes()), "qidibox": bool(self.qidi_slots()), "toolchanger": bool(self.tc_tools())}},
            "actions": [a for a in ACTIONS if a in eff], "macros": eff,
            "light": {"object": light.get("object")} if light and light.get("on") and light.get("off") else None,
            "screen": {"interactive": "http://%s%s" % (host, scr.get("interactive", "/screen/"))} if scr and scr.get("snapshot") else None,
            # "view": how the picture is turned. A choice made in Settings wins over what the printer (Moonraker / config.json) says
            "camera": dict({"mode": cam["mode"], "live": cam["live"], "snapshot": bool(cam["snapshot"]), "viewFromUi": isinstance(st.get("camera"), dict),
                            "printerView": cam["view"]}, **(self.clean_view(st["camera"]) if isinstance(st.get("camera"), dict) else cam["view"])),
            "toolFans": c.get("tool_fans"), "chamber": c.get("chamber"), "toolSpools": c.get("tool_spools"),
            "tempLimits": c.get("temp_limits"), "spoolman": bool(self.spoolman_base()), "spoolmanUrl": self.spoolman_url(),
            "spoolmanServer": self.spoolman_base(), "spoolmanFromUi": bool(self.spoolman) and self.spoolman != Handler.spoolman_cfg,
        }

    _locks, _locks_guard = {}, threading.Lock()

    @property
    def busy(self):
        """One command at a time per printer (a long filament load on one printer does not hold up the others)."""
        with Handler._locks_guard:
            return Handler._locks.setdefault(self.pid, threading.Lock())

    # AFC lane data (what the lane card shows): material, color, weight. Fixed AFC commands with validated values only.
    LANE_EDIT = {"material": ("SET_MATERIAL", "MATERIAL"), "color": ("SET_COLOR", "COLOR"), "weight": ("SET_WEIGHT", "WEIGHT")}

    def lane_script(self, req):
        if self.provider() != "afc":
            raise ValueError("this printer does not use AFC")
        lane = req.get("lane")
        if lane not in self.afc_lanes() or not re.fullmatch(r"[\w.\-]{1,40}", str(lane)):
            raise ValueError("unknown lane")
        cmds, lines = self.commands(), []
        for key in ("material", "color", "weight"):      # material first: AFC validates the color against the lane's material
            if req.get(key) is None:
                continue
            name, param = self.LANE_EDIT[key]
            if name not in cmds:
                raise ValueError("this AFC version has no " + name)
            v = req[key]
            if key == "material":
                v = str(v).strip()
                if not re.fullmatch(r"[A-Za-z0-9+.\-]{1,24}", v):
                    raise ValueError("material: letters, digits and + . - only (max 24)")
            elif key == "color":
                v = str(v).strip().lstrip("#")
                if not re.fullmatch(r"[0-9a-fA-F]{6}", v):
                    raise ValueError("color must be 6 hex digits")
                v = v.upper()
            else:
                try:
                    w = float(v)
                except (TypeError, ValueError):
                    raise ValueError("weight must be a number")
                if isinstance(v, bool) or not 0 <= w <= 20000:
                    raise ValueError("weight must be 0-20000 g")
                v = str(int(round(w)))
            lines.append("%s LANE=%s %s=%s" % (name, lane, param, v))
        if not lines:
            raise ValueError("nothing to change")
        return "\n".join(lines)

    def lane_control(self, req):
        script = self.lane_script(req)
        if not self.busy.acquire(blocking=False):
            return self.jreply(409, {"error": "another command is still running"})
        try:
            self.run_gcode(script, 30)
            self.jreply(200, {"ok": True, "cmd": script.replace("\n", " ; ")})
        except Exception as e:  # noqa: BLE001
            self.jreply(502, {"error": str(e)})
        finally:
            self.busy.release()

    def command_for(self, action, tool, lane=None):
        """Tool buttons run a macro template from config.json (admin-owned), never text sent by the browser."""
        if action not in ("load", "unload", "pickup", "park") or not isinstance(tool, int) or isinstance(tool, bool):
            return None
        afc = self.provider() == "afc"
        if afc:
            if lane not in self.afc_lanes():
                return None
        elif not 0 <= tool < self.slot_count():
            return None
        eff = self.effective_macros().get(action)
        if not eff:
            return None
        tmpl = eff["template"]
        base = tmpl.split(" ", 1)[0]
        if eff["source"] != "config" and not self.base_ok(base):
            return None
        return tmpl.replace("{lane}", lane or "").replace("{nz}", str(tool) if tool else "").replace("{n}", str(tool))

    # print controls use Moonraker's own endpoints and are only valid in certain print states
    PRINT_ACTIONS = {"pause": ("pause", ("printing",)),
                     "resume": ("resume", ("paused",)),
                     "cancel": ("cancel", ("printing", "paused")),
                     "estop": ("emergency_stop", None)}     # M112: allowed in every state (no state lookup, so it works even when Klipper is unhappy); Klipper then needs a firmware restart

    def print_state(self):
        url = "http://%s:7125/printer/objects/query?print_stats" % self.printer
        with urllib.request.urlopen(url, timeout=8) as r:
            return json.load(r)["result"]["status"]["print_stats"]["state"]

    def print_control(self, action):
        endpoint, allowed = self.PRINT_ACTIONS[action]
        try:
            state = self.print_state() if allowed else None
            if allowed and state not in allowed:
                return self.jreply(409, {"error": "refused: cannot %s while printer is %s" % (action, state)})
            url = "http://%s:7125/printer/%s" % (self.printer, endpoint if endpoint == "emergency_stop" else "print/" + endpoint)
            logging.info("print control: %s", action)
            with urllib.request.urlopen(urllib.request.Request(url, method="POST"), timeout=60) as r:
                r.read()
            self.jreply(200, {"ok": True, "cmd": action})
        except Exception as e:  # noqa: BLE001
            self.jreply(502, {"error": str(e)})

    # ---- print tuning: baby-stepping, speed and flow factors, pressure advance. Meant for use during a print, so there is
    # no print-state check; every value is range-checked and turned into one fixed command.
    Z_STEPS = (0.005, 0.01, 0.025, 0.05)
    TUNE = {"speed": ("M220 S%d", 10, 300), "flow": ("M221 S%d", 50, 200),
            "pa": ("SET_PRESSURE_ADVANCE ADVANCE=%.4f", 0, 2), "smooth": ("SET_PRESSURE_ADVANCE SMOOTH_TIME=%.4f", 0, 0.2)}

    def tune_command(self, req):
        """The G-code for one tuning request, or raises ValueError (bad request) / PermissionError (outside the limit)."""
        what = req.get("what")
        try:
            v = float(req.get("value", 0))
        except (TypeError, ValueError):
            raise ValueError("bad value")
        if isinstance(req.get("value"), bool) or v != v:                      # no booleans, no NaN
            raise ValueError("bad value")
        if what in self.TUNE:
            fmt, lo, hi = self.TUNE[what]
            if not lo <= v <= hi:
                raise ValueError("%s must be %s to %s" % (what, lo, hi))
            return fmt % (round(v) if "%d" in fmt else v)
        if what not in ("z_adjust", "z_reset"):
            raise ValueError("unknown setting")
        st = self.mget("/printer/objects/query?gcode_move=homing_origin&toolhead=homed_axes")["result"]["status"]
        move = " MOVE=1" if "z" in (st["toolhead"].get("homed_axes") or "") else ""      # apply it now when Z is homed, otherwise from the next move
        if what == "z_reset":
            return "SET_GCODE_OFFSET Z=0" + move
        if round(abs(v), 4) not in self.Z_STEPS:
            raise ValueError("step must be one of %s mm" % ", ".join(str(s) for s in self.Z_STEPS))
        cur, lim = float(st["gcode_move"]["homing_origin"][2]), float(self.cfg.get("z_offset_limit") or 1.0)
        if abs(cur + v) > lim + 1e-9:
            raise PermissionError("refused: the Z offset would be %+.3f mm, beyond the %.2f mm limit" % (cur + v, lim))
        return "SET_GCODE_OFFSET Z_ADJUST=%+.3f%s" % (v, move)

    def tune_control(self, req):
        try:
            cmd = self.tune_command(req)
        except ValueError as e:
            return self.jreply(400, {"error": str(e)})
        except PermissionError as e:
            return self.jreply(409, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            return self.jreply(502, {"error": str(e)})
        try:
            self.run_gcode(cmd, 15)
            self.jreply(200, {"ok": True, "cmd": cmd})
        except Exception as e:  # noqa: BLE001
            self.jreply(502, {"error": str(e)})

    def mesh_steps(self):
        """What it takes to probe the bed from the printer's current state: a list of (what it does, G-code). Only a printer that
        is idle and ready is touched. Needed preparation is added: home all axes, level the gantry (QGL / Z tilt) when the
        printer has that and it is not levelled yet, home Z again after that, and heat a cold bed (config `mesh_bed_temp`,
        default 60 C; 0 = do not heat), switched off again afterwards."""
        if "BED_MESH_CALIBRATE" not in self.commands():
            raise PermissionError("this printer has no BED_MESH_CALIBRATE command")
        objs = self.objects()
        levelling = [o for o in ("quad_gantry_level", "z_tilt") if o in objs]
        st = self.mget("/printer/objects/query?print_stats&toolhead&webhooks&heater_bed" + "".join("&" + o for o in levelling))["result"]["status"]
        ready = (st.get("webhooks") or {}).get("state", "ready")
        if ready != "ready":
            raise PermissionError("Klipper is not ready (%s)" % ready)
        if st["print_stats"]["state"] in ("printing", "paused"):
            raise PermissionError("refused: printer is %s" % st["print_stats"]["state"])
        homed = all(a in (st["toolhead"].get("homed_axes") or "") for a in "xyz")
        steps = [] if homed else [("Home all axes", "G28")]
        if levelling and (not homed or not (st.get(levelling[0]) or {}).get("applied")):
            label, cmd = ("Level the gantry", "QUAD_GANTRY_LEVEL") if levelling[0] == "quad_gantry_level" else ("Level the bed (Z tilt)", "Z_TILT_ADJUST")
            steps += [(label, cmd), ("Home Z again", "G28 Z")]
        temp = min(int(self.cfg.get("mesh_bed_temp", 60) or 0), self.temp_limit("heater_bed") or 0)
        heat = st.get("heater_bed") is not None and temp > 0 and not float((st["heater_bed"] or {}).get("target") or 0)
        if heat:
            steps.append(("Heat the bed to %d C" % temp, "M190 S%d" % temp))
        steps.append(("Probe the bed", "BED_MESH_CALIBRATE"))
        if heat:
            steps.append(("Switch the bed heater off", "M140 S0"))
        return steps

    def mesh_plan(self):
        try:
            self.jreply(200, {"steps": [label for label, _ in self.mesh_steps()]})
        except PermissionError as e:
            self.jreply(409, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self.jreply(502, {"error": str(e)})

    def mesh_control(self, req):
        """Probe the bed, doing whatever preparation is needed first (see mesh_steps). The new mesh stays in the printer's
        memory: saving it needs SAVE_CONFIG, which restarts Klipper, so that is left to the user at the printer."""
        if not self.busy.acquire(blocking=False):
            return self.jreply(409, {"error": "another command is still running"})
        heated = off = False
        label = ""
        try:
            try:
                steps = self.mesh_steps()
            except PermissionError as e:
                return self.jreply(409, {"error": str(e)})
            for label, cmd in steps:
                heated = heated or cmd.startswith("M190")
                self.run_gcode(cmd, 900)
                off = off or cmd == "M140 S0"
            self.jreply(200, {"ok": True, "steps": [s[0] for s in steps]})
        except Exception as e:  # noqa: BLE001
            if heated and not off:                                 # a step failed after we heated the bed: do not leave it on
                try:
                    self.run_gcode("M140 S0", 15)
                except Exception:  # noqa: BLE001
                    pass
            self.jreply(502, {"error": "%s failed: %s" % (label, e) if label else str(e)})
        finally:
            self.busy.release()

    # Console: one line of G-code typed in the browser. Anything Klipper accepts, except what restarts or stops the printer, writes
    # firmware, or runs a shell command on the host (gcode_shell_command): there is no login here, so those stay off.
    CONSOLE_DENY = re.compile(CMD_DENY.pattern + r"|^RUN_SHELL|^SHELL_COMMAND", re.I)

    def gcode_control(self, req):
        script = req.get("script")
        if not isinstance(script, str) or not script.strip() or len(script) > 500 or re.search(r"[\x00-\x1f\x7f]", script):
            return self.jreply(400, {"error": "enter one line of G-code (up to 500 characters)"})
        word = script.split()[0]
        if self.CONSOLE_DENY.match(word):
            return self.jreply(403, {"error": "%s is not allowed here: restarts, emergency stop, firmware and shell commands use the printer's own controls" % word.upper()})
        try:
            self.run_gcode(script.strip(), 600)
            self.jreply(200, {"ok": True})
        except Exception as e:  # noqa: BLE001
            self.jreply(502, {"error": str(e)})

    def fan_control(self, req):
        """Set a controllable fan: the part fan ("fan") or a fan_generic. heater_fan* (hotend/PSU fans) are
        driven by Klipper from heater temperature and are deliberately not settable here."""
        name = str(req.get("fan", ""))
        try:
            speed = float(req.get("speed"))
        except (TypeError, ValueError):
            speed = -1
        if not 0 <= speed <= 1 or not re.fullmatch(r"[A-Za-z0-9_]+", name):
            return self.jreply(400, {"error": "bad request"})
        try:
            with urllib.request.urlopen("http://%s:7125/printer/objects/list" % self.printer, timeout=8) as r:
                objs = json.load(r)["result"]["objects"]
            if name != "fan" and ("fan_generic " + name) not in objs:
                return self.jreply(400, {"error": "not a controllable fan"})
            cmd = ("M106 S%d" % round(speed * 255)) if name == "fan" else "SET_FAN_SPEED FAN=%s SPEED=%.2f" % (name, speed)
            self.run_gcode(cmd, 15)
            self.jreply(200, {"ok": True, "cmd": cmd})
        except Exception as e:  # noqa: BLE001
            self.jreply(502, {"error": str(e)})

    RESTARTS = {"klipper": "/printer/restart", "firmware": "/printer/firmware_restart"}

    def restart_control(self, req):
        """Restart Klipper or do a firmware restart through Moonraker. The console refuses RESTART / FIRMWARE_RESTART
        (CMD_DENY), so these two buttons are the only way; they never run while a print is going."""
        path = self.RESTARTS.get(str(req.get("kind")))
        if not path:
            return self.jreply(400, {"error": "bad request"})
        try:
            state = self.mget("/printer/objects/query?print_stats", 6)["result"]["status"]["print_stats"]["state"]
        except Exception:  # noqa: BLE001
            state = None                       # Klipper is down or in shutdown: that is exactly when a restart is wanted
        if state in ("printing", "paused"):
            return self.jreply(409, {"error": "refused: printer is %s" % state})
        try:
            logging.info("restart: %s", req.get("kind"))
            with urllib.request.urlopen(urllib.request.Request("http://%s:7125%s" % (self.printer, path), data=b"", method="POST"), timeout=20) as r:
                r.read()
            self.jreply(200, {"ok": True})
        except urllib.error.HTTPError as e:
            self.jreply(502, {"error": self.moonraker_error(e)})
        except Exception as e:  # noqa: BLE001
            self.jreply(502, {"error": str(e)})

    def tag_control(self, req):
        """Snapmaker U1: make the printer read the RFID tag in one channel again (FILAMENT_DT_UPDATE CHANNEL=n)."""
        try:
            ch = int(req.get("channel"))
        except (TypeError, ValueError):
            ch = -1
        if not 0 <= ch <= 3:
            return self.jreply(400, {"error": "bad request"})
        try:
            if "filament_detect" not in self.objects():
                return self.jreply(400, {"error": "this printer has no RFID reader"})
            self.run_gcode("FILAMENT_DT_UPDATE CHANNEL=%d" % ch, 15)
            self.jreply(200, {"ok": True})
        except Exception as e:  # noqa: BLE001
            self.jreply(502, {"error": str(e)})

    # Fluidd-style movement. Only while the printer is idle; jogs are sent as ABSOLUTE moves computed from the current
    # position (and clamped to the axis limits), so a failed move can never leave the printer stuck in relative mode.
    JOG_STEPS = (0.1, 1, 10, 50, 100)
    JOG_FEED = {"X": 6000, "Y": 6000, "Z": 600}      # mm/min
    HOME = {"all": "G28", "xy": "G28 X Y", "z": "G28 Z"}

    def move_control(self, req):
        action = req.get("action")
        try:
            if action == "home":
                script = self.HOME.get(str(req.get("axes")))
            elif action == "motors_off":
                script = "M84"
            elif action == "goto":      # absolute position for one axis, clamped to the axis limits
                axis, pos = str(req.get("axis")), float(req.get("pos"))
                script = ("goto", axis, pos) if axis in self.JOG_FEED and -1000 < pos < 1000 else None
            else:  # jog
                axis, dist = str(req.get("axis")), float(req.get("dist"))
                if axis not in self.JOG_FEED or abs(dist) not in self.JOG_STEPS:
                    script = None
                else:
                    script = ("jog", axis, dist)
        except (TypeError, ValueError):
            script = None
        if not script:
            return self.jreply(400, {"error": "bad request"})
        if not self.busy.acquire(blocking=False):
            return self.jreply(409, {"error": "another command is still running"})
        try:
            base = "http://%s:7125/printer/objects/query?print_stats&toolhead&gcode_move" % self.printer
            with urllib.request.urlopen(base, timeout=8) as r:
                st = json.load(r)["result"]["status"]
            if st["print_stats"]["state"] in ("printing", "paused"):
                return self.jreply(409, {"error": "refused: printer is %s" % st["print_stats"]["state"]})
            if isinstance(script, tuple):
                kind, axis, dist = script
                i = "XYZ".index(axis)
                if axis.lower() not in st["toolhead"].get("homed_axes", ""):
                    return self.jreply(409, {"error": "home %s first" % axis})
                lo, hi = st["toolhead"]["axis_minimum"][i], st["toolhead"]["axis_maximum"][i]
                want = dist if kind == "goto" else st["gcode_move"]["gcode_position"][i] + dist
                target = min(hi, max(lo, want))
                script = "G90\nG1 %s%.3f F%d" % (axis, target, self.JOG_FEED[axis])
            self.run_gcode(script, 300)
            self.jreply(200, {"ok": True, "cmd": script})
        except Exception as e:  # noqa: BLE001
            self.jreply(502, {"error": str(e)})
        finally:
            self.busy.release()

    def temp_limit(self, heater):
        """Safety cap for a heater the dashboard may set: nozzles, the bed, or a heater_generic (chamber etc.)."""
        lim = self.cfg.get("temp_limits") or {}
        if re.fullmatch(r"extruder\d*", heater):
            return lim.get("extruder", 300)
        if heater == "heater_bed":
            return lim.get("heater_bed", 110)
        if re.fullmatch(r"heater_generic [\w\-]+", heater):
            return lim.get("heater_generic", 150)
        return None

    def temp_control(self, req):
        """Set a heater target (0 = off). Klipper's own max_temp still applies on top of these caps."""
        heater = str(req.get("heater", ""))
        try:
            target = float(req.get("target"))
        except (TypeError, ValueError):
            target = -1
        cap = self.temp_limit(heater)
        if cap is None or not 0 <= target <= cap:
            return self.jreply(400, {"error": "bad request"})
        cmd = "SET_HEATER_TEMPERATURE HEATER=%s TARGET=%d" % (heater.split(" ", 1)[-1] if heater.startswith("heater_generic ") else heater, round(target))
        try:
            self.run_gcode(cmd, 15)
            self.jreply(200, {"ok": True, "cmd": cmd})
        except Exception as e:  # noqa: BLE001
            self.jreply(502, {"error": str(e)})

    LED_TYPES = ("led", "neopixel", "dotstar", "pca9632", "pca9533")
    LIGHT_HINT = re.compile(r"light|lamp|led|illum|chamber|cavity|enclosure|case", re.I)
    LIGHT_SKIP = re.compile(r"nozzle|logo|status|fan|heater|beeper|buzzer|power|psu|relay|probe|lcd|stepper|sensor|enable|reset|kill", re.I)

    def light_cfg(self):
        """The light button. Order: choice made in Settings (false = none), config.json "light" (false = none), else the best LED / light pin found."""
        ui = self.settings().get("light")
        if ui is False:
            return {}
        if isinstance(ui, dict):
            picked = self.light_for(ui.get("object"))
            if picked:
                return picked
        light = self.cfg.get("light")
        if light is False:
            return {}
        if isinstance(light, dict) and light.get("on") and light.get("off"):
            return light
        return self.cached("light", 60, self.detect_light) or {}

    def light_choices(self):
        """LEDs and light-named pins on the printer as (rank, object, on, off), best first. Never an unnamed pin."""
        cmds, found = self.commands(), []
        for o in self.objects():
            kind, _, name = o.partition(" ")
            if not name or not re.fullmatch(r"[\w.\-]{1,60}", name) or self.LIGHT_SKIP.search(name):
                continue
            if kind in self.LED_TYPES and "SET_LED" in cmds:
                found.append((0 if self.LIGHT_HINT.search(name) else 1, o,
                              "SET_LED LED=%s RED=1 GREEN=1 BLUE=1 WHITE=1" % name, "SET_LED LED=%s RED=0 GREEN=0 BLUE=0 WHITE=0" % name))
            elif kind == "output_pin" and "SET_PIN" in cmds and self.LIGHT_HINT.search(name):
                found.append((0, o, "SET_PIN PIN=%s VALUE=1" % name, "SET_PIN PIN=%s VALUE=0" % name))
        return sorted(found)

    def light_for(self, obj):
        for _, o, on, off in self.light_choices():
            if o == obj:
                return {"object": o, "on": on, "off": off}
        return None

    def detect_light(self):
        ch = self.light_choices()
        return {"object": ch[0][1], "on": ch[0][2], "off": ch[0][3]} if ch else {}

    def screen_cfg(self):
        """The remote touchscreen page: choice made in Settings (false = off), else config.json."""
        st = self.settings()
        if "screen" in st:
            return st["screen"] if isinstance(st["screen"], dict) else None
        s = self.cfg.get("screen")
        return s if isinstance(s, dict) and s.get("snapshot") else None

    def screen_probe(self):
        """Does the printer serve a touchscreen picture at /screen/snapshot (e.g. the Paxx firmware's Remote Screen)?"""
        try:
            with urllib.request.urlopen("http://%s/screen/snapshot" % self.printer, timeout=4) as r:
                return {"ok": r.status == 200 and r.headers.get("Content-Type", "").startswith("image/"), "reason": "found"}
        except urllib.error.HTTPError:
            return {"ok": False, "reason": "not-found"}
        except Exception:  # noqa: BLE001
            return {"ok": False, "reason": "unreachable"}

    def light_control(self, req):
        """Chamber LED (led cavity_led, RGBW; white only). Safe at any time, including during a print."""
        on = req.get("on")
        light = self.light_cfg()
        if not isinstance(on, bool) or not light.get("on") or not light.get("off"):
            return self.jreply(400, {"error": "no light configured"})
        cmd = light["on"] if on else light["off"]
        try:
            self.run_gcode(cmd, 15)
            self.jreply(200, {"ok": True, "cmd": cmd})
        except Exception as e:  # noqa: BLE001
            self.jreply(502, {"error": str(e)})

    # ---- config file browser / editor (Moonraker "config" root) ----
    EDIT_EXT = (".cfg", ".conf", ".ini", ".txt", ".md", ".yaml", ".yml")      # everything else is view-only
    MAX_EDIT = 512 * 1024
    backups = "/var/lib/feedline/backups"

    @staticmethod
    def safe_path(p):
        """Relative path inside the config root: plain names only, no '..', no hidden files."""
        p = str(p or "")
        segs = p.split("/")
        if not p or len(p) > 200 or any(not re.fullmatch(r"[\w .()+@\-]+", s) or s in (".", "..") or s.startswith(".") for s in segs):
            return None
        return "/".join(segs)

    def editable(self, p):
        return p.lower().endswith(self.EDIT_EXT) and not re.match(r"factory", os.path.basename(p), re.I)

    def fetch_config(self, p):
        url = "http://%s:7125/server/files/config/%s" % (self.printer, urllib.parse.quote(p))
        with urllib.request.urlopen(url, timeout=15) as r:
            return r.read(self.MAX_EDIT * 2 + 1), r.headers.get("Last-Modified", "")

    def config_read(self, query):
        p = self.safe_path(urllib.parse.parse_qs(query).get("path", [""])[0])
        if not p:
            return self.jreply(400, {"error": "bad path"})
        try:
            raw, mod = self.fetch_config(p)
            if len(raw) > self.MAX_EDIT * 2:
                return self.jreply(413, {"error": "file too large to open"})
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return self.jreply(415, {"error": "not a UTF-8 text file"})
            self.reply(200, "application/json", json.dumps({"path": p, "content": text, "modified": mod,
                                                            "editable": self.editable(p) and len(raw) <= self.MAX_EDIT}).encode())
        except Exception as e:  # noqa: BLE001
            self.jreply(502, {"error": str(e)})

    def config_save(self, req):
        if not Handler.cfg.get("allow_config_edit", True):
            return self.jreply(403, {"error": "config editing is disabled (allow_config_edit)"})
        p, content, base = self.safe_path(req.get("path")), req.get("content"), str(req.get("modified") or "")
        if not p or not isinstance(content, str) or not self.editable(p):
            return self.jreply(400, {"error": "bad request or file is view-only"})
        data = content.encode("utf-8")
        if len(data) > self.MAX_EDIT or b"\x00" in data:
            return self.jreply(400, {"error": "file too large or not text"})
        if not self.busy.acquire(blocking=False):
            return self.jreply(409, {"error": "another command is still running"})
        try:
            old, mod = self.fetch_config(p)
            if base and mod and base != mod:
                return self.jreply(409, {"error": "the file changed on the printer since you opened it - reload it first"})
            bdir = self.backups if self.pid == "main" else os.path.join(self.backups, self.pid)      # each printer keeps its own backups
            os.makedirs(bdir, exist_ok=True)                      # keep the previous version before anything is overwritten
            bk = os.path.join(bdir, "%s.%s.bak" % (p.replace("/", "__"), time.strftime("%Y%m%d-%H%M%S")))
            with open(bk, "wb") as fh:
                fh.write(old)
            prefix = p.replace("/", "__") + "."
            for stale in sorted(f for f in os.listdir(bdir) if f.startswith(prefix) and f.endswith(".bak"))[:-20]:
                os.remove(os.path.join(bdir, stale))              # keep the newest 20 backups per file
            d, name = os.path.split(p)
            boundary = "----feedline%d" % int(time.time() * 1000)
            parts = []
            for k, v in (("root", "config"), ("path", d)):
                parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode())
            parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\nContent-Type: application/octet-stream\r\n\r\n" % (boundary, name)).encode() + data + b"\r\n")
            parts.append(("--%s--\r\n" % boundary).encode())
            body = b"".join(parts)
            rq = urllib.request.Request("http://%s:7125/server/files/upload" % self.printer, data=body, method="POST",
                                        headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
            logging.info("saving config %s (%d bytes, backup %s)", p, len(data), bk)
            with urllib.request.urlopen(rq, timeout=30) as r:
                r.read()
            check, newmod = self.fetch_config(p)
            if check != data:
                return self.jreply(502, {"error": "saved but the printer's copy does not match; backup: " + os.path.basename(bk)})
            self.jreply(200, {"ok": True, "modified": newmod, "backup": os.path.basename(bk)})
        except Exception as e:  # noqa: BLE001
            self.jreply(502, {"error": str(e)})
        finally:
            self.busy.release()

    # ---- log export: the current klippy.log and moonraker.log (download only) ----
    LOGS = ("klippy.log", "moonraker.log")

    def logs_export(self, query):
        name = urllib.parse.parse_qs(query).get("name", [""])[0]
        stamp = time.strftime("%Y%m%d-%H%M%S")

        def get(n):
            with urllib.request.urlopen("http://%s:7125/server/files/logs/%s" % (self.printer, n), timeout=90) as r:
                return r.read()
        try:
            if name == "all":
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                    for n in self.LOGS:
                        z.writestr(n, get(n))
                body, fname, ctype = buf.getvalue(), "feedline-logs-%s.zip" % stamp, "application/zip"
            elif name in self.LOGS:
                body, fname, ctype = get(name), "%s-%s.log" % (name[:-4], stamp), "text/plain; charset=utf-8"
            else:
                return self.reply(400, "text/plain", b"bad log name")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % fname)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:  # noqa: BLE001
            self.reply(502, "text/plain", str(e).encode())

    # ---- cosmetic settings and graphics packs (the web UI may change these; nothing here can run G-code) ----
    def save_settings(self, req):
        st = self.settings()
        if "name" in req:
            name = str(req.get("name") or "").strip()[:60]
            if name:
                st["name"] = name
            else:
                st.pop("name", None)
        if "provider" in req:
            pv = req.get("provider")
            if pv not in ("auto", "extruders", "afc", "u1", "qidibox", "toolchanger"):
                return self.jreply(400, {"error": "unknown filament system"})
            if pv == "toolchanger" and not self.tc_tools():
                return self.jreply(400, {"error": "no toolchanger found on this printer"})
            if pv == "qidibox" and not self.qidi_slots():
                return self.jreply(400, {"error": "no QIDI Box found on this printer"})
            if pv == "afc" and not self.afc_lanes():
                return self.jreply(400, {"error": "no AFC (BoxTurtle) found on this printer"})
            if pv == "u1" and "filament_detect" not in self.objects():
                return self.jreply(400, {"error": "this printer has no Snapmaker filament detection"})
            if pv == "auto":
                st.pop("provider", None)
            else:
                st["provider"] = pv
            st.get("graphics", {}).pop("toolchanger", None)   # the filament graphic follows the filament system again
            st.get("graphics", {}).pop("unit", None)
            st.pop("macros", None)                            # tool-button macros were for the old system
        if "screen" in req:                                   # remote touchscreen: true = on (Paxx paths), false = off, null = follow config.json
            v = req["screen"]
            if v is None:
                st.pop("screen", None)
            elif v is True:
                st["screen"] = {"snapshot": "/screen/snapshot", "interactive": "/screen/"}
            elif v is False:
                st["screen"] = False
            else:
                return self.jreply(400, {"error": "bad screen setting"})
        if "toolhead" in req:                                 # drawn printer: the look of the toolhead (null = generic)
            v = req["toolhead"]
            if v is None:
                st.pop("toolhead", None)
            elif v in TOOLHEADS:
                st["toolhead"] = v
            else:
                return self.jreply(400, {"error": "unknown toolhead"})
        if "docks" in req:                                    # toolchanger: how many docks are drawn (null = one per tool)
            v = req["docks"]
            if v is None:
                st.pop("docks", None)
            elif isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 12:
                st["docks"] = v
            else:
                return self.jreply(400, {"error": "docks must be 1 to 12"})
        if "motion" in req:                                   # drawn printer: toolhead and bed follow the printer (true / false; null = config.json)
            v = req["motion"]
            if v is None:
                st.pop("motion", None)
            elif isinstance(v, bool):
                st["motion"] = v
            else:
                return self.jreply(400, {"error": "bad motion setting"})
        if "camera" in req:                                   # camera picture: {"rotation", "flipH", "flipV"}, null = as the printer says
            v = req["camera"]
            if v is None:
                st.pop("camera", None)
            elif isinstance(v, dict) and v.get("rotation") in (0, 90, 180, 270) and all(isinstance(v.get(k, False), bool) for k in ("flipH", "flipV")):
                st["camera"] = self.clean_view(v)
            else:
                return self.jreply(400, {"error": "bad camera setting"})
        if "light" in req:                                    # light button: a detected LED / pin, false = none, null = automatic
            v = req["light"]
            if v is None:
                st.pop("light", None)
            elif v is False:
                st["light"] = False
            elif isinstance(v, str) and self.light_for(v):
                st["light"] = {"object": v}
            else:
                return self.jreply(400, {"error": "that LED was not found on this printer"})
        mc = req.get("macros")
        if isinstance(mc, dict):
            cur = st.setdefault("macros", {})
            for a in ACTIONS:
                if a in mc:
                    if mc[a] is None:
                        cur.pop(a, None)
                    else:
                        try:
                            cur[a] = self.clean_macro(mc[a])
                        except ValueError as e:
                            return self.jreply(400, {"error": str(e)})
            if not cur:
                st.pop("macros", None)
        g = req.get("graphics")
        if isinstance(g, dict):
            cur = st.setdefault("graphics", {})
            for kind in BUILTIN_PACKS:
                v = g.get(kind)
                if v is None:
                    continue
                if v == "" or v not in self.pack_ids(kind):
                    cur.pop(kind, None)
                else:
                    cur[kind] = v
            u = g.get("unit")
            if u in ("afc", "u1", "none"):
                cur["unit"] = u
            elif u == "":
                cur.pop("unit", None)
        write_json_atomic(self.settings_path(), st)
        self.jreply(200, {"ok": True, "config": self.public_config()})

    @staticmethod
    def clean_num(v, lo=-10000, hi=10000):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ValueError("bad number")
        return round(float(v), 2)

    def clean_anchors(self, a):
        out = {}
        for k in RECT_ANCHORS:
            r = (a or {}).get(k)
            if isinstance(r, dict):
                out[k] = {n: self.clean_num(r.get(n), 0 if n in "wh" else -10000) for n in ("x", "y", "w", "h")}
        for k in POINT_ANCHORS:
            p = (a or {}).get(k)
            if isinstance(p, dict):
                out[k] = {n: self.clean_num(p.get(n)) for n in ("x", "y")}
        tl = (a or {}).get("tools")
        if isinstance(tl, list):
            out["tools"] = [{n: self.clean_num(t.get(n)) for n in ("x", "y")} if isinstance(t, dict) else None for t in tl[:16]]
        return out

    def save_pack(self, req):
        kind, pid = req.get("kind"), str(req.get("id") or "")
        if kind not in BUILTIN_PACKS or not PACK_ID.fullmatch(pid) or pid in [p["id"] for p in BUILTIN_PACKS[kind]]:
            return self.jreply(400, {"error": "bad pack id (use a-z, 0-9, - and _; built-in ids are reserved)"})
        d = os.path.join(self.state, "packs", kind, pid)
        old = load_json(os.path.join(d, "pack.json"), {})
        pack = {"label": (str(req.get("label") or pid).strip()[:60] or pid), "file": old.get("file")}
        f = req.get("file")
        if isinstance(f, dict):
            m = re.fullmatch(r"data:([\w/+.\-]+);base64,([A-Za-z0-9+/=\s]+)", str(f.get("data") or ""))
            ext = MIME_EXT.get(m.group(1)) if m else None
            if not ext or (kind == "toolchanger" and ext != ".svg"):
                return self.jreply(400, {"error": "unsupported image type" + (" (slot graphics must be SVG)" if kind == "toolchanger" else "")})
            try:
                raw = base64.b64decode(m.group(2), validate=False)
            except Exception:  # noqa: BLE001
                return self.jreply(400, {"error": "bad image data"})
            if not raw or len(raw) > MAX_PACK_IMAGE:
                return self.jreply(413, {"error": "image too large (3 MB max)"})
            if ext == ".svg" and not re.search(rb"<svg[\s>]", raw[:4096]):
                return self.jreply(400, {"error": "not an SVG file"})
            pack["file"] = "image" + ext if kind == "printer" else "slot.svg"
        if not pack["file"]:
            return self.jreply(400, {"error": "a graphic is required"})
        try:
            if kind == "printer":
                pack["width"], pack["height"] = int(self.clean_num(req.get("width"), 20, 8000)), int(self.clean_num(req.get("height"), 20, 8000))
                pack["anchors"] = self.clean_anchors(req.get("anchors"))
            else:
                pack["layout"] = {"columns": None}
        except ValueError:
            return self.jreply(400, {"error": "bad dimensions or anchors"})
        os.makedirs(d, exist_ok=True)
        if isinstance(f, dict):
            for fn in os.listdir(d):
                if fn.startswith("image") or fn == "slot.svg":
                    os.remove(os.path.join(d, fn))
            with open(os.path.join(d, pack["file"]), "wb") as fh:
                fh.write(raw)
        write_json_atomic(os.path.join(d, "pack.json"), pack)
        logging.info("saved %s pack %s", kind, pid)
        self.jreply(200, {"ok": True, "config": self.public_config()})

    def delete_pack(self, req):
        kind, pid = req.get("kind"), str(req.get("id") or "")
        d = os.path.join(self.state, "packs", str(kind), pid)
        if kind not in BUILTIN_PACKS or not PACK_ID.fullmatch(pid) or not os.path.isdir(d):
            return self.jreply(404, {"error": "no such custom pack"})
        for fn in os.listdir(d):
            os.remove(os.path.join(d, fn))
        os.rmdir(d)
        st = self.settings()
        if (st.get("graphics") or {}).get(kind) == pid:
            st["graphics"].pop(kind)
            write_json_atomic(self.settings_path(), st)
        self.jreply(200, {"ok": True, "config": self.public_config()})

    def serve_pack(self, route):
        m = re.fullmatch(r"/packs/(printer|toolchanger)/([a-z0-9][a-z0-9_\-]{0,39})/(pack\.json|image\.[a-z]{3,4}|slot\.svg)", route)
        if not m:
            return self.reply(404, "text/plain", b"not found")
        p = os.path.join(self.state, "packs", m.group(1), m.group(2), m.group(3))
        try:
            with open(p, "rb") as fh:
                body = fh.read()
        except OSError:
            return self.reply(404, "text/plain", b"not found")
        ctype = "application/json" if p.endswith(".json") else EXT_MIME.get(os.path.splitext(p)[1], "application/octet-stream")
        # uploaded SVGs are untrusted: render them sandboxed so scripts inside can never run on this origin
        self.reply(200, ctype, body, {"Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:",
                                      "X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"})

    def serve_builtin_graphic(self, route):
        m = re.fullmatch(r"/graphics/([\w\-]+\.(?:svg|png))", route)
        if not m:
            return self.reply(404, "text/plain", b"not found")
        try:
            with open(os.path.join(self.root, "graphics", m.group(1)), "rb") as fh:
                body = fh.read()
        except OSError:
            return self.reply(404, "text/plain", b"not found")
        self.reply(200, "image/svg+xml" if m.group(1).endswith(".svg") else "image/png", body,
                   {"Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'", "Cache-Control": "no-cache"})

    # POST /control: the method that handles each "action". Print controls, tool buttons and macros are handled in do_POST
    # itself, because they share the one-command-at-a-time lock and the "not while printing" rule.
    ROUTES = {"tune": "tune_control", "mesh_calibrate": "mesh_control", "gcode": "gcode_control", "fan": "fan_control", "restart": "restart_control", "tag_reread": "tag_control", "temp": "temp_control", "light": "light_control",
              "home": "move_control", "jog": "move_control", "goto": "move_control", "motors_off": "move_control",
              "set_lane": "lane_control", "set_slot": "box_slot_control", "save_config": "config_save",
              "save_settings": "save_settings", "save_pack": "save_pack", "delete_pack": "delete_pack",
              "add_printer": "add_printer", "remove_printer": "remove_printer", "set_embed": "set_embed", "set_spoolman": "set_spoolman"}

    def do_POST(self):
        if self.blocked():
            return
        if self.path.split("?")[0] != "/control":
            return self.reply(404, "text/plain", b"not found")
        if self.headers.get("X-Feedline-Control") != "1":      # blocks cross-site form posts
            return self.jreply(403, {"error": "missing control header"})
        self.select_printer()
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n < 0 or n > 6 * 1024 * 1024:
                return self.jreply(413, {"error": "request too large"})
            req = json.loads(self.rfile.read(n) or b"{}")
            action = req.get("action")
            if action in self.PRINT_ACTIONS:
                return self.print_control(action)
            if action in self.ROUTES:
                return getattr(self, self.ROUTES[action])(req)
            if action == "run_macro":
                cmd = self.macro_command(req)
            else:
                cmd = self.command_for(req.get("action"), req.get("tool"), req.get("lane"))
        except ValueError as e:
            return self.jreply(400, {"error": str(e)})
        except Exception:  # noqa: BLE001
            cmd = None
        if not cmd:
            return self.jreply(400, {"error": "bad request"})
        cmds = [cmd]
        if req.get("action") == "load" and self.provider() == "afc":
            # Load on a BoxTurtle lane swaps: if another lane is already loaded to the toolhead, unload it first
            try:
                cur = self.mget("/printer/objects/query?AFC=current_load")["result"]["status"]["AFC"].get("current_load")
                if isinstance(cur, str) and cur and cur != req.get("lane") and cur in self.afc_lanes():
                    pre = self.command_for("park", req.get("tool"), cur)
                    if pre:
                        cmds.insert(0, pre)
            except Exception:  # noqa: BLE001
                pass
        if not self.busy.acquire(blocking=False):
            return self.jreply(409, {"error": "another command is still running"})
        try:
            state_url = "http://%s:7125/printer/objects/query?print_stats" % self.printer
            with urllib.request.urlopen(state_url, timeout=8) as r:
                state = json.load(r)["result"]["status"]["print_stats"]["state"]
            if state in ("printing", "paused"):
                return self.jreply(409, {"error": "refused: printer is %s" % state})
            for c in cmds:                                   # one after the other; Moonraker answers when each has finished
                self.run_gcode(c, 900)
            self.jreply(200, {"ok": True, "cmd": " then ".join(cmds)})
        except Exception as e:  # noqa: BLE001
            self.jreply(502, {"error": str(e)})
        finally:
            self.busy.release()

    def do_GET(self):
        if self.blocked():
            return
        path = self.path
        route = path.split("?")[0]
        self.select_printer()
        if route == "/api/macros":
            return self.jreply(200, {"macros": self.user_macros(), "tool": self.effective_macros(), "detected": self.detect_macros(),
                                     "commands": sorted(c for c in self.commands() if not CMD_DENY.match(c) and not re.fullmatch(r"[GM]\d+", c)),
                                     "tools": self.tool_count()})
        if route == "/api/screen/probe":
            return self.jreply(200, self.screen_probe())
        if route == "/api/mesh/plan":       # what Calibrate would do right now (read-only)
            return self.mesh_plan()
        if route == "/api/events":          # print events for the Android app: ?since=<id of the last one seen>&wait=<seconds to wait for a new one>
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            try:
                since = int(q["since"][0]) if "since" in q else None
                wait = min(25, max(0, int(q.get("wait", ["0"])[0])))
            except ValueError:
                return self.jreply(400, {"error": "bad request"})
            return self.jreply(200, EVENTS.since(since, wait))
        if route == "/api/overview":
            return self.jreply(200, self.overview())
        if route == "/api/printers":
            return self.jreply(200, {"printers": self.printer_list()})
        if route == "/config/list":
            url = "http://%s:7125/server/files/list?root=config" % self.printer
        elif route == "/logs/export":
            return self.logs_export(urllib.parse.urlparse(path).query)
        elif route == "/config/read":
            return self.config_read(urllib.parse.urlparse(path).query)
        elif route in ("/", "/index.html"):
            with open(os.path.join(self.root, "index.html"), "rb") as f:
                return self.reply(200, "text/html; charset=utf-8", f.read())
        elif route == "/manifest.webmanifest":              # makes the page installable as an app (Chrome / Android "Install app")
            return self.reply(200, "application/manifest+json", json.dumps(PWA_MANIFEST).encode(), {"Cache-Control": "no-cache"})
        elif route == "/sw.js":
            return self.reply(200, "text/javascript; charset=utf-8", PWA_SW.encode(), {"Cache-Control": "no-cache"})
        elif route in ("/printer/objects/query", "/printer/objects/list", "/server/gcode_store", "/server/files/metadata",
                     "/server/history/list", "/server/history/totals", "/server/spoolman/status"):            # job history (read-only)
            url = "http://%s:7125%s" % (self.printer, path)
        elif (re.fullmatch(r"/server/files/gcodes/(?:%s/)*\.thumbs/%s\.png" % (THUMB_SEG, THUMB_SEG), urllib.parse.unquote(route))
              and not any(s in (".", "..") for s in urllib.parse.unquote(route).split("/"))):      # no stepping out of the gcodes folder
            url = "http://%s:7125%s" % (self.printer, route)        # slicer thumbnail of the current job (read-only)
        elif route == "/api/config":
            return self.jreply(200, self.public_config())
        elif route.startswith("/packs/"):
            return self.serve_pack(route)
        elif route.startswith("/graphics/"):
            return self.serve_builtin_graphic(route)
        elif route in ("/cam/snapshot", "/webcam/snapshot.jpg"):          # one camera frame (the browser polls this)
            url = self.webcam().get("snapshot")
            if not url:
                return self.reply(404, "text/plain", b"no camera snapshot configured")
        elif route == "/screen/snapshot":                 # live view of the printer's touchscreen (view only)
            scr = self.screen_cfg()
            if not scr or not scr.get("snapshot"):
                return self.reply(404, "text/plain", b"no touchscreen configured")
            url = "http://%s%s" % (self.printer, scr["snapshot"])
        elif route == "/api/v1/spool" and self.spoolman_base():
            url = self.spoolman_base() + path
        else:
            return self.reply(404, "text/plain", b"not found")
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                self.reply(r.status, r.headers.get("Content-Type", "application/json"), r.read())
        except Exception as e:  # noqa: BLE001
            self.reply(502, "text/plain", str(e).encode())


# ---- print events: a background thread watches every printer's print state and records what changed, so a phone can be told ----
class Events:
    """The most recent print events, in memory. Ids are millisecond timestamps, so they keep growing across restarts."""
    def __init__(self, keep=100):
        self.cond, self.items, self.keep, self.last = threading.Condition(), [], keep, int(time.time() * 1000)

    def add(self, **ev):
        with self.cond:
            self.last = max(self.last + 1, int(time.time() * 1000))
            ev.update(id=self.last, time=time.time())
            self.items.append(ev)
            del self.items[:-self.keep]
            self.cond.notify_all()

    def since(self, last, wait=0):
        """Events newer than id `last`; waits up to `wait` seconds for one. Without `last` (a client's first call) none are
        returned, only the newest id, so a new client starts from now instead of replaying old events."""
        with self.cond:
            if last is not None and wait > 0 and not any(e["id"] > last for e in self.items):
                self.cond.wait(wait)
            return {"last": self.last, "events": [e for e in self.items if last is not None and e["id"] > last]}


EVENTS = Events()


def fmt_duration(sec):
    sec = int(sec or 0)
    h, m = sec // 3600, sec % 3600 // 60
    return ("%dh %02dm" % (h, m)) if h else ("%d min" % m)


def print_event(prev, cur, ps):
    """What to tell the user when a printer's print state goes from `prev` to `cur` (Moonraker's print_stats), or None.
    The first look at a printer (prev None) never produces an event."""
    if prev is None or prev == cur:
        return None
    fname = os.path.basename(str(ps.get("filename") or ""))
    msg = str(ps.get("message") or "").strip()[:200]
    if cur == "complete":
        return {"kind": "complete", "title": "Print finished", "text": (fname + (" in " + fmt_duration(ps.get("total_duration")) if ps.get("total_duration") else "")).strip() or "The print is done"}
    if cur == "error":
        return {"kind": "error", "title": "Print failed", "text": msg or fname or "The printer reported an error"}
    if cur == "cancelled":
        return {"kind": "cancelled", "title": "Print cancelled", "text": fname or "The print was cancelled"}
    if cur == "paused" and prev == "printing":
        return {"kind": "paused", "title": "Print paused", "text": msg or fname or "The print is paused"}
    return None


def watch_printers(interval=5):
    """Poll every printer's print state (read-only) and record changes in EVENTS. Never ends; never sends a command."""
    h = object.__new__(Handler)
    h.pid, h.headers = "main", {}
    seen, skip = {}, {}
    while True:
        try:
            for row in h.printer_list():
                now = time.time()
                if skip.get(row["id"], 0) > now:
                    continue
                try:
                    with urllib.request.urlopen("http://%s:7125/printer/objects/query?print_stats" % row["host"], timeout=4) as r:
                        ps = json.load(r)["result"]["status"]["print_stats"]
                except Exception:  # noqa: BLE001
                    skip[row["id"]] = now + 20            # printer off or unreachable: look again in a while
                    continue
                cur = ps.get("state")
                ev = print_event(seen.get(row["id"]), cur, ps)
                seen[row["id"]] = cur
                if ev:
                    EVENTS.add(printer=row["id"], name=row["name"], **ev)
        except Exception:  # noqa: BLE001
            logging.exception("print watcher")
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--bind", default="0.0.0.0", help="address to listen on (127.0.0.1 = this machine only, e.g. behind a reverse proxy)")
    ap.add_argument("--printer", help="Moonraker host (overrides config.json)")
    ap.add_argument("--spoolman", help="Spoolman host:port (overrides config.json)")
    ap.add_argument("--printers", help="more printers besides the main one, \"host, Name=host, ...\" (overrides \"printers\" in config.json)")
    ap.add_argument("--root", default="/opt/feedline", help="folder holding index.html and graphics/")
    ap.add_argument("--config", help="admin config file (default: <root>/config.json)")
    ap.add_argument("--state", default="/var/lib/feedline", help="writable folder for settings.json, custom graphics and config backups")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = dict(DEFAULT_CFG)
    cfg.update(load_json(a.config or os.path.join(a.root, "config.json"), {}))
    Handler.printer = a.printer or cfg.get("printer")
    Handler.spoolman_cfg = a.spoolman if a.spoolman is not None else cfg.get("spoolman")
    Handler.spoolman = (load_json(os.path.join(a.state, "spoolman.json"), {}).get("url")) or Handler.spoolman_cfg      # Settings tab wins over config.json
    if not Handler.printer:
        ap.error("no printer: set \"printer\" in config.json or pass --printer HOST")
    Handler.cfg, Handler.root, Handler.state = cfg, a.root, a.state
    Handler.fixed_printers = parse_printers(a.printers if a.printers is not None else cfg.get("printers"), Handler.printer)
    for p in Handler.fixed_printers:
        logging.info("printer %s: %s (%s), set in config", p["id"], p["name"], p["host"])
    Handler.backups = os.path.join(a.state, "backups")
    if cfg.get("events", True):
        threading.Thread(target=watch_printers, daemon=True).start()
    logging.info("Feedline listening on %s:%d for printer %s", a.bind, a.port, Handler.printer)
    http.server.ThreadingHTTPServer((a.bind, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
