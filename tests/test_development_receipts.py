import json
import sqlite3
import unittest
from unittest.mock import patch

from role_orchestrator.handoff_runner import Runner, parse
from role_orchestrator.handoffs import Handoffs
from role_orchestrator.role_contracts import output_payload, role_contract, validate_fields
from role_orchestrator.contracts import strict_json


class DevelopmentReceiptTests(unittest.TestCase):
    def setUp(self):
        actor = {'instruction': 'Review the artifact.', 'model': 'gpt-6.2-sol', 'effort': 'medium'}
        self.plan = parse({'op': 'status', 'plan': {
            'producer': dict(actor, member='source'),
            'consumer': dict(actor, member='acl-005'),
            'slot': 'input', 'required_fields': ['source'],
        }}, 'fixture')
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE jobs(task_id TEXT PRIMARY KEY, intent TEXT, ack TEXT)')
        self.h = Handoffs(':memory:', 'r')
        self.addCleanup(self.h.close)
        self.runner = Runner(self.db, self.h, None, None, None, 'r', self.plan)
        self.task = {'id': 'consume', 'role': 'acl-005', 'instruction': 'Review.', 'input': '{}'}
        self.intent = {'task': self.task, 'op': 'consume', 'member': 'acl-005', 'attempt_id': 'a'}
        self.h.bind('acl-005', 'input', 'source', ['source'])
        self.h.publish('source', 'input', {'source': 's'})
        self.h.validate('acl-005', 'input')
        self.h.claim_ready('acl-005')
        self.db.execute('INSERT INTO jobs VALUES(?,?,NULL)', ('consume', json.dumps(self.intent)))

    def observe(self, result, status='succeeded'):
        wire = dict(self.task, schema_version=1, run_id='r', attempt_id='a')
        receipt = {'status': status, 'result': result, 'task': {
            'id': 'consume', 'role': 'acl-005', 'version': 'v1', 'input': '{}'}}
        with patch('role_orchestrator.attempts.AttemptReader.read',
                   return_value=(json.dumps(wire).encode(), json.dumps(receipt).encode())):
            return self.runner.reconcile(self.intent)

    def test_empty_review_cannot_complete(self):
        result = self.observe({'actual_member_id': 'acl-005', 'actual_role_version': 'v1', 'text': ''})
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(self.runner.run({'op':'status'})['state'], 'failed')

    def test_bound_terminal_failure_reports_code(self):
        result = self.observe({'failure': {'version': 1, 'code': 'startup_failure',
                              'stage':'startup','execution':'not_started','retryable':False}}, 'failed')
        self.assertEqual((result['state'], result['reason']), ('failed','startup_failure'))

    def test_ambiguous_failure_remains_uncertain(self):
        result = self.observe({'failure': {'version': 1, 'code': 'transport_failure',
                              'stage': 'exchange', 'execution': 'unknown', 'retryable': False}}, 'failed')
        self.assertEqual(result['state'], 'uncertain')
        self.assertIsNone(self.db.execute('SELECT ack FROM jobs').fetchone()[0])

    def test_wrong_type_and_missing_input_are_rejected(self):
        contract = self.plan['consumer']['contract']
        with self.assertRaisesRegex(ValueError, 'role_fields'):
            validate_fields({'goal': {}}, contract['inputs'])
        result = self.observe({'actual_member_id': 'acl-005', 'actual_role_version': 'v1',
                               'text': '{"cases":false,"counterexamples":[]}'})
        self.assertEqual(result['reason'], 'invalid_role_output')
        self.assertIsNone(self.h.read('acl-005', 'oracle'))

    def test_patch_review_contract_binds_input_digest_criterion_and_decision(self):
        contract = role_contract('acl-005', profile='development-patch-v1')
        inputs = {'patch_sha256': 'a'*64, 'criterion': {'id': 'criterion-1'}}
        valid = {'criterion_id':'criterion-1','verdict':'accept','reason':'verified','patch_sha256':'a'*64}
        self.assertEqual(output_payload(json.dumps(valid), contract, inputs), valid)
        for change in ({'criterion_id':'other'}, {'patch_sha256':'b'*64}, {'verdict':'maybe'}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'review_target'):
                output_payload(json.dumps({**valid, **change}), contract, inputs)

    def test_review_becomes_an_artifact(self):
        payload = {'cases': [{'id': 'c', 'verdict': 'accept'}], 'counterexamples': []}
        result = self.observe({'actual_member_id': 'acl-005', 'actual_role_version': 'v1',
                               'text': json.dumps(payload)})
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(self.h.read('acl-005', result['artifact']['kind'])['payload'], payload)
        self.assertEqual(self.runner.run({'op':'status'})['output']['payload'], payload)

    def test_initial_patch_cannot_bypass_its_assignment(self):
        actor = dict(instruction='Perform your assigned task.', model='gpt-6.2-sol', effort='medium')
        for slot, base, diff in [('wrong-base', 'b' * 40, 'some diff'), ('empty-diff', 'a' * 40, '  ')]:
            with self.subTest(slot=slot):
                consumer = 'reader-' + slot
                plan = parse({'op': 'status', 'plan': {
                    'producer': dict(actor, member='acl-007', profile='development-patch-v1'),
                    'consumer': dict(actor, member=consumer), 'slot':slot, 'required_fields': [],
                    'producer_input': {'base_sha':'a' * 40},
                }}, 'fixture')
                runner = Runner(self.db, self.h, None, None, None, 'r', plan)
                self.h.bind(consumer, slot, 'acl-007', plan['producer']['contract']['outputs'])
                ref = runner.run({'op':'publish-initial', 'payload': {
                    'base_sha':base, 'diff':diff, 'tests':[], 'unverified':[],
                }})
                self.assertEqual(runner.run({'op':'validate'})['state'], 'needs_revision')
                self.assertEqual(runner.run({'op':'accept-review', 'reviewed_ref':ref})['state'], 'needs_revision')
                self.assertEqual(self.h.claim_ready(consumer), 'blocked')

    def test_json_decoders_agree_on_duplicate_keys_and_overflow(self):
        from role_orchestrator.assignment import decode
        from role_orchestrator.handoff_runner import load
        for reader in (decode, load, strict_json):
            for text in ('{"a":1,"a":2}', '{"x":[1e999]}', '{"x":NaN}'):
                with self.subTest(reader=reader, text=text), self.assertRaises(ValueError):
                    reader(text)
