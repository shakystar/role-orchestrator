import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


class DevelopmentAuthorPreflightTests(unittest.TestCase):
    def test_criteria_author_conflict_stops_before_submit(self):
        bridge = os.environ.get('ROLE_RUNTIME_BIN')
        self.assertTrue(bridge, 'set ROLE_RUNTIME_BIN to the real native fixture bridge')
        runtime = Path(bridge).resolve()
        self.assertTrue(os.access(runtime, os.X_OK), runtime)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'docs/team').mkdir(parents=True)
            (root / 'docs/features').mkdir()
            (root / 'docs/team/roster.md').write_text(
                '| acl-007 | Author | Patches | python-resume-author |\n'
                '| acl-005 | Reviewer | Reviews | oracle-reviewer-v2 |\n')
            (root / 'docs/features/development-handoffs.md').write_text(
                '| python-resume-author / Author | Source | Patch | Sol / high |\n'
                '| oracle-reviewer-v2 / Review | Patch | Verdict | Astra / high |\n')

            def git(*args):
                return subprocess.check_output(['git', '-C', str(root), *args],
                                               text=True).strip()

            git('init', '-q')
            git('add', 'docs')
            git('-c', 'user.email=fixture@invalid', '-c', 'user.name=Fixture', 'commit', '-qm', 'fixture')
            revision = git('rev-parse', 'HEAD')

            # Log submit requests from stdin before forwarding each frame unchanged.
            # This counts requests independently of the bridge's receipts.
            probe = root / 'bridge-probe'
            probe.write_text(
                f'#!{sys.executable}\n'
                'import json, os, subprocess, sys\n'
                'bridge = os.environ["PREFLIGHT_BRIDGE"]\n'
                'child = subprocess.Popen([bridge, *sys.argv[1:]], stdin=subprocess.PIPE)\n'
                'for line in sys.stdin.buffer:\n'
                '    request = json.loads(line)\n'
                '    if "instruction" in request:\n'
                '        with open(os.environ["PREFLIGHT_TRACE"], "a") as trace:\n'
                '            print(json.dumps([request.get("id"), request.get("role")]), file=trace)\n'
                '    child.stdin.write(line)\n'
                '    child.stdin.flush()\n'
                'child.stdin.close()\n'
                'sys.exit(child.wait())\n')
            probe.chmod(0o700)

            def run(name, authors, diff):
                criterion = {'id': 'v020-parent-review'}
                review = {'team': 'independent-review', 'revision': 1, 'kind': 'criteria',
                          'evidence': {'criteria': criterion}}
                if authors is not None:
                    review['authors'] = authors
                case = {'case_id': name, 'criterion': criterion, 'review': review,
                        'author': {'goal': 'Propose a fix.', 'base_sha': revision, 'source': {},
                                   'regression': {}, 'acceptance': {}, 'output_contract': {}},
                        'fixtures': {'author': {'base_sha': revision, 'diff': diff, 'tests': [],
                                                'unverified': []}, 'reviewer': {}}}
                trace = root / (name + '.submits')
                trace.touch()
                env = dict(os.environ, PREFLIGHT_TRACE=str(trace), PREFLIGHT_BRIDGE=str(runtime))
                proc = subprocess.run(
                    [sys.executable, '-m', 'role_orchestrator.development_case', 'run',
                     '--state', str(root / name), '--runtime', str(probe), '--lab', str(root),
                     '--revision', revision, '--backend', 'fixture'],
                    input=json.dumps(case), capture_output=True, text=True, timeout=40, env=env)
                events = [json.loads(line) for line in trace.read_text().splitlines()]
                return proc.returncode, json.loads(proc.stdout or proc.stderr), events

            # Calibrate the probe and test the preflight controls. The intentionally
            # invalid author output stops both controls at a known post-submit point.
            for name, authors in (('agreed-author', ['acl-007']), ('unspecified-author', None)):
                with self.subTest(control=name):
                    code, result, events = run(name, authors, '')
                    self.assertEqual((code, result.get('state'), result.get('stage')),
                                     (1, 'failed', 'author'), result)
                    self.assertEqual(len(events), 1, events)

            code, result, events = run('conflicting-author', ['acl-010'], 'synthetic test patch\n')
            self.assertEqual(events, [], (result, events))
            self.assertIn(code, (1, 2), result)
            diagnostic = json.dumps(result, ensure_ascii=False)
            self.assertIn('acl-010', diagnostic)
            self.assertRegex(diagnostic, r'(?i)conflict|overlap|mismatch|충돌|불일치')
            self.assertRegex(diagnostic, r'(?i)\bauthors?\b|작성자')
            self.assertRegex(diagnostic, r'(?i)\b(?:owner|requester|caller|submitter)\b|책임자|소유자')
