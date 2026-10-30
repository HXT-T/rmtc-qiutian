from dataclasses import replace
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import unittest
from rmtc_bot.ai import AIWorker, decode_content
from rmtc_bot.config import Config
from rmtc_bot.engine import Engine
from rmtc_bot.protocol import Frame
from .helpers import frame, resource


class FakeGateway:
    def __init__(self, status=200, body=None, gate=None, headers=None, status_for=None):
        self.requests = []
        self.gate = gate
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = self.rfile.read(int(self.headers["Content-Length"]))
                owner.requests.append((self.path, dict(self.headers), json.loads(data)))
                if gate:
                    gate.wait(3)
                self.send_response(status_for(json.loads(data)) if status_for else status)
                self.send_header("Content-Type", "application/json")
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                value = body if body is not None else {"choices": [{"message": {"content": '{"strategy":"collect","events":[]}'}}]}
                self.wfile.write(json.dumps(value).encode())
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    @property
    def url(self):
        return "http://127.0.0.1:%d/v1/chat/completions" % self.server.server_port

    def __exit__(self, *args):
        if self.gate:
            self.gate.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


def finish(worker):
    end = time.monotonic() + 3
    while worker.busy and time.monotonic() < end:
        time.sleep(.005)
    if worker.busy:
        raise AssertionError("AI worker did not finish")


class AITests(unittest.TestCase):
    def test_http_contract_and_usable_plan(self):
        with FakeGateway() as gateway:
            logs = []
            c = replace(Config(), ai_url=gateway.url)
            worker = AIWorker(c, "test-not-a-real-token", lambda kind, **kw: logs.append((kind, kw)))
            engine = Engine(c, worker)
            engine.step(frame())
            finish(worker)
            engine.step(frame(tick=1))
            self.assertEqual(engine.strategy, "collect")
            self.assertEqual(worker.successes, 1)
            path, headers, body = gateway.requests[0]
            self.assertEqual(path, "/v1/chat/completions")
            self.assertEqual(headers["Authorization"], "Bearer test-not-a-real-token")
            self.assertIn("Content-Length", headers)
            self.assertNotIn("Transfer-Encoding", headers)
            self.assertIs(body["stream"], False)
            self.assertEqual(body["model"], "deepseek-flash")
            self.assertEqual(len(gateway.requests), 1)
            self.assertNotIn("test-not-a-real-token", repr(logs))

    def test_inflight_never_blocks_decisions_and_single_flight(self):
        gate = threading.Event()
        with FakeGateway(gate=gate) as gateway:
            c = replace(Config(), ai_url=gateway.url)
            worker = AIWorker(c, "test")
            e = Engine(c, worker)
            started = time.perf_counter()
            first = e.step(frame(entities=[resource((3, 2))]))
            second = e.step(frame(tick=1, entities=[resource((3, 2))]))
            self.assertLess(time.perf_counter() - started, 0.1)
            self.assertEqual(first["action"], "move")
            self.assertEqual(second["tick"], 1)
            self.assertTrue(worker.busy)
            self.assertFalse(worker.request(e.latest, {}))
            gate.set()
            finish(worker)

    def test_old_generation_and_aged_result_are_ignored(self):
        c = Config(ai_max_age=2)
        worker = AIWorker(c, "")
        e = Engine(c, worker)
        e.step(frame())
        old = worker.generation
        e.step(frame(session="new"))
        plan = {"strategy": "combat", "events": [{"kind": "reflection", "start": 0, "end": 100}]}
        worker.results.put({"session": "test-session", "generation": old, "tick": 0, "digest": "old", "plan": plan})
        worker.results.put({"session": "new", "generation": worker.generation, "tick": 0, "digest": "aged", "plan": plan})
        e.step(frame(tick=10, session="new"))
        self.assertFalse(e.events.events)
        self.assertEqual(e.strategy, "balanced")

    def test_error_classification_and_no_secret_logging(self):
        for status, code, disabled in ((401, "invalid_token", True),
                                       (429, "quota_exceeded", True),
                                       (429, "concurrency_limit", False),
                                       (503, "audit_unavailable", False),
                                       (502, "upstream_unavailable", False)):
            with self.subTest(status=status, code=code), FakeGateway(status, {"error": {"code": code}}) as gateway:
                w = AIWorker(replace(Config(), ai_url=gateway.url), "secret-for-test")
                w.reset("test-session")
                self.assertTrue(w.request(Frame.parse(frame()), {}))
                finish(w)
                result = list(w.poll())[0]
                self.assertEqual(result["code"], code)
                self.assertEqual(w.disabled, disabled)
                self.assertEqual(w.successes, 0)
                self.assertNotIn("secret-for-test", repr(result))
                self.assertGreater(w.next_request, time.monotonic())

    def test_http_success_is_distinct_from_valid_strategy(self):
        body = {"choices": [{"message": {"content": "not JSON"}}]}
        with FakeGateway(body=body) as gateway:
            w = AIWorker(replace(Config(), ai_url=gateway.url), "test")
            w.reset("test-session")
            w.request(Frame.parse(frame()), {})
            finish(w)
            self.assertEqual(w.successes, 1)
            result = list(w.poll())[0]
            self.assertEqual(result["status"], "failed")
            self.assertNotIn("plan", result)

    def test_redirect_is_not_followed(self):
        with FakeGateway(302, {}, headers={"Location": "http://127.0.0.1:1/leak"}) as gateway:
            w = AIWorker(replace(Config(), ai_url=gateway.url), "test")
            w.reset("test-session")
            w.request(Frame.parse(frame()), {})
            finish(w)
            result = list(w.poll())[0]
            self.assertEqual(result["http"], 302)
            self.assertEqual(w.successes, 0)

    def test_model_fences_only_no_executable_output(self):
        self.assertEqual(decode_content('```json\n{"events": []}\n```'), {"events": []})
        with self.assertRaises(ValueError):
            decode_content("run this code: print(1)")

    def test_stage1_and_empty_token_do_not_request(self):
        for c, token in ((Config(stage=1), "test"), (Config(), "")):
            w = AIWorker(c, token)
            w.reset("test-session")
            self.assertFalse(w.request(Frame.parse(frame()), {}))

    def test_invalid_gateway_path(self):
        for url in ("https://example.org/admin", "file:///data", "http://user:pass@example.org/v1/chat/completions"):
            with self.assertRaises(ValueError):
                AIWorker(replace(Config(), ai_url=url))

    def test_validated_semantic_result_changes_current_control(self):
        plan = {"strategy": "collect", "events": [{"kind": "invert", "start": 1, "end": 10,
                                                    "rect": [0, 0, 5, 5]}]}
        body = {"choices": [{"message": {"content": json.dumps(plan)}}]}
        with FakeGateway(body=body) as gateway:
            c = replace(Config(explore_weight=0.0), ai_url=gateway.url)
            worker = AIWorker(c, "test")
            engine = Engine(c, worker)
            engine.step(frame())
            finish(worker)
            result = engine.step(frame(tick=1, entities=[resource((3, 2))]))
            self.assertEqual(result["action"], "move")
            self.assertEqual(result["direction"], "left")
            self.assertEqual(len(engine.events.events), 1)


if __name__ == "__main__":
    unittest.main()
