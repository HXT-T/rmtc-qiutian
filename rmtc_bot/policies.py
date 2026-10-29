"""Sealed presets; decisions consume only the contestant's public perception."""
from dataclasses import replace

POLICIES = {"balanced_bot": "balanced", "greedy_collector": "collect",
            "aggressive_fighter": "combat", "defensive_bot": "survive"}


def policy_config(config, name):
    if name not in POLICIES:
        raise ValueError("unknown policy: " + name)
    return replace(config, decision_type=POLICIES[name])


class Policy:
    def __init__(self, config, name="balanced_bot", ai=None, log=None):
        from .engine import Engine
        self.engine = Engine(policy_config(config, name), ai, log)

    def decide(self, observation):
        # Session reset/duplicate handling remain in the existing Engine.
        return self.engine.step(observation)
