"""Public CLI integration for a development writer and its distinct reviewer."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(os.environ.get('ROLE_RUNTIME_BIN'), 'requires the real fixture bridge')
class NamedPatchHandoff(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name) / 'state'
        self.bridge = str(Path(os.environ['ROLE_RUNTIME_BIN']).resolve(strict=True))
        self.patch = dict(base_sha='a' * 40, diff='fixture patch bytes\n', tests=['check'], unverified=[])
        self.patch_hash = hashlib.sha256(self.patch['diff'].encode()).hexdigest()
        self.context = dict(criterion={'id': 'read-only'}, base_sha=self.patch['base_sha'],
                            source={'path': 'fixture.py'}, regression={'case': 'read-only'},
                            patch_sha256=self.patch_hash)
        actor = dict(model='gpt-6.2-sol', effort='medium', profile='development-patch-v1')
        self.plan = dict(producer=dict(actor, member='acl-007', instruction='Produce a patch.'),
                         consumer=dict(actor, member='acl-005', instruction='Review the patch.'),
                         slot='python-change', required_fields=[],
                         producer_input=dict(goal='repair the fixture', base_sha=self.patch['base_sha'],
                                             source=self.context['source'], regression=self.context['regression'],
                                             acceptance=self.context['criterion'], output_contract={'kind': 'patch'}),
                         consumer_input=dict(field='authored_revision', format='revision', context=self.context))
        self.review = dict(verdict='accept', criterion_id='read-only', patch_sha256=self.patch_hash,
                           reason='fixture review')

    def cli(self, op, **fields):
        cmd = [sys.executable, '-m', 'role_orchestrator.handoff_runner', '--state', str(self.state),
               '--runtime', self.bridge, '--backend', 'fixture']
        proc = subprocess.run(cmd, input=json.dumps(dict(plan=self.plan, op=op, **fields)), text=True,
                              capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, 0, (proc.stdout, proc.stderr))
        return json.loads(proc.stdout)

    def test_reviewer_receives_verified_writer_output_and_separate_context(self):
        self.cli('publish-initial', payload={'base_sha': self.patch['base_sha']})
        repair = self.cli('validate')
        writer = self.cli('repair', request_id=repair['request_id'], fixture=self.patch)
        self.assertEqual(writer['state'], 'published', writer)
        self.assertEqual(writer['actual_member_id'], 'acl-007')
        self.assertEqual(self.cli('validate')['state'], 'accepted')
        reviewer = self.cli('consume', fixture=self.review)
        self.assertEqual((reviewer['state'], reviewer['actual_member_id']), ('completed', 'acl-005'), reviewer)

        run_id = json.loads((self.state / 'run-identity.json').read_text())['run_id']
        cmd = [self.bridge, '--state', str(self.state / 'runtime'), '--run-id', run_id,
               '--inspect-attempt', reviewer['attempt_id']]
        envelope = json.loads(subprocess.check_output(cmd, timeout=10))
        wire = json.loads(base64.b64decode(envelope['wire']))
        body = json.loads(wire['input'])
        expected_ref = dict(**writer['artifact'], member_id='acl-007', run_id=run_id, artifact=self.patch)
        self.assertEqual(body['inputs'], dict(**self.context, authored_revision=expected_ref))
        self.assertEqual(body['payload'], self.patch)
        self.assertEqual(self.cli('status')['output']['payload'], self.review)
        self.assertEqual(self.cli('consume', fixture={'unwanted': True}), reviewer)

    def test_wrong_context_patch_digest_blocks_review_before_runtime_execution(self):
        self.context['patch_sha256'] = '0' * 64
        self.cli('publish-initial', payload=self.patch)
        self.assertEqual(self.cli('validate')['state'], 'accepted')
        blocked = self.cli('consume', fixture=self.review)
        self.assertEqual((blocked['state'], blocked['reason']), ('blocked', 'review_patch_input_mismatch'))
        self.assertEqual(list((self.state / 'runtime' / 'tasks').glob('*.json')), [])
