import json
import sys
import types
import unittest
from unittest.mock import patch
from rmtc_bot.config import Config
from rmtc_bot.ros_node import run
from .helpers import frame, resource


class ROSAdapterTests(unittest.TestCase):
    def test_topics_qos_registration_serialization_and_shutdown(self):
        state = {"publishers": {}, "subscriptions": {}, "shutdown": False}
        class String:
            def __init__(self):
                self.data = ""
        class Profile:
            def __init__(self, **kw):
                self.__dict__.update(kw)
        class Pub:
            def __init__(self, q):
                self.qos, self.values = q, []
            def publish(self, msg):
                self.values.append(json.loads(msg.data))
        class Node:
            def __init__(self, name):
                state["name"] = name
            def create_publisher(self, kind, topic, q):
                pub = Pub(q)
                state["publishers"][topic] = pub
                return pub
            def create_subscription(self, kind, topic, cb, q):
                state["subscriptions"][topic] = (cb, q)
                return object()
            def get_logger(self):
                return types.SimpleNamespace(info=lambda s: None, warning=lambda s: None, error=lambda s: None)
            def destroy_node(self):
                state["destroyed"] = True
        def spin(node):
            cb, _ = state["subscriptions"]["/team_A/perception"]
            message = String()
            message.data = json.dumps(frame(entities=[resource((3, 2))]))
            cb(message)
            cb(message)  # cached action may be re-published for resume
            message.data = json.dumps(frame(tick=100))
            cb(message)  # final frame is processed, never published as action
            message.data = '{"broken":true}'
            cb(message)
        rclpy = types.ModuleType("rclpy")
        rclpy.init = lambda **kw: None
        rclpy.spin = spin
        rclpy.ok = lambda: True
        rclpy.shutdown = lambda: state.update(shutdown=True)
        node_module = types.ModuleType("rclpy.node")
        node_module.Node = Node
        qmodule = types.ModuleType("rclpy.qos")
        qmodule.QoSProfile = Profile
        qmodule.HistoryPolicy = types.SimpleNamespace(KEEP_LAST="KEEP_LAST")
        qmodule.ReliabilityPolicy = types.SimpleNamespace(RELIABLE="RELIABLE")
        qmodule.DurabilityPolicy = types.SimpleNamespace(TRANSIENT_LOCAL="TRANSIENT_LOCAL", VOLATILE="VOLATILE")
        msg = types.ModuleType("std_msgs.msg")
        msg.String = String
        with patch.dict(sys.modules, {"rclpy": rclpy, "rclpy.node": node_module, "rclpy.qos": qmodule,
                                      "std_msgs": types.ModuleType("std_msgs"), "std_msgs.msg": msg}):
            run(Config(stage=1), "", lambda *args, **kw: None)
        self.assertEqual(set(state["publishers"]), {"/team_A/action", "/team_A/registration"})
        self.assertEqual(set(state["subscriptions"]), {"/team_A/perception", "/referee/event_text",
                         "/referee/team_registry", "/referee/mission"})
        pub = state["publishers"]["/team_A/action"]
        self.assertEqual(pub.qos.depth, 1)
        self.assertEqual(pub.qos.durability, "VOLATILE")
        self.assertEqual(pub.qos.reliability, "RELIABLE")
        self.assertEqual(pub.values, [{"tick": 0, "session_id": "test-session", "action": "move", "direction": "right"}] * 2)
        registration = state["publishers"]["/team_A/registration"]
        self.assertEqual(registration.qos.durability, "TRANSIENT_LOCAL")
        self.assertEqual(registration.values, [{"team": "A", "name": "秋天"}])
        self.assertEqual(state["subscriptions"]["/referee/event_text"][1].depth, 128)
        self.assertTrue(state["shutdown"])
        self.assertTrue(state["destroyed"])


if __name__ == "__main__":
    unittest.main()
