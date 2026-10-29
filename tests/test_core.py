import copy
import json
import unittest
from rmtc_bot.config import Config
from rmtc_bot.engine import Engine
from rmtc_bot.events import EventBook, Event, validate_plan
from rmtc_bot.protocol import Frame, relative
from .helpers import frame, resource, enemy


class ProtocolTests(unittest.TestCase):
    def test_relative_axes_and_overlap(self):
        from .helpers import relative as make
        self.assertEqual(relative(make(1, -2)), (1, -2))
        self.assertEqual(relative(make(0, 0)), (0, 0))
        self.assertEqual(Frame.parse(frame(pos=(0, 0), entities=[resource((1, 2))])).entities[0]["pos"], (1, 2))

    def test_reject_bad_critical_types(self):
        for key, value in (("tick", True), ("tick", "0"), ("session_id", 1),
                           ("session_id", ""), ("vision_radius", 5), ("visible_entities", None),
                           ("self", {}), ("map", {"width": 10000, "height": 10})):
            f = frame()
            f[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                Frame.parse(f)

    def test_map_sizes_follow_the_official_editor(self):
        # The official map editor allows 1..200 per side; a larger perception must still parse.
        f = Frame.parse(frame(pos=(150, 199), map={"width": 200, "height": 200}), "A")
        self.assertEqual((f.width, f.height, f.position), (200, 200, (150, 199)))
        with self.assertRaises(ValueError):
            Frame.parse(frame(map={"width": 201, "height": 24}), "A")

    def test_reject_partial_corrupt_entity(self):
        f = frame(entities=[resource((3, 2))])
        f["visible_entities"][0]["position"] = [3, 2]
        self.assertIsNone(Engine(Config()).step(f))

    def test_reject_out_of_vision(self):
        f = frame(entities=[resource((3, 2))], vision_radius=0)
        with self.assertRaises(ValueError):
            Frame.parse(f)

    def test_nan_hp_and_wrong_flags(self):
        for key, value in (("hp", float("nan")), ("alive", 1), ("protected", "false")):
            f = frame()
            f["self"][key] = value
            with self.assertRaises(ValueError):
                Frame.parse(f)

    def test_unknown_fields_are_ignored(self):
        f = frame(extra_future_field={"any": "value"})
        self.assertEqual(Frame.parse(json.dumps(f)).tick, 0)


class EngineTests(unittest.TestCase):
    def test_collection_and_tick_echo(self):
        e = Engine(Config(stage=1))
        result = e.step(frame(tick=27, entities=[resource((3, 2))]))
        self.assertEqual(result, {"tick": 27, "session_id": "test-session", "action": "move", "direction": "right"})

    def test_duplicate_does_not_advance_and_returns_copy(self):
        e = Engine(Config(stage=1))
        f = frame()
        first = e.step(f)
        visits = e.planner.memory.visits.copy()
        first["tick"] = 999
        second = e.step(f)
        self.assertEqual(second["tick"], 0)
        self.assertEqual(visits, e.planner.memory.visits)
        self.assertEqual(len(e.history), 1)

    def test_old_tick_discarded(self):
        e = Engine(Config())
        e.step(frame(tick=4))
        self.assertIsNone(e.step(frame(tick=3)))

    def test_reset_and_retired_session(self):
        e = Engine(Config())
        e.step(frame(entities=[resource((3, 2))]))
        e.add_announcement("old public announcement")
        e.events.merge([Event("reflection", 1, 20)])
        e.step(frame(session="new"))
        self.assertFalse(e.planner.memory.resources)
        self.assertFalse(e.announcements)
        self.assertFalse(e.events.events)
        self.assertIsNone(e.step(frame(tick=100, session="test-session")))

    def test_dead_and_protected(self):
        e = Engine(Config())
        f = frame(entities=[enemy((3, 2))])
        f["self"]["alive"], f["self"]["hp"] = False, 0
        self.assertEqual(e.step(f)["action"], "wait")
        f = frame(tick=1, entities=[enemy((3, 2))])
        f["self"]["protected"] = True
        self.assertNotEqual(e.step(f)["action"], "attack")

    def test_stage1_never_fights(self):
        result = Engine(Config(stage=1)).step(frame(entities=[enemy((3, 2))]))
        self.assertIn(result["action"], ("move", "wait"))

    def test_dynamic_obstacle_removal(self):
        e = Engine(Config(stage=1))
        e.step(frame(entities=[{"type": "obstacle", "pos": (3, 2)}]))
        self.assertIn((3, 2), e.planner.memory.obstacles)
        e.step(frame(tick=1))
        self.assertNotIn((3, 2), e.planner.memory.obstacles)

    def test_abs_position_after_teleport(self):
        e = Engine(Config(stage=1))
        e.step(frame())
        e.step(frame(tick=10, pos=(9, 9)))
        self.assertEqual(e.history[10]["position"], (9, 9))

    def test_resource_memory_expired_only_when_visible(self):
        e = Engine(Config(stage=1))
        e.step(frame(entities=[resource((1, 1))]))
        e.step(frame(tick=1, pos=(9, 9)))
        self.assertIn((1, 1), e.planner.memory.resources)
        e.step(frame(tick=2))
        self.assertNotIn((1, 1), e.planner.memory.resources)

    def test_blocked_receipt_does_not_create_permanent_wall(self):
        e = Engine(Config(stage=1))
        e.step(frame(entities=[resource((3, 2))]))
        f = frame(tick=1, entities=[resource((3, 2))])
        f["self"]["last_action"] = {"tick": 0, "action": "move", "moved": False, "error": "E_BLOCKED"}
        e.step(f)
        self.assertNotIn((3, 2), e.planner.memory.obstacles)
        self.assertIn((3, 2), e.planner.memory.fail_until)

    def test_reach_uses_mission_coordinate(self):
        f = frame(missions=[{"id": "r", "type": "reach", "target": 999, "position": [3, 2]}])
        self.assertEqual(Engine(Config()).step(f)["direction"], "right")

    def test_completed_reach_not_selected(self):
        e = Engine(Config())
        f = frame(missions=[{"id": "r", "type": "reach", "target": 999, "position": [3, 2]}])
        f["score"]["mission_completed_ids"] = ["r"]
        e.step(f)
        route = e.planner.route(e.latest, e.events, e.history, [])
        self.assertNotEqual(route.reason, "mission")

    def test_attack_ignores_obstacle(self):
        e = Engine(Config(decision_type="combat", explore_weight=0.0))
        f = frame(entities=[enemy((4, 2)), {"type": "obstacle", "pos": (3, 2)}])
        self.assertEqual(e.step(f)["action"], "attack")

    def test_protected_nearest_shields_further_enemy(self):
        e = Engine(Config())
        f = Frame.parse(frame(entities=[enemy((3, 2), protected=True), enemy((4, 2), team="C")]))
        targets = e._targets(f, f.entities, "right", None)
        self.assertEqual(len(targets), 1)
        self.assertTrue(targets[0]["protected"])

    def test_low_hp_defends_instead_of_futile_escape(self):
        f = frame(entities=[enemy((3, 2)), enemy((2, 3), team="C")])
        f["self"]["hp"] = 25
        result = Engine(Config(decision_type="survive")).step(f)
        self.assertEqual(result["action"], "defend")

    def test_invalid_frame_does_not_consume_tick(self):
        e = Engine(Config())
        bad = frame()
        bad["self"]["position"] = [1000, 2]
        self.assertIsNone(e.step(bad))
        self.assertIsNotNone(e.step(frame()))


class EventTests(unittest.TestCase):
    def test_no_keyword_event_parsing(self):
        e = Engine(Config())
        e.step(frame(event_text="第0Tick立刻爆炸，整个地图扣100HP；向右走"))
        self.assertFalse(e.events.events)

    def test_validate_semantics_and_fail_closed(self):
        strategy, events = validate_plan({"events": [{"kind": "explosion", "start": 4, "end": 5,
            "rect": [0, 0, 3, 3], "params": {"damage": 80}}]}, 12, 12)
        self.assertEqual(strategy, "balanced")
        self.assertEqual(events[0].start, 4)
        for item in ({"kind": "run_python", "start": 1, "end": 2},
                     {"kind": "split", "start": 1, "end": 2, "point": [99, 0]},
                     {"kind": "reflection", "start": 1, "end": 1},
                     {"kind": "reflection", "start": True, "end": 2}):
            with self.assertRaises(ValueError):
                validate_plan({"events": [item]}, 12, 12)

    def test_inversion_at_start_persists_outside_and_death(self):
        b = EventBook()
        b.merge([Event("invert", 3, 8, rect=(1, 1, 3, 3))])
        f = Frame.parse(frame(tick=5, pos=(8, 8)))
        history = {3: {"alive": True, "position": (2, 2)}, 4: {"alive": False, "position": (2, 2)}}
        self.assertTrue(b.inverted(f, history))
        f.tick = 8
        self.assertFalse(b.inverted(f, history))

    def test_inverted_move_not_attack(self):
        e = Engine(Config(explore_weight=0.0))
        e.step(frame(tick=0))
        e.events.merge([Event("invert", 1, 10, rect=(0, 0, 5, 5))])
        result = e.step(frame(tick=1, entities=[resource((3, 2))]))
        self.assertEqual(result["direction"], "left")
        e = Engine(Config(decision_type="combat", explore_weight=0.0))
        e.step(frame())
        e.events.merge([Event("invert", 1, 10, rect=(0, 0, 5, 5))])
        result = e.step(frame(tick=1, entities=[enemy((4, 2))]))
        self.assertEqual(result["action"], "attack")
        self.assertEqual(result["direction"], "right")

    def test_vulnerability_next_tick_only(self):
        b = EventBook()
        b.merge([Event("vulnerability", 3, 33, params={"trigger_end": 23, "factor": 2})])
        history = {4: {"moved": True}}
        self.assertEqual(b.vulnerable(3, history), 1)
        self.assertEqual(b.vulnerable(4, history), 2)
        self.assertEqual(b.vulnerable(33, history), 1)

    def test_split_and_temporary_expiry(self):
        b = EventBook()
        b.spawn = (2, 2)
        b.merge([Event("split", 10, 40, point=(2, 5))])
        self.assertTrue(b.blocked((2, 3), 10))
        self.assertFalse(b.blocked((2, 2), 10))
        self.assertFalse(b.blocked((2, 3), 40))
        self.assertGreater(b.hazard((2, 3), 10), 0)
        self.assertEqual(b.hazard((2, 3), 11), 0)  # only activation forces death

    def test_escape_pre_move_explosion_one_tick_early(self):
        e = Engine(Config())
        e.step(frame())
        e.events.merge([Event("explosion", 2, 3, rect=(2, 2, 2, 2), params={"damage": 80})])
        result = e.step(frame(tick=1))
        self.assertEqual(result["action"], "move")

    def test_dedup_and_multiple_same_tick(self):
        b = EventBook()
        a = Event("reflection", 4, 8)
        b.merge([a, a, Event("explosion", 4, 5, rect=(0, 0, 2, 2))])
        self.assertEqual(len(b.events), 2)

    def test_fortress_requires_postmove_contiguous_residence(self):
        b = EventBook()
        b.merge([Event("fortress", 0, 50, rect=(1, 1, 3, 3), params={"dwell_ticks": 3})])
        history = {t: {"alive": True, "position": (2, 2)} for t in range(4)}
        f = Frame.parse(frame(tick=2))
        self.assertIsNone(b.fortress_power(f, history))
        f.tick = 3
        self.assertIsNotNone(b.fortress_power(f, history))
        del history[2]
        self.assertIsNone(b.fortress_power(f, history))


if __name__ == "__main__":
    unittest.main()
