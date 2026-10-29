import json
from pathlib import Path
import tempfile
import unittest
from rmtc_bot.audit import AuditLog, analyze


class AuditTests(unittest.TestCase):
    def test_receipt_must_match_own_submitted_tick_and_session(self):
        temp_root = Path(__file__).resolve().parents[1] / "work" / "tests"
        temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as directory:
            path = Path(directory) / "audit.jsonl"
            log = AuditLog(path)
            log("session", session_id="one", stage=1, team="A")
            log("receipt", tick=5, last_action={"tick": 4, "action": "move", "moved": True})
            log("decision", tick=5, action={"action": "move"}, planning_ms=1.2)
            log("receipt", tick=6, last_action={"tick": 5, "action": "move", "moved": True})
            log("receipt", tick=6, last_action={"tick": 5, "action": "move", "moved": True})
            log("session", session_id="two", stage=1, team="A")
            log("receipt", tick=0, last_action={"tick": 5, "action": "move", "moved": True})
            log.close()
            report = analyze(path)
            self.assertEqual(report["sessions"][0]["receipts"], 1)
            self.assertEqual(report["sessions"][0]["moved_receipts"], 1)
            self.assertTrue(report["sessions"][0]["action_receipt_observed"])
            self.assertFalse(report["sessions"][1]["action_receipt_observed"])


if __name__ == "__main__":
    unittest.main()
