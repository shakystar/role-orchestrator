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

        def reconcile(args, current, dispatch, *, state_fd):
            self.assertEqual(current['input']['fixture'], fixtures[current['task_id']])
            return self.observation(args, current, dispatch)

        def interrupt(args, current, dispatch, *, state_fd):
            result = reconcile(args, current, dispatch, state_fd=state_fd)
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
            with self.subTest(backend=backend), broker.state_directory(
                self.args.state, os.getuid()
            ) as state_fd, patch.object(broker, 'invoke') as invoke:
                broker.native(self.args, current, False, state_fd=state_fd)
                command, data = invoke.call_args.args
                self.assertEqual(command, ['role_orchestrator.assignment', 'status', '--state', 'demo--1',
                                           '--runtime', self.args.runtime, '--verify-native-receipt'])
                self.assertEqual(invoke.call_args.kwargs, {'state_fd': state_fd})
                self.assertEqual(data, b'')

    def test_pinned_state_and_child_cwd(self):
        parent = os.stat('.')
        root = self.args.state.stat()
        identity = (root.st_dev, root.st_ino)
        with TemporaryDirectory() as modules:
            Path(modules, 'planning_cwd_probe.py').write_text(
                "import json, os, sys\n"
                "if len(sys.argv) > 1:\n"
                "    raise SystemExit(7)\n"
                "info = os.stat('.')\n"
                "print(json.dumps({'identity': [info.st_dev, info.st_ino]}))\n",
                encoding='utf-8',
            )
            with patch.dict(os.environ, {'PYTHONPATH': modules}), broker.state_directory(
                self.args.state, os.getuid()
            ) as state_fd:
                info = os.fstat(state_fd)
                self.assertEqual((info.st_dev, info.st_ino), identity)
                broker.save(state_fd, {'probe': True})
                self.assertEqual((self.args.state / 'team.json').read_bytes(),
                                 canonical({'probe': True}) + b'\n')
                result = broker.invoke(['planning_cwd_probe'], b'', state_fd=state_fd)
                self.assertEqual(result, {'returncode': 0, 'value': {'identity': list(identity)}})
                self.assertTrue(os.path.samestat(parent, os.stat('.')))
                failed = broker.invoke(['planning_cwd_probe', 'fail'], b'', state_fd=state_fd)
                self.assertEqual(failed['returncode'], 7)
                self.assertTrue(os.path.samestat(parent, os.stat('.')))
