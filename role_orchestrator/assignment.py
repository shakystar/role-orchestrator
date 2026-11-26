import argparse
from contextlib import redirect_stdout
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from .contracts import IDENTIFIER as ID, attempt_id, strict_json
from .attempts import AttemptReader, ConfirmedAssignmentMismatch, terminal_failure
from .role_contracts import canonical, digest, output_payload, validate_inputs
from .planning_catalog import PROFILE, bound_plan
from .roster import AssignmentError, make_plan
from .runtime import Runtime, load_identity

VERSION = 2
PLAN = 'assignment-v2.plan.json'
VERDICT = 'assignment-v2.verdict.json'


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def decode(data):
    value = strict_json(data)
    if not isinstance(value, dict):
        raise ValueError('expected JSON object')
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


def contract_inputs(payload, plan):
    value = decode(payload)
    if plan['contract'].get('profile') == PROFILE and 'fixture' in value:
        if plan['backend'] != 'fixture' or not isinstance(value['fixture'], dict):
            raise ValueError('invalid_planning_fixture')
        del value['fixture']  # Transport metadata; the native input and its hash remain intact.
    return value


def observe(binary, state, ids, plan, request=None):
    if request is None:
        if not plan.get('input_sha256'):
            return dict(status='uncertain', reason='historical_input_unavailable')
        request = dict(id=plan['task_id'], role=plan['member_id'], instruction=plan['instruction'])
    try:
        proof = AttemptReader(binary, state, ids['run_id']).bound(
            ids['attempt_id'], request, plan.get('input_sha256'))
        if proof is None:
            return dict(status='uncertain', reason='attempt_receipt_absent')
        receipt, receipt_hash = proof
        failure = terminal_failure(receipt)
    except ConfirmedAssignmentMismatch:
        return dict(status='failed', reason='assignment_mismatch')
    except (ValueError, TypeError, KeyError, OSError, AttributeError, RecursionError,
            subprocess.SubprocessError):
        return dict(status='uncertain', reason='attempt_evidence_unavailable_or_invalid')
    if failure:
        return dict(status='failed', reason=failure, receipt_sha256=receipt_hash)
    if receipt.get('status') != 'succeeded':
        return dict(status='uncertain', reason='attempt_not_terminal')
    result = receipt.get('result')
    actor = dict(actual_member_id=plan['member_id'], actual_role_version=plan['expected_role_version'])
    for key, expected in actor.items():
        actual = result.get(key) if isinstance(result, dict) else None
        if actual is None or actual == '':
            return dict(status='failed', reason=key + '_missing')
        if actual != expected:
            return dict(status='failed', reason=key + '_mismatch')
    verdict = dict(status='succeeded', receipt_sha256=receipt_hash, **actor)
    contract = plan.get('contract')
    if contract is not None:
        try:
            payload = output_payload(result.get('text'), contract, contract_inputs(receipt['task']['input'], plan))
        except (ValueError, TypeError, KeyError, RecursionError):
            return dict(status='failed', reason='invalid_role_output', receipt_sha256=receipt_hash)
        verdict['artifact'] = dict(kind=contract['kind'], payload=payload, sha256=digest(canonical(payload)),
            output_sha256=digest(result['text'].encode()),
            contract_sha256=contract['sha256'], input_sha256=digest(receipt['task']['input'].encode()),
            receipt_sha256=receipt_hash, member_id=plan['member_id'], task_id=plan['task_id'], **ids)
    turn_id = result.get('turn_id')
    if turn_id is not None:
        output_sha256 = None
        text = result.get('text')
        if isinstance(text, str):
            try:
                output_sha256 = digest(text.encode())
            except UnicodeError:
                pass  # Optional evidence must not change the native verdict.
        turn = dict(session=result.get('session'), turn_id=turn_id, completed=True,
                    assignment_id=plan['task_id'], role_version=verdict['actual_role_version'],
                    output_sha256=output_sha256, usage=result.get('turn_usage'),
                    usage_scope='final_matched_tokenUsage.last')
        verdict['turn_evidence'] = dict(assignment_id=plan['task_id'],
                                       output_sha256=output_sha256, turn=turn)
    return verdict


def assess(binary, state, ids, plan, request):
    try:
        with open(os.devnull, 'w', encoding='utf-8') as sink, redirect_stdout(sink):
            runtime = Runtime(binary, state, plan['backend'], 'codex', model=plan['desired']['model'], reasoning_effort=plan['desired']['effort'])
            try:
                reply = runtime.execute(request)
            finally:
                runtime.close()
        if not matches(reply, status='succeeded'):
            raise ValueError('unconfirmed runtime response')
    except Exception:
        pass  # Transport or teardown can fail after the runtime has persisted the outcome.
    return observe(binary, state, ids, plan, request)


def run(args):
    payload = read_input()
    if args.profile == PROFILE:
        context = decode(payload).get('context')
        if not isinstance(context, dict):
            raise AssignmentError('planning_context_required')
        plan = bound_plan(args, args.revision, VERSION, context=context)
    else:
        plan = make_plan(args, VERSION)
    if plan['contract'] is not None:
        try:
            validate_inputs(contract_inputs(payload, plan), plan['contract'])
        except ValueError as exc:
            raise AssignmentError(str(exc)) from None
    state, binary = args.state.absolute(), str(args.runtime.resolve())
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if args.profile == PROFILE:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise AssignmentError('private_state_directory_required')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AssignmentError('state_in_use') from None
        if any(state.iterdir()):
            raise AssignmentError('state_not_empty_use_status_or_a_new_state')
        run_id = load_identity(state)['run_id']
        ids = dict(run_id=run_id, attempt_id=attempt_id(run_id, args.task))
        plan.update(ids)
        plan['input_sha256'] = hashlib.sha256(payload.encode('utf-8')).hexdigest()
        publish(state / PLAN, plan, fd)
        request = dict(id=args.task, role=args.member, instruction=plan['instruction'], input=payload)
        verdict = dict(version=VERSION, **ids, **assess(binary, state, ids, plan, request),
                       requested_model=plan['desired']['model'], requested_effort=plan['desired']['effort'],
                       observed_model=None, observed_effort=None)
        publish(state / VERDICT, verdict, fd)
        print(encode(verdict))
        return 0 if verdict['status'] == 'succeeded' else 1
    finally:
        os.close(fd)


def status(state, binary=None, *, verify_native_receipt=False):
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
    # Older plans do not pin the input. Keep their original incomplete state
    # visible rather than attributing an unverifiable receipt to them.
    recheck_success = verify_native_receipt and matches(verdict, status='succeeded')
    if (recheck_success or (binary is not None and 'input_sha256' in plan
                            and (verdict is None or verdict.get('status') == 'uncertain'))):
        ids = {key: plan[key] for key in ('run_id', 'attempt_id')}
        result = (observe(str(binary.resolve()), state, ids, plan) if binary is not None
                  else dict(status='uncertain', reason='runtime_unavailable'))
        if recheck_success and result['status'] == 'succeeded' and not matches(verdict, **result):
            result = dict(status='uncertain', reason='cached_verdict_does_not_match_native_receipt')
        verdict = dict(version=VERSION, **ids, **result,
                       requested_model=plan['desired']['model'], requested_effort=plan['desired']['effort'],
                       observed_model=None, observed_effort=None)
    print(encode(dict(plan=plan, verdict=verdict)))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    reading = argv[:1] == ['status']
    parser = argparse.ArgumentParser(prog='python -m role_orchestrator.assignment' + (' status' if reading else ''))
    parser.add_argument('--state', required=True, type=Path)
    if reading:
        parser.add_argument('--runtime', type=Path, help='recheck incomplete evidence without submitting work')
        parser.add_argument('--verify-native-receipt', action='store_true', help='recheck cached success against native evidence')
    else:
        parser.epilog = 'Read-only lookup: python -m role_orchestrator.assignment status --state PATH'
        for flag in ('lab', 'runtime'):
            parser.add_argument('--' + flag, required=True, type=Path)
        for flag in ('revision', 'member', 'task'):
            parser.add_argument('--' + flag, required=True)
        parser.add_argument('--backend', required=True, choices=('fixture', 'codex'))
        parser.add_argument('--profile', default='lab-v1', choices=('lab-v1', 'development-patch-v1', 'review-v1', PROFILE))
    args = parser.parse_args(argv[1:] if reading else argv)
    try:
        if reading:
            return status(args.state, args.runtime, verify_native_receipt=args.verify_native_receipt)
        return run(args)
    except AssignmentError as exc:
        print(encode({'error': str(exc)}), file=sys.stderr)
    except (Exception, KeyboardInterrupt):
        print(encode({'error': 'assignment_incomplete_check_status_do_not_resubmit'}), file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())

