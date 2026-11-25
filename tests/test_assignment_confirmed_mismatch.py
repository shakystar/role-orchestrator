import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from role_orchestrator.assignment import observe
from role_orchestrator.attempts import AttemptReader, ConfirmedAssignmentMismatch


class AssignmentMismatchTests(unittest.TestCase):
    ids = dict(run_id='run-1', attempt_id='attempt-1')
    request = dict(id='task-1', role='member-1', instruction='test')
    input_text = '{}'
    plan = dict(task_id='other-task', member_id='member-1', instruction='test',
                input_sha256=hashlib.sha256(input_text.encode()).hexdigest())

    def evidence(self, wire_changes=None, receipt_changes=None):
        wire = dict(self.request, schema_version=1, input=self.input_text, **self.ids)
        wire.update(wire_changes or {})
        task = {key: wire[key] for key in ('id', 'role', 'input')}
        receipt = dict(self.ids, task=dict(task, version='v1'), status='succeeded')
        receipt.update(receipt_changes or {})
        return json.dumps(wire).encode(), json.dumps(receipt).encode()

    def observe(self, plan=None, request=None):
        return observe('bridge', Path('.'), self.ids, self.plan if plan is None else plan, request)

    def test_confirmed_mismatch(self):
        with patch.object(AttemptReader, 'read', return_value=self.evidence()):
            reader = AttemptReader('bridge', Path('.'), self.ids['run_id'])
            with self.assertRaises(ConfirmedAssignmentMismatch):
                reader.bound(self.ids['attempt_id'], dict(self.request, id='other-task'),
                             self.plan['input_sha256'])
            self.assertEqual(self.observe(), dict(status='failed', reason='assignment_mismatch'))

    def test_unconfirmed_mismatch_remains_uncertain(self):
        cases = {
            'nonstring id': self.evidence({'id': None}),
            'blank id': self.evidence({'id': ' '}),
            'invalid schema': self.evidence({'schema_version': 2}),
            'other request field': self.evidence({'instruction': 'different'}),
            'wrong input': self.evidence({'input': '{"different":true}'}),
            'inconsistent receipt': self.evidence(receipt_changes={'task': {}}),
            'invalid receipt status': self.evidence(receipt_changes={'status': None}),
            'unsupported receipt status': self.evidence(receipt_changes={'status': 'bogus'}),
            'unfinalized receipt status': self.evidence(receipt_changes={'status': 'running'}),
            'malformed receipt': (self.evidence()[0], b'{'),
        }
        for label, evidence in cases.items():
            with self.subTest(label=label), patch.object(AttemptReader, 'read', return_value=evidence):
                self.assertEqual(self.observe(), dict(
                    status='uncertain', reason='attempt_evidence_unavailable_or_invalid'))

    def test_absent_and_unreadable_evidence_remain_uncertain(self):
        with patch.object(AttemptReader, 'read', return_value=None):
            self.assertEqual(self.observe(), dict(status='uncertain', reason='attempt_receipt_absent'))
        with patch.object(AttemptReader, 'read', side_effect=OSError):
            self.assertEqual(self.observe(), dict(
                status='uncertain', reason='attempt_evidence_unavailable_or_invalid'))

    def test_legacy_plan_without_input_hash(self):
        plan = dict(self.plan, task_id='task-1')
        del plan['input_sha256']
        evidence = self.evidence(receipt_changes=dict(status='failed', error='legacy runtime error'))
        with patch.object(AttemptReader, 'read', return_value=evidence) as read:
            self.assertEqual(self.observe(plan), dict(status='uncertain', reason='historical_input_unavailable'))
            read.assert_not_called()
            outcome = dict(status='failed', reason='runtime_failed',
                           receipt_sha256=hashlib.sha256(evidence[1]).hexdigest())
            self.assertEqual(self.observe(plan, self.request), outcome)
            self.assertEqual(self.observe(dict(plan, input_sha256=self.plan['input_sha256'])),
                             outcome)
