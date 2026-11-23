"""Approval CLI contract tests with a mock control client."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import role_orchestrator.approval as approval


class RPCError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class FakeClient:
    def __init__(self):
        self.replies = {}
        self.calls = []
        self.action = 'revise'

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == 'revisions.check' and self.action in ('complete', 'investigate', 'defer'):
            raise RPCError('unsupported')
        reply = self.replies[(method, params.get('task_id'))]
        if isinstance(reply, Exception):
            raise reply
        return reply


class ApprovalBindingTests(unittest.TestCase):
    module = approval

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        errors = patch.object(self.module, 'ControlError', RPCError)
        errors.start()
        self.addCleanup(errors.stop)
        self.client = FakeClient()
        self.approval = self.module.Approval(Path(self.tmp.name) / 'state.db', self.client, 'run', 'review')
        self.task_id = self.approval.task_id
        self.digest = 'a' * 64
        self.check_key = ('revisions.check', None)
        self.revision_key = ('receipts.get', self.task_id)
        self.review_key = ('receipts.get', 'review')
        self.eligibility = dict(task_id=self.task_id, project='project', role='producer', task_kind='approved_revision')
        self.client.replies[self.check_key] = self.eligibility
        self.client.replies[self.review_key] = {
            'task': {'id': 'review', 'role': 'reviewer', 'project': 'project'},
            'status': 'completed', 'artifact_valid': True,
            'artifact': {'id': 'review.json', 'version': 'v1', 'sha256': self.digest},
        }

    def terminal(self, failed=False):
        return {
            'task': {'id': self.task_id, 'role': 'producer', 'project': 'project'},
            'task_kind': 'approved_revision', 'attempt_id': 'attempt-1',
            'status': 'failed' if failed else 'completed',
            'execution': 'not_started' if failed else 'completed',
            'failure': {'code': 'launch_failed', 'execution': 'not_started'} if failed else None,
            'artifact_valid': not failed,
            'artifact': dict(id='', version='', sha256='') if failed else
                dict(id=f'revision-{self.task_id}.json', version='v1', sha256='b' * 64),
        }

    def assert_invalid(self, receipt):
        self.client.replies[self.revision_key] = receipt
        self.assertEqual(self.approval.status(), dict(task_id=self.task_id, status='uncertain', error_code='invalid_receipt'))

    def test_terminal_binding_required_for_success_and_failure(self):
        self.assertTrue(self.approval.insert_intent(self.digest, 'project'))
        for failed in (False, True):
            native = self.terminal(failed)
            self.client.replies[self.revision_key] = native
            expected = dict(task_id=self.task_id, status=native['status'])
            expected.update(error_code='launch_failed') if failed else expected.update(artifact=native['artifact'])
            self.assertEqual(self.approval.status(), expected)
            for container, key, value in (
                ('task', 'id', 'another-task'), ('task', 'role', 'reviewer'),
                ('task', 'project', 'another-project'), (None, 'task_kind', 'ordinary_producer'),
                (None, 'attempt_id', True), (None, 'attempt_id', ' '),
                (None, 'execution', 'running'),
            ):
                with self.subTest(failed=failed, key=key, value=value):
                    receipt = copy.deepcopy(native)
                    (receipt if container is None else receipt[container])[key] = value
                    self.assert_invalid(receipt)
            for key in ('task_kind', 'attempt_id', 'execution'):
                with self.subTest(failed=failed, missing=key):
                    receipt = copy.deepcopy(native)
                    del receipt[key]
                    if key == 'task_kind':
                        receipt['task'][key] = 'approved_revision'
                    self.assert_invalid(receipt)

    def test_success_requires_exact_artifact_and_consistent_execution(self):
        self.assertTrue(self.approval.insert_intent(self.digest, 'project'))
        for key, value in [('id', 'unrelated.json'), ('version', 'v2'), ('version', 1)]:
            with self.subTest(key=key, value=value):
                receipt = self.terminal()
                receipt['artifact'][key] = value
                self.assert_invalid(receipt)
        self.assert_invalid(dict(self.terminal(), artifact_valid=False))
        self.assert_invalid(dict(self.terminal(), execution='not_started'))
        self.assert_invalid(dict(self.terminal(), failure=self.terminal(True)['failure']))

    def test_failure_requires_structured_not_started_evidence(self):
        self.assertTrue(self.approval.insert_intent(self.digest, 'project'))
        for failure in (None, 'launch_failed', {'code': 'launch_failed'},
                        {'code': 'launch_failed', 'execution': 'completed'},
                        {'code': 'bad code', 'execution': 'not_started'},
                        {'execution': 'not_started'}):
            with self.subTest(failure=failure):
                self.assert_invalid(dict(self.terminal(True), failure=failure))
        self.assert_invalid(dict(self.terminal(True), execution='completed'))
        self.assert_invalid(dict(self.terminal(True), status='uncertain'))

    def assert_no_admission(self, status, code):
        self.client.calls.clear()
        self.assertEqual(self.approval.approve(self.digest), dict(task_id=self.task_id, status=status, error_code=code))
        self.assertEqual(self.client.calls, [
            ('receipts.get', {'task_id': 'review'}),
            ('revisions.check', {'review_task_id': 'review', 'artifact_sha256': self.digest}),
        ])
        self.assertIsNone(self.approval.read_intent())
        self.assertFalse(self.approval.state.exists())
        self.assertFalse(self.approval.lock.parent.exists())
        self.assertEqual(self.approval.status()['status'], 'unapproved')

    def test_unsupported_saved_actions_leave_no_intent(self):
        for action in ('complete', 'investigate', 'defer'):
            with self.subTest(action=action):
                self.client.action = action
                self.assert_no_admission('unsupported', 'unsupported')

    def test_other_preflight_errors_leave_no_intent(self):
        for code in ('scope_mismatch', 'invalid_request', 'not_found', 'conflict'):
            with self.subTest(code=code):
                self.client.replies[self.check_key] = RPCError(code)
                self.assert_no_admission('conflict' if code == 'conflict' else 'unapproved', code)
        self.client.replies[self.check_key] = OSError('transport lost')
        self.assert_no_admission('unapproved', 'check_unavailable')

    def test_malformed_preflight_leaves_no_intent(self):
        for reply in (None, {}, dict(self.eligibility, task_id='wrong'),
                      dict(self.eligibility, project='wrong'), dict(self.eligibility, role='reviewer'),
                      dict(self.eligibility, task_kind='ordinary_producer'),
                      dict(self.eligibility, error_code='unsupported')):
            with self.subTest(reply=reply):
                self.client.replies[self.check_key] = reply
                self.assert_no_admission('unapproved', 'invalid_check')

    def test_preflight_precedes_submission_and_replay_only_observes(self):
        self.client.replies[('revisions.approve', None)] = dict(task_id=self.task_id, status='queued', cached=False)
        self.client.replies[self.revision_key] = {
            'task': self.terminal()['task'], 'status': 'queued',
        }
        self.assertEqual(self.approval.approve(self.digest)['status'], 'pending')
        self.assertEqual(self.approval.read_intent().phase, 'accepted')
        params = {'review_task_id': 'review', 'artifact_sha256': self.digest}
        self.assertEqual(self.client.calls, [
            ('receipts.get', {'task_id': 'review'}), ('revisions.check', params),
            ('revisions.approve', params), ('receipts.get', {'task_id': self.task_id}),
        ])
        self.client.calls.clear()
        self.client.replies[self.revision_key] = self.terminal()
        self.assertEqual(self.approval.approve(self.digest)['status'], 'completed')
        self.assertEqual(self.client.calls, [('receipts.get', {'task_id': self.task_id})])
