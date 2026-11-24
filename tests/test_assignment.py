"""Synthetic public-CLI integration tests; run with ROLE_RUNTIME_BIN set.

The supplied CLI contract does not name the wire-level verdict statuses. Tests
compare them with the matched-actor control rather than guessing literal labels.
All assignment calls use the fixture backend and a local Git repository.
"""

import base64
import binascii
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


MEMBER = 'acl-004'
OTHER_MEMBER = 'acl-005'
ROSTER = 'docs/team/roster.md'
HANDOFFS = 'docs/features/development-handoffs.md'
ROSTER_TEXT = (
    '| ID | Name | Role | Assignment |\n'
    '| --- | --- | --- | --- |\n'
    '| acl-004 | Kim | QA | fixture-author-v2 |\n'
)
HANDOFF_TEXT = (
    '| Handoff | Scenario | Vectors | Model |\n'
    '| --- | --- | --- | --- |\n'
    '| fixture-author-v2 / cases | scenario | vectors | Sol / medium |\n'
)
CLI = [sys.executable, '-m', 'role_orchestrator.assignment']


def receipt_output(stdout, mode):
    """Find one base64 JSON receipt without assuming an envelope field name."""
    matches = []

    def visit(value):
        if isinstance(value, dict):
            return {key: visit(item) for key, item in value.items()}
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, str):
            return value
        try:
            native = base64.b64decode(value, validate=True)
            receipt = json.loads(native)
        except (ValueError, UnicodeError, binascii.Error):
            return value
        if not isinstance(receipt, dict):
            return value
        result = receipt.get('result')
        if not isinstance(result, dict) or 'actual_member_id' not in result:
            return value
        if mode == 'wrong':
            result['actual_member_id'] = OTHER_MEMBER
        elif mode == 'missing':
            del result['actual_member_id']
        delivered = native if mode == 'valid' else json.dumps(receipt).encode()
        matches.append((native, delivered))
        return base64.b64encode(delivered).decode('ascii')

    try:
        envelope = json.loads(stdout)
    except (ValueError, UnicodeError):
        token = stdout.strip()
        patched = visit(token.decode('ascii'))
        output = stdout.replace(token, patched.encode('ascii'), 1)
    else:
        patched = visit(envelope)
        output = json.dumps(patched).encode() + b'\n'
    if len(matches) != 1:
        raise ValueError(f'expected one inspect receipt with an actual member ID; found {len(matches)}')
    return (stdout if mode == 'valid' else output), *matches[0]


def bridge_main():
    args = sys.argv[1:]
    real = os.environ['ASSIGN_TEST_REAL_BRIDGE']
    trace = Path(os.environ['ASSIGN_TEST_TRACE'])
    inspect = any(arg == '--inspect-attempt' or arg.startswith('--inspect-attempt=') for arg in args)
    event = {'args': args, 'cwd': os.getcwd(), 'inspect': inspect}

    def log(item):
        with trace.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(item) + '\n')

    log({'call': event})
    try:
        result = subprocess.run(
            [real, *args], stdout=subprocess.PIPE if inspect else None,
            timeout=15, check=False,
        )
        if not inspect:
            return result.returncode
        if result.returncode:
            sys.stdout.buffer.write(result.stdout)
            return result.returncode
        output, native, delivered = receipt_output(result.stdout, os.environ['ASSIGN_TEST_MODE'])
        log({'receipt': event, 'native': base64.b64encode(native).decode('ascii'),
             'delivered': base64.b64encode(delivered).decode('ascii')})
        sys.stdout.buffer.write(output)
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f'fixture bridge wrapper: {exc}', file=sys.stderr)
        return 1


class AssignmentCLI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configured = os.environ.get('ROLE_RUNTIME_BIN')
        if not configured:
            raise unittest.SkipTest('set ROLE_RUNTIME_BIN to the local, executable role-runtime bridge')
        cls.bridge = Path(configured).expanduser().resolve()
        if not cls.bridge.is_file() or not os.access(cls.bridge, os.X_OK):
            raise AssertionError(f'ROLE_RUNTIME_BIN is not an executable file: {cls.bridge}')

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='assignment-cli-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.lab = self.root / 'lab'
        self.lab.mkdir()
        self.env = os.environ.copy()
        self.env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_ALLOW_PROTOCOL='file', GIT_TERMINAL_PROMPT='0')
        self.git('init', '--quiet', '--template=')
        self.write_doc(ROSTER, ROSTER_TEXT)
        self.write_doc(HANDOFFS, HANDOFF_TEXT)
        self.revision = self.commit('synthetic roster v2 and supported handoff')
        self.wrapper = self.root / 'bridge-wrapper'
        module = str(Path(__file__).resolve())
        self.wrapper.write_text(
            '#!/usr/bin/env python3\n'
            'import runpy\n'
            f'raise SystemExit(runpy.run_path({module!r})["bridge_main"]())\n',
            encoding='utf-8',
        )
        self.wrapper.chmod(0o700)
        self.runs = 0

    def run_command(self, args, *, expected_exit, env=None, cwd=None, data=b''):
        args = [str(arg) for arg in args]
        try:
            result = subprocess.run(args, cwd=cwd, env=self.env if env is None else env,
                                    input=data, capture_output=True, timeout=30, check=False)
        except subprocess.TimeoutExpired as exc:
            self.fail(f'{args!r} timed out; stdout={exc.stdout!r}; stderr={exc.stderr!r}')
        detail = (f'{args!r} exited {result.returncode}\n'
                  f'stdout:\n{result.stdout.decode("utf-8", errors="replace")}\n'
                  f'stderr:\n{result.stderr.decode("utf-8", errors="replace")}')
        self.assertEqual(result.returncode, expected_exit, detail)
        return result, detail

    def git(self, *args):
        result, _ = self.run_command(['git', *args], expected_exit=0, cwd=self.lab)
        return result.stdout.decode().strip()

    def write_doc(self, relative, data):
        target = self.lab / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data, encoding='utf-8')

    def commit(self, message):
        self.git('add', '--', ROSTER, HANDOFFS)
        self.git('-c', 'user.name=Synthetic Fixture', '-c', 'user.email=fixture@example.invalid',
                 '-c', 'commit.gpgsign=false', 'commit', '--quiet', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def read_json_object(self, data, label, detail):
        try:
            report = json.loads(data)
        except (ValueError, UnicodeError) as exc:
            self.fail(f'invalid {label} JSON: {exc}\n{detail}')
        self.assertIsInstance(report, dict, detail)
        return report

    def status(self, state, env, prior=''):
        result, detail = self.run_command([*CLI, 'status', '--state', state],
                                          expected_exit=0, env=env)
        detail = '\n'.join(filter(None, (prior, detail)))
        report = self.read_json_object(result.stdout, 'status stdout', detail)
        self.assertIn('plan', report, detail)
        verdict = report.get('verdict')
        self.assertIsInstance(verdict, dict, detail)
        self.assertIsInstance(verdict.get('status'), str, detail)
        self.assertTrue(verdict['status'].strip(), detail)
        return report, detail

    def assign(self, *, expected_exit, revision=None, member=MEMBER, mode='valid',
               pre_execution=False):
        self.runs += 1
        state = self.root / f'state-{self.runs}'
        trace = self.root / f'bridge-{self.runs}.jsonl'
        env = dict(self.env, ASSIGN_TEST_REAL_BRIDGE=str(self.bridge),
                   ASSIGN_TEST_TRACE=str(trace), ASSIGN_TEST_MODE=mode)
        result, detail = self.run_command(
            [*CLI, '--lab', self.lab, '--revision', revision or self.revision,
             '--member', member, '--task', 'T', '--state', state,
             '--runtime', self.wrapper, '--backend', 'fixture'],
            expected_exit=expected_exit, env=env, data=json.dumps({
                'scenario': {}, 'identity': {}, 'windows': {},
                'fixture': {'criterion_id': 'duplicate', 'initial_state': {}, 'actions': ['approve'],
                            'expected_state': 'completed', 'expected_new_dispatches': 1},
            }).encode(),
        )
        if pre_execution:
            report = self.read_json_object(result.stderr, 'assignment stderr', detail)
        else:
            persisted, detail = self.status(state, env, detail)
            if expected_exit == 0:
                self.assertEqual(persisted['verdict']['status'], 'succeeded', detail)
                report = persisted
            else:
                verdict = self.read_json_object(result.stdout, 'assignment stdout', detail)
                self.assertEqual(verdict, persisted['verdict'], detail)
                report = persisted
        events = ([self.read_json_object(line, 'bridge trace entry', detail)
                   for line in trace.read_text(encoding='utf-8').splitlines()]
                  if trace.exists() else [])
        return report, state, env, events, detail

    def check_receipt(self, run, mode):
        _, _, env, events, detail = run
        receipts = [entry for entry in events if 'receipt' in entry]
        self.assertTrue(receipts, f'no native inspect receipt; bridge events={events!r}\n{detail}')
        entry = receipts[-1]
        native_bytes = base64.b64decode(entry['native'])
        native = json.loads(native_bytes)
        self.assertEqual(native['result'].get('actual_member_id'), MEMBER, detail)
        expected = copy.deepcopy(native)
        if mode == 'wrong':
            expected['result']['actual_member_id'] = OTHER_MEMBER
        elif mode == 'missing':
            del expected['result']['actual_member_id']
        self.assertEqual(json.loads(base64.b64decode(entry['delivered'])), expected, detail)

        # Read the same attempt directly without submitting another attempt.
        call = entry['receipt']
        result, inspect_detail = self.run_command([self.bridge, *call['args']], expected_exit=0,
                                                  cwd=call['cwd'], env=env)
        try:
            _, persisted, _ = receipt_output(result.stdout, 'valid')
        except (ValueError, UnicodeError) as exc:
            self.fail(f'cannot read the original native receipt: {exc}\n{detail}\n{inspect_detail}')
        self.assertEqual(persisted, native_bytes, f'{detail}\n{inspect_detail}')

    def check_different_verdict(self, control, rejected, reason):
        detail = f'matched control:\n{control[4]}\nrejected case:\n{rejected[4]}'
        self.assertEqual(control[0]['verdict']['status'], 'succeeded', detail)
        verdict = rejected[0].get('verdict')
        self.assertIsInstance(verdict, dict, detail)
        self.assertEqual(verdict.get('status'), 'failed', detail)
        self.assertEqual(verdict.get('reason'), reason, detail)

    def check_pre_execution_rejection(self, control, rejected, reason):
        report, state, _, events, detail = rejected
        self.assertEqual(control[0]['verdict']['status'], 'succeeded', control[4])
        self.assertEqual(report.get('error'), reason, detail)
        self.assertFalse(state.exists() or state.is_symlink(), f'unexpected state at {state}\n{detail}')
        self.assertEqual(events, [], f'unexpected bridge call or receipt\n{detail}')

    def test_valid(self):
        self.check_receipt(self.assign(expected_exit=0), 'valid')

    def test_wrong_actor(self):
        valid = self.assign(expected_exit=0)
        self.check_receipt(valid, 'valid')
        wrong = self.assign(expected_exit=1, mode='wrong')
        self.check_receipt(wrong, 'wrong')
        self.check_different_verdict(valid, wrong, 'actual_member_id_mismatch')

    def test_missing_actor(self):
        valid = self.assign(expected_exit=0)
        self.check_receipt(valid, 'valid')
        missing = self.assign(expected_exit=1, mode='missing')
        self.check_receipt(missing, 'missing')
        self.check_different_verdict(valid, missing, 'actual_member_id_missing')

    def test_old_role_rejected(self):
        valid = self.assign(expected_exit=0)
        self.check_receipt(valid, 'valid')
        self.write_doc(ROSTER, ROSTER_TEXT.replace('fixture-author-v2', 'fixture-author-v1'))
        stale = self.commit('roster refers to an old role with no matching handoff')
        rejected = self.assign(expected_exit=1, revision=stale, pre_execution=True)
        self.check_pre_execution_rejection(valid, rejected, 'invalid_or_ambiguous_handoff_row')

    def test_missing_member(self):
        valid = self.assign(expected_exit=0)
        self.check_receipt(valid, 'valid')
        rejected = self.assign(expected_exit=1, member=OTHER_MEMBER, pre_execution=True)
        self.check_pre_execution_rejection(valid, rejected, 'invalid_or_ambiguous_roster_row')

    def test_historical_status_after_head_changes(self):
        run = self.assign(expected_exit=0)
        before, state, env, _, detail = run
        self.check_receipt(run, 'valid')
        self.write_doc(ROSTER, ROSTER_TEXT.replace('fixture-author-v2', 'fixture-author-v1'))
        changed = self.commit('change the member assignment at HEAD')
        self.assertNotEqual(changed, self.revision)
        self.assertEqual(self.git('rev-parse', 'HEAD'), changed)
        after, detail = self.status(state, env, detail)
        self.assertEqual(after['plan'], before['plan'], detail)
        self.assertEqual(after['verdict'], before['verdict'], detail)
        self.check_receipt(run, 'valid')

    def test_unsupported_model(self):
        valid = self.assign(expected_exit=0)
        self.check_receipt(valid, 'valid')
        self.write_doc(HANDOFFS, HANDOFF_TEXT.replace('Sol / medium', 'unsupported-fixture-model / medium'))
        unsupported = self.commit('request an unsupported synthetic model')
        rejected = self.assign(expected_exit=1, revision=unsupported, pre_execution=True)
        self.check_pre_execution_rejection(valid, rejected, 'unsupported_desired_model_or_effort')


if __name__ == '__main__':
    unittest.main()

