import random
import unittest
from rmtc_bot.config import Config
from rmtc_bot.engine import Engine
from rmtc_bot.events import Event
from rmtc_bot.planner import add
from rmtc_bot.protocol import DIRECTIONS
from .helpers import frame, enemy, resource


class ScenarioTests(unittest.TestCase):
    def test_expired_hidden_split_walls_not_permanent(self):
        e = Engine(Config())
        e.step(frame())
        e.events.merge([Event("split", 1, 3, point=(3, 3))])
        e.step(frame(tick=1, entities=[{"type": "obstacle", "pos": (3, 2)}]))
        self.assertIn((3, 2), e.planner.memory.obstacles)
        e.step(frame(tick=3, pos=(10, 10)))
        self.assertNotIn((3, 2), e.planner.memory.obstacles)

    def test_preexisting_wall_survives_split_end(self):
        e = Engine(Config())
        e.step(frame(entities=[{"type": "obstacle", "pos": (3, 2)}]))
        e.events.merge([Event("split", 1, 3, point=(3, 3))])
        e.step(frame(tick=1, entities=[{"type": "obstacle", "pos": (3, 2)}]))
        e.step(frame(tick=3, pos=(10, 10)))
        self.assertIn((3, 2), e.planner.memory.obstacles)

    def test_deep_explosion_escapes_multiple_ticks_early(self):
        e = Engine(Config())
        e.step(frame())
        e.events.merge([Event("explosion", 4, 5, rect=(0, 0, 4, 4), params={"damage": 80})])
        pos = (2, 2)
        for tick in range(1, 4):
            result = e.step(frame(tick=tick, pos=pos))
            self.assertEqual(result["action"], "move")
            pos = add(pos, DIRECTIONS[result["direction"]])
        self.assertFalse(e.events.active("explosion", 4)[0].contains(pos))

    def test_two_different_announcements_same_tick_kept(self):
        e = Engine(Config())
        e.step(frame())
        e.add_announcement("公告一")
        e.add_announcement("公告二")
        e.add_announcement("公告一")
        self.assertEqual(len(e.announcements), 2)

    def test_four_seats_500_ticks_with_dropout_respawn_and_restart(self):
        # Synthetic public frames, not referee execution or a competition score.
        rng = random.Random(45)
        for team in "ABCD":
            config = Config(team=team, total_ticks=500)
            e = Engine(config)
            pos = (2, 2)
            last_action = None
            for tick in range(500):
                if tick % 37 == 0 and tick > 0:
                    pos = (rng.randrange(2, 10), rng.randrange(2, 10))
                if tick == 251:
                    e = Engine(config)  # fresh process with no previous map
                visible = []
                for delta in ((1, 0), (0, 1), (-1, 0)):
                    p = add(pos, delta)
                    if 0 <= p[0] < 12 and 0 <= p[1] < 12:
                        if rng.random() < .4:
                            other_team = next(t for t in "ABCD" if t != team)
                            visible.append(enemy(p, hp=rng.choice((20, 60, 100)), team=other_team))
                        elif rng.random() < .2:
                            visible.append(resource(p, value=rng.choice((10, 20))))
                f = frame(tick=tick, pos=pos, entities=visible)
                f["self"]["team"] = team
                f["self"]["last_action"] = last_action
                f["self"]["hp"] = rng.choice((20, 60, 100))
                dead = tick % 61 in (0, 1, 2) and tick > 0
                if dead:
                    f["self"].update(alive=False, hp=0)
                    f["visible_entities"] = []
                f["self"]["protected"] = tick % 61 in (3, 4)
                result = e.step(f)
                self.assertIsNotNone(result)
                self.assertEqual(result["tick"], tick)
                self.assertEqual(result["session_id"], "test-session")
                self.assertIn(result["action"], ("move", "wait", "attack", "defend"))
                if dead:
                    self.assertEqual(result["action"], "wait")
                if f["self"]["protected"]:
                    self.assertNotEqual(result["action"], "attack")
                last_action = {"tick": tick, "action": result["action"], "moved": False}
                if result["action"] == "move":
                    p = add(pos, DIRECTIONS[result["direction"]])
                    self.assertTrue(0 <= p[0] < 12 and 0 <= p[1] < 12)
                    occupied = {item["pos"] for item in visible if item["type"] == "robot"}
                    self.assertNotIn(p, occupied)
                    pos = p
                    last_action.update(moved=True, effective_direction=result["direction"])


if __name__ == "__main__":
    unittest.main()
