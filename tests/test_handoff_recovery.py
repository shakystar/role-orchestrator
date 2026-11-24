import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import test_handoff_runner as runner_tests
from role_orchestrator import handoff_runner as runner
from role_orchestrator.attempts import AttemptReader
from role_orchestrator.handoffs import Handoffs


# Discover adds tests/ to sys.path. Reuse the integration fixture without
# inheriting (and rediscovering) its test methods. Its class name is immaterial.
_FIXTURE_METHODS = ('setUp', 'runtime', 'cli', 'native_receipts', 'publish_valid')
(_Fixture,) = {
    cls for cls in vars(runner_tests).values()
    if isinstance(cls, type) and issubclass(cls, unittest.TestCase)
    and cls.__module__ == runner_tests.__name__
    and all(name in vars(cls) for name in _FIXTURE_METHODS)
}
PLAN = runner_tests.PLAN
PRODUCER = PLAN['producer']['member']
CONSUMER = PLAN['consumer']['member']
SLOT = PLAN['slot']
CRASH = 86

# The child runs the actual CLI against the actual Go fixture. Stop only
# after the targeted store method returns, so its transaction has committed.
# Replay guards catch calls even if Go or SQLite would deduplicate them.
_CHILD = '''
import os
import sys
from role_orchestrator import handoff_runner as runner

mode = sys.argv[1]
if mode in ('replay', 'replay-completed'):
    def unexpected(*args, **kwargs):
        os._exit(87)
    runner.Runtime.execute = unexpected
    if mode == 'replay-completed':
        runner.Handoffs.finish = unexpected
else:
    original = getattr(runner.Handoffs, mode)
    def stop_after_commit(self, *args, **kwargs):
        original(self, *args, **kwargs)
        os._exit(86)
    setattr(runner.Handoffs, mode, stop_after_commit)
raise SystemExit(runner.main(sys.argv[2:]))
'''


def repair_request(h, consumer, slot, producer):
    state = h.state(consumer)
    entry = runner.slot_state(state, slot) or {}
    requests = entry.get('open_requests', state.get('open_requests', []))
    if isinstance(requests, dict):
        requests = requests.values()
    (request,) = [r for r in requests if r.get('slot') == slot
                  and r.get('target_task_id') == producer and not r.get('external_owner')]
    return request['request_id']


def execution_rows(path, run_id, consumer):
    with closing(sqlite3.connect(path)) as db:
        return db.execute('SELECT receipt FROM executions WHERE run=? AND consumer=?',
                          (run_id, consumer)).fetchall()


class HandoffStoreRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'handoffs.sqlite3'
        self.run_id = 'recovery-test'
        self.h = Handoffs(self.path, self.run_id)
        self.addCleanup(self.h.close)

    def test_repair_reads_accepted_upstream_v2_without_repinning_normal_execution(self):
        h = self.h
        h.bind('P', 'upstream', 'U', ['summary'])
        h.bind('C', 'result', 'P', ['summary', 'evidence'])
        upstream_v1 = {'summary': 'upstream v1'}
        u1 = h.publish('U', 'upstream', upstream_v1)
        self.assertEqual(h.validate('P', 'upstream')['state'], 'accepted')
        self.assertEqual(h.claim_ready('P'), 'ready')
        normal = h.read_claimed('P', 'upstream')
        self.assertEqual(normal['artifact'], u1)
        self.assertEqual(normal['payload'], upstream_v1)
        self.assertEqual(h.publish('P', 'result', {'summary': 'missing evidence'})['revision'], 1)
        self.assertEqual(h.finish('P', 'normal-receipt'), 'completed')
        h.validate('C', 'result')
        rid = repair_request(h, 'C', 'result', 'P')

        upstream_v2 = {'summary': 'upstream v2'}
        u2 = h.publish('U', 'upstream', upstream_v2, base_revision=1)
        self.assertEqual(u2['revision'], 2)
        self.assertEqual(h.validate('P', 'upstream')['state'], 'accepted')
        self.assertEqual(h.claim_repair(rid)['state'], 'ready')
        repaired_input = h.read_claimed('P', 'upstream', request_id=rid)
        self.assertEqual(repaired_input['artifact'], u2)
        self.assertEqual(repaired_input['payload'], upstream_v2)
        payload = {'summary': 'corrected', 'evidence': repaired_input['payload']['summary']}
        p2 = h.publish('P', 'result', payload, base_revision=1, request_id=rid)
        self.assertEqual(p2['revision'], 2)
        # An output carrying the old normal pins would be stale, not accepted.
        self.assertEqual(h.validate('C', 'result')['state'], 'accepted')
        self.assertEqual(h.read('P', 'result'), {'artifact': p2, 'payload': payload})
        self.assertEqual(h.read_claimed('P', 'upstream'), normal)
        self.assertEqual(execution_rows(self.path, self.run_id, 'P'), [('normal-receipt',)])

    def test_read_waits_for_rollback_of_insert_on_the_same_store(self):
        h = self.h
        original_payload = {'summary': 'committed'}
        original = h.publish('P', 'result', original_payload)
        inserted = threading.Event()
        reader_at_lock = threading.Event()
        reader_contended = threading.Event()
        allow_rollback = threading.Event()
        local = threading.local()
        real_query, real_lock = h._q, h._lock

        class InjectedRollback(Exception):
            pass

        class ObservedLock:
            def __enter__(self):
                if getattr(local, 'reader', False):
                    if real_lock.acquire(blocking=False):
                        real_lock.release()
                    else:
                        reader_contended.set()
                    reader_at_lock.set()
                return real_lock.__enter__()

            def __exit__(self, *exc):
                return real_lock.__exit__(*exc)

        def pause_after_insert(sql, *params):
            result = real_query(sql, *params)
            if sql.lstrip().upper().startswith('INSERT INTO ARTIFACTS '):
                inserted.set()
                if not allow_rollback.wait(10):
                    raise AssertionError('test did not release the writer')
                raise InjectedRollback()
            return result

        def read():
            local.reader = True
            return h.read('P', 'result')

        with patch.object(h, '_q', pause_after_insert), patch.object(h, '_lock', ObservedLock()):
            with ThreadPoolExecutor(max_workers=2) as pool:
                writer = pool.submit(h.publish, 'P', 'result', {'summary': 'rolled back'},
                                     base_revision=1)
                try:
                    self.assertTrue(inserted.wait(5), 'writer did not insert revision 2')
                    reader = pool.submit(read)
                    self.assertTrue(reader_at_lock.wait(5), 'reader did not reach the transaction lock')
                    self.assertTrue(reader_contended.is_set(), 'writer did not hold the reader lock')
                    self.assertFalse(reader.done(), 'read returned while the insert was uncommitted')
                finally:
                    allow_rollback.set()
                with self.assertRaises(InjectedRollback):
                    writer.result(timeout=5)
                self.assertEqual(reader.result(timeout=5),
                                 {'artifact': original, 'payload': original_payload})
        self.assertEqual(h.read('P', 'result'), {'artifact': original, 'payload': original_payload})


class HandoffCliRecoveryTests(unittest.TestCase):
    runtime = _Fixture.runtime
    cli = _Fixture.cli
    native_receipts = _Fixture.native_receipts
    publish_valid = _Fixture.publish_valid

    def setUp(self):
        if 'ROLE_RUNTIME_BIN' in os.environ:
            self.assertTrue(os.environ['ROLE_RUNTIME_BIN'], 'ROLE_RUNTIME_BIN is set but empty')
        _Fixture.setUp(self)
        self.handoffs_path = self.state / 'handoffs.sqlite3'

    def store(self):
        return closing(Handoffs(self.handoffs_path, self.run_id))

    def jobs(self):
        if not self.db_path.exists():
            return {}
        with closing(sqlite3.connect(self.db_path)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone() is None:
                return {}
            return {task_id: (json.loads(intent), None if ack is None else json.loads(ack))
                    for task_id, intent, ack in db.execute('SELECT task_id, intent, ack FROM jobs')}

    def child(self, mode, op, *, code=0, **fields):
        proc = subprocess.run(
            [sys.executable, '-c', _CHILD, mode, '--state', str(self.state),
             '--runtime', str(self.binary), '--backend', 'fixture'],
            cwd=runner_tests.ROOT, input=json.dumps({'plan': PLAN, 'op': op, **fields}),
            capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(proc.returncode, code, (mode, op, proc.stdout, proc.stderr))
        if code == 0:
            return json.loads(proc.stdout)

    def verified_body(self, intent, ack):
        self.assertEqual(intent['phase'], 'submitted')
        attempt = intent['attempt_id']
        evidence = AttemptReader(str(self.binary), self.state, self.run_id).read(attempt)
        self.assertIsNotNone(evidence, 'no native evidence for the saved attempt')
        wire_bytes, receipt_bytes = evidence
        wire, receipt = json.loads(wire_bytes), json.loads(receipt_bytes)
        self.assertEqual(wire['attempt_id'], attempt)
        self.assertEqual(wire['input'], intent['task']['input'])
        self.assertEqual(receipt['status'], 'succeeded')
        self.assertEqual(receipt['task']['input'], wire['input'])
        self.assertEqual(ack['task_id'], intent['task']['id'])
        self.assertEqual(ack['attempt_id'], attempt)
        self.assertEqual(ack['receipt_id'], attempt)
        self.assertEqual(ack['receipt_sha256'], hashlib.sha256(receipt_bytes).hexdigest())
        return json.loads(wire['input'])

    def exercise_prebound(self, producer, fields, *, matches):
        plan = {**PLAN, 'required_fields': ['summary', 'detail']}
        # The existing slot is claimable even with a conflicting contract.
        # Both versions of the field contract are satisfied by this payload.
        with self.store() as h:
            h.bind(CONSUMER, SLOT, producer, fields)
            h.publish(producer, SLOT, {'summary': 'ready', 'detail': 'd', 'alternate': 'a'})
            self.assertEqual(h.validate(CONSUMER, SLOT)['state'], 'accepted')
        self.assertEqual(execution_rows(self.handoffs_path, self.run_id, CONSUMER), [])
        before = self.native_receipts()
        result = self.cli('consume', plan=plan, fixture={'summary': 'consumed'},
                          code=0 if matches else 2)
        if matches:
            self.assertEqual(result['state'], 'completed')
            self.assertEqual(execution_rows(self.handoffs_path, self.run_id, CONSUMER),
                             [(result['receipt_id'],)])
            self.assertEqual(set(self.jobs()), {'consume'})
            self.assertEqual(len(set(self.native_receipts()) - set(before)), 1)
        else:
            self.assertEqual(result['state'], 'error')
            self.assertRegex(result['reason'], '(?i)bind|contract')
            self.assertEqual(execution_rows(self.handoffs_path, self.run_id, CONSUMER), [])
            self.assertEqual(self.jobs(), {})
            self.assertEqual(self.native_receipts(), before)
            with self.store() as h:
                self.assertIsNone(h.prepared_claim(CONSUMER, task_id='consume'))

    def test_prebound_conflicting_producer_is_rejected(self):
        other = 'prebound-other' if PRODUCER != 'prebound-other' else 'prebound-alternate'
        self.exercise_prebound(other, ['summary', 'detail'], matches=False)

    def test_prebound_conflicting_fields_are_rejected(self):
        self.exercise_prebound(PRODUCER, ['summary', 'alternate'], matches=False)

    def test_prebound_equal_contract_can_dispatch(self):
        self.exercise_prebound(PRODUCER, ['detail', 'summary'], matches=True)

    def test_consume_resumes_committed_claim_with_original_draft_and_input(self):
        v1 = self.publish_valid()
        first = {'summary': 'first consume draft'}
        before = self.native_receipts()
        self.child('claim_ready', 'consume', code=CRASH, fixture=first)
        self.assertEqual(self.jobs(), {})
        self.assertEqual(self.native_receipts(), before)
        self.assertEqual(execution_rows(self.handoffs_path, self.run_id, CONSUMER), [(None,)])
        with self.store() as h:
            self.assertEqual(h.prepared_claim(CONSUMER, task_id='consume'), {'fixture': first})
            v2 = h.publish(PRODUCER, SLOT, {'summary': 'new forecast'}, base_revision=1)
            self.assertEqual(v2['revision'], 2)
            self.assertEqual(h.validate(CONSUMER, SLOT)['state'], 'accepted')
        ack = self.cli('consume', fixture={'summary': 'different retry draft'})
        self.assertEqual(ack['state'], 'completed')
        self.assertEqual(set(self.jobs()), {'consume'})
        intent, saved = self.jobs()['consume']
        self.assertEqual(saved, ack)
        body = self.verified_body(intent, ack)
        self.assertEqual(body['fixture'], first)
        self.assertEqual(body['artifact'], v1)
        self.assertEqual(body['payload'], {'summary': 'forecast'})
        self.assertEqual(len(set(self.native_receipts()) - set(before)), 1)

    def request_repair(self):
        self.assertEqual(self.cli('publish-initial', payload={})['revision'], 1)
        self.cli('validate')
        with self.store() as h:
            return repair_request(h, CONSUMER, SLOT, PRODUCER)

    def test_repair_resumes_committed_claim_with_original_draft_and_upstream_pins(self):
        upstream = 'recovery-upstream'
        if upstream in (PRODUCER, CONSUMER):
            upstream += '-other'
        input_slot = 'repair-input' if SLOT != 'repair-input' else 'repair-input-other'
        with self.store() as h:
            h.bind(PRODUCER, input_slot, upstream, ['summary'])
            u1 = h.publish(upstream, input_slot, {'summary': 'original upstream payload'})
            self.assertEqual(h.validate(PRODUCER, input_slot)['state'], 'accepted')
        rid = self.request_repair()
        first = {'summary': 'first repair draft'}
        before = self.native_receipts()
        self.child('claim_repair', 'repair', code=CRASH, request_id=rid, fixture=first)
        self.assertEqual(self.jobs(), {})
        self.assertEqual(self.native_receipts(), before)
        with self.store() as h:
            draft = h.prepared_claim(PRODUCER, request_id=rid)
            self.assertEqual(draft['fixture'], first)
            self.assertEqual(draft['request_id'], rid)
            self.assertEqual(draft['base_revision'], 1)
            self.assertEqual(h.read_claimed(PRODUCER, input_slot, request_id=rid)['artifact'], u1)
            current, inputs = h.read_repair_inputs(PRODUCER, SLOT, rid)
            expected = json.loads(json.dumps({'current': current, 'inputs': inputs}))
            self.assertIn('original upstream payload', json.dumps(expected['inputs']))
            u2 = h.publish(upstream, input_slot, {'summary': 'new upstream payload'}, base_revision=1)
            self.assertEqual(u2['revision'], 2)
            self.assertEqual(h.validate(PRODUCER, input_slot)['state'], 'accepted')
        ack = self.cli('repair', request_id=rid, fixture={'summary': 'different retry draft'})
        self.assertEqual(ack['state'], 'published')
        ((task_id, (intent, saved)),) = self.jobs().items()
        self.assertEqual(task_id, ack['task_id'])
        self.assertEqual(saved, ack)
        body = self.verified_body(intent, ack)
        self.assertEqual(body, {'slot': SLOT, **draft, **expected})
        self.assertEqual(ack['artifact']['revision'], 2)
        self.assertEqual(len(set(self.native_receipts()) - set(before)), 1)

    def test_repair_recovers_after_publication_before_ack_without_dispatch(self):
        rid = self.request_repair()
        first = {'summary': 'published before the crash'}
        self.child('publish', 'repair', code=CRASH, request_id=rid, fixture=first)
        ((task_id, (intent, saved)),) = self.jobs().items()
        self.assertEqual(intent['phase'], 'submitted')
        self.assertIsNone(saved)
        with self.store() as h:
            published = h.read(PRODUCER, SLOT)
            self.assertEqual(published['artifact']['revision'], 2)
            self.assertEqual(published['payload'], first)
        receipts = self.native_receipts()
        ack = self.child('replay', 'repair', request_id=rid, fixture={'summary': 'retry ignored'})
        self.assertEqual(ack['state'], 'published')
        self.assertEqual(ack['request_id'], rid)
        self.assertEqual(ack['base_revision'], 1)
        self.assertEqual(ack['artifact'], published['artifact'])
        self.assertEqual(self.verified_body(intent, ack)['fixture'], first)
        self.assertEqual(self.jobs(), {task_id: (intent, ack)})
        with self.store() as h:
            self.assertEqual(h.read(PRODUCER, SLOT), published)
        self.assertEqual(self.native_receipts(), receipts)

    def test_consume_recovers_after_finish_before_ack_without_dispatch(self):
        v1 = self.publish_valid()
        first = {'summary': 'finished before the crash'}
        self.child('finish', 'consume', code=CRASH, fixture=first)
        (intent, saved) = self.jobs()['consume']
        self.assertEqual(intent['phase'], 'submitted')
        self.assertIsNone(saved)
        receipt_id = intent['attempt_id']
        self.assertEqual(execution_rows(self.handoffs_path, self.run_id, CONSUMER), [(receipt_id,)])
        receipts = self.native_receipts()
        ack = self.child('replay-completed', 'consume', fixture={'summary': 'retry ignored'})
        self.assertEqual(ack['state'], 'completed')
        body = self.verified_body(intent, ack)
        self.assertEqual(body['fixture'], first)
        self.assertEqual(body['artifact'], v1)
        self.assertEqual(self.jobs(), {'consume': (intent, ack)})
        self.assertEqual(execution_rows(self.handoffs_path, self.run_id, CONSUMER), [(receipt_id,)])
        self.assertEqual(self.native_receipts(), receipts)
