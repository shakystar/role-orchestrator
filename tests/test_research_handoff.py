import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from role_orchestrator import research_handoff as r


class ResearchHandoffTest(unittest.TestCase):
    def test_author_as_reviewer_is_never_dispatched(self):
        state = dict(case={'kind': 'request'}, data={'draft': {'authors': ['acl-025']}},
                     receipts={'observe': {}, 'draft': {}}, branch=None,
                     pending=None, pin={})
        with patch.object(r, 'native') as native, patch.object(r, 'save'):
            result = r.advance(SimpleNamespace(), None, state,
                               {'review': (r.TEAM, 'acl-025')}, {}, {})
        self.assertEqual(result['branch']['reason'], 'reviewer_participated')
        native.assert_not_called()

    def test_review_conflict_checkpoint_and_invalid_reports(self):
        draft = dict(authors=['author'], claims={'claim': {}})
        valid = dict(draft_sha256=r.digest(r.canonical(draft)), conflict=True, opinions={})
        vectors = (
            (valid, None),
            (dict(valid, draft_sha256='0' * 64), 'invalid_or_conflicted_review'),
            (dict(valid, conflict=1), 'invalid_or_conflicted_review'),
            ({k: v for k, v in valid.items() if k != 'conflict'}, 'invalid_or_conflicted_review'),
            (dict(valid, opinions=[]), 'invalid_or_conflicted_review'),
            (dict(valid, opinions={'bad': {}}), 'invalid_opinion'),
            (dict(valid, conflict=False), 'invalid_or_conflicted_review'),
        )
        args = SimpleNamespace(one_step=False)
        member, task_id = 'acl-025', f'{r.TEAM}--{r.STEPS["review"]}'
        route = {'review': (r.TEAM, member)}
        teams = {r.TEAM: dict(lead='lead', members={member: {'role_version': 'v1'}})}
        for value, error in vectors:
            with self.subTest(value=value):
                pending = dict(phase='review', task_id=task_id, member_id=member, input={})
                state = dict(case=dict(kind='request', id='case-1', evidence={}),
                             data=dict(draft=draft, draft_sha256=valid['draft_sha256']),
                             receipts={'observe': {}, 'draft': {}}, branch=None,
                             pending=pending, pin={}, fixtures=None)
                receipt = dict(task_id=task_id, member_id=member,
                               artifact={'payload': {'artifact': value}})
                saved = []

                def save_checkpoint(_, checkpoint):
                    saved.append(deepcopy(checkpoint))

                with patch.object(r, 'native', return_value=receipt) as native, \
                     patch.object(r, 'link', return_value=receipt) as link, \
                     patch.object(r, 'save', side_effect=save_checkpoint) as save:
                    result = r.advance(args, None, state, route, {}, teams)
                    native.assert_called_once_with(args, pending, False, state_fd=None)
                    link.assert_called_once_with(args, pending, receipt)
                    save.assert_called_once()
                    resumed = saved[0]
                    self.assertIsNone(resumed['pending'])
                    self.assertEqual(resumed['receipts']['review'],
                                     dict(task_id=task_id, member_id=member,
                                          case_id='case-1', role_version='v1'))
                    if error is None:
                        expected = dict(action='reassign', phase='review', reason='reviewer_conflict')
                        self.assertEqual(resumed['data']['review'], value)
                    else:
                        expected = dict(action='hold', phase='review', reason=error, terminal=True)
                        self.assertNotIn('review', resumed['data'])
                    self.assertEqual(result['branch'], expected)
                    self.assertEqual(resumed['branch'], expected)
                    replay = r.advance(args, None, resumed, route, {}, teams)
                    self.assertEqual(replay['branch'], expected)
                    self.assertEqual(resumed, state)
                    native.assert_called_once()
                    link.assert_called_once()
                    save.assert_called_once()

    def test_packet_holds_only_affected_claim_and_keeps_dissent(self):
        case = {'evidence': {'S1': {'facts': {'denominator': None}, 'owner': 'support'}}}
        draft = {'claims': {
            'observation': {'requires': [], 'support': ['S1']},
            'demand': {'requires': [{'ref': 'S1', 'field': 'denominator'}], 'support': ['S1']},
        }}
        opinions = {
            'limited': dict(claim='observation', verdict='support', missing=[]),
            'denominator': dict(claim='demand', verdict='undetermined', missing=[]),
        }
        state = dict(case=case, data=dict(draft=draft, draft_sha256='fixed',
                                         review={'opinions': opinions}))
        synthesis = dict(draft_sha256='fixed', next_question='What is the denominator?',
                         dispositions={'limited': 'acknowledge', 'denominator': 'retain_dissent'})
        updates, _ = r.apply('synthesize', synthesis, state, {'review': {'member_id': 'acl-025'}})
        packet = updates['packet']
        self.assertEqual(packet['gates']['observation']['status'], 'eligible')
        self.assertEqual(packet['gates']['demand']['status'], 'revision_hold')
        self.assertEqual(packet['gates']['demand']['upstream'][0]['to'], 'support')
        self.assertEqual(packet['review']['opinions'], opinions)
        state['data'].update(updates)
        decision = dict(action='select_followup', reason='Bounded finding',
                        claims=['demand'], packet_sha256=updates['packet_sha256'])
        with self.assertRaisesRegex(ValueError, 'invalid_product_decision'):
            r.apply('decision', decision, state, {})
        decision['claims'] = ['observation']
        self.assertEqual(r.apply('decision', decision, state, {})[1], decision)


if __name__ == '__main__':
    unittest.main()
