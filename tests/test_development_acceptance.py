from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from role_orchestrator import development_acceptance as acceptance
from role_orchestrator.role_contracts import digest


class DevelopmentAcceptanceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.args = SimpleNamespace(action='run', state=root, execution=root/'run.json',
                                    oracle=root/'regression', baseline_log=root/'baseline.log',
                                    candidate_log=root/'candidate.log', role_baseline_log=root/'role-base.log',
                                    role_candidate_log=root/'role-candidate.log', candidate_source=root/'repo')
        self.args.oracle.write_bytes(b'regression example\n')
        self.args.baseline_log.write_bytes(b'expected assertion failed\n')
        self.args.candidate_log.write_bytes(b'passed\n')
        self.args.role_baseline_log.write_bytes(
            b'F\nFAIL: test_status (test_case.GoalTest.test_status)\n'
            b'AssertionError: wrong status\nRan 1 test in 0.002s\n\nFAILED (failures=1)\n')
        self.args.role_candidate_log.write_bytes(b'.\nRan 1 test in 0.002s\n\nOK\n')
        self.case = {'case_id': 'case', 'goal': 'Make the regression pass.'}
        patch_sha = digest(b'patch')
        self.reviewed = {
            'state': 'reviewed',
            'author': {'receipt_sha256': 'a'*64, 'payload': {'base_sha': 'b'*40, 'diff': 'patch'}},
            'regression': {'receipt_sha256': 'd'*64, 'payload': {'diff': 'role test patch'}},
            'reviewer': {'receipt_sha256': 'c'*64, 'payload': {
                'criterion_id': 'criterion', 'verdict': 'accept', 'patch_sha256': patch_sha}},
        }
        self.run = dict(case_id='case', base_sha='b'*40, criterion_id='criterion',
                        candidate_commit='c'*40, candidate_tree='d'*40,
                        candidate_build_image='sha256:' + 'e'*64,
                        command='python -m unittest', patch_sha256=patch_sha,
                        author_receipt='a'*64, review_receipt='c'*64,
                        role_regression={'receipt_sha256': 'd'*64, 'diff_sha256': digest(b'role test patch'),
                                         'baseline_log_sha256': digest(self.args.role_baseline_log.read_bytes()),
                                         'candidate_log_sha256': digest(self.args.role_candidate_log.read_bytes()),
                                         'baseline_exit_code': 1, 'candidate_exit_code': 0},
                        regression_sha256=digest(self.args.oracle.read_bytes()),
                        baseline=dict(exit_code=1, log_sha256=digest(self.args.baseline_log.read_bytes())),
                        candidate=dict(exit_code=0, log_sha256=digest(self.args.candidate_log.read_bytes())))

    def close(self, decision=None, *, no_dispatch=False):
        self.args.execution.write_text(json.dumps(self.run))

        def read_evidence(args, case, task, member, inputs, *, profile):
            self.assertEqual((task, member, profile), ('evidence', 'acl-009', 'lab-v1'))
            output = {'criteria': [{'criterion_id': 'criterion', 'status': 'pass'}],
                      'evidence_refs': [inputs['evidence']['reference']], 'unresolved': []}
            output.update(decision or {})
            return {'status': 'succeeded', 'artifact': {'payload': output}}

        with patch.object(acceptance, 'run_case', return_value=self.reviewed) as inspect, \
                patch.object(acceptance, 'verify_candidate') as verify, \
                patch.object(acceptance, 'stage', side_effect=read_evidence) as stage:
            verify.return_value = {'candidate_commit': self.run['candidate_commit'],
                                   'candidate_tree': self.run['candidate_tree'],
                                   'candidate_build_image': self.run['candidate_build_image']}
            try:
                return acceptance.close(self.args, self.case)
            finally:
                self.assertEqual(inspect.call_args.args[0].action, 'status')
                if no_dispatch:
                    stage.assert_not_called()

    def test_review_requires_matching_executor_and_evidence_decision(self):
        self.assertEqual(self.close()['state'], 'accepted')
        with patch.object(self.args, 'candidate_source', None):
            self.assertEqual(self.close()['state'], 'needs_review')
        for change in ({'criteria': []}, {'criteria': [{'criterion_id': 'other', 'status': 'pass'}]},
                       {'criteria': [{'criterion_id': 'criterion', 'status': 'unverified'}]},
                       {'evidence_refs': ['different reference']}, {'unresolved': ['missing observation']}):
            with self.subTest(change=change):
                self.assertEqual(self.close(change)['state'], 'needs_review')
        for base, candidate in ((0, 0), (1, 1)):
            with self.subTest(base=base, candidate=candidate):
                self.run['baseline']['exit_code'] = base
                self.run['candidate']['exit_code'] = candidate
                self.assertEqual(self.close(no_dispatch=True)['state'], 'blocked')
        self.run['candidate']['exit_code'] = 0
        self.run['role_regression']['baseline_exit_code'] = False
        self.assertEqual(self.close(no_dispatch=True)['state'], 'blocked')
        self.run['role_regression']['baseline_exit_code'] = 1
        self.args.role_candidate_log.write_bytes(b'Ran 0 tests in 0.000s\n\nOK\n')
        self.run['role_regression']['candidate_log_sha256'] = digest(self.args.role_candidate_log.read_bytes())
        self.assertEqual(self.close(no_dispatch=True)['state'], 'blocked')

    def test_candidate_tree_contains_exactly_both_reviewed_diffs(self):
        repo = self.args.candidate_source
        repo.mkdir()
        def git(*args):
            return subprocess.check_output(['git', '-C', str(repo), '-c', 'user.name=Fixture',
                                            '-c', 'user.email=f@invalid', *args], text=True)
        git('init', '-q')
        (repo/'source.py').write_text('value = 1\n')
        git('add', 'source.py')
        git('commit', '-qm', 'baseline')
        base = git('rev-parse', 'HEAD').strip()
        (repo/'source.py').write_text('value = 2\n')
        product = git('diff', '--', 'source.py')
        git('add', 'source.py')
        (repo/'test_regression.py').write_text('from source import value\nassert value == 2\n')
        git('add', '-N', 'test_regression.py')
        regression = git('diff', '--', 'test_regression.py')
        git('add', 'test_regression.py')
        git('commit', '-qm', 'tested candidate')
        candidate = git('rev-parse', 'HEAD').strip()
        tree = git('rev-parse', 'HEAD^{tree}').strip()
        reviewed = {'author': {'payload': {'base_sha': base, 'diff': product}},
                    'regression': {'payload': {'base_sha': base, 'diff': regression}}}
        run = {'base_sha': base, 'candidate_commit': candidate, 'candidate_tree': tree,
               'candidate_build_image': 'sha256:' + 'a'*64}
        acceptance.verify_candidate(repo, reviewed, run)
        with self.assertRaises(ValueError):
            acceptance.verify_candidate(repo, reviewed, {**run, 'candidate_tree': base})
        for change in ('missing', 'extra'):
            git('checkout', '-q', '--detach', base)
            (repo/'source.py').write_text('value = 2\n')
            if change == 'extra':
                (repo/'test_regression.py').write_text('from source import value\nassert value == 2\n')
                (repo/'unrelated.py').write_text('print("extra")\n')
            git('add', '.')
            git('commit', '-qm', change)
            wrong = {**run, 'candidate_commit': git('rev-parse', 'HEAD').strip(),
                     'candidate_tree': git('rev-parse', 'HEAD^{tree}').strip()}
            with self.subTest(change=change), self.assertRaises(ValueError):
                acceptance.verify_candidate(repo, reviewed, wrong)

    def test_rejects_mismatched_patch_target_or_files_before_dispatch(self):
        original = deepcopy(self.run)
        for field in ('base_sha', 'case_id', 'criterion_id', 'patch_sha256', 'regression_sha256',
                      'author_receipt', 'review_receipt'):
            with self.subTest(field=field):
                self.run = {**original, field: 'different'}
                with self.assertRaises(ValueError):
                    self.close(no_dispatch=True)
        self.run = original
        for field in ('receipt_sha256', 'diff_sha256'):
            with self.subTest(field=field):
                self.run = {**original, 'role_regression': {**original['role_regression'], field: 'other'}}
                with self.assertRaises(ValueError):
                    self.close(no_dispatch=True)
        self.run = original
        for log in (self.args.baseline_log, self.args.candidate_log,
                    self.args.role_baseline_log, self.args.role_candidate_log):
            with self.subTest(log=log):
                raw = log.read_bytes()
                log.write_bytes(raw + b'changed')
                with self.assertRaises(ValueError):
                    self.close(no_dispatch=True)
                log.write_bytes(raw)
        self.reviewed.pop('reviewer')
        self.assertEqual(self.close(no_dispatch=True)['state'], 'blocked')
