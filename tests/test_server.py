"""Tests for the parts of feedline_server.py that decide things without talking to a printer.

Run from the repository root:   python -m unittest discover -s tests -v
Standard library only. Printer answers are faked; nothing here opens a network connection.
"""
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("feedline_server", os.path.join(HERE, "..", "feedline_server.py"))
fs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fs)


def handler(objects=(), commands=(), settings=None, cfg=None, status=None):
    """A Handler that answers from the given fake printer instead of the network."""
    class Fake(fs.Handler):
        def objects(self):
            return list(objects)

        def commands(self):
            return {c.upper() for c in commands}

        def macro_info(self):
            return {}

        def settings(self):
            return dict(settings or {})

        def mget(self, path, timeout=8):
            return {"result": {"status": status or {}}}

    h = object.__new__(Fake)
    h.pid, h.printer = "test", "printer.test"
    h.cfg = dict(fs.DEFAULT_CFG, **(cfg or {}))
    return h


def klipper(kinematics, x=0, **sections):
    return dict({"printer": {"kinematics": kinematics}, "stepper_x": {"position_max": x}}, **sections)


class GuessModel(unittest.TestCase):
    def guess(self, cfg, objs=(), **info):
        return fs.guess_model(cfg, list(objs), info)[0]

    def test_qidi_by_bed_size(self):
        qidi = dict(klipper_path="/home/mks/klipper")
        self.assertEqual(self.guess(klipper("corexy", 307, chamber_fan={}), **qidi), "qidi-plus4")
        self.assertEqual(self.guess(klipper("corexy", 325, chamber_fan={}), **qidi), "qidi-xmax3")
        self.assertEqual(self.guess(klipper("corexy", 245, chamber_fan={}), **qidi), "qidi-q1pro")

    def test_voron_family(self):
        pi = dict(klipper_path="/home/pi/klipper")
        self.assertEqual(self.guess(klipper("corexy", 300, quad_gantry_level={}), **pi), "voron-24")
        self.assertEqual(self.guess(klipper("corexy", 300, z_tilt={}, stepper_z2={}), **pi), "voron-trident")
        self.assertEqual(self.guess(klipper("corexy", 120), **pi), "voron-v0")
        self.assertEqual(self.guess(klipper("corexz", 250), **pi), "voron-switchwire")

    def test_other_brands(self):
        self.assertEqual(self.guess(klipper("corexy", 300, prtouch_v2={}), klipper_path="/usr/share/klipper"), "creality-k1max")
        self.assertEqual(self.guess(klipper("cartesian", 220), klipper_path="/usr/share/klipper"), "creality-ender3")
        self.assertEqual(self.guess(klipper("cartesian", 235), klipper_path="/home/mks/klipper"), "elegoo-neptune4")
        self.assertEqual(self.guess(klipper("corexy", 350, quad_gantry_level={}), klipper_path="/home/sovol/klipper"), "sovol-sv08")
        self.assertEqual(self.guess(klipper("delta"), hostname="flsun-t1"), "flsun-delta")

    def test_brand_neutral_fallbacks(self):
        self.assertEqual(self.guess(klipper("delta")), "delta")
        self.assertEqual(self.guess(klipper("cartesian", 235), klipper_path="/home/pi/klipper"), "bedslinger")
        self.assertEqual(self.guess(klipper("corexy", 250), ["temperature_sensor chamber"], klipper_path="/home/pi/klipper"), "corexy-box")
        self.assertEqual(self.guess(klipper("corexy", 250), klipper_path="/home/pi/klipper"), "corexy-frame")

    def test_u1_and_unknown(self):
        self.assertEqual(self.guess(klipper("corexy", 270), ["filament_detect"]), "u1")
        self.assertIsNone(self.guess({}))

    def test_every_model_is_a_selectable_graphic(self):
        ids = [p["id"] for p in fs.BUILTIN_PACKS["printer"]]
        self.assertEqual(len(ids), len(set(ids)))
        for mid, _ in fs.MODELS:
            self.assertIn(mid, ids)
            self.assertTrue(fs.PACK_ID.fullmatch(mid))


class ThumbPath(unittest.TestCase):
    def thumbs(self, *items):
        return [{"relative_path": p, "width": w} for p, w in items]

    def test_largest_relative_png(self):
        t = self.thumbs(("/abs/x.png", 400), (".thumbs/b-300x300.png", 300), (".thumbs/b-32x32.png", 32), (".thumbs/b.jpg", 500))
        self.assertEqual(fs.thumb_path("a/b.gcode", t), "/server/files/gcodes/a/.thumbs/b-300x300.png")

    def test_rejects_paths_that_leave_the_folder(self):
        self.assertIsNone(fs.thumb_path("b.gcode", self.thumbs(("../x.png", 300), ("..\\y.png", 300), ("/etc/z.png", 300))))
        self.assertIsNone(fs.thumb_path("b.gcode", None))

    def test_names_are_percent_encoded(self):
        self.assertEqual(fs.thumb_path("[a] b.gcode", self.thumbs((".thumbs/[a] b-96x96.png", 96))),
                         "/server/files/gcodes/.thumbs/%5Ba%5D%20b-96x96.png")


class Parsing(unittest.TestCase):
    def test_spoolman_addresses(self):
        p = fs.Handler.parse_spoolman
        self.assertEqual(p("192.168.1.20:7912"), ("http", "192.168.1.20:7912", "192.168.1.20", ""))
        self.assertEqual(p("https://spoolman.lan/prefix/"), ("https", "spoolman.lan", "spoolman.lan", "/prefix"))
        for bad in ("", "ftp://host", "http://user:pw@host", "http://host/?q=1", "http://host/a/../b", "http://ho st"):
            with self.assertRaises(ValueError):
                p(bad)

    def test_config_paths(self):
        s = fs.Handler.safe_path
        self.assertEqual(s("printer.cfg"), "printer.cfg")
        self.assertEqual(s("macros/tool (1).cfg"), "macros/tool (1).cfg")
        for bad in ("", "../printer.cfg", "a/../b.cfg", ".hidden", "a/.hidden", "a//b", "a\\b", "x" * 201, "a;b"):
            self.assertIsNone(s(bad), bad)

    def test_camera_view(self):
        c = fs.Handler.clean_view
        self.assertEqual(c({"rotation": 90, "flip_horizontal": True}), {"rotation": 90, "flipH": True, "flipV": False})
        self.assertEqual(c({"rotation": 45}), {"rotation": 0, "flipH": False, "flipV": False})
        self.assertEqual(c({"rotation": "x", "flipV": 1}), {"rotation": 0, "flipH": False, "flipV": True})

    def test_numbers_for_anchors(self):
        n = fs.Handler.clean_num
        self.assertEqual(n(12.345), 12.35)
        for bad in (True, "1", float("nan"), 1e9, None):
            with self.assertRaises(ValueError):
                n(bad)


class Providers(unittest.TestCase):
    TC = ["toolchanger", "tool T0", "tool T1", "tool T2", "extruder", "extruder1", "extruder2"]
    BOX = ["box_stepper slot0", "box_stepper slot1", "box_stepper slot2", "box_stepper slot3", "extruder"]

    def test_auto_detection_order(self):
        self.assertEqual(handler(["filament_detect", "AFC", "AFC_stepper lane1"]).provider(), "u1")
        self.assertEqual(handler(["AFC", "AFC_stepper lane1"] + self.BOX).provider(), "afc")
        self.assertEqual(handler(self.BOX + self.TC).provider(), "qidibox")
        self.assertEqual(handler(self.TC).provider(), "toolchanger")
        self.assertEqual(handler(["extruder"]).provider(), "extruders")

    def test_a_forced_provider_needs_its_hardware(self):
        self.assertEqual(handler(["extruder"], settings={"provider": "toolchanger"}).provider(), "extruders")
        self.assertEqual(handler(["extruder"], settings={"provider": "qidibox"}).provider(), "extruders")
        self.assertEqual(handler(self.TC, settings={"provider": "extruders"}).provider(), "extruders")

    def test_qidi_box_buttons_send_what_qidis_fluidd_sends(self):
        # E_LOAD / E_UNLOAD are not in Klipper's command list (no description), so they must not depend on it
        for commands in ([], ["LOAD_T0", "LOAD_T1", "LOAD_T2", "LOAD_T3", "ULOAD_T0", "T0", "UNLOAD_T0"]):
            h = handler(self.BOX, commands)
            self.assertEqual((h.tool_count(), h.slot_count()), (1, 4))
            self.assertEqual(h.command_for("load", 3), "E_LOAD slot=3")
            self.assertEqual(h.command_for("unload", 0), "E_UNLOAD slot=0")
            self.assertIsNone(h.command_for("load", 4))
            self.assertEqual(h.command_for("park", 2), "E_BOX slot=2")      # the Eject button
            self.assertIsNone(h.command_for("pickup", 0))

    def test_qidi_box_buttons_can_be_overridden(self):
        h = handler(self.BOX, ["ULOAD_T0", "ULOAD_T1", "ULOAD_T2", "ULOAD_T3"], cfg={"macros": {"unload": "ULOAD_T{n}"}})
        self.assertEqual((h.command_for("load", 1), h.command_for("unload", 1)), ("E_LOAD slot=1", "ULOAD_T1"))

    def test_setting_a_qidi_box_slot(self):
        h = handler(self.BOX)
        h.box_table = lambda: {"fila": {1: "PLA Rapido", 18: "ASA"}, "color": {1: "#FAFAFA", 2: "#060606"}, "vendor": {0: "Generic", 1: "QIDI"}}
        self.assertEqual(h.slot_script({"slot": 2, "filament": 18, "color": 2, "vendor": 0}),
                         "SAVE_VARIABLE VARIABLE=filament_slot2 VALUE=18\nSAVE_VARIABLE VARIABLE=color_slot2 VALUE=2\nSAVE_VARIABLE VARIABLE=vendor_slot2 VALUE=0")
        self.assertEqual(h.slot_script({"slot": 16, "color": 1}), "SAVE_VARIABLE VARIABLE=color_slot16 VALUE=1")      # the external spool
        for bad in ({"slot": 4, "filament": 1}, {"slot": 2}, {"slot": 2, "filament": 99}, {"slot": 2, "color": "2"}, {"slot": 2, "vendor": True},
                    {"slot": "2", "filament": 1}, {"slot": 2, "filament": "1; M112"}):
            with self.assertRaises(ValueError, msg=bad):
                h.slot_script(bad)
        with self.assertRaises(ValueError):
            handler(self.TC).slot_script({"slot": 0, "filament": 1})

    def test_toolchanger_buttons(self):
        h = handler(self.TC, ["T0", "T1", "T2", "SELECT_TOOL", "UNSELECT_TOOL"])
        self.assertEqual((h.command_for("pickup", 2), h.command_for("park", 1)), ("T2", "UNSELECT_TOOL"))
        h = handler(self.TC, ["SELECT_TOOL"])
        self.assertEqual((h.command_for("pickup", 2), h.command_for("park", 1)), ("SELECT_TOOL T=2", None))
        self.assertIsNone(h.command_for("pickup", 3))

    def test_tool_numbers_must_be_integers(self):
        h = handler(self.TC, ["T0", "T1", "T2"])
        for bad in (True, "1", 1.0, None, -1):
            self.assertIsNone(h.command_for("pickup", bad))
        self.assertIsNone(h.command_for("restart", 0))


class Tuning(unittest.TestCase):
    def tuner(self, z=0.0, homed="xyz", **cfg):
        return handler(cfg=cfg, status={"gcode_move": {"homing_origin": [0, 0, z, 0]}, "toolhead": {"homed_axes": homed}})

    def test_commands(self):
        h = self.tuner()
        cases = {("z_adjust", 0.025): "SET_GCODE_OFFSET Z_ADJUST=+0.025 MOVE=1", ("z_adjust", -0.005): "SET_GCODE_OFFSET Z_ADJUST=-0.005 MOVE=1",
                 ("z_reset", 0): "SET_GCODE_OFFSET Z=0 MOVE=1", ("speed", 150): "M220 S150", ("flow", 97.6): "M221 S98",
                 ("pa", 0.045): "SET_PRESSURE_ADVANCE ADVANCE=0.0450", ("smooth", 0.03): "SET_PRESSURE_ADVANCE SMOOTH_TIME=0.0300"}
        for (what, value), cmd in cases.items():
            self.assertEqual(h.tune_command({"what": what, "value": value}), cmd)

    def test_out_of_range_and_non_numbers(self):
        h = self.tuner()
        for what, value in (("speed", 5), ("speed", 301), ("flow", 20), ("pa", -1), ("pa", 3), ("smooth", 0.5), ("z_adjust", 0.03), ("z_adjust", 5),
                            ("speed", "abc"), ("speed", float("nan")), ("speed", True), ("gcode", 1), ("z_adjust", "0.01; M112")):
            with self.assertRaises(ValueError, msg=(what, value)):
                h.tune_command({"what": what, "value": value})

    def test_z_offset_limit(self):
        with self.assertRaises(PermissionError):
            self.tuner(z=0.98).tune_command({"what": "z_adjust", "value": 0.05})
        self.assertEqual(self.tuner(z=0.98).tune_command({"what": "z_adjust", "value": -0.05}), "SET_GCODE_OFFSET Z_ADJUST=-0.050 MOVE=1")
        with self.assertRaises(PermissionError):
            self.tuner(z=0.29, z_offset_limit=0.3).tune_command({"what": "z_adjust", "value": 0.025})

    def test_no_move_when_z_is_not_homed(self):
        self.assertEqual(self.tuner(homed="xy").tune_command({"what": "z_adjust", "value": 0.01}), "SET_GCODE_OFFSET Z_ADJUST=+0.010")


class Limits(unittest.TestCase):
    def test_heater_caps(self):
        h = handler(cfg={"temp_limits": {"extruder": 280, "heater_bed": 100, "heater_generic": 60}})
        self.assertEqual((h.temp_limit("extruder"), h.temp_limit("extruder3"), h.temp_limit("heater_bed"), h.temp_limit("heater_generic chamber")), (280, 280, 100, 60))
        for not_settable in ("temperature_sensor x", "heater_fan hotend", "extruder; M112", ""):
            self.assertIsNone(h.temp_limit(not_settable))

    def test_macro_parameters(self):
        h = handler(["gcode_macro CLEAN_NOZZLE", "gcode_macro _HIDDEN", "gcode_macro FIRMWARE_RESTART_NOW"])
        self.assertEqual(h.macro_command({"name": "CLEAN_NOZZLE", "params": {"TEMP": "240", "EMPTY": ""}}), "CLEAN_NOZZLE TEMP=240")
        for bad in ({"name": "_HIDDEN"}, {"name": "NOPE"}, {"name": "FIRMWARE_RESTART_NOW"},
                    {"name": "CLEAN_NOZZLE", "params": {"TEMP": "240; M112"}}, {"name": "CLEAN_NOZZLE", "params": {"bad key": "1"}}):
            with self.assertRaises(ValueError, msg=bad):
                h.macro_command(bad)


class PrintEvents(unittest.TestCase):
    def test_transitions(self):
        ps = {"filename": "gcodes/benchy.gcode", "total_duration": 8130, "message": "Heater extruder not heating"}
        ev = fs.print_event("printing", "complete", ps)
        self.assertEqual((ev["kind"], ev["title"], ev["text"]), ("complete", "Print finished", "benchy.gcode in 2h 15m"))
        self.assertEqual(fs.print_event("printing", "error", ps)["text"], "Heater extruder not heating")
        self.assertEqual(fs.print_event("paused", "cancelled", ps)["kind"], "cancelled")
        self.assertEqual(fs.print_event("printing", "paused", {})["kind"], "paused")

    def test_no_event(self):
        for prev, cur in ((None, "complete"), (None, "error"), ("complete", "complete"), ("standby", "printing"), ("paused", "printing"),
                          ("standby", "paused"), ("complete", "standby")):
            self.assertIsNone(fs.print_event(prev, cur, {}), (prev, cur))

    def test_queue_replays_only_newer_events(self):
        q = fs.Events()
        first = q.since(None)                                    # a client's first call: just the newest id
        self.assertEqual(first["events"], [])
        q.add(kind="complete", title="a")
        q.add(kind="error", title="b")
        got = q.since(first["last"])
        self.assertEqual([e["title"] for e in got["events"]], ["a", "b"])
        self.assertEqual(q.since(got["last"])["events"], [])
        self.assertTrue(got["events"][0]["id"] < got["events"][1]["id"])

    def test_queue_is_bounded(self):
        q = fs.Events(keep=3)
        for i in range(5):
            q.add(kind="x", title=str(i))
        self.assertEqual([e["title"] for e in q.since(0)["events"]], ["2", "3", "4"])


class MeshCalibrate(unittest.TestCase):
    def run_it(self, state="standby", homed="xyz", commands=("BED_MESH_CALIBRATE",), objs=("heater_bed",), bed=60, applied=True,
               klippy="ready", cfg=None, fail=None):
        sent = []

        class Fake(fs.Handler):
            def commands(self):
                return {c.upper() for c in commands}

            def objects(self):
                return list(objs)

            def mget(self, path, timeout=8):
                st = {"print_stats": {"state": state}, "toolhead": {"homed_axes": homed}, "webhooks": {"state": klippy},
                      "quad_gantry_level": {"applied": applied}, "z_tilt": {"applied": applied}}
                if "heater_bed" in objs:
                    st["heater_bed"] = {"target": bed}
                return {"result": {"status": st}}

            def run_gcode(self, script, timeout=15):
                sent.append(script)
                if fail and script.startswith(fail):
                    raise RuntimeError("boom")

            def jreply(self, code, obj):
                sent.append((code, obj))

        h = object.__new__(Fake)
        h.pid, h.printer, h.cfg = "test", "printer.test", dict(fs.DEFAULT_CFG, **(cfg or {}))
        h.mesh_control({})
        return sent

    def test_hot_bed_homed_just_probes(self):
        out = self.run_it(bed=65)
        self.assertEqual(out, ["BED_MESH_CALIBRATE", (200, {"ok": True, "steps": ["Probe the bed"]})])

    def test_cold_bed_is_heated_and_switched_off(self):
        out = self.run_it(bed=0)
        self.assertEqual(out[:3], ["M190 S60", "BED_MESH_CALIBRATE", "M140 S0"])

    def test_homes_first(self):
        self.assertEqual(self.run_it(homed="xy", bed=60)[:2], ["G28", "BED_MESH_CALIBRATE"])
        self.assertEqual(self.run_it(homed="", bed=60)[:2], ["G28", "BED_MESH_CALIBRATE"])

    def test_levels_the_gantry_when_needed(self):
        out = self.run_it(objs=("heater_bed", "quad_gantry_level"), applied=False, bed=60)
        self.assertEqual(out[:3], ["QUAD_GANTRY_LEVEL", "G28 Z", "BED_MESH_CALIBRATE"])
        self.assertEqual(self.run_it(objs=("heater_bed", "z_tilt"), applied=False, bed=60, homed="")[:4], ["G28", "Z_TILT_ADJUST", "G28 Z", "BED_MESH_CALIBRATE"])
        self.assertEqual(self.run_it(objs=("heater_bed", "quad_gantry_level"), applied=True, bed=60)[0], "BED_MESH_CALIBRATE")

    def test_heating_can_be_turned_off_and_is_capped(self):
        self.assertEqual(self.run_it(bed=0, cfg={"mesh_bed_temp": 0})[0], "BED_MESH_CALIBRATE")
        self.assertEqual(self.run_it(bed=0, cfg={"mesh_bed_temp": 400})[0], "M190 S110")
        self.assertEqual(self.run_it(objs=(), bed=0)[0], "BED_MESH_CALIBRATE")

    def test_refused(self):
        for kw in ({"state": "printing"}, {"state": "paused"}, {"commands": ()}, {"klippy": "shutdown"}, {"klippy": "startup"}):
            out = self.run_it(**kw)
            self.assertEqual(len(out), 1, kw)
            self.assertEqual(out[0][0], 409, kw)

    def test_failure_names_the_step_and_switches_the_heater_off(self):
        out = self.run_it(bed=0, fail="BED_MESH_CALIBRATE")
        self.assertEqual(out[:3], ["M190 S60", "BED_MESH_CALIBRATE", "M140 S0"])
        self.assertEqual(out[3][0], 502)
        self.assertIn("Probe the bed failed", out[3][1]["error"])
        self.assertEqual(self.run_it(bed=0, fail="M190")[-1][0], 502)            # heating itself failed: still switched off, still reported


class ConsoleGcode(unittest.TestCase):
    def send(self, script):
        sent = []

        class Fake(fs.Handler):
            def run_gcode(self, s, timeout=15):
                sent.append(s)

            def jreply(self, code, obj):
                sent.append(code)

        h = object.__new__(Fake)
        h.pid, h.printer, h.cfg = "test", "printer.test", dict(fs.DEFAULT_CFG)
        h.gcode_control({"script": script})
        return sent

    def test_sends_a_line(self):
        self.assertEqual(self.send("  M115 "), ["M115", 200])
        self.assertEqual(self.send("SET_PIN PIN=led VALUE=1"), ["SET_PIN PIN=led VALUE=1", 200])

    def test_refuses_dangerous_and_bad_input(self):
        for bad in ("M112", "m112", "RESTART", "FIRMWARE_RESTART", "SAVE_CONFIG", "SHUTDOWN_NOW", "EMERGENCY_STOP", "FLASH_X", "RUN_SHELL_COMMAND CMD=x",
                    "M115\nRESTART", "", "   ", "x" * 501):
            out = self.send(bad)
            self.assertEqual(len(out), 1, bad)
            self.assertIn(out[0], (400, 403), bad)
        self.assertEqual(self.send(None), [400])


class TagReread(unittest.TestCase):
    def send(self, req, objs=("filament_detect",)):
        sent = []

        class Fake(fs.Handler):
            def objects(self):
                return set(objs)

            def run_gcode(self, s, timeout=15):
                sent.append(s)

            def jreply(self, code, obj):
                sent.append(code)

        h = object.__new__(Fake)
        h.pid, h.printer, h.cfg = "test", "printer.test", dict(fs.DEFAULT_CFG)
        h.tag_control(req)
        return sent

    def test_rereads_a_channel(self):
        self.assertEqual(self.send({"channel": 2}), ["FILAMENT_DT_UPDATE CHANNEL=2", 200])
        self.assertEqual(self.send({"channel": "0"}), ["FILAMENT_DT_UPDATE CHANNEL=0", 200])

    def test_refuses_bad_channel_or_printer(self):
        for bad in ({"channel": 4}, {"channel": -1}, {"channel": "x"}, {}, {"channel": "1;M112"}):
            self.assertEqual(self.send(bad), [400], bad)
        self.assertEqual(self.send({"channel": 1}, objs=()), [400])


if __name__ == "__main__":
    unittest.main()
