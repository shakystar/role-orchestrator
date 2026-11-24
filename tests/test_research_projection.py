import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from role_orchestrator import research_handoff as r


def observation():
    return dict(observations=['S1 records one event'], hypotheses=[], signals=[],
                unknowns=[], falsifiers=[], needs=[dict(ref='S1', field='denominator')])


class ResearchProjectionTest(unittest.TestCase):
    def test_observe_pending_checkpoint_uses_raw_receipt_without_reissuing(self):
        raw = observation()
        raw.update(case_id='ignored', evidence={'forged': True}, explanation='ignored')
        raw['needs'][0]['owner'] = 'forged'
        receipt = dict(artifact={'payload': {'artifact': raw}}, member_id='observer')
        original = deepcopy(receipt)
        state = dict(case={'id': 'synthetic', 'kind': 'request', 'evidence': {
            'S1': {'facts': {'denominator': None}, 'owner': 'source-owner'}}},
            data={}, fixtures=None, receipts={}, pending=None, branch=None, pin={})
        args = SimpleNamespace(one_step=True)
        route = {'observe': (r.TEAM, 'observer')}
        approved = {r.TEAM: ('goal', 'Approved research')}
        teams = {r.TEAM: {'lead': 'author', 'members': {'observer': {'role_version': 'v1'}}}}
        saved = []
        with patch.object(r, 'native', side_effect=[OSError('wait'), receipt]) as native, \
             patch.object(r, 'link', side_effect=lambda args, current, result: result), \
             patch.object(r, 'save', side_effect=lambda fd, s: saved.append(r.canonical(s))):
            first = r.advance(args, None, state, route, approved, teams)
            self.assertEqual(first['branch']['action'], 'hold')
            self.assertEqual(first['receipts'], {})
            pending = r.strict_json(saved[-1].decode())
            self.assertEqual(pending['pending']['phase'], 'observe')
            after = r.advance(args, None, pending, route, approved, teams)
        first_call, resumed_call = native.call_args_list
        self.assertEqual(first_call.args[1], resumed_call.args[1])
        self.assertIs(first_call.args[2], True)
        self.assertIs(resumed_call.args[2], False)
        self.assertEqual(after['next_phase'], 'draft')
        self.assertEqual(after['data']['observation'], observation())
        self.assertEqual(after['data']['upstream'][0]['to'], 'source-owner')
        self.assertEqual(after['receipts']['observe']['member_id'], 'observer')
        self.assertEqual(receipt, original)

    def test_required_schema_is_not_repaired(self):
        state = {'case': {'evidence': {'S1': {'facts': {}}}}, 'data': {}}
        value = observation()
        del value['falsifiers']
        value['extra'] = []
        with self.assertRaisesRegex(ValueError, 'invalid_observation'):
            r.apply('observe', value, state, {})
        value = observation()
        value['needs'][0]['field'] = None
        with self.assertRaisesRegex(ValueError, 'invalid_dependency'):
            r.apply('observe', value, state, {})

    def test_observer_missing_denominator_gates_inference_not_limited_observation(self):
        case = {'source': {'author': 'requester', 'text': 'Original signal'}, 'contributors': [],
                'evidence': {'S1': {'facts': {'denominator': None}, 'owner': 'source-owner',
                                    'text': 'One event was recorded'}}}
        state = dict(case=case, data={})
        receipts = {phase: {'member_id': member} for phase, member in
                    [('observe', 'observer'), ('draft', 'author'), ('review', 'reviewer')]}
        observed, _ = r.apply('observe', observation(), state, receipts)
        state['data'].update(observed)
        base = dict(support=['S1'], counter=[], requires=[], alternatives=['Sampling'],
                    falsifiers=['Contrary evidence'], limits='S1 only', reproduction='Unverified')
        draft = dict(claims={
            'limited': dict(base, statement='S1 records one event',
                            claim_scope='limited_observation', eligible=False),
            'expanded': dict(base, statement='Demand is growing', claim_scope='broader_inference'),
            'legacy': dict(base, statement='Demand is growing'),
        }, authors=['reviewer'])
        updates, _ = r.apply('draft', draft, state, receipts)
        state['data'].update(updates)
        self.assertNotIn('reviewer', updates['draft']['authors'])
        self.assertNotIn('eligible', updates['draft']['claims']['limited'])
        opinions = {'o_' + cid: dict(claim=cid, verdict='support', reason='Reviewed', missing=[])
                    for cid in draft['claims']}
        sha = updates['draft_sha256']
        reviewed, _ = r.apply('review', dict(draft_sha256=sha, conflict=False, opinions=opinions),
                              state, receipts)
        state['data'].update(reviewed)
        # Exercise the same inputs after serialization, including the legacy omitted scope.
        state = r.strict_json(r.canonical(state).decode())
        synthesis = dict(draft_sha256=sha, dispositions=dict.fromkeys(opinions, 'acknowledge'),
                         next_question='What is the denominator?', gates={'expanded': 'eligible'})
        updates, _ = r.apply('synthesize', synthesis, state, receipts)
        state['data'].update(updates)
        packet = updates['packet']
        self.assertEqual(packet['case'], case)
        self.assertEqual(packet['gates']['limited']['status'], 'eligible')
        self.assertEqual(packet['receipts']['review']['member_id'], 'reviewer')
        for cid in ('expanded', 'legacy'):
            self.assertEqual(packet['gates'][cid]['status'], 'revision_hold')
            self.assertEqual(packet['gates'][cid]['reasons'], ['missing_evidence'])
            self.assertEqual(packet['gates'][cid]['upstream'], observed['upstream'])
        decision = dict(packet_sha256=updates['packet_sha256'], action='select_followup',
                        reason='Follow up', claims=['expanded'])
        with self.assertRaisesRegex(ValueError, 'invalid_product_decision'):
            r.apply('decision', decision, state, receipts)
        decision['claims'] = ['limited']
        self.assertEqual(r.apply('decision', dict(decision, role='ignored'), state, receipts)[1],
                         decision)

        # Simulate stale eligibility in a sealed packet; never rewrite its hash or gates.
        legacy = deepcopy(packet)
        legacy['gates']['legacy'] = dict(status='eligible', reasons=[], opinions=['o_legacy'], upstream=[])
        legacy_hash = r.digest(r.canonical({k: v for k, v in legacy.items() if k != 'receipts'}))
        state['data'].update(packet=legacy, packet_sha256=legacy_hash)
        decision.update(packet_sha256=legacy_hash, claims=['legacy'])
        with self.assertRaisesRegex(ValueError, 'packet_requires_resynthesis'):
            r.apply('decision', decision, state, receipts)
        self.assertEqual(state['data']['packet']['gates']['legacy']['status'], 'eligible')
