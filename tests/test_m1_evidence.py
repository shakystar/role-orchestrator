import copy
import json
import unittest
from unittest.mock import patch

from role_orchestrator import assignment
from role_orchestrator.m1_evidence import verify_m1


OUTPUT = 'a' * 64
EXPECTED = dict(assignment_id='task-1', member_id='member-1', role_version='rev-1')


def observation(session='s1', turn_id='t1'):
    turn = dict(session=session, turn_id=turn_id, completed=True, assignment_id='task-1',
                role_version='rev-1', output_sha256=OUTPUT, usage={'tokens': 7})
    return dict(status='succeeded', receipt_sha256='b' * 64,
                actual_member_id='member-1', actual_role_version='rev-1',
                turn_evidence=dict(assignment_id='task-1', output_sha256=OUTPUT, turn=turn))


def check(*observations):
    return verify_m1(observations, **EXPECTED)


class M1EvidenceTests(unittest.TestCase):
    def test_repeated_delivery_before_and_after_checkpoint(self):
        first = observation()
        checkpoint = json.loads(json.dumps([first]))
        retry = copy.deepcopy(first)
        retry['receipt_sha256'] = 'c' * 64
        result = check(*checkpoint, retry)
        self.assertEqual(check(first), result)
        self.assertEqual((result['status'], result['turn_count']), ('verified', 1))
        self.assertEqual(result['usage'], {'tokens': 7})

    def test_empty_unscoped_usage_is_unknown_across_checkpoint(self):
        first = observation()
        first['turn_evidence']['turn']['usage'] = {}
        checkpoint = json.loads(json.dumps([first]))
        before = check(first)
        self.assertEqual(check(*checkpoint), before)
        self.assertEqual((before['status'], before['action'], before['turn_count']),
                         ('uncertain', 'hold', 1))
        self.assertEqual(before['reasons'], ['incomplete_turn_usage'])
        self.assertEqual(before['usage'], {})
        self.assertIs(before['usage_complete'], False)
        known = observation()
        known['receipt_sha256'] = 'c' * 64
        self.assertEqual(check(*checkpoint, known), check(known))
        self.assertEqual(check(known, *checkpoint), check(known))

    def test_empty_usage_still_rejects_malformed_inputs(self):
        from role_orchestrator.m1_evidence import _NATIVE_USAGE_SCOPE

        for scope, usage in ((None, []), (_NATIVE_USAGE_SCOPE, {})):
            with self.subTest(scope=scope, usage=usage):
                item = observation()
                turn = item['turn_evidence']['turn']
                turn['usage'] = usage
                if scope is not None:
                    turn['usage_scope'] = scope
                restored = json.loads(json.dumps(item))
                result = check(item)
                self.assertEqual(check(restored), result)
                self.assertEqual((result['status'], result['action']), ('failed', 'reject'))
                self.assertIn('invalid_turn_usage', result['reasons'])

    def test_native_all_zero_usage_remains_known_across_checkpoint(self):
        from role_orchestrator.m1_evidence import _NATIVE_USAGE_FIELDS, _NATIVE_USAGE_SCOPE

        item = observation()
        turn = item['turn_evidence']['turn']
        counts = {name: 0 for name in _NATIVE_USAGE_FIELDS}
        turn.update(usage_scope=_NATIVE_USAGE_SCOPE, usage=counts)
        result = check(item)
        self.assertEqual(check(json.loads(json.dumps(item))), result)
        self.assertEqual((result['status'], result['action'], result['turn_count']),
                         ('verified', 'accept', 1))
        self.assertEqual(result['usage'], counts)
        self.assertIs(result['usage_complete'], True)
        self.assertEqual(result['usage_scope'], _NATIVE_USAGE_SCOPE)

    def test_distinct_turns_with_identical_output_count_twice(self):
        for other in (observation(turn_id='t2'), observation(session='s2')):
            with self.subTest(other=other['turn_evidence']['turn']):
                result = check(observation(), other)
                self.assertEqual(result['status'], 'failed')
                self.assertIn('unauthorized_repeat', result['reasons'])
                self.assertEqual(result['turn_count'], 2)
                self.assertEqual(result['usage'], {'tokens': 14})

    def test_missing_completion_or_turn_holds_across_checkpoint(self):
        no_completion = observation()
        del no_completion['turn_evidence']['turn']['completed']
        no_turn = observation()
        del no_turn['turn_evidence']['turn']['turn_id']
        for record in (no_completion, no_turn):
            with self.subTest(record=record):
                before = check(record)
                checkpoint = json.loads(json.dumps([record]))
                after = check(*checkpoint)
                for verdict in (before, after):
                    self.assertEqual((verdict['status'], verdict['action']), ('uncertain', 'hold'))
                    self.assertFalse(verdict['automatic_dispatch'])
        self.assertEqual(check(no_completion, observation())['status'], 'verified')

    def test_wrong_assignment_or_role_revision_fails(self):
        for field, bad_value, reason in (
                ('assignment_id', 'task-2', 'assignment_mismatch'),
                ('role_version', 'rev-2', 'actor_or_role_revision_mismatch')):
            with self.subTest(field=field):
                record = observation()
                record['turn_evidence']['turn'][field] = bad_value
                result = check(record)
                self.assertEqual(result['status'], 'failed')
                self.assertIn(reason, result['reasons'])
                self.assertFalse(result['automatic_dispatch'])

    def test_conflicting_usage_for_same_turn_fails_without_credit(self):
        retry = observation()
        retry['turn_evidence']['turn']['usage']['tokens'] = 8
        result = check(observation(), retry)
        self.assertEqual(result['reasons'], ['conflicting_turn_evidence'])
        self.assertEqual(result['turn_count'], 1)
        self.assertEqual(result['usage'], {})

    def test_legacy_receipt_is_not_m1_proof_when_explicitly_checked(self):
        legacy = observation()
        del legacy['turn_evidence']
        self.assertEqual(check(legacy)['action'], 'hold')


class NativeM1IntegrationTests(unittest.TestCase):
    def native_result(self):
        return dict(text='same output', actual_member_id='member-1', actual_role_version='rev-1',
                    session='s1', turn_id='t1',
                    turn_usage=dict(input_tokens=11, output_tokens=5, cached_input_tokens=3))

    def observe_native(self, result, receipt_hash='b' * 64):
        ids = dict(run_id='run-1', attempt_id='attempt-1')
        request = dict(id='task-1', role='member-1', instruction='synthetic input')
        plan = dict(task_id='task-1', member_id='member-1', expected_role_version='rev-1',
                    instruction=request['instruction'], input_sha256='c' * 64)
        receipt = dict(status='succeeded', task=dict(input=request['instruction']), result=result)
        with patch.object(assignment.AttemptReader, 'bound', return_value=(receipt, receipt_hash)) as bound:
            verdict = assignment.observe('native', 'state', ids, plan, request)
        bound.assert_called_once_with(ids['attempt_id'], request, plan['input_sha256'])
        return verdict

    def test_native_observe_and_replay_across_checkpoint(self):
        native = self.native_result()
        first = self.observe_native(native)
        before = check(first)
        self.assertEqual((before['status'], before['action'], before['turn_count']),
                         ('verified', 'accept', 1))
        self.assertEqual(before['usage'], native['turn_usage'])
        self.assertEqual(before['usage_scope'], 'final_matched_tokenUsage.last')
        self.assertTrue(before['usage_complete'])
        self.assertFalse(before['automatic_dispatch'])
        checkpoint = json.loads(json.dumps([first]))
        self.assertEqual(check(*checkpoint, self.observe_native(native, 'd' * 64)), before)
        second = self.observe_native(dict(native, turn_id='t2'), 'e' * 64)
        repeated = check(*checkpoint, second)
        self.assertEqual((repeated['action'], repeated['turn_count']), ('reject', 2))
        self.assertIn('unauthorized_repeat', repeated['reasons'])

    def test_unknown_native_usage_keeps_verified_turn_count(self):
        native = self.native_result()
        del native['turn_usage']
        first = self.observe_native(native)
        self.assertIsNone(first['turn_evidence']['turn']['usage'])
        checkpoint = json.loads(json.dumps([first]))
        before = check(first)
        self.assertEqual(check(*checkpoint, self.observe_native(native)), before)
        self.assertEqual((before['status'], before['action'], before['turn_count']),
                         ('uncertain', 'hold', 1))
        self.assertEqual(before['reasons'], ['incomplete_turn_usage'])
        self.assertEqual(before['usage'], {})
        self.assertFalse(before['usage_complete'])
        self.assertFalse(before['automatic_dispatch'])
        zero = dict(input_tokens=0, output_tokens=0, cached_input_tokens=0)
        known = self.observe_native(dict(native, turn_usage=zero), 'd' * 64)
        for replay in (check(*checkpoint, known), check(known, *checkpoint)):
            self.assertEqual((replay['status'], replay['turn_count']), ('verified', 1))
            self.assertTrue(replay['usage_complete'])
            self.assertEqual(replay['usage'], zero)

    def test_missing_turn_id_preserves_native_output_but_not_bad_bindings(self):
        native = self.native_result()
        del native['turn_id']
        legacy = self.observe_native(native)
        self.assertEqual(legacy, dict(status='succeeded', receipt_sha256='b' * 64,
                                     actual_member_id='member-1', actual_role_version='rev-1'))
        self.assertEqual(check(legacy)['action'], 'hold')
        for rejected, reason in (
                (self.observe_native(dict(native, actual_member_id='member-2')),
                 'actor_or_role_revision_mismatch'),
                (self.observe_native(dict(native, actual_role_version='rev-2')),
                 'actor_or_role_revision_mismatch'),
                (self.observe_native(native, 'not-a-sha256'), 'invalid_receipt_or_output_binding'),
                (dict(status='failed', reason='receipt_binding_mismatch'),
                 'invalid_receipt_or_output_binding')):
            with self.subTest(rejected=rejected):
                self.assertNotIn('turn_evidence', rejected)
                verdict = check(rejected)
                self.assertEqual((verdict['status'], verdict['action']), ('failed', 'reject'))
                self.assertIn(reason, verdict['reasons'])
                self.assertFalse(verdict['automatic_dispatch'])
