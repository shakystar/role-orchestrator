import argparse
import base64
from contextlib import redirect_stdout
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from .contracts import attempt_id
from .runtime import Runtime, load_identity

VERSION = 2
PLAN = 'assignment-v2.plan.json'
VERDICT = 'assignment-v2.verdict.json'
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}')
MODELS = {'Sol': 'gpt-6.2-sol', 'Astra': 'gpt-6.2-astra'}
EFFORTS = {'low', 'medium', 'high', 'xhigh', 'max', 'ultra'}


class AssignmentError(Exception):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError('duplicate key')
    return result


def decode(data):
    value = json.loads(data, object_pairs_hook=unique_object)
    if not isinstance(value, dict):
        raise ValueError('expected JSON object')
    encode(value)  # Reject non-finite numbers, including overflowing exponents.
    return value


def read_input():
    limit = 1024 * 1024
    raw = sys.stdin.buffer.read(limit + 1)
    if len(raw) > limit:
        raise AssignmentError('stdin_json_too_large')
    try:
        serialized = encode(decode(raw))
        size = len(serialized.encode('utf-8'))
    except (ValueError, RecursionError):
        raise AssignmentError('invalid_stdin_json_object') from None
    if size > 64 * 1024:
        raise AssignmentError('serialized_input_exceeds_64_kib')
    return serialized


def literal(cell):
    return cell[1:-1].strip() if len(cell) >= 2 and cell[0] == cell[-1] == '`' else cell


def select_row(markdown, key, code, handoff=False):
    found = []
    fence = ''
    for line in map(str.strip, markdown.splitlines()):
        mark = re.match(r'`{3,}|~{3,}', line)
        if fence:
            if mark and mark[0].startswith(fence) and not line[mark.end():].strip():
                fence = ''
            continue
        if mark:
            fence = mark[0]
        elif line.startswith('|'):
            cells = re.split(r'(?<!\\)\|', line)[1:]
            label = cells[0].partition('/')[0] if handoff else cells[0]
            if literal(label.strip()) == key:
                found.append([cell.strip().replace(r'\|', '|') for cell in cells[:-1]] if cells[-1] == '' else [])
    if len(found) != 1 or len(found[0]) != 4 or not all(found[0]):
        raise AssignmentError(code)
    return found[0]


def git(lab, *args):
    try:
        return subprocess.run(
            ['git', '--no-pager', '--no-replace-objects', '-C', str(lab), *args],
            check=True, capture_output=True, timeout=15,
            env={**os.environ, 'GIT_NO_LAZY_FETCH': '1', 'GIT_TERMINAL_PROMPT': '0'},
        ).stdout.decode('utf-8')
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise AssignmentError('lab_revision_read_failed') from None


def make_plan(args):
    revision = args.revision.lower()
    if (not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', revision)
            or git(args.lab, 'rev-parse', '--verify', revision + '^{commit}').strip() != revision):
        raise AssignmentError('full_commit_sha_required')
    if not ID.fullmatch(args.member) or not ID.fullmatch(args.task):
        raise AssignmentError('invalid_member_or_task_id')
    roster = git(args.lab, 'show', revision + ':docs/team/roster.md')
    _, name, duty, handoff = select_row(roster, args.member, 'invalid_or_ambiguous_roster_row')
    handoff = literal(handoff)
    if not ID.fullmatch(handoff):
        raise AssignmentError('invalid_handoff_id')
    specs = git(args.lab, 'show', revision + ':docs/features/development-handoffs.md')
    label, inputs, outputs, desired = select_row(specs, handoff, 'invalid_or_ambiguous_handoff_row', handoff=True)
    title = label.partition('/')[2].strip()
    if not title:
        raise AssignmentError('invalid_handoff_title')
    model, sep, effort = map(str.strip, desired.partition('/'))
    if not sep or model not in MODELS or effort not in EFFORTS:
        raise AssignmentError('unsupported_desired_model_or_effort')
    return dict(version=VERSION, revision=revision, backend='fixture', member_id=args.member,
                task_id=args.task, handoff_id=handoff, expected_role_version='v1',
                instruction=f'{title}\n담당: {name} — {duty}\n입력: {inputs}\n산출물: {outputs}',
                desired={'model': MODELS[model], 'effort': effort})


def publish(path, value, directory_fd):
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent) as tmp:
        tmp.write(encode(value) + '\n')
        tmp.flush()
        os.fsync(tmp.fileno())
        os.link(tmp.name, path)  # Atomic, and never replaces an existing record.
    os.fsync(directory_fd)


def matches(obj, **expected):
    return isinstance(obj, dict) and all(
        type(obj.get(key)) is type(value) and obj[key] == value for key, value in expected.items()
    )


def verify(binary, state, ids, plan, request):
    reply = subprocess.run(
        [binary, '--state', str(state / 'runtime'), '--run-id', ids['run_id'],
         '--inspect-attempt', ids['attempt_id']], check=True, capture_output=True, timeout=15,
    )
    evidence = decode(reply.stdout)
    if not matches(evidence, version=1, status='observed', **ids):
        raise AssignmentError('inspect_envelope_mismatch')
    wire = decode(base64.b64decode(evidence['wire'], validate=True))
    receipt = decode(base64.b64decode(evidence['receipt'], validate=True))
    if not matches(wire, schema_version=1, **ids, **request):
        raise AssignmentError('wire_request_mismatch')
    task = {key: request[key] for key in ('id', 'role', 'input')}
    if receipt.get('status') != 'succeeded' or not matches(receipt.get('task'), **task):
        raise AssignmentError('receipt_task_or_status_mismatch')
    result = receipt.get('result')
    actor = dict(actual_member_id=plan['member_id'], actual_role_version=plan['expected_role_version'])
    for key, expected in actor.items():
        actual = result.get(key) if isinstance(result, dict) else None
        if actual is None or actual == '':
            raise AssignmentError(key + '_missing')
        if actual != expected:
            raise AssignmentError(key + '_mismatch')
    return actor


def assess(binary, state, ids, plan, request):
    try:
        with open(os.devnull, 'w', encoding='utf-8') as sink, redirect_stdout(sink):
            runtime = Runtime(binary, state, 'fixture', 'codex')
            try:
                reply = runtime.execute(request)
            finally:
                runtime.close()
        if not matches(reply, status='succeeded'):
            raise ValueError('unconfirmed runtime response')
    except Exception:
        return dict(status='failed', reason='runtime_success_unconfirmed')
    try:
        return dict(status='succeeded', **verify(binary, state, ids, plan, request))
    except AssignmentError as exc:
        return dict(status='failed', reason=str(exc))
    except Exception:
        return dict(status='failed', reason='attempt_evidence_unavailable_or_invalid')


def run(args):
    if args.backend == 'codex':
        raise AssignmentError('live_execution_refused_until_m2_model_effort_support')
    payload, plan = read_input(), make_plan(args)
    state, binary = args.state.absolute(), str(args.runtime.resolve())
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AssignmentError('state_in_use') from None
        if any(state.iterdir()):
            raise AssignmentError('state_not_empty_use_status_or_a_new_state')
        run_id = load_identity(state)['run_id']
        ids = dict(run_id=run_id, attempt_id=attempt_id(run_id, args.task))
        plan.update(ids)
        publish(state / PLAN, plan, fd)
        request = dict(id=args.task, role=args.member, instruction=plan['instruction'], input=payload)
        verdict = dict(version=VERSION, **ids, **assess(binary, state, ids, plan, request))
        publish(state / VERDICT, verdict, fd)
        print(encode(verdict))
        return 0 if verdict['status'] == 'succeeded' else 1
    finally:
        os.close(fd)


def status(state):
    try:
        plan = decode((state / PLAN).read_bytes())
    except FileNotFoundError:
        raise AssignmentError('assignment_v2_plan_not_found') from None
    if not matches(plan, version=VERSION):
        raise AssignmentError('unsupported_assignment_state_version')
    try:
        verdict = decode((state / VERDICT).read_bytes())
    except FileNotFoundError:
        verdict = None
    if verdict is not None and not matches(verdict, version=VERSION, run_id=plan['run_id'], attempt_id=plan['attempt_id']):
        raise AssignmentError('verdict_does_not_match_plan')
    print(encode(dict(plan=plan, verdict=verdict)))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    reading = argv[:1] == ['status']
    parser = argparse.ArgumentParser(prog='python -m role_orchestrator.assignment' + (' status' if reading else ''))
    parser.add_argument('--state', required=True, type=Path)
    if not reading:
        parser.epilog = 'Read-only lookup: python -m role_orchestrator.assignment status --state PATH'
        for flag in ('lab', 'runtime'):
            parser.add_argument('--' + flag, required=True, type=Path)
        for flag in ('revision', 'member', 'task'):
            parser.add_argument('--' + flag, required=True)
        parser.add_argument('--backend', required=True, choices=('fixture', 'codex'))
    args = parser.parse_args(argv[1:] if reading else argv)
    try:
        return status(args.state) if reading else run(args)
    except AssignmentError as exc:
        print(encode({'error': str(exc)}), file=sys.stderr)
    except (Exception, KeyboardInterrupt):
        print(encode({'error': 'assignment_incomplete_check_status_do_not_resubmit'}), file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())

