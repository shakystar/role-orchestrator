"""Synthetic integration tests for the Python handoff CLI and the real Go fixture bridge.

Run from the repo root with ROLE_RUNTIME_BIN set to the built bridge:
    uv run --frozen python -m unittest discover -s tests -p test_handoff_runner.py -v

The supplied excerpts were not executable here. This suite assumes the CLI is
role_orchestrator.handoff_runner, Runtime can provision a fresh state directory,
and the inspector's native receipt bytes exist under STATE/runtime. A configured
binary that fails those assumptions fails the tests rather than skipping them.

Faults are staged between CLI processes, not by killing a live process. The
replay assertions cover durable effects, not the number of bridge invocations.
The suite does not cover power loss, concurrent runners, wire-request tampering,
result-text-only tampering, or the real Codex backend.
"""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from role_orchestrator.attempts import AttemptReader
from role_orchestrator.runtime import Runtime, load_identity


ROOT = Path(__file__).resolve().parents[1]
PRODUCER = 'author-a'
CONSUMER = 'reviewer-b'
SLOT = 'report'
MODEL = 'gpt-6.2-sol'
EFFORT = 'low'
PLAN = {
    'producer': {'member': PRODUCER, 'instruction': 'Revise the synthetic report.',
                 'model': MODEL, 'effort': EFFORT},
    'consumer': {'member': CONSUMER, 'instruction': 'Read the synthetic report.',
                 'model': MODEL, 'effort': EFFORT},
    'slot': SLOT,
    'required_fields': ['summary'],
}


def reviewed_ref(artifact):
    return {key: artifact[key] for key in ('revision', 'sha256', 'run_id', 'producer', 'kind', 'slot')
            if key in artifact}


class HandoffRunnerFixtureTests(unittest.TestCase):
    def setUp(self):
        configured = os.environ.get('ROLE_RUNTIME_BIN')
        if not configured:
            self.skipTest('set ROLE_RUNTIME_BIN to the real Go bridge executable')
        self.binary = Path(configured).expanduser().resolve(strict=True)
        self.assertTrue(self.binary.is_file() and os.access(self.binary, os.X_OK),
                        'ROLE_RUNTIME_BIN must name an executable file')
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / 'state'
        self.state.mkdir()
        with closing(self.runtime()) as runtime:
            self.run_id = runtime.identity['run_id']
        self.assertEqual(load_identity(self.state)['run_id'], self.run_id)
        self.db_path = self.state / 'runner.sqlite3'

    def runtime(self):
        return Runtime(str(self.binary), self.state, 'fixture', 'codex',
                       model=MODEL, reasoning_effort=EFFORT)

    def cli(self, op, *, binary=None, code=0, **fields):
        proc = subprocess.run(
            [sys.executable, '-m', 'role_orchestrator.handoff_runner',
             '--state', str(self.state), '--runtime', str(binary or self.binary),
             '--backend', 'fixture'],
            cwd=ROOT, input=json.dumps({'plan': PLAN, 'op': op, **fields}),
            capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(proc.returncode, code, (op, proc.stdout, proc.stderr))
        result = json.loads(proc.stdout)
        if code == 0:
            self.assertNotEqual(result.get('state') if isinstance(result, dict) else result,
                                'error', result)
        return result

    def job(self, task_id):
        with closing(sqlite3.connect(self.db_path)) as db:
            row = db.execute('SELECT intent, ack FROM jobs WHERE task_id=?', (task_id,)).fetchone()
        self.assertIsNotNone(row, f'no durable job for {task_id}')
        intent, ack = row
        return json.loads(intent), None if ack is None else json.loads(ack)

    def job_ids(self):
        with closing(sqlite3.connect(self.db_path)) as db:
            return {row[0] for row in db.execute('SELECT task_id FROM jobs')}

    def evidence(self, intent):
        return AttemptReader(self.binary, self.state, self.run_id).read(intent['attempt_id'])

    def native_receipts(self):
        # Find native receipt files by their JSON shape, not a guessed Go store
        # filename. Keep the bytes and file metadata to detect replay writes.
        receipts = {}
        for path in (self.state / 'runtime').rglob('*'):
            if path.is_symlink() or not path.is_file():
                continue
            data = path.read_bytes()
            try:
                doc = json.loads(data)
            except (UnicodeError, ValueError):
                continue
            if isinstance(doc, dict) and isinstance(doc.get('task'), dict) and 'status' in doc:
                stat = path.stat()
                receipts[path] = (data, stat.st_ino, stat.st_mtime_ns)
        return receipts

    def native_paths_for(self, receipt_bytes):
        paths = [path for path, (data, _, _) in self.native_receipts().items()
                 if data == receipt_bytes]
        self.assertTrue(paths, 'inspector bytes did not match a persisted native receipt')
        return paths

    def assert_native_ack(self, ack, member, fixture):
        intent, stored_ack = self.job(ack['task_id'])
        self.assertEqual(stored_ack, ack)
        self.assertEqual(intent['phase'], 'submitted')
        self.assertEqual(intent['member'], member)
        self.assertEqual(ack['attempt_id'], intent['attempt_id'])
        self.assertEqual(ack['receipt_id'], intent['attempt_id'])
        self.assertEqual((ack['actual_member_id'], ack['actual_role_version']), (member, 'v1'))

        evidence = self.evidence(intent)
        self.assertIsNotNone(evidence)
        wire_bytes, receipt_bytes = evidence
        paths = self.native_paths_for(receipt_bytes)
        self.assertEqual(ack['receipt_sha256'], hashlib.sha256(paths[0].read_bytes()).hexdigest())
        wire, receipt = json.loads(wire_bytes), json.loads(receipt_bytes)
        self.assertEqual(wire['schema_version'], 1)
        self.assertEqual((wire['run_id'], wire['attempt_id']), (self.run_id, intent['attempt_id']))
        task = intent['task']
        self.assertEqual(task['role'], member)
        self.assertEqual({key: wire[key] for key in task}, task)
        self.assertEqual(receipt['status'], 'succeeded')
        self.assertEqual({key: receipt['task'][key] for key in ('id', 'role', 'input')},
                         {key: task[key] for key in ('id', 'role', 'input')})
        self.assertEqual(receipt['task']['version'], 'v1')
        result = receipt['result']
        self.assertEqual((result['actual_member_id'], result['actual_role_version']), (member, 'v1'))
        self.assertEqual(json.loads(result['text']), fixture)
        return json.loads(wire['input'])

    def assert_replays(self, original, op, **fields):
        task_id = original['task_id']
        intent, _ = self.job(task_id)
        jobs = self.job_ids()
        receipts = self.native_receipts()
        self.assertTrue(receipts)

        # First exercise the saved ack path in a fresh CLI process. Then model
        # losing only the ack commit, leaving the Go receipt and handoff intact.
        self.assertEqual(self.cli(op, **fields), original)
        with closing(sqlite3.connect(self.db_path)) as db, db:
            changed = db.execute('UPDATE jobs SET ack=NULL WHERE task_id=? AND ack IS NOT NULL',
                                 (task_id,)).rowcount
            self.assertEqual(changed, 1)
        self.assertEqual(self.cli(op, **fields), original)
        self.assertEqual(self.job(task_id), (intent, original))
        self.assertEqual(self.job_ids(), jobs)
        self.assertEqual(self.native_receipts(), receipts)

    def publish_valid(self):
        initial = self.cli('publish-initial', payload={'summary': 'forecast'})
        self.assertEqual(initial['revision'], 1)
        self.assertEqual(self.cli('validate')['state'], 'accepted')
        return initial

    def test_semantic_rejection_revision_explicit_accept_and_replay(self):
        initial = self.publish_valid()
        feedback = {'criterion': 'meaning', 'field': 'summary', 'observed': 'forecast',
                    'required': 'measurement', 'check': 'compare with the synthetic reading'}
        rejected = self.cli('validate', feedback=feedback, reviewed_ref=reviewed_ref(initial))
        self.assertEqual(rejected['state'], 'needs_revision')
        rid = rejected['request_id']
        self.assertTrue(rid)
        still_rejected = self.cli('accept-review', reviewed_ref=reviewed_ref(initial))
        self.assertEqual((still_rejected['state'], still_rejected['request_id']), ('needs_revision', rid))
        self.assertEqual(self.cli('consume', fixture={'unexpected': True})['state'], 'blocked')
        self.assertEqual(self.job_ids(), set())
        self.assertEqual(self.native_receipts(), {})

        corrected = {'summary': 'measurement'}
        repair = self.cli('repair', request_id=rid, fixture=corrected)
        self.assertEqual((repair['state'], repair['request_id'], repair['base_revision']),
                         ('published', rid, 1))
        artifact = repair['artifact']
        self.assertEqual(artifact['revision'], 2)
        self.assertNotEqual(artifact['sha256'], initial['sha256'])
        repair_input = self.assert_native_ack(repair, PRODUCER, corrected)
        self.assertEqual((repair_input['slot'], repair_input['request_id'], repair_input['base_revision']),
                         (SLOT, rid, 1))
        self.assertEqual(repair_input['current']['artifact']['sha256'], initial['sha256'])
        self.assert_replays(repair, 'repair', request_id=rid, fixture={'summary': 'do not publish'})

        ref = reviewed_ref(artifact)
        self.assertEqual(self.cli('validate', reviewed_ref=ref)['state'], 'blocked')
        receipts = self.native_receipts()
        self.assertEqual(self.cli('consume', fixture={'unexpected': True})['state'], 'blocked')
        self.assertEqual(self.job_ids(), {repair['task_id']})
        self.assertEqual(self.native_receipts(), receipts)
        self.assertEqual(self.cli('accept-review', reviewed_ref=ref)['state'], 'accepted')

        output = {'consumed': True}
        consume = self.cli('consume', fixture=output)
        self.assertEqual(consume['state'], 'completed')
        consume_input = self.assert_native_ack(consume, CONSUMER, output)
        self.assertEqual(consume_input['artifact']['revision'], 2)
        self.assertEqual(consume_input['artifact']['sha256'], ref['sha256'])
        self.assertNotEqual(repair['attempt_id'], consume['attempt_id'])
        self.assert_replays(consume, 'consume', fixture={'do_not_run': True})
        self.assertEqual(self.cli('status')['state'], 'completed')

    def test_structural_rejection_blocks_dispatch_and_reuses_request(self):
        initial = self.cli('publish-initial', payload={'note': 'missing the required summary'})
        self.assertEqual(initial['revision'], 1)
        rejected = self.cli('validate')
        self.assertEqual(rejected['state'], 'needs_revision')
        rid = rejected['request_id']
        self.assertTrue(rid)
        repeated = self.cli('validate')
        self.assertEqual((repeated['state'], repeated['request_id']), ('needs_revision', rid))
        self.assertEqual(self.cli('consume', fixture={'unexpected': True})['state'], 'blocked')
        self.assertEqual(self.job_ids(), set())
        self.assertEqual(self.native_receipts(), {})

        corrected = {'summary': 'corrected structure'}
        repaired = self.cli('repair', request_id=rid, fixture=corrected)
        self.assertEqual(repaired['state'], 'published')
        self.assertEqual(repaired['artifact']['revision'], 2)
        self.assert_native_ack(repaired, PRODUCER, corrected)
        self.assertEqual(self.cli('validate')['state'], 'accepted')
        consumed = self.cli('consume', fixture={'consumed': True})
        self.assertEqual(consumed['state'], 'completed')
        self.assert_native_ack(consumed, CONSUMER, {'consumed': True})

    def prepare_consume(self, output):
        self.publish_valid()
        # The CLI can resolve this file, but Runtime cannot start it. This
        # reaches the pre-submit boundary without replacing any Python method.
        unstartable = self.state.parent / 'unstartable-runtime'
        unstartable.write_bytes(b'not executable\n')
        unstartable.chmod(0o600)
        result = self.cli('consume', binary=unstartable, code=2, fixture=output)
        self.assertEqual(result['state'], 'error')
        intent, ack = self.job('consume')
        self.assertEqual(intent['phase'], 'prepared')
        self.assertIsNone(ack)
        self.assertEqual(json.loads(intent['task']['input'])['fixture'], output)
        self.assertIsNone(self.evidence(intent))
        self.assertEqual(self.native_receipts(), {})
        return intent

    def stage_submitted(self, prepared):
        # Model a crash immediately after the submitted commit and before Go
        # receives the task. No CLI holds the state-directory lock here.
        submitted = {**prepared, 'phase': 'submitted'}
        with closing(sqlite3.connect(self.db_path)) as db, db:
            changed = db.execute('UPDATE jobs SET intent=? WHERE task_id=? AND ack IS NULL',
                                 (json.dumps(submitted), prepared['task']['id'])).rowcount
            self.assertEqual(changed, 1)
        return submitted

    def test_pre_submit_start_failure_recovers_original_intent(self):
        original_output = {'answer': 'original prepared fixture'}
        prepared = self.prepare_consume(original_output)
        ack = self.cli('consume', fixture={'answer': 'replacement must be ignored'})
        self.assertEqual(ack['state'], 'completed')
        self.assertEqual(ack['attempt_id'], prepared['attempt_id'])
        self.assertEqual(self.job('consume')[0], {**prepared, 'phase': 'submitted'})
        self.assert_native_ack(ack, CONSUMER, original_output)
        self.assertEqual(self.cli('status')['state'], 'completed')

    def test_submitted_without_receipt_stays_uncertain_across_restarts(self):
        prepared = self.prepare_consume({'answer': 'would succeed if dispatched'})
        submitted = self.stage_submitted(prepared)
        for fixture in ({'retry': 1}, {'retry': 2}):
            result = self.cli('consume', fixture=fixture)
            self.assertEqual((result['state'], result['reason']),
                             ('uncertain', 'receipt absent after intent'))
            self.assertEqual((result['task_id'], result['attempt_id']), ('consume', submitted['attempt_id']))
            self.assertEqual(self.job('consume'), (submitted, None))
            self.assertIsNone(self.evidence(submitted))
            self.assertEqual(self.native_receipts(), {})
        self.assertNotEqual(self.cli('status')['state'], 'completed')

    def test_native_receipt_actor_tamper_is_rejected_before_completion(self):
        output = {'answer': 'genuine fixture result'}
        submitted = self.stage_submitted(self.prepare_consume(output))
        # Leave a genuine Go receipt but do not let Python reconcile it yet.
        with closing(self.runtime()) as runtime:
            runtime.execute(submitted['task'])
        evidence = self.evidence(submitted)
        self.assertIsNotNone(evidence)
        _, receipt_bytes = evidence
        genuine = json.loads(receipt_bytes)
        self.assertEqual(genuine['status'], 'succeeded')
        self.assertEqual(genuine['result']['actual_member_id'], CONSUMER)
        paths = self.native_paths_for(receipt_bytes)
        genuine['result']['actual_member_id'] = 'wrong-member'
        tampered = json.dumps(genuine).encode('utf-8')
        for path in paths:
            path.write_bytes(tampered)

        result = self.cli('consume', fixture={'unexpected': True})
        self.assertEqual(result['state'], 'uncertain')
        self.assertEqual(result['attempt_id'], submitted['attempt_id'])
        self.assertEqual(self.job('consume'), (submitted, None))
        self.assertNotEqual(self.cli('status')['state'], 'completed')
        for path in paths:
            self.assertEqual(path.read_bytes(), tampered)
            path.write_bytes(receipt_bytes)

        receipts = self.native_receipts()
        ack = self.cli('consume', fixture={'unexpected': True})
        self.assertEqual(ack['state'], 'completed')
        self.assert_native_ack(ack, CONSUMER, output)
        self.assertEqual(self.native_receipts(), receipts)
        self.assertEqual(self.cli('status')['state'], 'completed')
