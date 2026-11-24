"""Bounded planning broker; native Assignment owns execution and receipts."""
import argparse
import fcntl
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace

from .contracts import strict_json
from .planning_catalog import PROFILE, bound_plan, fields, key, load, need, text
from .role_contracts import canonical, digest
from .roster import AssignmentError

LEAD_PROTOCOL = (
    'Select only necessary available member IDs; ordered work entries are {member_id, input} '
    'where input is a JSON object. Return decision {action, intent, ...}. '
    'intent is {explicit, hypotheses, counterevidence}, each an array of strings; '
    'explicit entries must quote user messages, hypotheses are not user approval. '
    'assign adds work; clarify adds question; revise adds request for upstream information; '
    'hold adds reason; accept adds proposal (object) and evidence (specialist task IDs). '
    'Do not change the approved goal or treat member evidence as user authorization.'
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


def save(path, state):
    temp = path.with_suffix('.tmp')
    with temp.open('wb') as handle:
        handle.write(canonical(state) + b'\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def invoke(command, data):
    try:
        result = subprocess.run(command, input=data, capture_output=True, timeout=600, check=False)
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


def native(args, current, dispatch):
    directory = args.state / current['task_id']
    command = [sys.executable, '-m', 'role_orchestrator.assignment']
    if dispatch:
        directory.mkdir(mode=0o700)
        invoke(command + ['--lab', args.lab, '--revision', args.revision, '--runtime', args.runtime,
                         '--state', str(directory), '--backend', args.backend, '--task', current['task_id'],
                         '--member', current['member_id'], '--profile', PROFILE],
               canonical(current['input']) + b'\n')
    flags = ['--verify-native-receipt'] if args.backend == 'codex' else []
    return invoke(command + ['status', '--state', str(directory), '--runtime', args.runtime] + flags, b'')


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
         and verdict.get('actual_member_id') == current['member_id'], 'native_success_required')
    for field in ('run_id', 'attempt_id'):
        need(text(plan.get(field)) and plan[field] == verdict.get(field), 'native_attempt_mismatch')
    artifact, sha = verdict.get('artifact'), verdict.get('receipt_sha256')
    need(isinstance(sha, str) and re.fullmatch(r'[0-9a-f]{64}', sha)
         and isinstance(artifact, dict) and isinstance(artifact.get('payload'), dict), 'native_artifact_missing')
    return dict(task_id=current['task_id'], member_id=current['member_id'], run_id=plan['run_id'],
                attempt_id=plan['attempt_id'], receipt_sha256=sha, artifact=artifact,
                evidence_kind='fixture' if args.backend == 'fixture' else 'native-verified-codex')


def advance(args, path, state, team):
    while state['branch'] is None:
        fresh = state['pending'] is None
        if fresh:
            if state['count'] >= team['max_steps']:
                state['branch'] = {'action': 'hold', 'reason': 'step_budget_exhausted', 'terminal': True}
                save(path, state)
                break
            lead = not state['queue']
            item = {'member_id': team['lead'], 'input': {}} if lead else state['queue'].pop(0)
            state['count'] += 1
            context = dict(principal=state['principal'], user_messages=list(state['messages']),
                           approved_goal=state['goal'], evidence=dict(state['links']), work=item['input'])
            if lead:
                context.update(protocol=LEAD_PROTOCOL, remaining=team['max_steps'] - state['count'],
                               available={m: b for m, b in team['members'].items()
                                          if m != team['lead'] and m not in state['used']})
            state['pending'] = dict(member_id=item['member_id'], input={'context': context},
                                    task_id=f"{args.team}--{state['count']}", lead=lead)
            save(path, state)  # The only path that can dispatch is after this durable intent.
        current = state['pending']
        try:
            observed = native(args, current, fresh)
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
        save(path, state)
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
    root = args.state
    stat = root.stat()
    need(root.is_dir() and not root.is_symlink() and stat.st_uid == uid
         and not stat.st_mode & 0o077, 'private_state_directory_required')
    path = root / 'team.json'
    with (root / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            state = strict_json(path.read_text(encoding='utf-8'))
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
                save(path, state)
        else:
            need(not (set(p.name for p in root.iterdir()) - {'.lock', 'team.tmp'}), 'unrecognized_state')
            need(fields(incoming, 'request') and text(incoming['request']), 'request_required')
            state = dict(pin=pin, principal=principal, goal=approval['text'], messages=[incoming['request']],
                         count=0, pending=None, branch=None, queue=[], used=[], journal=[], links={})
            save(path, state)
        return advance(args, path, state, team)


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
