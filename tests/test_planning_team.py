import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from role_orchestrator import planning_catalog as catalog, planning_team as broker
from role_orchestrator.role_contracts import canonical, digest


class PlanningTeamTest(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        route = dict(role_id='planner', role_version='v1', instruction='Use supplied evidence.',
                     model='gpt-6.2-sol', effort='low')
        raw = canonical({'version': 1, 'teams': {'demo': {
            'lead': 'lead', 'members': {'lead': route, 'expert': dict(route, role_id='specialist')},
            'goals': {'draft': {'uids': [os.getuid()], 'text': 'Draft a plan'}}, 'max_steps': 3}}})
        self.args = SimpleNamespace(lab='/fake/lab', revision='a' * 40, runtime='/fake/runtime',
                                    state=Path(tmp.name), backend='fixture', team='demo', goal='draft',
                                    catalog_sha256=digest(raw))
        git = patch.object(catalog, 'git', side_effect=lambda _, op, *rest:
                           self.args.revision if op == 'rev-parse' else raw.decode())
        git.start()
        self.addCleanup(git.stop)
        self.calls = []

    def observation(self, args, current, dispatch):
        task = current['task_id']
        self.calls.append((task, dispatch))
        intent = {'explicit': ['plan'], 'hypotheses': ['Cost could matter'], 'counterevidence': []}
        if task == 'demo--1':
            payload = {'decision': dict(action='assign', intent=intent, work=[{'member_id': 'expert', 'input': {}}])}
        elif task == 'demo--2':
            payload = {'artifact': {'options': [{'name': 'minimal'}]}}
        else:
            payload = {'decision': dict(action='accept', intent=intent,
                                        proposal={'recommendation': 'minimal'}, evidence=['demo--2'])}
        route = SimpleNamespace(task=task, member=current['member_id'], lab=args.lab, backend=args.backend)
        plan = catalog.bound_plan(route, args.revision, 2)
        attempt = {'run_id': 'run-' + task, 'attempt_id': 'first'}
        plan.update(attempt, input_sha256=digest(canonical(current['input'])))
        verdict = dict(attempt, status='succeeded', actual_member_id=current['member_id'],
                       actual_role_version=plan['expected_role_version'],
                       receipt_sha256='b' * 64, artifact={'payload': payload})
        return {'returncode': 0, 'value': {'plan': plan, 'verdict': verdict}}

    def test_reconcile_after_native_before_team_checkpoint(self):
        fixtures = {f'demo--{n}': {'marker': n} for n in (1, 2, 3)}

        def reconcile(args, current, dispatch):
            self.assertEqual(current['input']['fixture'], fixtures[current['task_id']])
            return self.observation(args, current, dispatch)

        def interrupt(args, current, dispatch):
            result = reconcile(args, current, dispatch)
            if current['task_id'] == 'demo--2':
                raise InterruptedError('simulated interruption')  # caught as an uncertain native call
            return result

        with patch.object(broker, 'native', side_effect=interrupt):
            result = broker.run(self.args, {'request': 'Make a plan', 'fixtures': fixtures})
            self.assertEqual(result['pending'], 'demo--2')
        with patch.object(broker, 'native', side_effect=reconcile):
            result = broker.run(self.args, None)
            self.assertEqual(result['branch']['action'], 'accept')
            self.assertEqual(result['artifacts']['demo--2']['evidence_kind'], 'fixture')
            self.assertEqual(broker.run(self.args, None), result)
        self.assertEqual(self.calls, [('demo--1', True), ('demo--2', True),
                                      ('demo--2', False), ('demo--3', True)])

    def test_missing_native_after_intent_stays_pending(self):
        with patch.object(broker, 'native', side_effect=[KeyboardInterrupt(), {'returncode': 2}]) as native:
            with self.assertRaises(KeyboardInterrupt):
                broker.run(self.args, {'request': 'Make a plan'})
            self.assertEqual(broker.run(self.args, None)['pending'], 'demo--1')
        self.assertEqual([call.args[2] for call in native.call_args_list], [True, False])

    def test_rejects_bad_routes(self):
        for members, budget in ((['missing'], 2), (['expert', 'expert'], 3), (['../expert'], 2), (['expert'], 1)):
            value = dict(action='assign', intent={'explicit': [], 'hypotheses': [], 'counterevidence': []},
                         work=[dict(member_id=member, input={}) for member in members])
            with self.subTest(members=members, budget=budget), self.assertRaises(ValueError):
                broker.decision(value, [], {'expert': {}}, {}, budget)

    def test_native_plan_and_actual_member_mismatch(self):
        current = dict(task_id='demo--1', member_id='lead', input={'context': {}}, lead=True)
        for target, field, bad in (('plan', 'expected_role_version', 'other'),
                                   ('verdict', 'actual_member_id', 'expert'),
                                   ('verdict', 'actual_role_version', 'v2'),
                                   ('verdict', 'actual_role_version', None)):
            with self.subTest(field=field):
                observation = self.observation(self.args, current, False)
                if bad is None:
                    del observation['value'][target][field]
                else:
                    observation['value'][target][field] = bad
                with self.assertRaises(ValueError):
                    broker.link(self.args, current, observation)

    def test_native_status_verifies_both_backends(self):
        current = {'task_id': 'demo--1'}
        for backend in ('fixture', 'codex'):
            self.args.backend = backend
            with self.subTest(backend=backend), patch.object(broker, 'invoke') as invoke:
                broker.native(self.args, current, False)
                command, data = invoke.call_args.args
                self.assertEqual(command[3:], ['status', '--state', str(self.args.state / 'demo--1'),
                                               '--runtime', self.args.runtime, '--verify-native-receipt'])
                self.assertEqual(data, b'')
