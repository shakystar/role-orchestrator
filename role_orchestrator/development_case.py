"""Run one patch author and one independent reviewer with resumable named assignments."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from . import assignment, goal_pipeline, parent_review
from .attempts import AttemptReader
from .handoff_runner import owner
from .role_contracts import canonical, digest, role_contract, validate_fields, validate_inputs


PROFILE = 'development-patch-v1'
AUTHOR = 'acl-007'
REVIEWER = 'acl-005'


def validate_case(case, backend):
    if not isinstance(case.get('case_id'), str) or not assignment.ID.fullmatch(case['case_id']):
        raise ValueError('invalid development case ID')
    if 'goal' in case:
        return goal_pipeline.validate_goal(case, backend)
    required = {'case_id', 'criterion', 'author'}
    if not required <= case.keys() <= required | {'fixtures'}:
        raise ValueError('invalid development case fields')
    validate_fields(case['criterion'], {'id': 'string'})
    validate_inputs(case['author'], role_contract(AUTHOR, profile=PROFILE))
    if 'fixtures' in case and (backend != 'fixture' or not isinstance(case['fixtures'], dict)
                               or set(case['fixtures']) != {'author', 'reviewer'}):
        raise ValueError('fixtures require a fixture backend and both stage outputs')


def stage(args, case, name, member, inputs, profile=PROFILE):
    state = args.state / name
    command = [sys.executable, '-m', 'role_orchestrator.assignment']
    runtime = str(args.runtime.resolve())
    if name in case.get('fixtures', {}):
        inputs = {**inputs, 'fixture': case['fixtures'][name]}
    data = canonical(inputs)
    if not state.exists():
        if args.action == 'status':
            return {'status': 'not_started'}
        if 'goal' in case and '-r' in name and sum(p.is_dir() for p in args.state.glob('*-r[12]')) >= 6:
            return {'status': 'blocked', 'reason': 'case correction limit reached'}
        child = subprocess.run([*command, '--state', str(state), '--runtime', runtime,
                                '--lab', str(args.lab), '--revision', args.revision,
                                '--backend', args.backend, '--profile', profile,
                                '--member', member, '--task', name],
                               input=data, capture_output=True)
        if child.returncode and not (state / assignment.PLAN).is_file():
            return {'status': 'uncertain' if state.exists() else 'failed',
                    'reason': 'assignment_plan_unavailable'}
    if not (state / assignment.PLAN).is_file():
        return {'status': 'uncertain', 'reason': 'assignment_plan_unavailable'}
    result = subprocess.run([*command, 'status', '--state', str(state), '--runtime', runtime],
                            capture_output=True, check=True)
    record = assignment.decode(result.stdout)
    plan, verdict = record['plan'], record['verdict']
    expected = dict(member_id=member, task_id=name, revision=args.revision,
                    backend=args.backend, input_sha256=digest(data))
    if (not assignment.matches(plan, **expected)
            or plan.get('contract') != role_contract(member, profile=profile)):
        raise ValueError('saved stage differs from this case')
    if not verdict or verdict.get('status') != 'succeeded':
        return verdict or {'status': 'uncertain', 'reason': 'assignment_incomplete'}
    # Re-read the native receipt before handing an artifact to another member.
    ids = {key: plan[key] for key in ('run_id', 'attempt_id')}
    return assignment.observe(runtime, state, ids, plan)


def correct_review(args, case, inputs, verdict):
    """Recover malformed, terminal review output while preserving each original receipt."""
    for revision in range(1, 3):
        if verdict.get('status') != 'failed' or verdict.get('reason') != 'invalid_role_output':
            break
        previous = 'reviewer' if revision == 1 else 'reviewer-r1'
        state = args.state / previous
        plan = assignment.decode((state / assignment.PLAN).read_bytes())
        request = dict(id=previous, role=REVIEWER, instruction=plan['instruction'])
        proof = AttemptReader(args.runtime, state, plan['run_id']).bound(
            plan['attempt_id'], request, plan['input_sha256'])
        if (proof is None or proof[1] != verdict.get('receipt_sha256')
                or proof[0].get('status') != 'succeeded'
                or not assignment.matches(proof[0].get('result'), actual_member_id=REVIEWER,
                                          actual_role_version=plan['expected_role_version'])):
            raise ValueError('invalid review receipt unavailable')
        raw = proof[0]['result'].get('text', '')
        corrected = dict(inputs, goal=case['goal'], correction={
            'previous_receipt': proof[1], 'previous_output': raw[:4096],
            'required_change': 'Return valid JSON with verdict exactly accept, revise, or blocked, the '
            'supplied criterion_id and patch_sha256, and your reason. Independently review the patch '
            'against the stated goal. You own static review; the separate executor tests it afterward. '
            'Identify any concrete defect or missing evidence relevant to this stage.'})
        verdict = stage(args, case, f'reviewer-r{revision}', REVIEWER, corrected)
    return verdict


def run_case(args, case):
    spec = None
    if 'review' in case:
        spec = parent_review.specification(case['review'], args.backend)
        case = {key: value for key, value in case.items() if key != 'review'}
    validate_case(case, args.backend)
    if spec is not None:
        conflict = parent_review.author_conflict(case['case_id'], spec, AUTHOR)
        if conflict is not None:
            return conflict
    goal_mode = 'goal' in case
    manifest = {'case_id': case['case_id'], 'input_sha256': digest(canonical(case)),
                'backend': args.backend, 'revision': args.revision, 'profile': 'python-goal-v1' if goal_mode else PROFILE}
    if spec and spec.missing and not parent_review.location(args, spec).exists():
        return parent_review.missing_result(case['case_id'], spec)
    if args.action == 'status' and not args.state.is_dir():
        return {'case_id': case['case_id'], 'state': 'not_started'}
    if args.action == 'run':
        # Resolve every required role before placing the first assignment.
        stages = [(name, member, 'lab-v1') for name, member in goal_pipeline.PLANNING.items()] if goal_mode else []
        stages.append(('author', AUTHOR, PROFILE))
        if spec is None:
            stages.append(('reviewer', REVIEWER, PROFILE))
            if goal_mode:
                stages.append(('evidence', 'acl-009', 'lab-v1'))
        for name, member, profile in stages:
            plan = assignment.make_plan(SimpleNamespace(lab=args.lab, revision=args.revision,
                member=member, task=name, backend=args.backend, profile=profile))
            expected = role_contract(member, profile=profile)
            if plan.get('contract') != expected or plan.get('handoff_id') != expected['role']:
                raise ValueError('roster does not supply required development role: ' + name)
        args.state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (parent_review.locked(args.state, args.action == 'status') if spec else owner(args.state)):
        path = args.state / 'case.json'
        if path.exists():
            if assignment.decode(path.read_bytes()) != manifest:
                raise ValueError('development case differs from saved manifest')
        elif args.action == 'status':
            return {'case_id': case['case_id'], 'state': 'uncertain', 'reason': 'manifest_missing'}
        else:
            if any(args.state.iterdir()):
                raise ValueError('refusing to adopt an unbound case directory')
            fd = os.open(args.state, os.O_RDONLY | os.O_DIRECTORY)
            try:
                assignment.publish(path, manifest, fd)
            finally:
                os.close(fd)

        if spec:
            resumed = parent_review.reconcile(args, case['case_id'], spec)
            if resumed is not None:
                return resumed

        report = {'case_id': case['case_id']}
        if goal_mode:
            def dispatch(name, member, inputs, profile):
                return stage(args, case, name, member, inputs, profile)
            prepared, stopped = goal_pipeline.prepare(case, dispatch, report)
            if stopped is not None:
                return stopped
            criterion, author_input = prepared
        else:
            criterion, author_input = case['criterion'], case['author']
        author = stage(args, case, 'author', AUTHOR, author_input)
        if author['status'] != 'succeeded':
            return {**report, 'stage': 'author', 'state': author['status'], 'detail': author}
        artifact = author['artifact']
        patch_hash = digest(artifact['payload']['diff'].encode())
        authored = {key: artifact[key] for key in ('task_id', 'member_id', 'run_id', 'attempt_id')}
        authored.update(sha256=artifact['output_sha256'], artifact=artifact['payload'])
        report.update(author=artifact, patch_sha256=patch_hash)
        if spec:
            return parent_review.start(args, case['case_id'], spec, criterion,
                                       author_input, author, patch_hash)
        review_input = {key: author_input[key] for key in ('base_sha', 'source', 'regression')}
        review_input.update(case_id=case['case_id'], criterion=criterion,
                            patch_sha256=patch_hash, authored_revision=authored)
        reviewer = stage(args, case, 'reviewer', REVIEWER, review_input)
        if goal_mode:
            reviewer = correct_review(args, case, review_input, reviewer)
        if reviewer['status'] != 'succeeded':
            return {**report, 'stage': 'reviewer', 'state': reviewer['status'], 'detail': reviewer}
        review = reviewer['artifact']
        decision = review['payload']['verdict']
        state = 'patch_reviewed' if goal_mode and decision == 'accept' else decision
        return {**report, 'stage': 'reviewer', 'state': state, 'reviewer': review}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'status'))
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--lab', type=Path, required=True)
    parser.add_argument('--subject-repo', type=Path)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--backend', choices=('fixture', 'codex'), required=True)
    args = parser.parse_args()
    try:
        case = assignment.decode(assignment.read_input())
        result = run_case(args, case)
        print(assignment.encode(result))
        return 0 if args.action == 'status' or result['state'] in ('accept', 'patch_reviewed') else 1
    except (assignment.AssignmentError, parent_review.Hold, KeyError, OSError,
            ValueError, subprocess.SubprocessError) as exc:
        print(assignment.encode({'state': 'error', 'reason': str(exc)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
