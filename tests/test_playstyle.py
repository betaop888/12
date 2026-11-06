"""Offline contract/scenario tests. No emulator or third-party packages needed."""
import ast
import json
import math
import os
from pathlib import Path
import random
import time
import traceback
import unittest

ROOT = Path(__file__).resolve().parents[1]
header, CODE = (ROOT / "default_up.pyla").read_text(encoding="utf-8").split("\n", 1)


def load_upstream_interpreter():
    path = os.environ.get("PYLA_SOURCE")
    if not path:
        return None
    tree = ast.parse((Path(path) / "utils.py").read_text(encoding="utf-8"))
    nodes = [node for node in tree.body
             if (isinstance(node, ast.FunctionDef) and node.name in ("is_safe_ast", "interpret_pyla_code"))
             or (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "SAFE_GLOBALS" for t in node.targets))]
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    namespace = {"ast": ast, "math": math, "random": random, "time": time, "traceback": traceback}
    exec(compile(module, "upstream-utils.py", "exec"), namespace)
    return namespace["interpret_pyla_code"]


UPSTREAM_INTERPRETER = load_upstream_interpreter()


def box(x, y):
    return [x - 5, y - 5, x + 5, y + 5]


class Clock:
    def __init__(self):
        self.now = 100.0
    def time(self):
        return self.now


class Scenario:
    def __init__(self):
        self.clock = Clock()
        self.actions = []
        self.state = {"time_since_holding_attack": None}
        self.player = box(0, 0)
        self.enemies = []
        self.summons = []
        self.teammates = []
        self.blocked = lambda move: False
        self.short_blocked = None
        self.visible = lambda pos, skill: True
        self.hold = 0
        self.safe, self.attack_range, self.super_range = 200, 400, 300
        self.super_type = "damage"
        self.info_override = None
        self.super_ready = self.gadget_ready = self.hyper_ready = False

    def run(self):
        def pos(b):
            return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
        def closest(boxes, player, walls, skill):
            if not boxes:
                return None, None
            p = pos(boxes[0])
            return p, math.hypot(p[0] - player[0], p[1] - player[1])
        def attack(touch_up=True, touch_down=True):
            self.actions.append(("attack", touch_up, touch_down))
        def action(name):
            return lambda: self.actions.append((name,))
        # Mirrors upstream SAFE_GLOBALS and its empty builtins, rather than
        # accidentally letting the playstyle use the test runner's builtins.
        scope = {"__builtins__": {}, "math": math, "random": random,
                 "abs": abs, "min": min, "max": max, "sum": sum,
                 "round": round, "len": len, "range": range, "zip": zip,
                 "map": map, "int": int, "float": float, "str": str,
                 "print": print, "time": self.clock,
                 "player_data": self.player, "enemy_data": self.enemies + self.summons,
                 "enemy_brawler_data": self.enemies, "enemy_spawnable_data": self.summons,
                 "teammate_data": self.teammates, "walls": [], "brawler": "test",
                 "brawlers_info": {"test": self.info_override or {"hold_attack": self.hold, "super_type": self.super_type}},
                 "get_entity_pos": pos, "get_brawler_range": lambda b: (self.safe, self.attack_range, self.super_range),
                 "must_brawler_hold_attack": lambda b, info: self.hold > 0,
                 "find_closest_enemy": closest, "is_enemy_hittable": lambda p, t, w, s: self.visible(t, s),
                 "is_path_blocked": lambda p, m, w, distance=None: (self.short_blocked(m) if distance is not None and self.short_blocked is not None else self.blocked(m)),
                 "attack": attack, "use_gadget": action("gadget"), "use_super": action("super"),
                 "use_hypercharge": action("hyper"), "is_super_ready": self.super_ready,
                 "is_gadget_ready": self.gadget_ready, "is_hypercharge_ready": self.hyper_ready,
                 "JOYSTICK_RADIUS": 100, "TILE_SIZE": 54, "persistent_data": self.state}
        if UPSTREAM_INTERPRETER:
            movement, scope = UPSTREAM_INTERPRETER(CODE, scope)
            if movement is None:
                raise AssertionError("Upstream interpreter returned no movement")
            return movement
        exec(compile(CODE, "default_up.pyla", "exec"), scope)
        return scope["movement"]


class PlaystyleTests(unittest.TestCase):
    def setUp(self):
        self.s = Scenario()

    def test_header_and_upstream_security_contract(self):
        self.assertEqual(json.loads(header)["gamemodes"], ["3v3", "5v5"])
        tree = ast.parse(CODE)
        for n in ast.walk(tree):
            self.assertNotIsInstance(n, (ast.Import, ast.ImportFrom))
            if isinstance(n, ast.Attribute):
                self.assertFalse(n.attr.startswith("_"))
            if isinstance(n, ast.Name):
                self.assertNotIn(n.id, {"exec", "eval", "compile", "getattr", "setattr", "delattr", "__import__", "open", "globals", "locals", "vars", "breakpoint"})
        upstream = os.environ.get("PYLA_SOURCE")
        if upstream:
            source = (Path(upstream) / "utils.py").read_text(encoding="utf-8")
            mod = ast.parse(source)
            fn = next(n for n in mod.body if isinstance(n, ast.FunctionDef) and n.name == "is_safe_ast")
            module = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
            scope = {"ast": ast}
            exec(compile(module, "upstream-utils.py", "exec"), scope)
            self.assertEqual(scope["is_safe_ast"](CODE), (True, None))

    def test_empty_field_advances_up_without_actions(self):
        x, y = self.s.run()
        self.assertAlmostEqual(x, 0)
        self.assertLess(y, 0)
        self.assertEqual(self.s.actions, [])

    def test_regroups_with_distant_teammate_on_our_side(self):
        self.s.teammates = [box(0, 300)]
        x, y = self.s.run()
        self.assertAlmostEqual(x, 0)
        self.assertGreater(y, 0)
        self.assertEqual(self.s.actions, [])

    def test_does_not_chase_nearby_teammate(self):
        self.s.teammates = [box(0, 80)]
        x, y = self.s.run()
        self.assertLess(y, 0)

    def test_rear_teammate_takes_priority_over_stale_enemy(self):
        self.s.enemies = [box(0, -300)]
        self.s.run()
        self.s.enemies = []
        self.s.teammates = [box(0, 300)]
        self.s.clock.now += .1
        self.assertGreater(self.s.run()[1], 0)

    def test_recent_enemy_heading_is_short_lived(self):
        self.s.enemies = [box(0, 300)]
        self.s.run()
        self.s.actions.clear()
        self.s.enemies = []
        self.s.clock.now += .1
        self.assertGreater(self.s.run()[1], 0)
        self.s.clock.now += .8
        self.assertLess(self.s.run()[1], 0)
        self.assertEqual(self.s.actions, [])

    def test_vanished_close_enemy_is_not_chased(self):
        self.s.enemies = [box(0, 80)]
        self.s.run()
        self.s.enemies = []
        self.s.clock.now += .1
        self.assertLess(self.s.run()[1], 0)

    def test_blind_search_returns_then_sweeps_and_advances(self):
        self.s.run()
        for _ in range(8):
            self.s.clock.now += .5
            move = self.s.run()
        self.assertGreater(move[1], 0)
        for _ in range(12):
            self.s.clock.now += .5
            move = self.s.run()
        self.assertGreater(move[0], 0)
        for _ in range(6):
            self.s.clock.now += .5
            move = self.s.run()
        self.assertLess(move[1], 0)
        self.assertEqual(self.s.actions, [])

    def test_search_resets_after_interruption_or_enemy_reappears(self):
        self.s.run()
        for _ in range(9):
            self.s.clock.now += .5
            self.s.run()
        self.assertGreater(self.s.run()[1], 0)
        self.s.clock.now += 3
        self.assertLess(self.s.run()[1], 0)
        self.s.enemies = [box(300, 0)]
        self.s.clock.now += .1
        self.assertGreater(self.s.run()[0], 0)
        self.s.enemies = []
        self.s.clock.now += 1.0
        self.assertLess(self.s.run()[1], 0)

    def test_visible_enemy_still_has_priority_over_regrouping(self):
        self.s.enemies = [box(500, 0)]
        self.s.teammates = [box(0, 300)]
        move = self.s.run()
        self.assertGreater(move[0], 0)
        self.assertLess(move[1], 0)

    def test_search_respects_blocked_directions(self):
        self.s.blocked = lambda move: move[1] > 70
        self.s.run()
        for _ in range(9):
            self.s.clock.now += .5
            move = self.s.run()
        self.assertFalse(self.s.blocked(move))
        self.assertNotEqual(move, (0, 0))

    def test_visible_spawnable_remains_a_valid_attack_target(self):
        self.s.summons = [box(0, -100)]
        self.s.run()
        self.assertIn(("attack", True, True), self.s.actions)

    def test_close_enemy_retreats_and_attacks(self):
        self.s.enemies = [box(0, -100)]
        self.assertGreater(self.s.run()[1], 0)
        self.assertIn(("attack", True, True), self.s.actions)

    def test_melee_closes_and_does_not_shoot_out_of_range(self):
        self.s.safe, self.s.attack_range = 0, 170
        self.s.enemies = [box(0, -300)]
        self.assertLess(self.s.run()[1], 0)
        self.assertEqual(self.s.actions, [])

    def test_blocked_direct_path_flanks_and_side_is_stable(self):
        self.s.blocked = lambda m: m[1] < -80
        first = self.s.run()
        self.assertFalse(self.s.blocked(first))
        self.s.clock.now += .1
        second = self.s.run()
        self.assertAlmostEqual(first[0], second[0])
        self.assertAlmostEqual(first[1], second[1])

    def test_fully_blocked_stops(self):
        self.s.blocked = lambda m: True
        self.assertEqual(self.s.run(), (0, 0))

    def test_alone_short_step_if_full_lookahead_blocks_every_direction(self):
        self.s.blocked = lambda move: True
        self.s.short_blocked = lambda move: move[0] <= 20
        x, y = self.s.run()
        self.assertGreater(x, 20)
        self.assertFalse(self.s.short_blocked((x, y)))

    def test_combat_does_not_use_alone_short_step_fallback(self):
        self.s.enemies = [box(0, -100)]
        self.s.blocked = lambda move: True
        self.s.short_blocked = lambda move: False
        self.assertEqual(self.s.run(), (0, 0))

    def test_alone_after_gap_still_moves_without_a_target(self):
        self.s.run()
        self.s.clock.now += 5
        self.s.actions.clear()
        x, y = self.s.run()
        self.assertEqual(x, 0)
        self.assertLess(y, 0)
        self.assertEqual(self.s.actions, [])

    def test_nearest_blocked_target_is_not_confused_with_far_visible_one(self):
        self.s.enemies = [box(0, -100), box(0, -200)]
        self.s.visible = lambda pos, skill: pos[1] != -100
        self.s.super_ready = self.s.gadget_ready = True
        self.s.run()
        self.assertEqual(self.s.actions, [])

    def test_spawnable_nearest_does_not_use_abilities(self):
        self.s.enemies = [box(0, -200)]
        self.s.summons = [box(0, -50)]
        self.s.super_ready = self.s.gadget_ready = self.s.hyper_ready = True
        self.s.run()
        self.assertEqual(self.s.actions, [("attack", True, True)])

    def test_super_and_gadget_only_near_visible_brawler_with_cooldown(self):
        self.s.enemies = [box(0, -100)]
        self.s.super_ready = self.s.gadget_ready = self.s.hyper_ready = True
        self.s.run()
        self.assertIn(("super",), self.s.actions)
        self.assertIn(("hyper",), self.s.actions)
        self.assertIn(("gadget",), self.s.actions)
        self.s.actions.clear()
        self.s.clock.now += .2
        self.s.run()
        self.assertEqual(self.s.actions, [("attack", True, True)])

    def test_mobility_super_conserved(self):
        self.s.enemies = [box(0, -100)]
        self.s.super_ready = True
        for kind in ("charge", "dash", "spawnable", "other", "other_target"):
            self.s.super_type = kind
            self.s.actions.clear()
            self.s.run()
            self.assertNotIn(("super",), self.s.actions)

    def test_hold_release_and_no_same_tick_repress(self):
        self.s.enemies = [box(0, -100)]
        self.s.hold = 3
        self.s.run()
        self.assertEqual(self.s.actions, [("attack", False, True)])
        self.s.actions.clear()
        self.s.clock.now += 2
        self.s.run()
        self.assertEqual(self.s.actions, [])
        self.s.clock.now += 1
        self.s.run()
        self.assertEqual(self.s.actions, [("attack", True, False)])
        self.assertIsNone(self.s.state["time_since_holding_attack"])
        self.s.actions.clear()
        self.s.run()
        self.assertEqual(self.s.actions, [("attack", False, True)])

    def test_held_attack_released_when_target_lost_or_blocked(self):
        for mode in ("lost", "blocked", "out_of_range"):
            with self.subTest(mode=mode):
                self.s = Scenario()
                self.s.enemies = [box(0, -100)]
                self.s.hold = 3
                self.s.run()
                self.s.actions.clear()
                if mode == "lost":
                    self.s.enemies.clear()
                elif mode == "blocked":
                    self.s.visible = lambda p, skill: False
                else:
                    self.s.enemies = [box(0, -1000)]
                self.s.run()
                self.assertEqual(self.s.actions, [("attack", True, False)])
                self.assertIsNone(self.s.state["time_since_holding_attack"])

    def test_separation_does_not_stop_forward_movement(self):
        self.s.teammates = [box(20, 0)]
        x, y = self.s.run()
        self.assertLess(x, 0)
        self.assertLess(y, 0)

    def test_all_upstream_brawler_configs(self):
        upstream = os.environ.get("PYLA_SOURCE")
        if not upstream:
            self.skipTest("set PYLA_SOURCE to check the upstream brawler database")
        data = json.loads((Path(upstream) / "cfg" / "brawlers_info.json").read_text(encoding="utf-8"))
        for name, info in data.items():
            with self.subTest(brawler=name):
                self.s = Scenario()
                self.s.info_override = info
                self.s.hold = info["hold_attack"]
                self.s.safe = info["safe_range"]
                self.s.attack_range = info["attack_range"]
                self.s.super_range = info["super_range"]
                self.s.enemies = [box(0, -100)]
                self.s.super_ready = self.s.gadget_ready = True
                x, y = self.s.run()
                self.assertTrue(math.isfinite(x) and math.isfinite(y))

    def test_various_scales_and_ranges_are_finite(self):
        for scale in (.5, 1, 2):
            for safe, attack_range in ((0, 170), (200, 400), (400, 700)):
                self.s = Scenario()
                self.s.safe, self.s.attack_range = safe * scale, attack_range * scale
                self.s.enemies = [box(100 * scale, -100 * scale)]
                x, y = self.s.run()
                self.assertTrue(math.isfinite(x) and math.isfinite(y))
                self.assertAlmostEqual(math.hypot(x, y), 100)


if __name__ == "__main__":
    unittest.main()
