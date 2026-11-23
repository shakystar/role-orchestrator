"""Read the native Go receipt after an interruption before verdict publication."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(os.environ.get('ROLE_RUNTIME_BIN'), 'requires the real runtime bridge')
class AssignmentReceiptRecovery(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / 'state'
        lab = self.root / 'lab'
        docs = lab / 'docs'
        (docs / 'team').mkdir(parents=True)
        (docs / 'features').mkdir()
        (docs / 'team/roster.md').write_text('| reader | Reader | Read | read |\n')
        (docs / 'features/development-handoffs.md').write_text('| read / Read | note | read | Sol / medium |\n')
        git = ['git', '-C', str(lab)]
        for args in (['init', '-q'], ['add', 'docs'],
                     ['-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
                      '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture']):
            subprocess.run(git + args, check=True)
        revision = subprocess.check_output(git + ['rev-parse', 'HEAD'], text=True).strip()
        self.cli = [sys.executable, '-m', 'role_orchestrator.assignment']
        cmd = self.cli + ['--lab', str(lab), '--revision', revision, '--member', 'reader',
                          '--task', 'read-note', '--state', str(self.state),
                          '--runtime', os.environ['ROLE_RUNTIME_BIN'], '--backend', 'fixture']
        response = subprocess.run(cmd, input='{"fixture":{"ok":true}}', text=True,
                                  check=True, capture_output=True)
        self.original = json.loads(response.stdout)
        # Exactly the durable state at the receipt-before-verdict boundary.
        (self.state / 'assignment-v2.verdict.json').unlink()
        self.plan = self.state / 'assignment-v2.plan.json'

    def inspect(self):
        def snapshot():
            return {p.relative_to(self.state): hashlib.sha256(p.read_bytes()).digest()
                    for p in self.state.rglob('*') if p.is_file()}

        before = snapshot()
        proc = subprocess.run(self.cli + ['status', '--state', str(self.state),
                                          '--runtime', os.environ['ROLE_RUNTIME_BIN']],
                              check=True, text=True, capture_output=True)
        self.assertEqual(snapshot(), before)
        return json.loads(proc.stdout)['verdict']

    def test_recover_pinned_attempt_without_submitting_or_writing(self):
        self.assertEqual(self.inspect(), self.original)
        self.assertEqual(self.inspect(), self.original)

    def test_old_plan_without_payload_digest_remains_unconfirmed(self):
        plan = json.loads(self.plan.read_text())
        del plan['input_sha256']
        self.plan.write_text(json.dumps(plan))
        self.assertIsNone(self.inspect())

    def test_receipt_with_different_pinned_input_is_uncertain(self):
        plan = json.loads(self.plan.read_text())
        plan['input_sha256'] = '0' * 64
        self.plan.write_text(json.dumps(plan))
        self.assertEqual(self.inspect()['status'], 'uncertain')
