"""Regression tests with extracted upstream engine/controller and simulated touches.

Set PYLA_SOURCE to the PylaAI source tree. No game, OpenCV or emulator is needed.
"""
import ast
import math
import os
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
import unittest

from test_playstyle import CODE, load_upstream_interpreter

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = os.environ.get("PYLA_SOURCE")


class Clock:
    def __init__(self):
        self.now = 100.0
    def time(self):
        return self.now
    def perf_counter(self):
        return self.now
    def sleep(self, seconds):
        pass


class Logger:
    enabled = False
    active = False
    def log_frame(self, **kwargs):
        pass
    def log(self, *args, **kwargs):
        pass


def extract_class(path, name, methods, namespace):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    selected = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in methods]
    if len(selected) != len(methods):
        raise AssertionError("Missing upstream methods: " + str(methods))
    cls = ast.ClassDef(name=name, bases=[], keywords=[], body=selected, decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[name]


@unittest.skipUnless(UPSTREAM, "set PYLA_SOURCE to check the engine patch")
class RespawnRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        target = Path(cls.tmp.name)
        shutil.copy(Path(UPSTREAM) / "play.py", target / "play.py")
        patch = ROOT / "patches/pylaai-respawn-recovery.patch"
        subprocess.run(["git", "apply", "--check", str(patch)], cwd=target, check=True)
        subprocess.run(["git", "apply", str(patch)], cwd=target, check=True)
        cls.clock = Clock()
        ns = {"math": math, "random": random, "time": cls.clock, "JOYSTICK_RADIUS": 100,
              "brawl_stars_width": 1920, "brawl_stars_height": 1080}
        cls.Play = extract_class(target / "play.py", "Play", {
            "main", "loop", "validate_game_data", "reset_movement_input", "movement_to_vector",
            "clamp_movement", "unstuck_movement_if_needed", "movement_direction_key", "rotate_movement"
        }, ns)
        cls.Controller = extract_class(Path(UPSTREAM) / "window_controller.py", "WindowController",
                                       {"move", "release_movement"}, ns)
        cls.interpret = staticmethod(load_upstream_interpreter())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.clock.now = 100.0
        self.events = []
        self.touch_active = False
        c = self.Controller()
        c.are_we_moving = False
        c.original_movement_joystick = (180, 900)
        c.movement_joystick_x = 180
        c.movement_joystick_y = 900
        c.width_ratio = c.height_ratio = c.scale_factor = 1
        c.PID_JOYSTICK = 1
        c.last_joystick_pos = (None, None)
        c.re_apply_movement = True
        def down(x, y, pointer_id):
            self.touch_active = True
            self.events.append("down")
        def move(x, y, pointer_id):
            self.events.append("move" if self.touch_active else "stale_move")
        def up(x, y, pointer_id):
            self.touch_active = False
            self.events.append("up")
        c.touch_down, c.touch_move, c.touch_up = down, move, up
        self.controller = c

        p = self.Play()
        p.window_controller = c
        p.match_logger = Logger()
        p.time_since_walls_checked = 100
        p.walls_treshold = 999
        p.last_walls_data = p.last_bushes_data = p.last_water_data = p.last_rope_fence_data = []
        p.time_since_player_last_found = 100
        p._player_detection_missing = p._missing_input_reset = False
        p._alone_next_input_refresh = 0.0
        p.time_since_last_proceeding = 100
        p.no_detection_proceed_delay = 8
        p.persistent_data = {"time_since_holding_attack": None}
        p.last_movement = ''
        p.last_movement_change_time = 0
        p.minimum_movement_delay = 0.0
        p.time_since_different_movement = 100
        p.fix_movement_keys = {"delay_to_trigger": 2.4, "duration": 1.4, "toggled": False,
                               "started_at": 100, "fixed": (0, 0), "last_direction_key": None,
                               "rotation_sign": 1, "rotation_angle_step": 1, "max_rotation_angle_step": 4}
        p.time_since_hypercharge_checked = p.time_since_gadget_checked = p.time_since_super_checked = 100
        p.hypercharge_treshold = p.gadget_treshold = p.super_treshold = 999
        p.is_super_ready = p.is_gadget_ready = p.is_hypercharge_ready = False
        p.TILE_SIZE = 54
        p.seconds_to_hold_attack_after_reaching_max = 1.5
        p.verbose_debug = False
        p.brawlers_info = {"test": {"hold_attack": 0, "super_type": "damage"}}
        p.enemy_targets = lambda data: list(data.get("enemy", [])) + list(data.get("enemy_spawnable", []))
        p.get_entity_pos = lambda box: ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        p.get_brawler_range = lambda name: (200, 400, 300)
        p.must_brawler_hold_attack = lambda name, info: False
        p.is_path_blocked = lambda player, move, walls, distance=None: False
        p.is_enemy_hittable = lambda *args: True
        p.find_closest_enemy = lambda *args: (None, None)
        for name in ("get_distance", "get_actual_player_box", "is_there_enemy", "is_there_enemy_spawnable",
                     "find_closest_enemy_spawnable", "is_enemy_spawnable", "use_hypercharge", "use_super", "use_gadget",
                     "get_random_movement", "find_closest_teammate", "is_there_poison_gas", "normalize_move"):
            setattr(p, name, lambda *args, **kwargs: None)
        self.attacks = []
        p.attack = lambda **kwargs: self.attacks.append(kwargs)
        p.get_movement = lambda: self.interpret(CODE, p.context)[0]
        p.do_movement = lambda movement: c.move(*movement)
        self.data = {"player": [[0, 0, 10, 10]], "enemy": [], "teammate": []}
        p.get_main_data = lambda frame: dict(self.data)
        p.track_no_detections = lambda data: None
        p.publish_debug_view = lambda *args: None
        self.manager = type("Main", (), {"get_latest_state": lambda self: "match"})()
        self.play = p

    def tick(self, elapsed=0):
        self.clock.now += elapsed
        self.play.main(None, "test", self.manager)

    def test_respawn_after_missing_player_moves_without_other_entities(self):
        self.tick()
        self.assertEqual(self.events, ["down", "move"])
        self.touch_active = False  # Game cancelled the joystick after death.
        self.data = {"enemy": [], "teammate": []}
        self.tick(.4)
        self.assertFalse(self.controller.are_we_moving)
        self.data = {"player": [[0, 0, 10, 10]], "enemy": [], "teammate": []}
        self.tick(.1)
        self.assertEqual(self.events, ["down", "move", "up", "down", "move"])
        self.assertTrue(self.touch_active)
        self.assertLess(self.play.last_movement[1], 0)

    def test_alone_heartbeat_recovers_if_detection_never_disappears(self):
        self.tick()
        self.touch_active = False  # Engine still thinks the joystick is held.
        self.tick(.5)
        self.assertEqual(self.events[-1], "stale_move")
        self.tick(1.6)
        self.assertEqual(self.events[-3:], ["up", "down", "move"])
        self.assertTrue(self.touch_active)
        self.assertLess(self.play.last_movement[1], 0)

    def test_heartbeat_does_not_disable_engine_unstuck(self):
        self.tick()
        self.tick(2.1)  # Empty-field joystick refresh.
        self.assertFalse(self.play.fix_movement_keys["toggled"])
        self.tick(.4)  # Same direction for > 2.4 s triggers upstream unstuck.
        self.assertTrue(self.play.fix_movement_keys["toggled"])

    def test_short_detection_gap_does_not_repress_joystick(self):
        self.tick()
        self.data = {"player": []}
        self.tick(.1)
        self.data = {"player": [[0, 0, 10, 10]], "enemy": [], "teammate": []}
        self.tick(.1)
        self.assertNotIn("up", self.events)
        self.assertEqual(self.events.count("down"), 1)

    def test_reacquire_after_medium_gap_resets_before_move(self):
        self.tick()
        self.data = {"player": []}
        self.tick(.1)
        self.data = {"player": [[0, 0, 10, 10]], "enemy": [], "teammate": []}
        self.tick(.2)
        self.assertEqual(self.events[-3:], ["up", "down", "move"])

    def test_lost_held_attack_released_once(self):
        self.tick()
        self.play.persistent_data["time_since_holding_attack"] = 100
        self.data = {"player": []}
        self.tick(.4)
        self.tick(.2)
        self.assertEqual(self.attacks, [{"touch_up": True, "touch_down": False}])
        self.assertIsNone(self.play.persistent_data["time_since_holding_attack"])

    def test_alone_rearming_does_not_happen_when_teammate_visible(self):
        self.data["teammate"] = [[100, 100, 110, 110]]
        self.tick()
        self.tick(2.1)
        self.tick(2.1)
        self.assertNotIn("up", self.events)
        self.assertEqual(self.events.count("down"), 1)


if __name__ == "__main__":
    unittest.main()
