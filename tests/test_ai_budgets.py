from dataclasses import replace
import time
import unittest
from rmtc_bot.ai import AIWorker
from rmtc_bot.config import Config
from rmtc_bot.engine import Engine
from rmtc_bot.protocol import Frame
from .helpers import frame
from .test_ai import FakeGateway, finish


class BudgetTests(unittest.TestCase):
    def test_request_budget_survives_session_reset_and_counts_failures(self):
        with FakeGateway(502, {'error': {'code': 'upstream_unavailable'}}) as gateway:
            worker = AIWorker(replace(Config(), ai_url=gateway.url, ai_max_attempts=1), 'test')
            worker.reset('test-session')
            self.assertTrue(worker.request(Frame.parse(frame()), {}))
            finish(worker)
            worker.reset('another')
            self.assertFalse(worker.request(Frame.parse(frame(session='another')), {}))
            self.assertEqual(worker.attempts, 1)
            self.assertEqual(worker.lifetime_successes, 0)
            self.assertEqual(len(gateway.requests), 1)

    def test_success_budget_is_token_lifetime_not_reset_by_new_session(self):
        with FakeGateway() as gateway:
            worker = AIWorker(replace(Config(), ai_url=gateway.url, ai_max_calls=1), 'test')
            worker.reset('test-session')
            worker.request(Frame.parse(frame()), {})
            finish(worker)
            worker.reset('another')
            self.assertEqual(worker.successes, 0)
            self.assertEqual(worker.lifetime_successes, 1)
            self.assertFalse(worker.request(Frame.parse(frame(session='another')), {}))

    def test_schema_failure_backoff_and_bounded_batch_retry(self):
        with FakeGateway(body={'choices': [{'message': {'content': '{"events":"wrong"}'}}]}) as gateway:
            worker = AIWorker(replace(Config(), ai_url=gateway.url, ai_max_digest_attempts=2), 'test')
            engine = Engine(worker.config, worker)
            engine.step(frame())
            finish(worker)
            engine.step(frame(tick=1))
            self.assertEqual(worker.failures, 1)
            self.assertGreater(worker.next_request, time.monotonic())
            worker.next_request = 0  # advance the test clock without sleeping
            engine.step(frame(tick=2))
            finish(worker)
            engine.step(frame(tick=3))
            self.assertEqual(worker.failures, 2)
            worker.next_request = worker.last_request_at = 0  # advance the test clock
            self.assertFalse(worker.request(engine.latest, engine.announcements))
            self.assertEqual(len(gateway.requests), 2)
            self.assertTrue(worker.request(engine.latest, {'different opaque public text': 3}))
            finish(worker)

    def test_new_announcement_is_not_held_back_by_failure_backoff(self):
        with FakeGateway(body={'choices': [{'message': {'content': '{"events":"wrong"}'}}]}) as gateway:
            worker = AIWorker(replace(Config(), ai_url=gateway.url), 'test')
            engine = Engine(worker.config, worker)
            engine.step(frame())
            finish(worker)
            engine.step(frame(tick=1))
            self.assertGreater(worker.next_request, time.monotonic())  # backoff for this batch
            worker.last_request_at = time.monotonic() - worker.config.ai_min_interval  # spacing elapsed
            self.assertFalse(worker.request(engine.latest, engine.announcements))  # same text waits
            self.assertTrue(worker.request(engine.latest, ['new public announcement']))  # new text goes now
            finish(worker)
            self.assertEqual(len(gateway.requests), 2)

    def test_one_malformed_event_does_not_discard_valid_events(self):
        import json
        from rmtc_bot.events import validate_plan
        plan = {'strategy': 'collect', 'events': [
            {'kind': 'heal', 'start': 5, 'end': 25, 'point': [3, 3]},
            {'kind': 'chili', 'start': 5, 'end': 30},            # missing point
            {'kind': 'heal', 'start': 5, 'end': 25, 'point': [8, 3]}]}
        with self.assertRaises(ValueError):
            validate_plan(plan, 12, 12)  # strict mode unchanged
        strategy, events, errors = validate_plan(plan, 12, 12, partial=True)
        self.assertEqual((strategy, [e.point for e in events]), ('collect', [(3, 3), (8, 3)]))
        self.assertEqual([i for i, _ in errors], [1])
        logs = []
        body = {'choices': [{'message': {'content': json.dumps(plan)}}]}
        with FakeGateway(body=body) as gateway:
            worker = AIWorker(replace(Config(), ai_url=gateway.url), 'test')
            engine = Engine(worker.config, worker, log=lambda kind, **kw: logs.append((kind, kw)))
            engine.step(frame())
            finish(worker)
            engine.step(frame(tick=1))
        self.assertEqual(len(engine.events.events), 2)
        partial = [kw for kind, kw in logs if kind == 'ai_partial']
        self.assertEqual(partial[0]['rejected'][0]['index'], 1)
        self.assertIsNone(worker.accepted_digest)  # batch may be retried to recover the bad item

    def test_second_slot_only_for_new_text_and_newest_ack_wins(self):
        import threading
        gate = threading.Event()
        with FakeGateway(gate=gate) as gateway:
            worker = AIWorker(replace(Config(), ai_url=gateway.url, ai_max_inflight=2), 'test')
            f = Frame.parse(frame())
            self.assertTrue(worker.request(f, ['first announcement']))   # slow call in flight
            worker.last_request_at = time.monotonic() - 2                  # spacing elapsed
            self.assertFalse(worker.request(f, ['first announcement']))  # same text waits
            self.assertTrue(worker.request(f, ['second announcement']))  # new text uses slot 2
            worker.last_request_at = time.monotonic() - 2
            self.assertFalse(worker.request(f, ['third announcement']))  # never more than two
            gate.set()
            finish(worker)
        worker.acknowledge('new', tick=30)
        worker.acknowledge('old', tick=10)  # an older batch finishing last does not win
        self.assertEqual(worker.accepted_digest, 'new')
        worker.reset('next-session')
        worker.acknowledge('fresh', tick=0)
        self.assertEqual(worker.accepted_digest, 'fresh')
        self.assertEqual(Config().ai_max_inflight, 1)  # off by default until validated

    def test_json_mode_is_requested_unless_switched_off(self):
        import json
        for flag in (True, False):
            with FakeGateway() as gateway:
                worker = AIWorker(replace(Config(), ai_url=gateway.url, ai_json_mode=flag), 'test')
                worker.request(Frame.parse(frame()), ['announcement'])
                finish(worker)
                body = gateway.requests[0][2]  # (path, headers, parsed body)
            self.assertEqual('response_format' in body, flag)
        from rmtc_bot.ai import SYSTEM
        self.assertIn('JSON', SYSTEM)  # providers require the word JSON in the prompt for json_object mode

    def test_thinking_is_switched_off_in_every_request(self):
        with FakeGateway() as gateway:
            worker = AIWorker(replace(Config(), ai_url=gateway.url), 'test')
            worker.request(Frame.parse(frame()), ['announcement'])
            finish(worker)
            body = gateway.requests[0][2]
        self.assertEqual(body['thinking'], {'type': 'disabled'})
        self.assertEqual(body['model'], 'deepseek-flash')  # handbook: the unified model
        self.assertEqual((body['max_tokens'], body['response_format']), (512, {'type': 'json_object'}))

    def test_gateway_rejecting_optional_fields_drops_them_one_at_a_time(self):
        reject = lambda body: 400 if 'response_format' in body or 'thinking' in body else 200
        with FakeGateway(status_for=reject) as gateway:
            worker = AIWorker(replace(Config(), ai_url=gateway.url), 'test')
            engine = Engine(worker.config, worker)
            for tick in range(4):
                worker.next_request = worker.last_request_at = 0  # advance the test clock
                engine.step(frame(tick=tick))
                finish(worker)
            bodies = [b for _, _, b in gateway.requests]
            self.assertEqual([('response_format' in b, 'thinking' in b) for b in bodies[:3]],
                             [(True, True), (False, True), (False, False)])
            self.assertGreaterEqual(worker.successes, 1)

    def test_relative_time_is_computed_by_program_and_cross_checked(self):
        from rmtc_bot.events import absolutize, validate_plan
        item = {'kind': 'explosion', 'announce_tick': 50, 'delay': 25, 'duration': 1, 'rect': [4, 4, 9, 9], 'start': 75}
        self.assertEqual((absolutize(item, {50})['start'], absolutize(item, {50})['end']), (75, 76))
        pan = {'kind': 'vulnerability', 'announce_tick': 180, 'delay': 5, 'duration': 30, 'params': {'trigger_ticks': 20}}
        self.assertEqual(absolutize(pan, {181})['params']['trigger_end'], 205)
        with self.assertRaises(ValueError):
            absolutize(dict(item, start=80), {50})          # stated start disagrees with 50 + 25
        with self.assertRaises(ValueError):
            absolutize(item, {120})                          # no announcement arrived near Tick 50
        with self.assertRaises(ValueError):
            absolutize({'kind': 'heal', 'delay': 5, 'point': [1, 1]}, {50})  # missing announce_tick/duration
        absolute = {'kind': 'heal', 'start': 30, 'end': 50, 'point': [1, 1]}
        self.assertIs(absolutize(absolute, {10}), absolute)  # absolute output passes through unchanged
        plan = {'events': [item, dict(item, start=99)]}
        _, events, errors = validate_plan(plan, 12, 12, partial=True, relative=True, known_ticks={50})
        self.assertEqual(([(e.start, e.end) for e in events], [i for i, _ in errors]), ([(75, 76)], [1]))

    def test_relative_prompt_only_when_switched_on(self):
        from rmtc_bot.ai import SYSTEM, SYSTEM_RELATIVE
        for flag, prompt in ((False, SYSTEM), (True, SYSTEM_RELATIVE)):
            with FakeGateway() as gateway:
                worker = AIWorker(replace(Config(), ai_url=gateway.url, ai_relative_time=flag), 'test')
                worker.request(Frame.parse(frame()), ['announcement'])
                finish(worker)
            self.assertEqual(gateway.requests[0][2]['messages'][0]['content'], prompt)
        self.assertNotEqual(SYSTEM, SYSTEM_RELATIVE)

    def test_gateway_rejecting_json_mode_falls_back_to_plain_requests(self):
        reject = lambda body: 400 if 'response_format' in body else 200
        with FakeGateway(status_for=reject) as gateway:
            worker = AIWorker(replace(Config(), ai_url=gateway.url), 'test')
            engine = Engine(worker.config, worker)
            engine.step(frame())
            finish(worker)
            self.assertFalse(worker.json_mode)
            engine.step(frame(tick=1))
            worker.next_request = worker.last_request_at = 0  # advance the test clock
            engine.step(frame(tick=2))
            finish(worker)
            bodies = [b for _, _, b in gateway.requests]
            self.assertIn('response_format', bodies[0])
            self.assertNotIn('response_format', bodies[-1])
            self.assertGreaterEqual(worker.successes, 1)

    def test_old_generation_http_success_still_counts_lifetime_quota(self):
        import threading
        gate = threading.Event()
        with FakeGateway(gate=gate) as gateway:
            worker = AIWorker(replace(Config(), ai_url=gateway.url, ai_max_calls=1), 'test')
            worker.reset('test-session')
            worker.request(Frame.parse(frame()), {})
            worker.reset('new')
            gate.set()
            finish(worker)
            self.assertEqual(worker.successes, 0)
            self.assertEqual(worker.lifetime_successes, 1)
            worker.next_request = 0
            self.assertFalse(worker.request(Frame.parse(frame(session='new')), {}))

    def test_invalid_budget_types_rejected(self):
        for kwargs in ({'ai_max_attempts': True}, {'ai_max_calls': 1.5},
                       {'ai_max_digest_attempts': 0}, {'ai_max_digest_attempts': 11}):
            with self.assertRaises(ValueError):
                Config(**kwargs)

    def test_truncated_json_is_not_accepted_even_if_it_parses(self):
        body = {'choices': [{'finish_reason': 'length', 'message': {'content': '{"events":[]}'}}]}
        with FakeGateway(body=body) as gateway:
            worker = AIWorker(replace(Config(), ai_url=gateway.url), 'test')
            worker.reset('test-session')
            worker.request(Frame.parse(frame()), {})
            finish(worker)
            result = list(worker.poll())[0]
            self.assertTrue(result['http_success'])
            self.assertNotIn('plan', result)


if __name__ == '__main__':
    unittest.main()
