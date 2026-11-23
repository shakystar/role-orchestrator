import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from role_orchestrator.role_contracts import canonical, digest


MEMBERS = {
    'acl-001': 'scenario-writer',
    'acl-002': 'identity-designer',
    'acl-003': 'crash-window-analyst',
    'acl-004': 'fixture-author-v2',
    'acl-005': 'oracle-reviewer-v2',
    'acl-006': 'go-receipt-author',
    'acl-007': 'python-resume-author',
    'acl-008': 'regression-author-v2',
    'acl-009': 'evidence-reader',
}


@unittest.skipUnless(os.environ.get('ROLE_RUNTIME_BIN'), 'native fixture bridge required')
class GoalIntakeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        repo = self.root / 'repo'
        (repo / 'docs/team').mkdir(parents=True)
        (repo / 'docs/features').mkdir()
        (repo / 'docs/team/roster.md').write_text(''.join(
            f'| {member} | {role} | {role} | {role} |\n' for member, role in MEMBERS.items()))
        (repo / 'docs/features/development-handoffs.md').write_text(''.join(
            f'| {role} / {role} | Inputs | Output | Sol / medium |\n' for role in MEMBERS.values()))
        source = repo / 'status.py'
        source.write_text('def status(started):\n    return "started"\n')
        git = ['git', '-C', str(repo)]
        subprocess.run([*git, 'init', '-q'], check=True)
        subprocess.run([*git, 'add', '.'], check=True)
        subprocess.run([*git, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@invalid',
                        'commit', '-qm', 'fixture'], check=True)
        self.base = subprocess.check_output([*git, 'rev-parse', 'HEAD'], text=True).strip()
        source.write_text('UNCOMMITTED_SENTINEL\n')
        self.case = {'case_id': 'status-before-start', 'goal': 'Report not_started before startup.',
                     'base_sha': self.base, 'source': {'path': str(repo)}}

    def invoke(self, action, *, backend='fixture', state='case'):
        command = [sys.executable, '-m', 'role_orchestrator.development_case', action,
                   '--state', str(self.root / state), '--runtime', os.environ['ROLE_RUNTIME_BIN'],
                   '--lab', str(self.root / 'repo'), '--revision', self.base, '--backend', backend]
        result = subprocess.run(command, input=json.dumps(self.case), text=True,
                                capture_output=True, timeout=120)
        return result.returncode, json.loads(result.stdout or result.stderr)

    def test_status_of_new_goal_is_read_only(self):
        code, report = self.invoke('status', backend='codex')
        self.assertEqual((code, report.get('state')), (0, 'not_started'), report)
        self.assertFalse((self.root / 'case').exists())

    def fixtures(self):
        criterion = 'status-absent'
        patch = '*** example author patch ***\n'
        vectors = {'criterion_id': criterion, 'initial_state': {'started': False},
                   'actions': ['query status'], 'expected_state': 'not_started',
                   'expected_new_dispatches': 0}
        return {
            'scenario': {'criterion_id': criterion, 'initial_state': {'started': False},
                         'source_files': ['status.py'],
                         'actions': ['query status'], 'observations': ['not_started']},
            'identity': {'keys': ['case_id'], 'serialization': 'opaque string',
                         'examples': ['status-before-start'], 'compatibility': 'unchanged'},
            'windows': {'windows': ['before startup'], 'source_refs': ['status.py']},
            'vectors': vectors,
            'oracle': {'cases': ['before startup'], 'counterexamples': ['always started'],
                       'verdict': 'accept', 'vectors_sha256': digest(canonical(vectors))},
            'regression': {'base_sha': self.base, 'diff': 'regression patch', 'command': 'python -m unittest',
                           'expected_control': 'pass', 'expected_fault': 'fail'},
            'author': {'base_sha': self.base, 'diff': patch, 'tests': ['status test'], 'unverified': []},
            'reviewer': {'criterion_id': criterion, 'verdict': 'accept', 'reason': 'fixture review',
                         'patch_sha256': digest(patch.encode())},
        }

    def test_pinned_source_native_handoffs_and_replay(self):
        self.case['fixtures'] = self.fixtures()
        code, report = self.invoke('run')
        self.assertEqual((code, report.get('state')), (0, 'patch_reviewed'), report)
        members = {'scenario': 'acl-001', 'identity': 'acl-002', 'windows': 'acl-003',
                   'vectors': 'acl-004', 'oracle': 'acl-005', 'regression': 'acl-008',
                   'author': 'acl-007', 'reviewer': 'acl-005'}
        runs = set()
        receipts = {}
        inputs = {}
        for stage, member in members.items():
            artifact = report[stage]
            self.assertEqual(artifact['member_id'], member)
            self.assertEqual(artifact['payload'], self.case['fixtures'][stage])
            runs.add(artifact['run_id'])
            records = list((self.root/'case'/stage/'runtime'/'tasks').glob('*.json'))
            self.assertEqual(len(records), 1)
            raw = records[0].read_bytes()
            self.assertEqual(digest(raw), artifact['receipt_sha256'])
            receipts[records[0]] = raw
            inputs[stage] = json.loads(json.loads(raw)['task']['input'])
        self.assertEqual(len(runs), len(members))
        self.assertIn('def status(started)', json.dumps(inputs['author']['source']))
        self.assertNotIn('UNCOMMITTED_SENTINEL', json.dumps(inputs))
        for stage, upstream in [('identity', 'scenario'), ('windows', 'identity'), ('vectors', 'windows'),
                                ('oracle', 'vectors'), ('regression', 'oracle'), ('author', 'regression')]:
            self.assertEqual(inputs[stage][upstream], report[upstream]['payload'])
        self.assertEqual(inputs['reviewer']['authored_revision']['artifact'], report['author']['payload'])
        self.assertEqual(inputs['reviewer']['patch_sha256'], report['reviewer']['payload']['patch_sha256'])
        self.assertEqual(self.invoke('status'), (code, report))
        self.assertEqual(self.invoke('run'), (code, report))
        self.assertEqual({p: p.read_bytes() for p in (self.root/'case').glob('*/runtime/tasks/*.json')},
                         receipts)

    def test_invalid_scenario_is_terminal_before_downstream_assignment(self):
        self.case['fixtures'] = self.fixtures()
        del self.case['fixtures']['scenario']['criterion_id']
        code, result = self.invoke('run')
        self.assertNotEqual(code, 0)
        self.assertEqual((result.get('stage'), result.get('state')), ('scenario', 'failed'), result)
        self.assertFalse((self.root/'case'/'identity').exists())
        self.assertEqual(len(list((self.root/'case').glob('*/runtime/tasks/*.json'))), 1)
        self.assertEqual(self.invoke('run'), (code, result))

    def test_oracle_cannot_approve_another_revision(self):
        self.case['fixtures'] = self.fixtures()
        self.case['fixtures']['oracle']['vectors_sha256'] = '0' * 64
        code, result = self.invoke('run')
        self.assertNotEqual(code, 0)
        self.assertNotIn(result.get('state'), ('patch_reviewed', 'accepted'))
        self.assertFalse((self.root/'case'/'regression').exists())
        stages = {p.parent.parent.parent.name for p in (self.root/'case').glob('*/runtime/tasks/*.json')}
        self.assertEqual(stages, {'scenario', 'identity', 'windows', 'vectors', 'oracle'})

    def test_revision_request_reuses_prior_work_and_reviews_the_new_vectors(self):
        outputs = self.fixtures()
        revised = {**outputs['vectors'], 'actions': ['query status', 'query status again']}
        original_oracle = {**outputs['oracle'], 'verdict': 'revise'}
        outputs.update({'oracle': original_oracle, 'vectors-r1': revised,
                        'oracle-r1': {**original_oracle, 'verdict': 'accept',
                                      'vectors_sha256': digest(canonical(revised))}})
        self.case['fixtures'] = outputs
        code, report = self.invoke('run')
        self.assertEqual((code, report.get('state')), (0, 'patch_reviewed'), report)
        self.assertEqual(report['vectors']['payload'], revised)
        self.assertEqual(report['oracle']['payload'], outputs['oracle-r1'])
        directory = self.root/'case'
        def receipts():
            return {p: p.read_bytes() for p in directory.glob('*/runtime/tasks/*.json')}
        before = receipts()
        self.assertEqual(len(before), 10)
        received = {p.parent.parent.parent.name: json.loads(json.loads(raw)['task']['input'])
                    for p, raw in before.items()}
        self.assertEqual(received['vectors-r1']['correction']['feedback'], original_oracle)
        self.assertEqual(received['vectors-r1']['correction']['review_receipt'],
                         report['revisions']['oracle']['receipt_sha256'])
        self.assertEqual(received['oracle-r1']['vectors'], revised)
        self.assertEqual(received['regression']['vectors'], revised)
        self.assertEqual(received['regression']['oracle'], outputs['oracle-r1'])
        self.assertEqual(self.invoke('run'), (code, report))
        self.assertEqual(receipts(), before)

    def test_malformed_patch_verdict_requests_a_bounded_correction(self):
        outputs = self.fixtures()
        outputs['reviewer-r1'] = outputs['reviewer']
        outputs['reviewer'] = {**outputs['reviewer-r1'], 'verdict': 'unverified'}
        self.case['fixtures'] = outputs
        code, report = self.invoke('run')
        self.assertEqual((code, report.get('state')), (0, 'patch_reviewed'), report)
        self.assertEqual(report['reviewer']['task_id'], 'reviewer-r1')
        state = self.root/'case'
        receipts = {p.parent.parent.parent.name: p.read_bytes()
                    for p in state.glob('*/runtime/tasks/*.json')}
        self.assertEqual(len(receipts), 9)
        first = json.loads(receipts['reviewer'])
        second = json.loads(receipts['reviewer-r1'])
        self.assertEqual(first['status'], 'succeeded')
        self.assertEqual(json.loads(first['result']['text'])['verdict'], 'unverified')
        inputs = json.loads(second['task']['input'])
        self.assertEqual(inputs['correction']['previous_receipt'], digest(receipts['reviewer']))
        self.assertEqual(inputs['goal'], self.case['goal'])
        self.assertEqual(inputs['patch_sha256'], report['patch_sha256'])
        self.assertEqual(self.invoke('status'), (0, report))
        self.assertFalse((state/'reviewer-r2').exists())
