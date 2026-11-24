import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from role_orchestrator.handoffs import Handoffs


class HandoffPolicyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'handoffs.db'
        self.h = Handoffs(self.path, 'r')
        self.addCleanup(lambda: self.h.close())
        for who in ('editor', 'auditor'):
            self.h.bind(who, 'report', 'author', {'items.0.count': 'integer', 'title': 'string', 'ok': 'boolean'})
        self.ref = self.h.publish('author', 'report', {'items': [{'count': False}]})

    def feedback(self, who='editor', observed='sample', goal='on', field='ok', criterion='mode'):
        return self.h.validate(who, 'report', criterion=criterion, field=field, observed=observed,
                               required=goal, check='verify ' + goal, reviewed_ref=self.ref)

    def reopen(self):
        self.h.close()
        self.h = Handoffs(self.path, 'r')

    def test_all_field_errors_in_one_request_and_one_repair(self):
        request = self.h.validate('editor', 'report')
        self.assertEqual(request['observed'], [
            {'field_path': 'items.0.count', 'observed': 'bool', 'expected': 'integer'},
            {'field_path': 'ok', 'observed': 'missing', 'expected': 'boolean'},
            {'field_path': 'title', 'observed': 'missing', 'expected': 'string'},
        ])
        self.reopen()
        self.assertEqual(request, self.h.validate('editor', 'report'))
        rid = request['request_id']
        self.assertEqual(self.h.claim_repair(rid)['state'], 'ready')
        self.h.publish('author', 'report', {'items': [{'count': 0}], 'ok': True, 'title': 'done'},
                       base_revision=1, request_id=rid)
        self.assertEqual(self.h.validate('editor', 'report')['state'], 'accepted')

    def test_request_identity_and_distinct_compatible_requests(self):
        request = self.feedback()
        self.reopen()
        repeat = self.feedback(observed='same issue in other words')
        self.assertEqual(repeat['request_id'], request['request_id'])
        other = self.feedback(field='title')
        self.assertNotEqual(other['request_id'], request['request_id'])
        self.assertEqual(other['state'], 'needs_revision')
        peer = self.feedback(who='auditor')
        self.assertNotEqual(peer['request_id'], request['request_id'])
        self.assertEqual(self.h.claim_repair(request['request_id'])['state'], 'ready')
        self.assertEqual(self.h.claim_repair(peer['request_id'])['state'], 'uncertain')

    def test_conflict_blocks_all_dispatches_on_the_same_revision(self):
        first = self.feedback()
        unrelated = self.feedback(field='title')
        second = self.feedback(who='auditor', goal='off')
        self.assertEqual(second['reason'], 'conflict')
        third = self.feedback(field='mode-switch', criterion='independent')
        self.assertEqual((third['state'], third['reason']), ('blocked', 'conflict'))
        self.reopen()
        for request in (first, second, unrelated, third):
            result = self.h.claim_repair(request['request_id'])
            self.assertEqual((result['state'], result.get('reason')), ('blocked', 'conflict'))
        self.assertEqual(self.feedback()['state'], 'blocked')
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM repair_intents').fetchone()[0], 0)

    def test_historical_pending_siblings_are_read_without_new_ids(self):
        first = self.feedback()
        with closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute('SELECT * FROM requests WHERE id=?', (first['request_id'],)).fetchone()
            condition = json.loads(row[7])
            condition[3] = 'historical duplicate observation'
            db.execute('INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       ('historic-alias', *row[1:7], json.dumps(condition, separators=(',', ':')), *row[8:]))
        self.reopen()
        self.assertEqual(self.feedback(observed='new wording')['request_id'], first['request_id'])
        receipt = self.h.claim_repair('historic-alias')
        self.assertEqual(receipt['state'], 'ready')
        self.assertEqual(self.h.claim_repair(first['request_id'])['repair_task_id'], receipt['repair_task_id'])
        for request_id in ('historic-alias', first['request_id']):
            self.assertEqual(self.h.repair_task_id(request_id, 'author', 'report'), receipt['repair_task_id'])
            self.assertIsNone(self.h.repair_task_id(request_id, 'someone-else', 'report'))

    def test_historical_single_field_request_is_enriched_without_changing_its_id(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            first_field = [False, 'structure', 'items.0.count', 'bool', 'provide items.0.count', 'field is integer']
            db.execute('INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       ('old-request', 'r', 'editor', 'report', 'author', 1, self.ref['sha256'],
                        json.dumps(first_field, separators=(',', ':')), 'needs_revision', None, 0))
        self.reopen()
        request = self.h.validate('editor', 'report')
        self.assertEqual(request['request_id'], 'old-request')
        self.assertEqual(len(request['observed']), 3)
        self.assertEqual(self.h.claim_repair('old-request')['state'], 'ready')

    def test_late_conflict_preserves_already_claimed_work(self):
        first = self.feedback()
        self.assertEqual(self.h.claim_repair(first['request_id'])['state'], 'ready')
        second = self.feedback(who='auditor', goal='off')
        self.assertEqual(second['reason'], 'conflict')
        self.assertEqual(self.h.claim_repair(first['request_id'])['state'], 'uncertain')
        self.h.publish('author', 'report', {'ok': True}, base_revision=1, request_id=first['request_id'])
        self.assertEqual(self.h.claim_repair(first['request_id'])['state'], 'completed')

    def test_output_and_receipt_roll_back_together_and_replay_pins_the_first_revision(self):
        self.h.bind('coder', 'report', 'author', [])
        self.h.validate('coder', 'report')
        self.assertEqual(self.h.claim_ready('coder'), 'ready')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TRIGGER stop_update BEFORE UPDATE ON executions "
                       "BEGIN SELECT RAISE(ABORT, 'interrupt completion'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.h.finish_with_output('coder', 'receipt-1', 'patch', {'diff': 'one'})
        self.reopen()
        self.assertIsNone(self.h.read('coder', 'patch'))
        self.assertEqual(self.h.state('coder')['state'], 'uncertain')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DROP TRIGGER stop_update')
        artifact = self.h.finish_with_output('coder', 'receipt-1', 'patch', {'diff': 'one'})
        self.assertEqual(self.h.state('coder')['receipt_id'], 'receipt-1')
        self.h.publish('coder', 'patch', {'diff': 'two'}, base_revision=1)
        self.reopen()
        self.assertEqual(self.h.read('coder', 'patch', revision=1), {'artifact': artifact, 'payload': {'diff': 'one'}})
        self.assertEqual(self.h.finish_with_output('coder', 'receipt-1', 'patch', {'diff': 'one'}), artifact)
        with self.assertRaises(ValueError):
            self.h.finish_with_output('coder', 'receipt-1', 'patch', {'diff': 'changed'})
        with self.assertRaises(ValueError):
            self.h.finish_with_output('coder', 'receipt-2', 'patch', {'diff': 'one'})
        with closing(sqlite3.connect(self.path)) as db:
            (refs,) = db.execute("SELECT input_refs FROM artifacts WHERE producer='coder' AND revision=1").fetchone()
            self.assertEqual(json.loads(refs)[0]['sha256'], self.ref['sha256'])

    def test_readonly_store_cannot_create_or_modify_state(self):
        absent = self.path.parent / 'absent.db'
        with self.assertRaises(sqlite3.OperationalError):
            Handoffs.open_readonly(absent, 'r')
        self.assertFalse(absent.exists())
        expected = self.h.state('editor')
        with closing(Handoffs.open_readonly(self.path, 'r')) as readonly:
            self.assertEqual(readonly.state('editor'), expected)
            with self.assertRaises(sqlite3.OperationalError):
                readonly.bind('editor', 'new-slot', 'author', [])
        self.assertEqual(self.h.state('editor'), expected)
