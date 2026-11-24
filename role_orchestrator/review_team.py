'''Replay-safe broker for the independent review team.'''

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

from .role_contracts import canonical


TEAM = 'independent-review'
PROFILE = 'review-v1'
FINAL = 'acl-005'  # Keep the assignment module's existing role label.
SPECIALISTS = {
    'criteria': 'acl-010',
    'counterexample': 'acl-011',
    'reproduction': 'acl-014',
    'provenance': 'acl-015',
}
MEMBER_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*')
FULL_REVISION = re.compile(r'(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})')
SHA256 = re.compile(r'[0-9a-fA-F]{64}')
REQUEST_FILE = 'request.json'


class Invalid(ValueError):
    pass


class Hold(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Invalid(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Invalid(f'duplicate JSON key: {key!r}')
        result[key] = value
    return result


def reject_constant(value):
    raise Invalid(f'invalid JSON constant: {value}')


def finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise Invalid('JSON number is out of range')
    return result


def decode(data):
    return json.loads(data.decode('utf-8'), object_pairs_hook=unique_object,
                      parse_constant=reject_constant, parse_float=finite_float)


def encode(value):
    # Broker records and dispatch stdin append LF; intent hashes do not.
    # Assignment plan and artifact hashes instead use role_contracts.canonical.
    return json.dumps(value, ensure_ascii=True, allow_nan=False,
                      sort_keys=True, separators=(',', ':')).encode('ascii')


def matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def validate_output(value, member=None):
    if not isinstance(value, dict) or set(value) != {'decision', 'reason', 'owner'}:
        raise Invalid('member output must contain exactly decision, reason, and owner')
    decisions = ('accept', 'revise', 'blocked', 'hold') if member == FINAL else ('accept', 'revise', 'blocked')
    if value['decision'] not in decisions:
        raise Invalid('invalid member decision')
    if (not isinstance(value['reason'], str) or not value['reason'].strip()
            or not isinstance(value['owner'], str) or not value['owner'].strip()):
        raise Invalid('member reason and owner must be nonblank strings')
    return value


def validate_request(value, backend):
    required = {'team', 'task', 'kind', 'stateful', 'authors', 'evidence'}
    if (not isinstance(value, dict) or not required <= value.keys()
            or value.keys() - required - {'fixtures'}):
        raise Invalid('invalid request fields')
    if (value['team'] != TEAM or not isinstance(value['task'], str)
            or not value['task'].strip()):
        raise Invalid('expected independent-review and a nonblank string task')
    kind = value['kind']
    if not isinstance(kind, str) or (kind != 'patch' and kind not in SPECIALISTS):
        raise Invalid('unknown review kind')
    if type(value['stateful']) is not bool:
        raise Invalid('stateful must be a boolean')
    authors = value['authors']
    if not isinstance(authors, list) or not authors or not all(
            matches(MEMBER_ID, author) for author in authors):
        raise Invalid('authors must be a nonempty array of member IDs')
    if not isinstance(value['evidence'], dict) or not value['evidence']:
        raise Invalid('evidence must be a nonempty object')
    if 'fixtures' in value:
        if backend != 'fixture':
            raise Invalid('fixtures are not allowed with the codex backend')
        if not isinstance(value['fixtures'], dict):
            raise Invalid('fixtures must be an object keyed by member ID')
        for member, output in value['fixtures'].items():
            if not matches(MEMBER_ID, member):
                raise Invalid('invalid fixture member ID')
            validate_output(output, member)

    route = review_route(kind, value['stateful'])
    overlapping = [member for member in route if member in authors]
    if overlapping:
        raise Invalid('self-review is not allowed: ' + ', '.join(overlapping))
    if backend == 'fixture':
        preflight_fixtures(value, route)
    return route


def preflight_fixtures(review, route):
    fixtures = review.get('fixtures', {})
    if not isinstance(fixtures, dict):
        raise Invalid('fixtures must be an object keyed by member ID')
    for member in route:
        if member not in fixtures:
            raise Invalid(f'missing fixture for {member}; use a new state directory with that fixture')
        if validate_output(fixtures[member], member)['decision'] != 'accept':
            break


def review_route(kind, stateful):
    if kind == 'patch':
        route = ['acl-012']
        if stateful:
            route.append('acl-013')
        route.extend(('acl-014', 'acl-015'))
    else:
        route = [SPECIALISTS[kind]]
    route.append(FINAL)
    return route


def write_record(path, value, directory_fd):
    # Exclusive creation makes records immutable. A partial record after an
    # interruption is treated as uncertain, not repaired by retrying a launch.
    data = encode(value) + b'\n'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.fsync(directory_fd)


def read_record(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise Hold(f'not a regular state record: {path.name}')
            data = stream.read()
        value = decode(data)
        if not isinstance(value, dict) or data != encode(value) + b'\n':
            raise Hold(f'invalid state record: {path.name}')
        return value
    except (OSError, ValueError, RecursionError) as exc:
        raise Hold(f'unreadable state record: {path.name}') from exc


def intent_name(member):
    return member + '.intent.json'


def dispatch_names(member):
    return member + '.ready.json', member + '.started.json'


def dispatch_ticket(request, member):
    key = hashlib.sha256(encode(['review-dispatch-v1', request, member])).hexdigest()
    return {'task_id': key, 'member_id': member}


def prepare_state(root, directory_fd, request, *, readonly=False):
    names = {path.name for path in root.iterdir()}
    if REQUEST_FILE not in names:
        if names:
            raise Hold('nonempty team state has no request intent')
        if not readonly:
            write_record(root / REQUEST_FILE, request, directory_fd)
            names.add(REQUEST_FILE)
            for member in request['route']:
                ready, _ = dispatch_names(member)
                write_record(root / ready, dispatch_ticket(request, member), directory_fd)
                names.add(ready)
        return names

    previous = read_record(root / REQUEST_FILE)
    if previous.keys() != request.keys():
        raise Hold('invalid or unsupported team request record')
    if encode(previous) != encode(request):
        raise Invalid('request changed; use a new state directory')

    allowed = {REQUEST_FILE}
    if 'parent' in request:
        allowed.add('outcome.json')
    gap = False
    for member in request['route']:
        intent = intent_name(member)
        ready, started = dispatch_names(member)
        allowed.update((member, intent, ready, started))
        if ready in names and names.intersection((intent, member, started)):
            raise Hold(f'{member}: unused dispatch ticket conflicts with history')
        for ticket in names.intersection((ready, started)):
            if read_record(root / ticket) != dispatch_ticket(request, member):
                raise Hold(f'{member}: dispatch ticket does not match the request')
        if intent not in names:
            if member in names:
                raise Hold(f'{member}: member state has no dispatch intent')
            gap = True
        elif gap:
            raise Hold(f'{member}: dispatch history is not an ordered prefix')
    if names - allowed:
        raise Hold('unexpected entries in team state')
    return names


def validate_receipt(value, member):
    if not isinstance(value, dict) or value.get('status') != 'succeeded':
        raise Invalid('assignment did not report success')
    if value.get('actual_member_id') != member:
        raise Invalid('assignment receipt names a different member')
    artifact = value.get('artifact')
    if (not isinstance(artifact, dict) or not matches(SHA256, artifact.get('sha256'))
            or not matches(SHA256, value.get('receipt_sha256'))):
        raise Invalid('assignment receipt is missing artifact or receipt integrity fields')
    if (artifact.get('receipt_sha256') != value['receipt_sha256']
            or artifact.get('member_id') != member):
        raise Invalid('assignment artifact does not match the receipt')
    payload = artifact.get('payload')
    canonical_payload = canonical(payload)
    if hashlib.sha256(canonical_payload).hexdigest() != artifact['sha256']:
        raise Invalid('assignment artifact payload hash does not match')
    validated_payload = validate_output(payload, member)
    return {
        'actual_member_id': member,
        'artifact': {'payload': validated_payload,
                     'sha256': artifact['sha256']},
        'receipt_sha256': value['receipt_sha256'],
        **{key: value[key] for key in ('run_id', 'attempt_id') if key in value},
    }


def call_assignment(request, member, member_dir, assignment_input, reconcile):
    dispatch_stdin = encode(assignment_input) + b'\n'
    command = [sys.executable, '-m', 'role_orchestrator.assignment']
    if reconcile:
        command.extend(['status', '--state', str(member_dir),
                        '--runtime', request['runtime']])
        if 'parent' in request:
            command.append('--verify-native-receipt')
        stdin = b''
    else:
        command.extend(['--lab', request['lab'], '--revision', request['revision'],
                        '--runtime', request['runtime'], '--state', str(member_dir),
                        '--backend', request['backend'], '--member', member,
                        '--task', member, '--profile', PROFILE])
        stdin = dispatch_stdin
    operation = 'assignment status' if reconcile else 'assignment dispatch'
    try:
        completed = subprocess.run(command, input=stdin, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, check=False)
    except OSError as exc:
        raise Hold(f'{operation} could not be run') from exc
    if not reconcile and 'parent' in request:
        return call_assignment(request, member, member_dir, assignment_input, True)
    if completed.returncode != 0:
        raise Hold(f'{operation} exited with code {completed.returncode}')
    try:
        result = decode(completed.stdout)
        if reconcile:
            if not isinstance(result, dict):
                raise Invalid('assignment status is not an object')
            plan = result.get('plan')
            verdict = result.get('verdict')
            if not isinstance(plan, dict):
                raise Invalid('assignment status is missing its plan')
            verdict = verdict if isinstance(verdict, dict) else {}
            expected_plan = {
                'input_sha256': hashlib.sha256(canonical(assignment_input)).hexdigest(),
                'member_id': member,
                'task_id': member,
                'revision': request['revision'],
                'backend': request['backend'],
            }
            for field, expected in expected_plan.items():
                if plan.get(field) != expected:
                    raise Invalid(f'assignment plan {field} does not match the dispatch')
            contract = plan.get('contract')
            if not isinstance(contract, dict) or contract.get('profile') != PROFILE:
                raise Invalid('assignment plan has the wrong contract profile')
            if 'parent' in request and any(plan.get(key) != value for key, value in
                                           request['parent']['assignment_contracts'][member].items()):
                raise Invalid('assignment plan differs from the pinned roster contract')
            for field in ('run_id', 'attempt_id'):
                identifier = plan.get(field)
                if not isinstance(identifier, str) or not identifier.strip():
                    raise Invalid(f'assignment verdict {field} does not match the plan')
            attempt = {field: plan[field] for field in ('run_id', 'attempt_id')}
            if any(verdict.get(field) != identifier for field, identifier in attempt.items()):
                raise Invalid('assignment verdict identifiers do not match the plan')
            result = verdict
        return validate_receipt(result, member)
    except (ValueError, RecursionError) as exc:
        raise Hold(f'{operation} did not yield a valid succeeded receipt: {exc}') from exc


def run_member(root, directory_fd, names, request, member, prior):
    review = request['input']
    assignment_input = {'goal': review['task'], 'evidence': review['evidence'],
                        'upstream': [receipt['artifact'] for receipt in prior]}
    if request.get('parent', {}).get('policy_binding') and member == 'acl-012':
        assignment_input['upstream'] = []
    if request['backend'] == 'fixture':
        fixtures = review.get('fixtures', {})
        if member not in fixtures:
            raise Invalid(f'missing fixture for {member}; use a new state directory with that fixture')
        assignment_input['fixture'] = fixtures[member]

    member_dir = root / member
    intent = {'member_id': member,
              'input_sha256': hashlib.sha256(encode(assignment_input)).hexdigest()}
    marker = intent_name(member)
    if marker in names:
        if read_record(root / marker) != intent:
            raise Hold('dispatch intent does not match the current assignment input')
        try:
            nonempty = (stat.S_ISDIR(member_dir.lstat().st_mode)
                        and any(member_dir.iterdir()))
        except FileNotFoundError:
            nonempty = False
        if not nonempty:
            raise Hold('dispatch intent exists but member state is missing or empty')
        return call_assignment(request, member, member_dir, assignment_input, True)

    ready, started = dispatch_names(member)
    # Consume authorization durably before creating member records or submitting.
    # Missing tickets are never regenerated in an initialized directory.
    os.rename(root / ready, root / started)
    os.fsync(directory_fd)
    write_record(root / marker, intent, directory_fd)
    member_dir.mkdir(mode=0o700)
    return call_assignment(request, member, member_dir, assignment_input, False)


def validate_saved_attempt(value):
    allowed = {'run_id', 'attempt_id', 'receipt_sha256'}
    if not isinstance(value, dict) or value.keys() - allowed:
        raise Invalid('invalid terminal attempt fields')
    for key, identifier in value.items():
        if key == 'receipt_sha256':
            valid = matches(SHA256, identifier)
        else:
            valid = isinstance(identifier, str) and bool(identifier.strip())
        if not valid:
            raise Invalid(f'invalid terminal attempt {key}')
    return value


def terminal_attempts(saved, request):
    route = request['route']
    try:
        member = saved.get('member_id')
        artifacts = saved.get('artifacts')
        fields = {'status', 'member_id', 'reason', 'artifacts'}
        if member not in route or not isinstance(artifacts, list):
            raise Invalid('invalid terminal member or artifacts')
        diagnostic = 'attempt' in saved
        legacy_policy = set(saved) == fields and saved['status'] == 'hold'
        policy = saved.get('cause') == 'policy-findings' or legacy_policy
        if policy:
            if (member != 'acl-012' or not request.get('parent', {}).get('policy_binding')
                    or saved.get('status') != 'hold'
                    or not isinstance(saved.get('reason'), str)
                    or (not legacy_policy and (
                        set(saved) != fields | {'cause', 'task_id'}
                        or not saved['reason'].strip()
                        or saved['task_id'] != dispatch_ticket(request, member)['task_id']))):
                raise Invalid('invalid terminal policy diagnostic')
        expected = route[:route.index(member) + (not diagnostic)]
        if len(artifacts) != len(expected):
            raise Invalid('terminal artifacts do not cover the expected route prefix')
        attempts = {}
        for actual, record in zip(expected, artifacts):
            if (not isinstance(record, dict)
                    or record.get('actual_member_id') != actual):
                raise Invalid('terminal artifact member does not match the route')
            artifact = record.get('artifact')
            if not isinstance(artifact, dict) or set(artifact) != {'payload', 'sha256'}:
                raise Invalid('invalid terminal artifact fields')
            output = validate_output(artifact['payload'], actual)
            if hashlib.sha256(canonical(output)).hexdigest() != artifact['sha256']:
                raise Invalid('terminal artifact payload hash does not match')
            if (diagnostic or actual != member) and output['decision'] != 'accept':
                raise Invalid('terminal artifacts continue past a stopping decision')
            attempt = validate_saved_attempt({
                key: value for key, value in record.items() if key not in {'actual_member_id', 'artifact'}})
            if 'receipt_sha256' not in attempt:
                raise Invalid('terminal artifact is missing its receipt hash')
            attempts[actual] = attempt
        if diagnostic:
            diagnostic_fields = fields | {'attempt'}
            if (saved.get('status') != 'hold'
                    or saved.get('cause', 'member-unavailable') != 'member-unavailable'
                    or not isinstance(saved['reason'], str)
                    or not saved['reason'].strip()):
                raise Invalid('invalid terminal diagnostic')
            if 'task_id' in saved:
                if (set(saved) != diagnostic_fields | {'cause', 'task_id'}
                        or saved['task_id'] != dispatch_ticket(request, member)['task_id']
                        or saved['attempt'] != {}):
                    raise Invalid('invalid terminal member-unavailable identity')
            else:
                if set(saved) - {'cause'} != diagnostic_fields:
                    raise Invalid('invalid legacy terminal diagnostic')
                # Accept old records without rewriting them or trusting their hints.
                validate_saved_attempt(saved['attempt'])
        elif not policy:
            if set(saved) != fields | {'decision', 'owner'}:
                raise Invalid('invalid terminal outcome fields')
            decision = output['decision']
            status = {'accept': 'accepted', 'hold': 'hold'}.get(decision, 'feedback')
            if (saved['status'] != status or (decision == 'accept' and member != FINAL)
                    or any(saved[key] != value for key, value in output.items())):
                raise Invalid('terminal outcome does not match its final artifact')
        return attempts, policy
    except (ValueError, RecursionError) as exc:
        raise Hold(f'invalid terminal outcome: {exc}') from exc


def run_review(root, request, *, readonly=False):
    if request['backend'] == 'fixture':
        preflight_fixtures(request['input'], request['route'])
    if readonly and not root.exists():
        return {'status': 'pending', 'artifacts': []}, 0
    if not readonly:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_SH if readonly else fcntl.LOCK_EX)
        names = prepare_state(root, directory_fd, request, readonly=readonly)
        if REQUEST_FILE not in names:
            return {'status': 'pending', 'artifacts': []}, 0
        terminal = 'outcome.json' in names
        try:
            attempts, policy = (terminal_attempts(read_record(root / 'outcome.json'), request)
                                if terminal else ({}, False))
        except Hold as exc:
            return {'status': 'hold', 'cause': 'terminal-outcome', 'reason': str(exc),
                    'task': request['input']['task'], 'artifacts': []}, 3

        def finish(result, code):
            if 'parent' in request and not terminal and not readonly:
                terminal_attempts(result, request)
                write_record(root / 'outcome.json', result, directory_fd)
            return result, code

        receipts = []
        for member in request['route']:
            if intent_name(member) not in names:
                if terminal:
                    return {'status': 'hold', 'cause': 'terminal-history',
                            'member_id': member, 'reason': 'terminal dispatch history is incomplete',
                            'artifacts': receipts}, 3
                ready, _ = dispatch_names(member)
                if ready not in names:
                    return {'status': 'hold', 'cause': 'dispatch-history',
                            **dispatch_ticket(request, member),
                            'reason': 'no unused dispatch ticket; prior submission is uncertain',
                            'artifacts': receipts}, 3
                if readonly:
                    return {'status': 'pending', 'artifacts': receipts}, 0
            try:
                receipt = run_member(root, directory_fd, names, request, member, receipts)
            except (Hold, OSError) as exc:
                return finish({'status': 'hold', 'cause': 'member-unavailable',
                               **dispatch_ticket(request, member),
                               'reason': str(exc) or type(exc).__name__,
                               'attempt': {}, 'artifacts': receipts}, 3)
            recorded = attempts.get(member, {})
            if 'receipt_sha256' in recorded and any(
                    receipt.get(key) != value for key, value in recorded.items()):
                return {'status': 'hold', 'cause': 'terminal-history',
                        'member_id': member, 'reason': 'receipt identifiers differ from terminal outcome',
                        'artifacts': receipts}, 3
            receipts.append(receipt)
            if request.get('parent', {}).get('policy_binding') and member == 'acl-012':
                from .policy_review import findings
                try:
                    findings(request['input']['evidence'], receipt)
                except (ValueError, RecursionError) as exc:
                    return finish({'status': 'hold', 'cause': 'policy-findings',
                                   **dispatch_ticket(request, member),
                                   'reason': str(exc) or type(exc).__name__,
                                   'artifacts': receipts}, 3)
                if policy:
                    return {'status': 'hold', 'cause': 'terminal-history',
                            **dispatch_ticket(request, member),
                            'reason': 'terminal policy diagnostic did not reproduce',
                            'artifacts': receipts}, 3
            output = receipt['artifact']['payload']
            if output['decision'] != 'accept' or member == FINAL:
                status = {'accept': 'accepted', 'hold': 'hold'}.get(output['decision'], 'feedback')
                return finish({'status': status, 'member_id': member, 'artifacts': receipts,
                               **output}, 3 if status == 'hold' else 0)
        raise Hold('review ended without a final member')
    finally:
        os.close(directory_fd)


def absolute_path(value, option):
    if not value.strip() or '\x00' in value:
        raise Invalid(f'{option} requires a valid path')
    try:
        return Path(value).resolve()
    except (ValueError, OSError, RuntimeError) as exc:
        raise Invalid(f'{option} requires a resolvable path') from exc


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument('--lab', required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--runtime', required=True)
    parser.add_argument('--state', required=True)
    parser.add_argument('--backend', choices=('fixture', 'codex'), required=True)
    parser.add_argument('--readonly', action='store_true', help='query without writing or dispatching')
    try:
        args = parser.parse_args(argv)
        if not matches(FULL_REVISION, args.revision):
            raise Invalid('revision must be a full hexadecimal Git SHA')
        try:
            review = decode(sys.stdin.buffer.read())
        except (ValueError, RecursionError) as exc:
            raise Invalid(f'invalid request JSON: {exc}') from exc
        route = validate_request(review, args.backend)
        request = {'version': 1, 'profile': PROFILE, 'input': review, 'route': route,
                   'lab': str(absolute_path(args.lab, '--lab')),
                   'runtime': str(absolute_path(args.runtime, '--runtime')),
                   'revision': args.revision, 'backend': args.backend}
        result, code = run_review(absolute_path(args.state, '--state'), request,
                                  readonly=args.readonly)
    except Invalid as exc:
        result, code = {'status': 'rejected', 'reason': str(exc)}, 2
    except (Hold, OSError) as exc:
        result, code = {'status': 'hold', 'reason': str(exc)}, 3
    print(encode(result).decode('ascii'))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
