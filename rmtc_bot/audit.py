import json
import queue
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path


class AuditLog:
    """Bounded asynchronous disk writer, never print tokens or HTTP payloads."""
    def __init__(self, path=None):
        self.queue = queue.Queue(maxsize=10000)
        self.dropped = 0
        self.error = None
        self.path = Path(path) if path else None
        self.file = None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.file = self.path.open("a", encoding="utf-8")
        self.thread = threading.Thread(target=self._write, daemon=True, name="rmtc-log")
        self.thread.start()

    def __call__(self, kind, **fields):
        item = dict(time=datetime.now(timezone.utc).isoformat(), kind=kind, **fields)
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            self.dropped += 1

    def _write(self):
        while True:
            item = self.queue.get()
            if item is None:
                if self.file:
                    self.file.flush()
                    self.file.close()
                return
            try:
                if self.file:
                    self.file.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n")
                    self.file.flush()
                if item["kind"] in ("session", "ai_result", "ai_discarded", "perception_rejected"):
                    print(json.dumps(item, ensure_ascii=False), file=sys.stderr, flush=True)
            except Exception as exc:
                self.error = type(exc).__name__
                print("RMTC audit writer failed: " + self.error, file=sys.stderr)

    def close(self):
        self.queue.put(None)
        self.thread.join(timeout=5)


def analyze(path):
    """Post-match audit only. These logs are never consumed by the live planner."""
    sessions = {}
    malformed = 0
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        kind = item.get("kind")
        if kind == "session":
            current = sessions.setdefault(item["session_id"], {
                "session_id": item["session_id"], "stage": item["stage"], "team": item["team"],
                "decisions": 0, "receipts": 0, "moved_receipts": 0,
                "receipt_errors": {}, "http_successes": 0, "usable_ai_plans": 0,
                "resource_value": 0, "effective_damage": 0, "mission_completed": 0,
                "invalid_actions": 0, "planning_ms": [], "controller_faults": 0,
                "perception_rejections": 0, "_receipts": set(), "_ticks": set(), "_actions": {}})
        elif sessions:
            # One bot logs one active session at a time; session events delimit it.
            if kind == "decision":
                if item["tick"] not in current["_ticks"]:
                    current["_ticks"].add(item["tick"])
                    current["decisions"] += 1
                    current["planning_ms"].append(item["planning_ms"])
                    current["_actions"][item["tick"]] = item.get("action", {}).get("action")
                for key in ("resource_value", "effective_damage", "mission_completed", "invalid_actions"):
                    # v1.1 damage may decrease on death; retain latest evidence.
                    current[key] = item.get(key, current[key])
            elif kind == "receipt":
                receipt = item["last_action"]
                rt = receipt.get("tick")
                if (rt in current["_actions"] and receipt.get("action") == current["_actions"][rt]
                        and rt not in current["_receipts"]):
                    current["_receipts"].add(receipt.get("tick"))
                    current["receipts"] += 1
                    current["moved_receipts"] += int(receipt.get("moved") is True)
                    if receipt.get("error"):
                        errors = current["receipt_errors"]
                        code = receipt["error"]
                        errors[code] = errors.get(code, 0) + 1
            elif kind == "ai_result":
                current["http_successes"] += int(item.get("http_success", False))
            elif kind == "ai_plan":
                current["usable_ai_plans"] += 1
            elif kind == "controller_fault":
                current["controller_faults"] += 1
            elif kind == "perception_rejected":
                current["perception_rejections"] += 1
    for record in sessions.values():
        samples = sorted(record.pop("planning_ms"))
        record.pop("_receipts")
        record.pop("_ticks")
        record.pop("_actions")
        record["planning_p95_ms"] = samples[min(len(samples) - 1, int(len(samples) * .95))] if samples else None
        record["planning_max_ms"] = max(samples) if samples else None
        record["action_receipt_observed"] = record["receipts"] > 0
        record["formal_ai_eligibility"] = "须由裁判确认；本地 HTTP 成功不能证明真实模型模式及正式资格"
    return {"sessions": list(sessions.values()), "malformed_log_lines": malformed}
