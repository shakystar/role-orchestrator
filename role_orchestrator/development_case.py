"""Run one patch author and one independent reviewer with resumable named assignments."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from . import assignment
from .handoff_runner import owner
from .role_contracts import canonical, digest, role_contract, validate_fields, validate_inputs


PROFILE = 'development-patch-v1'
AUTHOR = 'acl-007'
REVIEWER = 'acl-005'


def validate_case(case, backend):
    required = {'case_id', 'criterion', 'author'}
    if not required <= case.keys() <= required | {'fixtures'}:
        raise ValueError('invalid development case fields')
    if not isinstance(case['case_id'], str) or not assignment.ID.fullmatch(case['case_id']):
        raise ValueError('invalid development case ID')
    validate_fields(case['criterion'], {'id': 'string'})
    validate_inputs(case['author'], role_contract(AUTHOR, profile=PROFILE))
    if 'fixtures' in case and (backend != 'fixture' or not isinstance(case['fixtures'], dict)
                               or set(case['fixtures']) != {'author', 'reviewer'}):
        raise ValueError('fixtures require a fixture backend and both stage outputs')


def stage(args, case, name, member, inputs):
    state = args.state / name
    command = [sys.executable, '-m', 'role_orchestrator.assignment']
    runtime = str(args.runtime.resolve())
    if 'fixtures' in case:
        inputs = {**inputs, 'fixture': case['fixtures'][name]}
    data = canonical(inputs)
    if not state.exists():
        if args.action == 'status':
            return {'status': 'not_started'}
        child = subprocess.run([*command, '--state', str(state), '--runtime', runtime,
                                '--lab', str(args.lab), '--revision', args.revision,
                                '--backend', args.backend, '--profile', PROFILE,
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
            or plan.get('contract') != role_contract(member, profile=PROFILE)):
        raise ValueError('saved stage differs from this case')
    if not verdict or verdict.get('status') != 'succeeded':
        return verdict or {'status': 'uncertain', 'reason': 'assignment_incomplete'}
    # Re-read the native receipt before handing an artifact to another member.
    ids = {key: plan[key] for key in ('run_id', 'attempt_id')}
    return assignment.observe(runtime, state, ids, plan)


def run_case(args, case):
    validate_case(case, args.backend)
    manifest = {'case_id': case['case_id'], 'input_sha256': digest(canonical(case)),
                'backend': args.backend, 'revision': args.revision, 'profile': PROFILE}
    if args.action == 'status' and not args.state.is_dir():
        return {'case_id': case['case_id'], 'state': 'not_started'}
    if args.action == 'run':
        # Resolve both roles before the first model call. A valid roster may
        # reassign a member to a different role outside this two-stage profile.
        for name, member in (('author', AUTHOR), ('reviewer', REVIEWER)):
            plan = assignment.make_plan(SimpleNamespace(lab=args.lab, revision=args.revision,
                member=member, task=name, backend=args.backend, profile=PROFILE))
            expected = role_contract(member, profile=PROFILE)
            if plan.get('contract') != expected or plan.get('handoff_id') != expected['role']:
                raise ValueError('roster does not supply required development role: ' + name)
        args.state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with owner(args.state):
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

        report = {'case_id': case['case_id']}
        author = stage(args, case, 'author', AUTHOR, case['author'])
        if author['status'] != 'succeeded':
            return {**report, 'stage': 'author', 'state': author['status'], 'detail': author}
        artifact = author['artifact']
        patch_hash = digest(artifact['payload']['diff'].encode())
        authored = {key: artifact[key] for key in ('task_id', 'member_id', 'run_id', 'attempt_id')}
        authored.update(sha256=artifact['output_sha256'], artifact=artifact['payload'])
        report.update(author=artifact, patch_sha256=patch_hash)
        review_input = {key: case['author'][key] for key in ('base_sha', 'source', 'regression')}
        review_input.update(case_id=case['case_id'], criterion=case['criterion'],
                            patch_sha256=patch_hash, authored_revision=authored)
        reviewer = stage(args, case, 'reviewer', REVIEWER, review_input)
        if reviewer['status'] != 'succeeded':
            return {**report, 'stage': 'reviewer', 'state': reviewer['status'], 'detail': reviewer}
        review = reviewer['artifact']
        return {**report, 'stage': 'reviewer', 'state': review['payload']['verdict'], 'reviewer': review}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'status'))
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--lab', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--backend', choices=('fixture', 'codex'), required=True)
    args = parser.parse_args()
    try:
        case = assignment.decode(assignment.read_input())
        result = run_case(args, case)
        print(assignment.encode(result))
        return 0 if args.action == 'status' or result['state'] == 'accept' else 1
    except (assignment.AssignmentError, KeyError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(assignment.encode({'state': 'error', 'reason': str(exc)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
