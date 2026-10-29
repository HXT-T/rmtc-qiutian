import argparse
from dataclasses import fields
import importlib.util
import json
import os
from pathlib import Path
import platform
import sys
from .audit import AuditLog, analyze
from .config import Config
from .engine import Engine
from .policies import POLICIES, policy_config


def main(argv=None):
    parser = argparse.ArgumentParser(description="RMTC autonomous ROS 2 contestant")
    subs = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "replay"):
        p = subs.add_parser(command)
        p.add_argument("--stage", type=int, choices=(1, 2), required=True)
        p.add_argument("--team", choices=list("ABCD"), default="A")
        p.add_argument("--policy", choices=sorted(POLICIES))
        p.add_argument("--contest", action="store_true", help="strict formal deployment; restart with new token each match")
        p.add_argument("--domain-id", type=int, help="ROS_DOMAIN_ID (0..232)")
        p.add_argument("--localhost-only", choices=("0", "1"))
        p.add_argument("--rmw", help="RMW_IMPLEMENTATION")
        p.add_argument("--name", default="秋天")
        p.add_argument("--config", help="sealed configuration JSON; no secrets")
        p.add_argument("--decision-config", help="JSON containing only registered decision_type")
        p.add_argument("--total-ticks", type=int)
        p.add_argument("--ai-url", default=os.environ.get("RMTC_AI_URL"))
        p.add_argument("--model", default=os.environ.get("RMTC_AI_MODEL"))
        p.add_argument("--log", help="own contestant JSONL audit output")
        if command == "replay":
            p.add_argument("--input", help="public perception JSONL; default stdin. No HTTP/ROS calls.")
    p = subs.add_parser("analyze", help="post-match audit (never a live strategy input)")
    p.add_argument("log")
    subs.add_parser("doctor", help="read-only runtime/environment checks")
    args = parser.parse_args(argv)
    if args.command == "analyze":
        print(json.dumps(analyze(args.log), ensure_ascii=False, indent=2))
        return 0
    if args.command == "doctor":
        info = {"python": platform.python_version(), "platform": platform.platform(),
                "rclpy_available": importlib.util.find_spec("rclpy") is not None,
                "std_msgs_available": importlib.util.find_spec("std_msgs") is not None,
                "ROS_DISTRO": os.environ.get("ROS_DISTRO"),
                "ROS_DOMAIN_ID": os.environ.get("ROS_DOMAIN_ID"),
                "ROS_LOCALHOST_ONLY": os.environ.get("ROS_LOCALHOST_ONLY"),
                "RMW_IMPLEMENTATION": os.environ.get("RMW_IMPLEMENTATION"),
                "team_token_present": bool(os.environ.get("RMTC_TEAM_TOKEN"))}
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    values = {}
    if args.config:
        values = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
        if not isinstance(values, dict) or set(values) - {f.name for f in fields(Config)}:
            parser.error("config contains unknown fields")
    values.update(stage=args.stage, team=args.team, name=args.name)
    if args.contest:
        values["contest"] = True
    domain = args.domain_id if args.domain_id is not None else os.environ.get("ROS_DOMAIN_ID")
    try:
        if domain is not None and not 0 <= int(domain) <= 232:
            raise ValueError()
    except (ValueError, TypeError):
        parser.error("ROS domain must be an integer in 0..232")
    if values.get("contest"):
        if domain is None:
            parser.error("contest requires --domain-id or explicit ROS_DOMAIN_ID")
        if args.stage == 2 and (not os.environ.get("RMTC_TEAM_TOKEN") or not (args.ai_url or values.get("ai_url"))):
            parser.error("stage 2 contest requires an explicit AI gateway and RMTC_TEAM_TOKEN")
        if args.model or values.get("ai_model"):
            # Organiser: only the unified default model is allowed, so the request carries no model field.
            parser.error("contest must use the organiser's default model: unset RMTC_AI_MODEL / --model")
    for key, value in (("ROS_DOMAIN_ID", domain), ("ROS_LOCALHOST_ONLY", args.localhost_only),
                       ("RMW_IMPLEMENTATION", args.rmw)):
        if value is not None:
            os.environ[key] = str(value)
    for key, arg in (("total_ticks", args.total_ticks), ("ai_url", args.ai_url), ("ai_model", args.model)):
        if arg is not None:
            values[key] = arg
    if args.decision_config:
        decision = json.loads(Path(args.decision_config).read_text(encoding="utf-8-sig"))
        if not isinstance(decision, dict) or set(decision) != {"decision_type"}:
            parser.error("decision_config.json may contain only decision_type")
        values["decision_type"] = decision["decision_type"]
    try:
        config = Config(**values)
        if args.policy:
            config = policy_config(config, args.policy)
    except (ValueError, TypeError) as exc:
        parser.error(str(exc))
    log = AuditLog(args.log)
    try:
        if args.command == "run":
            from .ros_node import run
            run(config, os.environ.get("RMTC_TEAM_TOKEN", ""), log)
        else:
            engine = Engine(config, log=log)
            source = Path(args.input).open(encoding="utf-8-sig") if args.input else sys.stdin
            try:
                for line in source:
                    if line.strip():
                        result = engine.step(line)
                        print(json.dumps(result, ensure_ascii=False), flush=True)
            finally:
                if args.input:
                    source.close()
    finally:
        log.close()
    return 2 if log.error else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
