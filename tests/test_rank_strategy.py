from collections import Counter
import unittest
from rmtc_bot.config import Config
from rmtc_bot.engine import Engine
from rmtc_bot.protocol import Frame
from .helpers import frame, enemy, resource


class RankStrategyTests(unittest.TestCase):
    def test_continue_fighting_after_damage_cap_when_other_work_done(self):
        f = frame(entities=[enemy((3, 2))])
        f['score']['effective_damage'] = 500
        self.assertEqual(Engine(Config(explore_weight=0.0)).step(f)['action'], 'attack')

    def test_postcap_still_respects_protection(self):
        f = frame(entities=[enemy((3, 2))])
        f['score']['effective_damage'] = 500
        f['self']['protected'] = True
        self.assertNotEqual(Engine(Config()).step(f)['action'], 'attack')

    def test_cap_does_not_make_reflection_free_damage(self):
        from rmtc_bot.events import Event
        engine = Engine(Config())
        engine.step(frame())
        engine.events.merge([Event('reflection', 1, 20, params={'reflection_damage': 20})])
        f = frame(tick=1, entities=[enemy((3, 2), defending=True)])
        f['score']['effective_damage'] = 500
        f['self']['hp'] = 10
        self.assertNotEqual(engine.step(f)['action'], 'attack')

    def test_resource_reward_accounts_for_crossing_score_cap(self):
        engine = Engine(Config())
        f = frame()
        f['score']['resource_value'] = 115
        engine.step(f)
        gain = engine.planner.resource_gain(engine.latest, 20)
        self.assertAlmostEqual(gain, 1.25 + 15 * .012)
        self.assertLess(engine.planner.resource_gain(engine.latest, 20, 5), gain)

    def test_value_mission_is_not_counted_repeatedly_in_one_route(self):
        engine = Engine(Config())
        engine.step(frame(missions=[{'id': 'v', 'type': 'resource_value', 'target': 20}]))
        first = engine.planner._mission_bonus(engine.latest, 10, Counter(planned_value=10))
        second = engine.planner._mission_bonus(engine.latest, 10, Counter(planned_value=20))
        third = engine.planner._mission_bonus(engine.latest, 10, Counter(planned_value=30))
        self.assertEqual(first + second + third, 5)

    def test_first_stage_resource_reward_unchanged(self):
        engine = Engine(Config(stage=1))
        engine.step(frame())
        self.assertEqual(engine.planner.resource_gain(engine.latest, 20), 4)


if __name__ == '__main__':
    unittest.main()
