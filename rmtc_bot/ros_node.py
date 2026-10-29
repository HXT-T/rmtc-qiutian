"""ROS imports are isolated so pure strategy/tests run on Windows without ROS."""
import json
import sys
from .ai import AIWorker
from .engine import Engine


def run(config, token, log):
    try:
        import rclpy
        from rclpy.node import Node
        from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
        from std_msgs.msg import String
    except ImportError as exc:
        raise RuntimeError("ROS 2 Humble is required. On Ubuntu: source /opt/ros/humble/setup.bash") from exc

    def qos(depth, retained=False):
        return QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=depth,
                          reliability=ReliabilityPolicy.RELIABLE,
                          durability=DurabilityPolicy.TRANSIENT_LOCAL if retained else DurabilityPolicy.VOLATILE)

    class Contestant(Node):
        def __init__(self):
            super().__init__("rmtc_contestant_" + config.team.lower())
            self.ai = AIWorker(config, token, log) if config.stage == 2 else None
            self.engine = Engine(config, self.ai, log)
            prefix = "/team_" + config.team
            self.actions = self.create_publisher(String, prefix + "/action", qos(1))
            self.registration = self.create_publisher(String, prefix + "/registration", qos(1, True))
            self.perception = self.create_subscription(String, prefix + "/perception", self.on_frame, qos(1))
            # Public text is opaque; only the AI worker interprets it.
            self.events = self.create_subscription(String, "/referee/event_text",
                                                  lambda msg: self.engine.add_announcement(msg.data), qos(128, True))
            self.registry = self.create_subscription(String, "/referee/team_registry", self.on_registry, qos(1, True))
            # Mission text also exists in every perception. It is not used without
            # a current perception/session; this prevents retained old task data.
            self.missions = self.create_subscription(String, "/referee/mission", lambda msg: None, qos(1, True))
            name = String()
            name.data = json.dumps({"team": config.team, "name": config.name}, ensure_ascii=False)
            self.registration.publish(name)
            self.get_logger().info("Ready: " + prefix + "; waiting for perception and referee start")
            if config.stage == 2 and not token:
                self.get_logger().warning("RMTC_TEAM_TOKEN is empty: local control works; AI/official mission eligibility unavailable")

        def on_registry(self, msg):
            try:
                value = json.loads(msg.data)
                if isinstance(value, dict) and isinstance(value.get("teams"), list):
                    log("registry", teams=value["teams"])
            except (ValueError, TypeError):
                log("registry_rejected")

        def on_frame(self, msg):
            try:
                result = self.engine.step(msg.data)
                # Final perceptions exist for receipts/score; the referee no
                # longer accepts actions at horizon (100 or 500 by default).
                if result is not None and result['tick'] < config.horizon:
                    out = String()
                    out.data = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                    self.actions.publish(out)
            except Exception as exc:
                # Unexpected faults are visible; do not guess an action or reuse an old tick.
                log("controller_fault", error_type=type(exc).__name__)
                self.get_logger().error("Controller fault: " + type(exc).__name__)

    rclpy.init(args=[])
    node = None
    try:
        node = Contestant()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
