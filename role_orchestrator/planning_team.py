"""Bounded planning broker; native Assignment owns execution and receipts."""
import argparse
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from types import SimpleNamespace

from .contracts import strict_json
from .planning_catalog import PROFILE, bound_plan, fields, key, load, need, text
from .role_contracts import canonical, digest
from .roster import AssignmentError

LEAD_PROTOCOL = (
    'As lead, separate what the user said from your assumptions, choose the smallest necessary '
    'specialist path, and synthesize linked specialist artifacts. Specialists supply evidence, not approval. '
    'Return decision with exactly action, intent, and the fields listed for that action. '
    'intent = {explicit: string[], hypotheses: string[], counterevidence: string[]}. '
    'Quote user messages verbatim in explicit; record assumptions and evidence against them separately. '
    'assign: work = ordered [{member_id: string, input: object}]; use distinct keys from available, '
    'not role names. Require 0 < len(work) < remaining to reserve a lead turn. '
    'clarify: question = nonempty string asking the user to resolve ambiguity. '
    'revise: request = nonempty string specifying missing or conflicting upstream information. '
    'hold: reason = nonempty string explaining why work cannot proceed. '
    'accept: proposal = nonempty object; evidence = nonempty unique specialist task IDs from evidence. '
    'Do not change the approved goal or treat a hypothesis or specialist artifact as user authorization.'
)


def decision(value, messages, available, links, remaining):
    need(isinstance(value, dict), 'decision_object_required')
    extra = {'assign': 'work', 'clarify': 'question', 'revise': 'request',
             'hold': 'reason', 'accept': 'proposal evidence'}
    action = value.get('action')
    need(isinstance(action, str) and action in extra
         and fields(value, 'action intent ' + extra[action]), 'invalid_decision')
    intent = value['intent']
    need(fields(intent, 'explicit hypotheses counterevidence')
         and all(isinstance(items, list) and all(map(text, items)) for items in intent.values())
         and all(any(quote in msg for msg in messages) for quote in intent['explicit']), 'invalid_intent')
    if action == 'assign':
        work = value['work']
        need(isinstance(work, list) and 0 < len(work) < remaining, 'invalid_or_unbounded_work')
        seen = set()
        for item in work:
            need(fields(item, 'member_id input') and key(item['member_id'])
                 and isinstance(item['input'], dict), 'invalid_work_item')
            member = item['member_id']
            need(member in available and member not in seen, 'unknown_or_duplicate_member')
            seen.add(member)
    elif action == 'accept':
        evidence = value['evidence']
        need(isinstance(value['proposal'], dict) and bool(value['proposal'])
             and isinstance(evidence, list) and bool(evidence) and all(map(text, evidence))
             and len(set(evidence)) == len(evidence)
             and set(evidence) <= set(links), 'unlinked_proposal')
    else:
        need(text(value[extra[action]]), 'missing_branch_information')
    return action


@contextmanager
def state_directory(path, uid):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        need(stat.S_ISDIR(info.st_mode) and info.st_uid == uid
             and not info.st_mode & 0o077, 'private_state_directory_required')
        yield fd
    finally:
        os.close(fd)


def save(state_fd, state):
    fd = os.open('team.tmp', os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                 0o600, dir_fd=state_fd)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(canonical(state) + b'\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.replace('team.tmp', 'team.json', src_dir_fd=state_fd, dst_dir_fd=state_fd)
    os.fsync(state_fd)


# Change directory only in the child, after Python startup in the original cwd.
_PINNED_MODULE = (
    "import os, runpy, sys\n"
    "fd = int(sys.argv.pop(1))\n"
    "module = sys.argv.pop(1)\n"
    "if sys.path and sys.path[0] == '':\n"
    "    sys.path[0] = os.getcwd()\n"
    "os.fchdir(fd)\n"
    "os.close(fd)\n"
    "runpy.run_module(module, run_name='__main__', alter_sys=True)\n"
)


def invoke(command, data, *, state_fd):
    argv = [sys.executable, '-c', _PINNED_MODULE, str(state_fd), *command]
    try:
        result = subprocess.run(argv, input=data, capture_output=True, timeout=600,
                                check=False, pass_fds=(state_fd,))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'error': type(exc).__name__}
    answer = {'returncode': result.returncode}
    try:
        answer['value'] = strict_json(result.stdout.decode())
        canonical(answer)
    except (ValueError, RecursionError):
        answer.pop('value', None)
        answer['error'] = 'invalid_native_json'
    return answer


def native(args, current, dispatch, *, state_fd):
    directory = current['task_id']
    command = ['role_orchestrator.assignment']
    if dispatch:
        os.mkdir(directory, mode=0o700, dir_fd=state_fd)
        invoke(command + ['--lab', args.lab, '--revision', args.revision, '--runtime', args.runtime,
                         '--state', directory, '--backend', args.backend, '--task', current['task_id'],
                         '--member', current['member_id'], '--profile', PROFILE],
               canonical(current['input']) + b'\n', state_fd=state_fd)
    return invoke(command + ['status', '--state', directory, '--runtime', args.runtime,
                             '--verify-native-receipt'], b'', state_fd=state_fd)


def link(args, current, observation):
    route = SimpleNamespace(lab=args.lab, revision=args.revision, backend=args.backend,
                            task=current['task_id'], member=current['member_id'])
    expected = bound_plan(route, args.revision, 2)
    expected.pop('version')  # Native assignment owns its envelope version.
    expected['input_sha256'] = digest(canonical(current['input']))
    need(expected['contract']['catalog_sha256'] == args.catalog_sha256, 'untrusted_binding')
    status = observation.get('value')
    need(observation.get('returncode') == 0 and isinstance(status, dict), 'native_status_unavailable')
    plan, verdict = status.get('plan'), status.get('verdict')
    need(isinstance(plan, dict) and all(plan.get(k) == v for k, v in expected.items()), 'native_plan_mismatch')
    need(isinstance(verdict, dict) and verdict.get('status') == 'succeeded'
         and verdict.get('actual_member_id') == current['member_id']
         and verdict.get('actual_role_version') == expected['expected_role_version'], 'native_success_required')
    for field in ('run_id', 'attempt_id'):
        need(text(plan.get(field)) and plan[field] == verdict.get(field), 'native_attempt_mismatch')
    artifact, sha = verdict.get('artifact'), verdict.get('receipt_sha256')
    need(isinstance(sha, str) and re.fullmatch(r'[0-9a-f]{64}', sha)
         and isinstance(artifact, dict) and isinstance(artifact.get('payload'), dict), 'native_artifact_missing')
    return dict(task_id=current['task_id'], member_id=current['member_id'], run_id=plan['run_id'],
                attempt_id=plan['attempt_id'], receipt_sha256=sha, artifact=artifact,
                evidence_kind='fixture' if args.backend == 'fixture' else 'native-verified-codex')


def advance(args, state_fd, state, team):
    while state['branch'] is None:
        fresh = state['pending'] is None
        if fresh:
            if state['count'] >= team['max_steps']:
                state['branch'] = {'action': 'hold', 'reason': 'step_budget_exhausted', 'terminal': True}
                save(state_fd, state)
                break
            task_id = f"{args.team}--{state['count'] + 1}"
            fixtures = state.get('fixtures')
            if fixtures is not None:
                need(args.backend == 'fixture', 'fixtures_require_fixture_backend')
                need(task_id in fixtures,
                     f'missing fixture for {task_id}; use a new state directory with that fixture')
            lead = not state['queue']
            item = {'member_id': team['lead'], 'input': {}} if lead else state['queue'].pop(0)
            state['count'] += 1
            context = dict(principal=state['principal'], user_messages=list(state['messages']),
                           approved_goal=state['goal'], evidence=dict(state['links']), work=item['input'])
            if lead:
                context.update(protocol=LEAD_PROTOCOL, remaining=team['max_steps'] - state['count'],
                               available={m: b for m, b in team['members'].items()
                                          if m != team['lead'] and m not in state['used']})
            work_input = {'context': context}
            if fixtures is not None:
                work_input['fixture'] = fixtures[task_id]
            state['pending'] = dict(member_id=item['member_id'], input=work_input,
                                    task_id=task_id, lead=lead)
            save(state_fd, state)  # The only path that can dispatch is after this durable intent.
        current = state['pending']
        try:
            observed = native(args, current, fresh, state_fd=state_fd)
        except OSError as exc:
            observed = {'error': type(exc).__name__}
        try:
            receipt = link(args, current, observed)
        except (ValueError, AssignmentError) as exc:
            return dict(action='hold', reason=str(exc), pending=current['task_id'], native_status=observed)
        state['pending'] = None
        state['journal'].append(receipt)
        payload = receipt['artifact']['payload']
        if current['lead']:
            try:
                value = payload.get('decision')
                action = decision(value, state['messages'], current['input']['context']['available'],
                                  state['links'], team['max_steps'] - state['count'])
            except ValueError as exc:
                state['branch'] = dict(action='hold', reason=str(exc), terminal=True)
            else:
                if action == 'assign':
                    state['queue'] = value['work']
                else:
                    state['branch'] = value if action == 'accept' else dict(value, reply_to=current['task_id'])
        else:
            if not isinstance(payload.get('artifact'), dict):
                state['branch'] = dict(action='hold', reason='invalid_specialist_artifact', terminal=True)
            else:
                state['links'][current['task_id']] = receipt
                state['used'].append(current['member_id'])
        save(state_fd, state)
    branch = state['branch']
    return dict(branch=branch, principal=state['principal'], backend=args.backend,
                artifacts=state['links'], lead_receipts=[row for row in state['journal']
                                                        if row['member_id'] == team['lead']])


def run(args, incoming):
    sha, team = load(args.lab, args.revision, args.team, args.catalog_sha256)
    uid = os.getuid()
    approval = team['goals'].get(args.goal)
    need(approval is not None and uid in approval['uids'], 'unapproved_goal_or_user')
    principal = {'team': args.team, 'goal': args.goal}
    pin = dict(catalog_sha256=sha, revision=args.revision, backend=args.backend, uid=uid,
               lab=args.lab, runtime=args.runtime, profile=PROFILE)
    with state_directory(args.state, uid) as state_fd, os.fdopen(
        os.open('.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=state_fd), 'wb'
    ) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            checkpoint = os.open('team.json', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=state_fd)
        except FileNotFoundError:
            checkpoint = None
        if checkpoint is not None:
            with os.fdopen(checkpoint, encoding='utf-8') as handle:
                state = strict_json(handle.read())
            need(state['pin'] == pin and state['principal'] == principal
                 and state['goal'] == approval['text'], 'checkpoint_pin_mismatch')
            if incoming is not None:
                branch = state['branch']
                need(fields(incoming, 'answer reply_to') and text(incoming['answer'])
                     and branch is not None and branch['action'] in ('clarify', 'revise', 'hold')
                     and incoming['reply_to'] == branch.get('reply_to')
                     and not branch.get('terminal'), 'answer_not_expected')
                state['messages'].append(incoming['answer'])
                state['branch'] = None
                save(state_fd, state)
        else:
            need(not (set(os.listdir(state_fd)) - {'.lock', 'team.tmp'}), 'unrecognized_state')
            with_fixtures = isinstance(incoming, dict) and 'fixtures' in incoming
            need(fields(incoming, 'request fixtures' if with_fixtures else 'request')
                 and text(incoming['request']), 'request_required')
            fixtures = incoming.get('fixtures')
            if with_fixtures:
                need(args.backend == 'fixture', 'fixtures_require_fixture_backend')
                step_ids = {f'{args.team}--{n}' for n in range(1, team['max_steps'] + 1)}
                need(isinstance(fixtures, dict) and fixtures.keys() <= step_ids
                     and all(isinstance(v, dict) for v in fixtures.values()), 'invalid_step_fixtures')
            state = dict(pin=pin, principal=principal, goal=approval['text'], messages=[incoming['request']],
                         count=0, pending=None, branch=None, queue=[], used=[], journal=[], links={},
                         fixtures=fixtures)
            save(state_fd, state)
        return advance(args, state_fd, state, team)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('lab', 'revision', 'runtime', 'catalog-sha256', 'team', 'goal'):
        parser.add_argument('--' + flag, required=True)
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--backend', required=True, choices=('fixture', 'codex'))
    args = parser.parse_args()
    try:
        args.lab, args.runtime = (str(Path(p).resolve(strict=True)) for p in (args.lab, args.runtime))
        raw = sys.stdin.buffer.read().strip()
        incoming = strict_json(raw.decode()) if raw else None
        if raw:
            need(isinstance(incoming, dict), 'input_object_required')
            canonical(incoming)
        result = run(args, incoming)
    except (OSError, ValueError, AssignmentError, RecursionError) as exc:
        sys.stdout.buffer.write(canonical({'error': str(exc)}) + b'\n')
        return 2
    sys.stdout.buffer.write(canonical(result) + b'\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
