import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from rmtc_bot.config import Config
from rmtc_bot.ai import AIWorker
from rmtc_bot.policies import Policy, POLICIES
from rmtc_bot.audit import analyze
from rmtc_bot.__main__ import main
from .helpers import frame


class DeploymentTests(unittest.TestCase):
    def test_contest_requires_explicit_network_and_gateway(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit):
                main(['run', '--stage', '2', '--contest'])
            with self.assertRaises(SystemExit):
                main(['run', '--stage', '2', '--contest', '--domain-id', '95'])

    def test_contest_refuses_a_model_override(self):
        # Organiser rule: only the unified default model; a stray RMTC_AI_MODEL must stop the launch.
        env = {'RMTC_TEAM_TOKEN': 'do-not-log', 'RMTC_AI_MODEL': 'other-model'}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit):
                main(['run', '--stage', '2', '--contest', '--domain-id', '95',
                      '--ai-url', 'http://127.0.0.1:9/v1/chat/completions'])

    def test_requests_carry_no_model_by_default(self):
        self.assertEqual(Config().ai_model, '')
        self.assertEqual(Config().ai_max_tokens, 1024)

    def test_formal_token_cannot_cross_session(self):
        worker = AIWorker(Config(contest=True), 'do-not-log')
        worker.reset('one')
        self.assertEqual(worker.token, 'do-not-log')
        worker.reset('two')
        self.assertEqual(worker.token, '')
        self.assertFalse(worker.request(None, {}))

    def test_presets_duplicate_and_new_match(self):
        for name in POLICIES:
            p = Policy(Config(), name)
            a = p.decide(frame())
            self.assertEqual(a, p.decide(frame()))
            b = p.decide(frame(session='next', pos=(5, 5)))
            self.assertEqual(b['session_id'], 'next')
            self.assertEqual(p.engine.latest.position, (5, 5))

    def test_damage_final_is_not_peak(self):
        rows = [dict(kind='session', session_id='one', stage=2, team='A')]
        for tick, damage in [(0, 100), (1, 40)]:
            rows.append(dict(kind='decision', tick=tick, effective_damage=damage, planning_ms=1))
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as d:
            path = Path(d)/'audit.jsonl'
            path.write_text('\n'.join(json.dumps(r) for r in rows), encoding='utf-8')
            self.assertEqual(analyze(path)['sessions'][0]['effective_damage'], 40)
