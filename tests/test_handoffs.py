import hashlib
import unittest
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier

from role_orchestrator.handoffs import Handoffs


def outcome(result):
    if isinstance(result, Mapping):
        return result.get('state', result.get('status'))
    return result


def public_values(value, *, include_keys=False):
    if isinstance(value, Mapping):
        if include_keys:
            yield from value
        for item in value.values():
            yield from public_values(item, include_keys=include_keys)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from public_values(item, include_keys=include_keys)
    else:
        yield value


def slot_views(value, slot):
    # A slot can be keyed by name or represented by a named record.
    if isinstance(value, Mapping):
        if slot in value:
            yield value[slot]
        if slot in value.values():
            yield value
        for item in value.values():
            yield from slot_views(item, slot)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from slot_views(item, slot)


class HandoffsTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / 'handoffs.sqlite3')
        self.handoffs = None
        self.addCleanup(self.close_handoffs)
        self.reopen()

    def close_handoffs(self):
        if self.handoffs is not None:
            self.handoffs.close()
            self.handoffs = None

    def reopen(self):
        self.close_handoffs()
        self.handoffs = Handoffs(self.path, 'synthetic-run')

    def race_on_connections(self, *operations):
        start = Barrier(len(operations))

        def run(operation):
            connection = Handoffs(self.path, 'synthetic-run')
            try:
                start.wait(timeout=10)
                return operation(connection)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=len(operations)) as workers:
            return list(workers.map(run, operations))

    def assert_request(self, result, producer):
        self.assertEqual(result['state'], 'needs_revision')
        self.assertTrue(result['request_id'])
        self.assertIn(producer, tuple(public_values(result['target'])))
        return result['request_id']

    def assert_current_revision(self, consumer, slot, revision):
        views = list(slot_views(self.handoffs.state(consumer), slot))
        self.assertTrue(views, f'No public status for slot {slot!r}')
        self.assertTrue(any(
            type(value) is int and value == revision
            for view in views for value in public_values(view)
        ), f'Revision {revision} not found in status for {slot!r}: {views!r}')

    def test_structure_rejection_reuses_notification_and_targets_original_producer(self):
        h = self.handoffs
        h.bind('reviewer', 'report', 'author', ['summary'])
        initial = h.publish('author', 'report', {'note': 'draft'})
        rejected = h.validate('reviewer', 'report')
        request_id = self.assert_request(rejected, 'author')
        self.assertEqual(h.validate('reviewer', 'report')['request_id'], request_id)
        self.assertEqual(outcome(h.claim_ready('reviewer')), 'blocked')

        self.reopen()
        h = self.handoffs
        status = h.state('reviewer')
        self.assertIn(request_id, tuple(public_values(status, include_keys=True)))
        self.assert_current_revision('reviewer', 'report', 1)
        repeated = h.validate('reviewer', 'report')
        self.assertEqual(self.assert_request(repeated, 'author'), request_id)

        with self.assertRaises(ValueError):
            h.publish('author', 'report', {'summary': 'corrected'},
                      base_revision=initial['revision'], request_id=request_id)
        self.assertEqual(outcome(h.claim_repair(request_id)), 'ready')
        h.publish('author', 'report', {'summary': 'corrected'},
                  base_revision=initial['revision'], request_id=request_id)
        self.assertEqual(h.validate('reviewer', 'report')['state'], 'accepted')
        self.assert_current_revision('reviewer', 'report', 2)
        self.assertEqual(outcome(h.claim_ready('reviewer')), 'ready')

    def test_meaning_feedback_blocks_the_same_revision_and_retains_added_criteria(self):
        h = self.handoffs
        h.bind('reviewer', 'report', 'author', ['summary', 'unit'])
        original = {'summary': 'rain forecast', 'unit': 'cm'}
        initial = h.publish('author', 'report', original)
        self.assertEqual(h.validate('reviewer', 'report')['state'], 'accepted')
        feedback = dict(criterion='meaning', field='summary', observed='rain forecast',
                        required='rain measurement', check='check reading provenance',
                        reviewed_ref=initial)
        request_id = self.assert_request(h.validate('reviewer', 'report', **feedback), 'author')
        self.assertEqual(h.validate('reviewer', 'report', **feedback)['request_id'], request_id)
        self.assertEqual(h.publish('author', 'report', original), initial)
        self.assertEqual(outcome(h.claim_ready('reviewer')), 'blocked')
        self.assert_current_revision('reviewer', 'report', 1)

        second = h.validate('reviewer', 'report', criterion='meaning', field='unit',
                            observed='cm', required='mm', check='check instrument units',
                            reviewed_ref=initial)
        self.assert_request(second, 'author')
        self.reopen()
        details = tuple(public_values(self.handoffs.state('reviewer'), include_keys=True))
        self.assertIn('rain measurement', details)
        self.assertIn('check instrument units', details)
        self.assertEqual(outcome(self.handoffs.claim_ready('reviewer')), 'blocked')

    def test_meaning_request_can_be_explicitly_accepted_after_correction(self):
        h = self.handoffs
        h.bind('reviewer', 'report', 'author', ['summary'])
        initial = h.publish('author', 'report', {'summary': 'forecast'})
        request_id = self.assert_request(h.validate(
            'reviewer', 'report', criterion='meaning', field='summary', observed='forecast',
            required='measurement', check='compare with the reading', reviewed_ref=initial),
            'author')
        self.assertEqual(self.assert_request(h.accept_review('reviewer', 'report', initial),
                                            'author'), request_id)
        self.assertEqual(outcome(h.claim_repair(request_id)), 'ready')
        corrected = h.publish('author', 'report', {'summary': 'measurement'},
                              base_revision=initial['revision'], request_id=request_id)
        self.assertEqual(corrected['revision'], 2)

        self.reopen()
        h = self.handoffs
        pending = h.validate('reviewer', 'report', reviewed_ref=corrected)
        self.assertEqual((pending['state'], pending['reason']), ('blocked', 'review_required'))
        self.assertEqual(pending['artifact'], corrected)
        self.assertIn(request_id, tuple(public_values(pending['open_requests'])))
        self.assertEqual(outcome(h.claim_ready('reviewer')), 'blocked')
        self.assertEqual(h.accept_review('reviewer', 'report', corrected)['state'], 'accepted')
        self.assertEqual(h.validate('reviewer', 'report')['state'], 'accepted')
        self.assertEqual(outcome(h.claim_ready('reviewer')), 'ready')

    def test_stale_review_cannot_change_the_current_revision(self):
        h = self.handoffs
        h.bind('reviewer', 'report', 'author', ['summary'])
        reviewed = h.publish('author', 'report', {'summary': 'forecast'})
        current = h.publish('author', 'report', {'summary': 'measurement'},
                            base_revision=reviewed['revision'])
        before = h.state('reviewer')
        for ref in (reviewed, dict(current, sha256=reviewed['sha256'])):
            with self.subTest(ref=ref):
                feedback = h.validate(
                    'reviewer', 'report', criterion='meaning', field='summary',
                    observed='forecast', required='measurement', check='compare with the reading',
                    reviewed_ref=ref)
                self.assertEqual((feedback['state'], feedback['reason']), ('blocked', 'stale_review'))
                acceptance = h.accept_review('reviewer', 'report', ref)
                self.assertEqual((acceptance['state'], acceptance['reason']),
                                 ('blocked', 'stale_review'))
                self.assertEqual(h.state('reviewer'), before)
        self.assertEqual(outcome(h.claim_ready('reviewer')), 'blocked')
        self.assertEqual(h.validate('reviewer', 'report', reviewed_ref=current)['state'], 'accepted')
        self.assertEqual(outcome(h.claim_ready('reviewer')), 'ready')

    def test_canonical_hash_and_compare_and_swap_retries(self):
        h = self.handoffs
        first = h.publish('author', 'report', {'z': 2, 'a': 1})
        self.assertEqual(first['producer'], 'author')
        self.assertEqual(first['kind'], 'report')
        self.assertEqual(first['revision'], 1)
        self.assertEqual(first['sha256'], hashlib.sha256(b'{"a":1,"z":2}').hexdigest())
        self.assertEqual(h.publish('author', 'report', {'a': 1, 'z': 2}), first)

        second = h.publish('author', 'report', {'z': 3, 'a': 1}, base_revision=1)
        self.assertEqual(second['revision'], 2)
        self.assertEqual(second['sha256'], hashlib.sha256(b'{"a":1,"z":3}').hexdigest())
        # The contract requires rejection but does not specify an exception type.
        with self.assertRaises(Exception):
            h.publish('author', 'report', {'a': 1, 'z': 4}, base_revision=1)

        self.reopen()
        h = self.handoffs
        self.assertEqual(h.publish('author', 'report', {'a': 1, 'z': 3}, base_revision=1), second)
        third = h.publish('author', 'report', {'a': 1, 'z': 5}, base_revision=2)
        self.assertEqual(third['revision'], 3)
        self.assertEqual(first['revision'], 1)
        self.assertEqual(first['sha256'], hashlib.sha256(b'{"a":1,"z":2}').hexdigest())

    def test_other_consumers_approval_does_not_cover_a_new_revision(self):
        h = self.handoffs
        h.bind('editor', 'report', 'author', ['summary'])
        h.bind('auditor', 'report', 'author', ['summary', 'citation'])
        initial = h.publish('author', 'report', {'summary': 'forecast', 'citation': 'log-1'})
        self.assertEqual(h.validate('editor', 'report')['state'], 'accepted')
        self.assertEqual(h.validate('auditor', 'report')['state'], 'accepted')
        request_id = self.assert_request(h.validate(
            'editor', 'report', criterion='meaning', field='summary', observed='forecast',
            required='measurement', check='compare with the reading', reviewed_ref=initial),
            'author')
        self.assertEqual(outcome(h.claim_repair(request_id)), 'ready')
        corrected = h.publish('author', 'report', {'summary': 'measurement'},
                              base_revision=initial['revision'], request_id=request_id)
        self.assertEqual(h.accept_review('editor', 'report', corrected)['state'], 'accepted')
        self.assert_current_revision('auditor', 'report', 2)
        self.assertEqual(outcome(h.claim_ready('auditor')), 'blocked')
        auditor_request = self.assert_request(h.validate('auditor', 'report'), 'author')
        self.assertNotEqual(auditor_request, request_id)
        self.assertEqual(outcome(h.claim_ready('editor')), 'ready')

    def test_submission_intent_survives_reopen_and_completion_requires_receipt(self):
        h = self.handoffs
        h.bind('reviewer', 'report', 'author', ['summary'])
        h.publish('author', 'report', {'summary': 'measurement'})
        self.assertEqual(h.validate('reviewer', 'report')['state'], 'accepted')
        before = h.state('reviewer')
        with self.assertRaises(Exception):
            h.finish('reviewer', 'receipt-before-intent')
        self.assertEqual(h.state('reviewer'), before)
        self.assertEqual(outcome(h.claim_ready('reviewer')), 'ready')

        self.reopen()
        h = self.handoffs
        self.assertEqual(outcome(h.claim_ready('reviewer')), 'uncertain')
        with self.assertRaises(Exception):
            h.finish('reviewer', '')
        self.assertEqual(outcome(h.claim_ready('reviewer')), 'uncertain')
        self.assertEqual(outcome(h.finish('reviewer', 'synthetic-receipt-1')), 'completed')
        self.reopen()
        self.assertIn('completed', tuple(public_values(self.handoffs.state('reviewer'))))
        self.assert_current_revision('reviewer', 'report', 1)
        self.assertEqual(outcome(self.handoffs.claim_ready('reviewer')), 'completed')

    def test_concurrent_normal_claims_pin_the_accepted_input_across_reopen(self):
        h = self.handoffs
        h.bind('reviewer', 'report', 'author', ['summary'])
        original = {'summary': 'first measurement'}
        initial = h.publish('author', 'report', original)
        self.assertEqual(h.validate('reviewer', 'report')['state'], 'accepted')
        with self.assertRaises(ValueError):
            h.read_claimed('reviewer', 'report')

        def claim(connection):
            return connection.claim_ready('reviewer')

        results = self.race_on_connections(claim, claim)
        self.assertCountEqual([outcome(result) for result in results], ['ready', 'uncertain'])
        pinned = {'artifact': initial, 'payload': original}
        self.assertEqual(h.read_claimed('reviewer', 'report'), pinned)

        latest = h.publish('author', 'report', {'summary': 'second measurement'},
                           base_revision=initial['revision'])
        self.assertEqual(h.validate('reviewer', 'report', reviewed_ref=latest)['state'], 'accepted')
        self.reopen()
        h = self.handoffs
        self.assert_current_revision('reviewer', 'report', latest['revision'])
        self.assertEqual(h.read_claimed('reviewer', 'report'), pinned)
        self.assertEqual(outcome(h.claim_ready('reviewer')), 'uncertain')

    def test_concurrent_repair_claims_coalesce_across_reviewers(self):
        h = self.handoffs
        for consumer in ('editor', 'auditor'):
            h.bind(consumer, 'report', 'author', ['summary'])
        draft = h.publish('author', 'report', {'note': 'draft'})
        editor = self.assert_request(h.validate('editor', 'report'), 'author')
        auditor = self.assert_request(h.validate('auditor', 'report'), 'author')
        self.assertNotEqual(editor, auditor)

        results = self.race_on_connections(
            lambda connection: connection.claim_repair(editor),
            lambda connection: connection.claim_repair(auditor),
        )
        states = [outcome(result) for result in results]
        self.assertCountEqual(states, ['ready', 'uncertain'])
        task_id = results[0]['repair_task_id']
        self.assertTrue(task_id)
        self.assertEqual(results[1]['repair_task_id'], task_id)

        self.reopen()
        h = self.handoffs
        for request_id in (editor, auditor):
            retry = h.claim_repair(request_id)
            self.assertEqual((outcome(retry), retry['repair_task_id']), ('uncertain', task_id))
        with self.assertRaises(ValueError):
            h.publish('author', 'report', {'summary': 'corrected'}, base_revision=draft['revision'])
        alias = (editor, auditor)[states.index('uncertain')]
        corrected = h.publish('author', 'report', {'summary': 'corrected'},
                              base_revision=draft['revision'], request_id=alias)
        self.assertEqual(corrected['revision'], draft['revision'] + 1)
        for consumer, request_id in (('editor', editor), ('auditor', auditor)):
            completed = h.claim_repair(request_id)
            self.assertEqual((outcome(completed), completed['repair_task_id']), ('completed', task_id))
            self.assertEqual(h.validate(consumer, 'report')['state'], 'accepted')

    def test_missing_external_artifact_is_requestable_and_marked_external(self):
        h = self.handoffs
        h.bind('reviewer', 'report', 'external:field-owner', ['summary'])
        result = h.validate('reviewer', 'report')
        self.assertEqual(result['state'], 'blocked')
        self.assertTrue(result['request_id'])
        target = tuple(public_values(result['target']))
        if 'external:field-owner' not in target:
            self.assertIn('field-owner', target)
            marked = 'external' in public_values(result) or any(
                isinstance(record, Mapping) and record.get('external') is True
                for record in (result, result['target'])
            )
            self.assertTrue(marked, f'External target not marked: {result!r}')
        self.assertEqual(h.validate('reviewer', 'report')['request_id'], result['request_id'])
        repair = h.claim_repair(result['request_id'])
        self.assertEqual((outcome(repair), repair['reason']), ('blocked', 'external'))
        self.assertEqual(outcome(h.claim_ready('reviewer')), 'blocked')

    def open_structure_request(self, consumer, producer, base_revision):
        h = self.handoffs
        draft = h.publish(producer, 'report', {'note': f'draft-{base_revision}'},
                          base_revision=base_revision)
        return draft, self.assert_request(h.validate(consumer, 'report'), producer)

    def dispatch_and_resolve_structure_request(self, consumer, producer, draft, request_id):
        h = self.handoffs
        self.assertEqual(outcome(h.claim_repair(request_id)), 'ready')
        corrected = h.publish(producer, 'report', {'summary': f"corrected-{draft['revision']}"},
                              base_revision=draft['revision'], request_id=request_id)
        self.assertEqual(h.validate(consumer, 'report')['state'], 'accepted')
        return corrected['revision']

    def test_third_repair_dispatch_for_one_slot_is_blocked(self):
        h = self.handoffs
        h.bind('reviewer', 'report', 'author', ['summary'])
        revision = 0
        notifications = set()
        for index in range(3):
            draft, request_id = self.open_structure_request('reviewer', 'author', revision)
            notifications.add(request_id)
            # Supersede each notification without dispatching a repair.
            corrected = h.publish('author', 'report', {'summary': f'independent fix {index}'},
                                  base_revision=draft['revision'])
            self.assertEqual(h.validate('reviewer', 'report')['state'], 'accepted')
            revision = corrected['revision']
        self.assertEqual(len(notifications), 3)

        dispatched = set()
        for _ in range(2):
            draft, request_id = self.open_structure_request('reviewer', 'author', revision)
            revision = self.dispatch_and_resolve_structure_request('reviewer', 'author', draft,
                                                                   request_id)
            dispatched.add(request_id)
        self.assertEqual(len(dispatched), 2)
        h.publish('author', 'report', {'note': 'third repair candidate'}, base_revision=revision)
        blocked = h.validate('reviewer', 'report')
        if blocked.get('request_id'):
            blocked = h.claim_repair(blocked['request_id'])
        self.assertEqual((outcome(blocked), blocked['reason']), ('blocked', 'slot_limit'))
        self.assertEqual(outcome(h.claim_ready('reviewer')), 'blocked')

    def test_seventh_repair_dispatch_is_blocked_across_slots_after_reopen(self):
        pending = []
        for index in range(7):
            with self.subTest(index=index):
                consumer, producer = f'reviewer-{index}', f'author-{index}'
                self.handoffs.bind(consumer, 'report', producer, ['summary'])
                draft, request_id = self.open_structure_request(consumer, producer, 0)
                pending.append((consumer, producer, draft, request_id))
        self.assertEqual(len({request_id for _, _, _, request_id in pending}), 7)
        for request in pending[:5]:
            self.dispatch_and_resolve_structure_request(*request)
        # An in-flight dispatch consumes budget even without a correction.
        sixth_id = pending[5][3]
        self.assertEqual(outcome(self.handoffs.claim_repair(sixth_id)), 'ready')

        self.reopen()
        h = self.handoffs
        self.assertEqual(outcome(h.claim_repair(sixth_id)), 'uncertain')
        consumer, _, _, seventh_id = pending[6]
        blocked = h.claim_repair(seventh_id)
        self.assertEqual((outcome(blocked), blocked['reason']), ('blocked', 'run_limit'))
        self.assertEqual(outcome(h.claim_ready(consumer)), 'blocked')


if __name__ == '__main__':
    unittest.main()

