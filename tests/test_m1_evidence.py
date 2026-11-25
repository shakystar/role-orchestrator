import copy
import json
import unittest

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
