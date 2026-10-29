"""Regression cases from the official match's late-game inactivity."""
import unittest
from rmtc_bot.config import Config
from rmtc_bot.engine import Engine
from rmtc_bot.events import EventBook
from rmtc_bot.planner import WorldMemory
from rmtc_bot.protocol import Frame, DIRECTIONS
from .helpers import frame, enemy, resource


class PursuitTests(unittest.TestCase):
    def test_complete_map_does_not_idle_without_visible_enemy(self):
        engine = Engine(Config())
        engine.step(frame())
        engine.planner.memory.seen = {(x, y) for x in range(12) for y in range(12)}
        engine.planner.memory.observed_at = {p: 0 for p in engine.planner.memory.seen}
        result = engine.step(frame(tick=20))
        self.assertEqual(result['action'], 'move')
        self.assertIn(result['direction'], DIRECTIONS)

    def test_stale_enemy_is_not_used_as_a_current_attack_target(self):
        engine = Engine(Config())
        engine.step(frame(entities=[enemy((6, 2))]))
        # Move out of that observation area; remembered enemy is outside vision.
        result = engine.step(frame(tick=1, pos=(1, 8)))
        self.assertEqual(result['action'], 'move')
        self.assertIn('B', engine.planner.memory.enemy_sightings)

    def test_reobserved_absent_enemy_is_removed(self):
        memory = WorldMemory()
        memory.update(Frame.parse(frame(entities=[enemy((6, 2))])))
        memory.update(Frame.parse(frame(tick=1)))
        self.assertNotIn('B', memory.enemy_sightings)

    def test_old_sighting_expires_and_dead_frame_is_not_observation(self):
        memory = WorldMemory()
        memory.update(Frame.parse(frame(entities=[enemy((6, 2))])))
        dead = frame(tick=1)
        dead['self'].update(alive=False, hp=0)
        memory.update(Frame.parse(dead))
        self.assertIn('B', memory.enemy_sightings)
        memory.update(Frame.parse(frame(tick=31, pos=(1, 8))))
        self.assertNotIn('B', memory.enemy_sightings)

    def test_resource_still_precedes_hunting(self):
        engine = Engine(Config())
        engine.step(frame(entities=[resource((3, 2)), enemy((6, 2))]))
        route = engine.planner.route(engine.latest, engine.events, engine.history,
                                     [e for e in engine.latest.entities if e['type'] == 'robot'])
        self.assertEqual(route.reason, 'resource')
        self.assertEqual(route.goal, (3, 2))

    def test_no_patrol_in_stage1(self):
        engine = Engine(Config(stage=1))
        engine.step(frame())
        engine.planner.memory.seen = {(x, y) for x in range(12) for y in range(12)}
        self.assertEqual(engine.step(frame(tick=20))['action'], 'wait')

    def test_new_session_drops_enemy_and_patrol_memory(self):
        engine = Engine(Config())
        engine.step(frame(entities=[enemy((6, 2))]))
        engine.planner.patrol_goal = (10, 10)
        engine.step(frame(session='new'))
        self.assertFalse(engine.planner.memory.enemy_sightings)
        self.assertIsNone(engine.planner.patrol_goal)

    def test_visible_enemy_approached_on_complete_map(self):
        engine = Engine(Config())
        engine.step(frame())
        engine.planner.memory.seen = {(x, y) for x in range(12) for y in range(12)}
        result = engine.step(frame(tick=20, entities=[enemy((6, 2))]))
        self.assertEqual(result['action'], 'move')
        self.assertEqual(result['direction'], 'right')


if __name__ == '__main__':
    unittest.main()
