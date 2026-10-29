import unittest
from rmtc_bot.config import Config
from rmtc_bot.engine import Engine
from rmtc_bot.events import Event
from rmtc_bot.scoring import after_tick, combat_points, marginal_value
from .helpers import frame, enemy, resource


class ScoringV11Tests(unittest.TestCase):
    def test_published_gain_then_death_example(self):
        self.assertEqual(after_tick(80, 20, 60), 160)
        self.assertEqual(after_tick(160, deaths=1), 100)

    def test_mutual_kill_credits_before_death_floor(self):
        self.assertEqual(after_tick(0, 20, 60, 1), 20)
        self.assertEqual(after_tick(0, 10, 30, 1), 0)

    def test_explore_weight_values_unseen_area_only_in_opening(self):
        mission = [{"id": "r", "type": "reach", "target": 1, "position": [11, 11]}]
        def route(tick, **kw):
            e = Engine(Config(**kw))
            e.step(frame(tick=tick, pos=(1, 1), missions=mission))
            return e.planner.route(e.latest, e.events, e.history, []).reason
        self.assertEqual(route(0, explore_weight=0.0), "mission")
        self.assertEqual(route(0), "explore")  # adopted default
        self.assertEqual(route(0, explore_weight=1.0), "explore")
        self.assertEqual(route(200, explore_weight=1.0), "mission")  # value faded
        values = {}
        for size in (18, 24):
            e = Engine(Config(explore_weight=1.0))
            e.step(frame(tick=0, map={"width": size, "height": size}))
            values[size] = e.planner.explore_value(e.latest, 81)
        self.assertAlmostEqual(values[18], 81 * 16 / 324 * 12.5 * 0.25)
        self.assertAlmostEqual(values[24] / values[18], 324 / 576)  # sparser on the 24x24 stage-2 map

    def test_multiple_deaths_and_environment_no_reward(self):
        self.assertEqual(after_tick(150, deaths=2), 30)
        self.assertEqual(after_tick(30, deaths=1), 0)

    def test_fractional_kill_share_is_preserved(self):
        self.assertAlmostEqual(after_tick(80, 5, 60/3, 1), 45)

    def test_new_cap_and_uncapped_damage(self):
        self.assertEqual(Config().damage_cap, 500)
        self.assertEqual(combat_points(300), 9)
        self.assertEqual(combat_points(500), 15)
        self.assertEqual(combat_points(900), 15)
        self.assertEqual(after_tick(900, 20, 60), 980)

    def test_death_can_reopen_scoring_below_cap(self):
        self.assertAlmostEqual(marginal_value(520, deaths=1), -1.2)
        self.assertEqual(marginal_value(600, deaths=1), 0)
        self.assertAlmostEqual(marginal_value(600, deaths=1, tie_weight=.01), -.6)

    def test_adjusted_damage_can_fall_and_completed_mission_survives(self):
        engine=Engine(Config())
        f=frame(missions=[{'id':'damage','type':'effective_damage','target':100}])
        f['score'].update(effective_damage=160,mission_completed_ids=['damage'])
        engine.step(f)
        f=frame(tick=1,missions=[{'id':'damage','type':'effective_damage','target':100}])
        f['score'].update(effective_damage=40,mission_completed_ids=['damage'])
        engine.step(f)
        self.assertEqual(engine.latest.score['effective_damage'],40)
        self.assertIn('damage',engine.latest.score['mission_completed_ids'])

    def test_low_hp_enemy_kill_has_value_beyond_ordinary_chip_damage(self):
        engine=Engine(Config(explore_weight=0.0))
        f=frame(entities=[enemy((3,2),hp=10),resource((2,3))])
        f['score']['resource_value']=120
        self.assertEqual(engine.step(f)['action'],'attack')

    def test_exploration_still_takes_immediate_kill_on_rule_sized_map(self):
        for tick in (0, 40, 120):
            f = frame(tick=tick, entities=[enemy((4, 2), hp=20)], map={"width": 24, "height": 24})
            self.assertEqual(Engine(Config(explore_weight=1.0)).step(f)["action"], "attack")

    def test_reflection_does_not_award_raw_reflected_hp_as_attack(self):
        engine=Engine(Config())
        engine.step(frame())
        engine.events.merge([Event('reflection',1,20,params={'reflection_damage':20})])
        f=frame(tick=1,entities=[enemy((3,2),hp=10,defending=True)])
        f['self']['hp']=10
        self.assertNotEqual(engine.step(f)['action'],'attack')


if __name__=='__main__':
    unittest.main()
