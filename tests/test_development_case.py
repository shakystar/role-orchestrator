import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from role_orchestrator.development_case import validate_case
from role_orchestrator.role_contracts import digest


def fixture():
    base = '0' * 40
    patch = 'synthetic test patch\n'
    return {
        'case_id': 'read-only-status', 'criterion': {'id': 'read-only-status'},
        'author': {'goal': 'Propose a fix.', 'base_sha': base, 'source': {}, 'regression': {},
                   'acceptance': {}, 'output_contract': {}},
        'fixtures': {
            'author': {'base_sha': base, 'diff': patch, 'tests': [], 'unverified': []},
            'reviewer': {'verdict': 'accept', 'criterion_id': 'read-only-status',
                         'patch_sha256': digest(patch.encode()), 'reason': 'Synthetic verification.'},
        },
    }


class DevelopmentCaseTests(unittest.TestCase):
    def test_live_profile_rejects_fixture_answers(self):
        with self.assertRaisesRegex(ValueError, 'fixture'):
            validate_case(fixture(), 'codex')

    @unittest.skipUnless(os.environ.get('ROLE_RUNTIME_BIN'), 'native fixture bridge required')
    def test_native_handoff_replay_and_rejection(self):
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
                return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()
            git('init', '-q')
            git('add', 'docs')
            git('-c', 'user.email=fixture@invalid', '-c', 'user.name=Fixture', 'commit', '-qm', 'fixture')
            revision = git('rev-parse', 'HEAD')
            case = fixture()

            def run(action, name='case'):
                proc = subprocess.run(
                    [sys.executable, '-m', 'role_orchestrator.development_case', action, '--state', str(root/name),
                     '--runtime', os.environ['ROLE_RUNTIME_BIN'], '--lab', str(root), '--revision', revision,
                     '--backend', 'fixture'], input=json.dumps(case), capture_output=True, text=True, timeout=40)
                return proc.returncode, json.loads(proc.stdout or proc.stderr)

            self.assertEqual(run('status')[1]['state'], 'not_started')
            self.assertFalse((root/'case').exists())
            code, first = run('run')
            self.assertEqual((code, first['state']), (0, 'accept'), first)
            self.assertNotEqual(first['author']['member_id'], first['reviewer']['member_id'])
            self.assertNotEqual(first['author']['run_id'], first['reviewer']['run_id'])
            def receipts():
                return {p: p.read_bytes() for p in (root/'case').glob('*/runtime/tasks/*.json')}
            before = receipts()
            self.assertEqual(len(before), 2)
            self.assertEqual(run('run'), (0, first))
            self.assertEqual(run('status'), (0, first))
            self.assertEqual(receipts(), before)
            case['fixtures']['reviewer']['patch_sha256'] = '1' * 64
            self.assertEqual(run('run')[0], 2)
            code, rejected = run('run', 'bad-review')
            self.assertEqual((code, rejected['state'], rejected['stage']), (1, 'failed', 'reviewer'))
            case['fixtures']['author']['diff'] = ''
            code, rejected = run('run', 'bad-author')
            self.assertEqual((code, rejected['state'], rejected['stage']), (1, 'failed', 'author'))
            self.assertFalse((root/'bad-author/reviewer').exists())
            roster = root/'docs/team/roster.md'
            roster.write_text(roster.read_text().replace('oracle-reviewer-v2', 'python-resume-author'))
            git('add', 'docs')
            git('-c', 'user.email=fixture@invalid', '-c', 'user.name=Fixture', 'commit', '-qm', 'reassign')
            revision = git('rev-parse', 'HEAD')
            self.assertEqual(run('run', 'wrong-roster')[0], 2)
            self.assertFalse((root/'wrong-roster').exists())
