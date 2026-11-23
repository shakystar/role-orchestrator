"""A definitive startup failure survives fresh CLI processes without dispatch."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(os.environ.get('ROLE_RUNTIME_BIN'), 'requires the real runtime bridge')
class NativeFailedHandoff(unittest.TestCase):
    def test_terminal_failure_replay_uses_only_durable_evidence(self):
        actor = dict(instruction='Read the note.', model='gpt-6.2-sol', effort='low')
        plan = dict(producer=dict(actor, member='writer'), consumer=dict(actor, member='reader'),
                    slot='note', required_fields=['note'])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'state'
            path = root / 'bin'
            path.mkdir()
            # Record process starts, then forward to the real Go bridge. Codex
            # is deliberately absent from PATH to force a pre-execution failure.
            log = root / 'calls'
            wrapper = path / 'bridge'
            wrapper.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_BRIDGE_CALLS"\n'
                               'exec "$ROLE_RUNTIME_BIN" "$@"\n')
            wrapper.chmod(0o755)
            env = {**os.environ, 'PATH': str(path), 'TEST_BRIDGE_CALLS': str(log)}

            def call(op, **kwargs):
                proc = subprocess.run(
                    [sys.executable, '-m', 'role_orchestrator.handoff_runner', '--backend', 'codex',
                     '--runtime', str(wrapper), '--state', str(state)],
                    input=json.dumps(dict(op=op, plan=plan, **kwargs)), text=True, env=env,
                    capture_output=True, timeout=35, check=True)
                return json.loads(proc.stdout)

            def count_submissions():
                return sum('--backend' in line for line in log.read_text().splitlines())

            call('publish-initial', payload={'note': 'sample'})
            self.assertEqual(call('validate')['state'], 'accepted')
            result = call('consume')
            self.assertEqual(result['state'], 'failed', result)
            self.assertTrue(result['reason'])
            receipts = list((state / 'runtime/tasks').glob('*.json'))
            self.assertEqual(len(receipts), 1)
            raw = receipts[0].read_bytes()
            native = json.loads(raw)
            self.assertEqual(native['status'], 'failed')
            self.assertEqual(result['reason'], native['result']['failure']['code'])
            before = hashlib.sha256(raw).digest()
            self.assertEqual(count_submissions(), 1)
            self.assertEqual(call('consume'), result)
            self.assertEqual(call('status')['state'], 'failed')
            self.assertEqual(count_submissions(), 1)
            self.assertEqual(hashlib.sha256(receipts[0].read_bytes()).digest(), before)
