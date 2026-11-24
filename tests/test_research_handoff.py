import unittest
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
